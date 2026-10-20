"""P0-04-2B offline candidate notification tests; never import/launch main.

No live sources, .env, historical cache writes, pushes or threads.
"""
import ast
from dataclasses import replace
from datetime import timedelta, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from test_market_data_state import (CAL, CODE, FREQS, FAKE_END, dt, frame, state, ROOT)
from market_data_state import MarketDataState, TradingCalendar, PYTDX_CONTRACT
from signal_policy import (candidate_notice_allowed, classify_market_signal, record_latest_signal,
                           SignalJournal, ConfirmedEventConflict, STRATEGY_REVISION,
                           DEFAULT_PROFILE, VALIDATION_REFERENCE, format_signal_notice)
from test_signal_policy import signal


def observe(q, now, *, label='2026-01-05 10:30', freq='1MIN', vol=1000.):
    batch = frame([label], freq)
    batch['vol'] = vol
    q.request(CODE, freq, now)
    q.observe(CODE, freq, batch, now)
    return q.snapshot(CODE, freq, now)


def progressing(freq='1MIN'):
    q = state(PYTDX_CONTRACT)
    observe(q, dt('2026-01-05 10:30:10'), freq=freq)
    snap = observe(q, dt('2026-01-05 10:30:20'), freq=freq, vol=1100.)
    return q, snap


def record(snap, name='BUY_B', **kwargs):
    opts = dict(market_state=snap, instrument=CODE, timeframe=snap.timeframe, scope='p',
                observed_at=snap.observed_at, bar_time=dt(snap.latest_bar_time),
                strategy_revision=STRATEGY_REVISION, calculation_profile=DEFAULT_PROFILE,
                validation_reference=VALIDATION_REFERENCE)
    opts.update(kwargs)
    return classify_market_signal(name, **opts)


def app_for(snap, journal, *, count=1):
    # Actual methods, not a replacement scanner; imports/__init__ never execute.
    tree = ast.parse((ROOT / 'main.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'QuantAlertApp')
    node.body = [n for n in node.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'_scan_once', '_signal_key', '_pass_cooldown', '_build_email_content'}]
    ns = dict(datetime=datetime, timedelta=timedelta, record_latest_signal=record_latest_signal,
              format_signal_notice=format_signal_notice,
              _load_tickers=lambda _: [SimpleNamespace(ts_code=CODE, display='offline')] * count,
              AlertItem=lambda **kw: SimpleNamespace(**kw))
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), 'main.py (candidate gate offline)', 'exec'), ns)
    app = ns['QuantAlertApp'].__new__(ns['QuantAlertApp'])
    app.config = SimpleNamespace(pools=[SimpleNamespace(pool_id='p', display_name='p', tickers_file='unused')],
                                 min_freq=snap.timeframe, min_bars_required=1, signal_cooldown_minutes=0)
    app.data_source = SimpleNamespace(get_minute_snapshot=lambda **_: (frame([snap.latest_bar_time], snap.timeframe), app.snap))
    app.current = signal(ts_code=CODE, bar_time=snap.latest_bar_time)
    app.strategy = SimpleNamespace(latest_signal=lambda **_: app.current)
    app.snap = snap
    app.signal_journal = journal
    app._ticker_display, app._last_seen_bar_time, app._last_sent_at = {}, {}, {}
    app._sent_cache = set()
    app._strategy_revision = STRATEGY_REVISION
    app.logs = []
    app._log = app.logs.append
    return app


class TestObservationFreshness(unittest.TestCase):
    def test_two_valid_live_revisions_allow_candidate_but_closure_stays_unknown(self):
        _, snap = progressing()
        self.assertEqual(snap.freshness, 'unknown')  # legacy field remains conservative
        self.assertEqual(snap.observation_freshness, 'fresh')
        self.assertEqual(snap.bars[-1].state, 'unknown')
        self.assertTrue(candidate_notice_allowed(record(snap), snap))

    def test_all_five_periods_support_independent_observation_quality(self):
        for freq in FREQS:
            _, snap = progressing(freq)
            self.assertEqual(snap.observation_freshness, 'fresh')
            self.assertEqual(snap.bars[-1].state, 'unknown')

    def test_new_latest_label_is_progress_not_just_same_bar_revision(self):
        q = state(PYTDX_CONTRACT)
        observe(q, dt('2026-01-05 10:29:50'), label='2026-01-05 10:29')
        snap = observe(q, dt('2026-01-05 10:30:20'))
        self.assertEqual(snap.observation_freshness, 'fresh')

    def test_first_success_is_not_proof(self):
        q = state(PYTDX_CONTRACT)
        snap = observe(q, dt('2026-01-05 10:30:20'))
        self.assertEqual(snap.source_status, 'ok')
        self.assertEqual(snap.observation_freshness, 'unknown')
        self.assertFalse(candidate_notice_allowed(record(snap), snap))

    def test_repeated_unchanged_success_does_not_unlock_candidate(self):
        q = state(PYTDX_CONTRACT)
        observe(q, dt('2026-01-05 10:30:10'))
        snap = observe(q, dt('2026-01-05 10:30:20'))
        self.assertEqual(snap.observation_freshness, 'unknown')
        self.assertIsNone(snap.last_progress_at)

    def test_progress_expires_even_when_successes_keep_arriving(self):
        q, snap = progressing()
        for minute in range(1, 5):
            snap = observe(q, dt('2026-01-05 10:30:20') + timedelta(minutes=minute), vol=1100.)
        self.assertEqual(snap.observation_freshness, 'stale')
        self.assertFalse(candidate_notice_allowed(record(snap), snap))

    def test_old_data_cannot_be_made_current_by_editing_ohlcv(self):
        q = state(PYTDX_CONTRACT)
        observe(q, dt('2026-01-05 10:30:10'), label='2026-01-05 09:31')
        snap = observe(q, dt('2026-01-05 10:30:20'), label='2026-01-05 09:31', vol=1200.)
        self.assertEqual(snap.observation_freshness, 'stale')
        self.assertFalse(candidate_notice_allowed(record(snap), snap))

    def test_cache_and_one_live_success_cannot_prove_progress(self):
        q = state(PYTDX_CONTRACT)
        q.observe(CODE, '1MIN', frame(['2026-01-05 10:30']), dt('2026-01-05 10:30:05'), cache_only=True)
        snap = observe(q, dt('2026-01-05 10:30:20'), vol=1200.)
        self.assertEqual(snap.observation_freshness, 'unknown')

    def test_historical_row_correction_does_not_refresh_latest_progress(self):
        q = state(PYTDX_CONTRACT)
        batch = frame(['2026-01-05 10:29', '2026-01-05 10:30'])
        for now in [dt('2026-01-05 10:30:10'), dt('2026-01-05 10:30:20')]:
            q.request(CODE, '1MIN', now)
            q.observe(CODE, '1MIN', batch, now)
            batch.loc[0, 'vol'] += 100
        self.assertEqual(q.snapshot(CODE, '1MIN', now).observation_freshness, 'unknown')

    def test_missing_calendar_or_request_time_does_not_unlock_candidate(self):
        for q in [MarketDataState(calendar=TradingCalendar()), state(PYTDX_CONTRACT)]:
            for now, volume in [(dt('2026-01-05 10:30:10'), 1000.), (dt('2026-01-05 10:30:20'), 1200.)]:
                batch = frame(['2026-01-05 10:30'])
                batch['vol'] = volume
                q.observe(CODE, '1MIN', batch, now)
            snap = q.snapshot(CODE, '1MIN', now)
            self.assertEqual(snap.observation_freshness, 'unknown')

    def test_failure_resets_progress_and_recovery_requires_new_evidence(self):
        q, snap = progressing()
        q.failure(CODE, '1MIN', dt('2026-01-05 10:30:21'), 'synthetic disconnect')
        snap = q.snapshot(CODE, '1MIN', dt('2026-01-05 10:30:22'))
        self.assertEqual(snap.observation_freshness, 'unavailable')
        self.assertFalse(candidate_notice_allowed(record(snap), snap))
        snap = observe(q, dt('2026-01-05 10:30:30'), vol=1300.)
        self.assertEqual(snap.observation_freshness, 'unknown')
        snap = observe(q, dt('2026-01-05 10:30:40'), vol=1400.)
        self.assertEqual(snap.observation_freshness, 'fresh')
        self.assertEqual(snap.last_error, 'synthetic disconnect')

    def test_invalid_batch_blocks_candidates_without_erasing_prior_observations(self):
        q, _ = progressing()
        old = q.history(CODE, '1MIN')
        bad = frame(['2026-01-05 10:30'])
        bad['close'] = float('nan')
        with self.assertRaises(ValueError):
            q.observe(CODE, '1MIN', bad, dt('2026-01-05 10:30:30'))
        snap = q.snapshot(CODE, '1MIN', dt('2026-01-05 10:30:30'))
        self.assertEqual(snap.observation_freshness, 'unavailable')
        self.assertEqual(q.history(CODE, '1MIN'), old)

    def test_lunch_and_next_day_require_new_current_session_progress(self):
        q = state(PYTDX_CONTRACT)
        observe(q, dt('2026-01-05 11:29:10'), label='2026-01-05 11:29')
        observe(q, dt('2026-01-05 11:29:20'), label='2026-01-05 11:29', vol=1100.)
        self.assertEqual(q.snapshot(CODE, '1MIN', dt('2026-01-05 12:00')).observation_freshness, 'unknown')
        for start, label in [('2026-01-05 13:01:10', '2026-01-05 13:01'),
                             ('2026-01-06 09:31:10', '2026-01-06 09:31')]:
            snap = observe(q, dt(start), label=label)
            self.assertEqual(snap.observation_freshness, 'unknown')
            snap = observe(q, dt(start) + timedelta(seconds=10), label=label, vol=1100.)
            self.assertEqual(snap.observation_freshness, 'fresh')

    def test_wrong_grid_or_far_future_labels_do_not_unlock(self):
        for label, freq in [('2026-01-05 10:31', '5MIN'), ('2026-01-06 10:30', '1MIN'),
                            ('2026-01-05 11:30', '5MIN')]:
            q = state(PYTDX_CONTRACT)
            observe(q, dt('2026-01-05 10:30:10'), label=label, freq=freq)
            snap = observe(q, dt('2026-01-05 10:30:20'), label=label, freq=freq, vol=1100.)
            self.assertNotEqual(snap.observation_freshness, 'fresh')

    def test_plausible_end_label_does_not_assert_completion_or_future_signal(self):
        q = state(PYTDX_CONTRACT)
        observe(q, dt('2026-01-05 10:29:10'), freq='5MIN')
        snap = observe(q, dt('2026-01-05 10:29:20'), freq='5MIN', vol=1100.)
        self.assertEqual(snap.observation_freshness, 'fresh')
        self.assertEqual(record(snap, 'CROSS_GC').status, 'candidate')
        self.assertEqual(snap.bars[-1].state, 'unknown')

    def test_closed_input_conflict_blocks_even_after_new_successes(self):
        q = state(FAKE_END)
        batch = frame(['2026-01-05 10:29', '2026-01-05 10:30'])
        q.request(CODE, '1MIN', dt('2026-01-05 10:29:30'))
        q.observe(CODE, '1MIN', batch, dt('2026-01-05 10:29:30'))
        batch.loc[0, 'vol'] += 100
        q.request(CODE, '1MIN', dt('2026-01-05 10:29:40'))
        q.observe(CODE, '1MIN', batch, dt('2026-01-05 10:29:40'))
        snap = q.snapshot(CODE, '1MIN', dt('2026-01-05 10:29:40'))
        self.assertTrue(snap.unresolved_conflict)
        self.assertEqual(snap.observation_freshness, 'unknown')
        self.assertFalse(candidate_notice_allowed(record(snap), snap))


class TestCandidatePolicyAndScan(unittest.TestCase):
    def test_bs_jb_js_and_causal_crosses_never_confirm_with_unknown_closure(self):
        _, snap = progressing()
        for name in ['BUY_B', 'SELL_S', 'BUY_JB', 'SELL_JS', 'CROSS_GC', 'CROSS_DC']:
            rec = record(snap, name)
            self.assertEqual(rec.status, 'candidate')
            self.assertIsNone(rec.bar_closed)
            self.assertIsNone(rec.confirmed_at)
            self.assertFalse(rec.can_use_for_trade_plan)

    def test_historical_markers_inactive_and_wrong_identity_cannot_notify(self):
        _, snap = progressing()
        for args in [dict(record_kind='historical_marker'), dict(active=False),
                     dict(instrument='000001.SZ'), dict(timeframe='5MIN'),
                     dict(bar_time=dt('2026-01-05 10:29'))]:
            self.assertFalse(candidate_notice_allowed(record(snap, **args), snap))

    def test_current_candidate_content_explicitly_warns_and_no_real_push(self):
        _, snap = progressing()
        with TemporaryDirectory() as directory:
            app = app_for(snap, SignalJournal(directory))
            alerts, _ = app._scan_once()
            self.assertEqual(len(alerts), 1)
            text = app._build_email_content(alerts)
            self.assertIn('candidate', text)
            self.assertIn('收盘状态未知', text)
            self.assertIn('不可用于交易计划', text)
            self.assertIn('K线未确认', format_signal_notice('BUY_JB'))

    def test_stale_failure_unknown_and_conflict_scans_record_without_alert(self):
        q, snap = progressing()
        for bad in [replace(snap, freshness='stale', observation_freshness='stale'),
                    replace(snap, source_status='error', freshness='unavailable', observation_freshness='unavailable'),
                    replace(snap, observation_freshness='unknown'),
                    replace(snap, unresolved_conflict=True)]:
            with TemporaryDirectory() as directory:
                app = app_for(bad, SignalJournal(directory))
                self.assertEqual(app._scan_once()[0], [])
                self.assertEqual(len(app.signal_journal.records()), 1)

    def test_same_bar_repeat_scan_dedup_keeps_observations_and_withdrawals(self):
        _, snap = progressing()
        with TemporaryDirectory() as directory:
            app = app_for(snap, SignalJournal(directory), count=2)
            alerts, _ = app._scan_once()
            self.assertEqual(len(alerts), 1)  # duplicated ticker in one pool
            # Same bookkeeping as run(): no sender/network is invoked.
            for alert in alerts:
                app._sent_cache.add(app._signal_key(alert.signal, alert.side, alert.pool_id))
            self.assertEqual(app._scan_once()[0], [])
            app.current.buy_b = False
            self.assertEqual(app._scan_once()[0], [])
            app.current.buy_b = True
            self.assertEqual(app._scan_once()[0], [])
            records = SignalJournal(directory).records()
            self.assertEqual([r.active for r in records], [True, True, True, True, False, True, True])

    def test_dedup_key_includes_period_and_normalizes_minute_format(self):
        _, snap = progressing()
        with TemporaryDirectory() as directory:
            app = app_for(snap, SignalJournal(directory))
            first = app._signal_key(app.current, 'BUY', 'p')
            app.current.bar_time = app.current.bar_time[:16]
            self.assertEqual(app._signal_key(app.current, 'BUY', 'p'), first)
            app.config.min_freq = '5MIN'
            self.assertNotEqual(app._signal_key(app.current, 'BUY', 'p'), first)
            self.assertNotEqual(app._signal_key(app.current, 'BUY_JB', 'p'), first)

    def test_missing_quality_sidecar_fails_closed_in_real_scanner(self):
        _, snap = progressing()
        with TemporaryDirectory() as directory:
            app = app_for(snap, SignalJournal(directory))
            app.data_source = SimpleNamespace(get_minute_bars=lambda **_: frame([snap.latest_bar_time]))
            self.assertEqual(app._scan_once()[0], [])
            self.assertEqual(app.signal_journal.records()[0].status, 'candidate')

    def test_confirmed_immutability_and_restart_validation_are_preserved(self):
        q = state(FAKE_END)
        now = dt('2026-01-05 10:29:30')
        q.request(CODE, '1MIN', now)
        q.observe(CODE, '1MIN', frame(['2026-01-05 10:29', '2026-01-05 10:30']), now)
        snap = q.snapshot(CODE, '1MIN', now)
        first = record(snap, 'CROSS_GC', bar_time=dt('2026-01-05 10:29'))
        self.assertEqual(first.status, 'confirmed')
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            journal.record(first)
            before = (Path(directory) / 'observations.jsonl').read_bytes()
            newer = record(replace(snap, freshness='stale'), 'CROSS_GC', bar_time=dt('2026-01-05 10:29'))
            with self.assertRaises(ConfirmedEventConflict):
                journal.record(newer)
            self.assertEqual((Path(directory) / 'observations.jsonl').read_bytes(), before)
            self.assertEqual(SignalJournal(directory).records()[0], first)


if __name__ == '__main__':
    unittest.main()
