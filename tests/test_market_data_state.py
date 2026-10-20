"""Offline P0-04-2 tests. Verified contracts here describe a FAKE provider only.

No server, .env, runtime cache writes, background threads or notification calls.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, datetime, timedelta
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from market_data_state import (
    SHANGHAI, TradingCalendar, SourceContract, PYTDX_CONTRACT, MarketDataState, bar_state,
)
from signal_policy import (
    classify_market_signal, SignalJournal, STRATEGY_REVISION, DEFAULT_PROFILE,
    VALIDATION_REFERENCE, ConfirmedEventConflict, record_latest_signal,
)
from test_bar_cache_merge import Fetcher
from test_signal_policy import signal

CODE = '600000.SH'
FREQS = ('1MIN', '5MIN', '15MIN', '30MIN', '60MIN')
# Explicit synthetic calendar, not a production weekday fallback.
CAL = TradingCalendar({date(2026, 1, d): d in {5, 6, 7, 8, 9, 12} for d in range(5, 13)})
FAKE_END = SourceContract('offline-fixture', 'end', 'TEST-ONLY verified fake-provider contract', True)
FAKE_START = replace(FAKE_END, label='start')


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def frame(labels, freq='1MIN', close=20.):
    result = pd.DataFrame([dict(ts_code=CODE, time=t, open=close, high=close + 1,
                                low=close - 1, close=close, vol=1000., amount=close * 1000)
                           for t in labels])
    result.attrs = {'freq': freq, 'ts_code': CODE}
    return result


def state(contract=FAKE_END, **kwargs):
    return MarketDataState(calendar=CAL, contract=contract, **kwargs)


class TestBarCompletion(unittest.TestCase):
    def judge(self, label, now, freq='1MIN', contract=PYTDX_CONTRACT, successor=None):
        return bar_state(label, freq, observed_at=dt(now), calendar=CAL,
                         contract=contract, successor=successor)

    def test_pytdx_all_periods_unknown_even_with_later_bar_or_after_close(self):
        self.assertFalse(PYTDX_CONTRACT.verified)
        for freq in FREQS:
            for now in ['2026-01-05 10:31', '2026-01-05 12:00', '2026-01-05 15:30', '2026-01-06 10:00']:
                with self.subTest(freq=freq, now=now):
                    self.assertEqual(self.judge('2026-01-05 10:30', now, freq,
                                               successor='2026-01-05 11:30').state, 'unknown')

    def test_all_periods_fake_verified_contract_start_and_end_boundaries(self):
        for freq in FREQS:
            minutes = int(freq[:-3])
            for contract in [FAKE_END, FAKE_START]:
                first_start = dt('2026-01-05 09:30')
                end = first_start + timedelta(minutes=minutes)
                label = end if contract.label == 'end' else first_start
                successor = label + timedelta(minutes=minutes)
                fmt = lambda t: t.strftime('%Y-%m-%d %H:%M:%S')
                args = dict(freq=freq, observed_at=first_start + timedelta(seconds=1), calendar=CAL, contract=contract)
                self.assertEqual(bar_state(fmt(label), **args).state, 'forming')
                args['observed_at'] = end
                self.assertEqual(bar_state(fmt(label), **args).state, 'unknown')
                closed = bar_state(fmt(label), successor=fmt(successor), **args)
                self.assertEqual(closed.state, 'closed')
                self.assertEqual(closed.confirmed_at, end.isoformat())

    def test_successor_needs_explicit_source_guarantee(self):
        result = self.judge('2026-01-05 09:31', '2026-01-05 09:32', contract=replace(FAKE_END, successor_proves_closed=False), successor='2026-01-05 09:32')
        self.assertEqual(result.state, 'unknown')

    def test_lunch_is_not_a_bar_and_does_not_finalize_latest(self):
        for freq in FREQS:
            self.assertEqual(self.judge('2026-01-05 11:30', '2026-01-05 12:00', freq, FAKE_END).state, 'unknown')
            self.assertEqual(self.judge('2026-01-05 12:00', '2026-01-05 13:30', freq, FAKE_END, '2026-01-05 14:00').state, 'unknown')

    def test_60min_afternoon_starts_new_session_not_lunch_spanning(self):
        self.assertEqual(self.judge('2026-01-05 14:00', '2026-01-05 13:10', '60MIN', FAKE_END).state, 'forming')
        self.assertEqual(self.judge('2026-01-05 13:00', '2026-01-05 13:10', '60MIN', FAKE_END).state, 'unknown')
        self.assertEqual(self.judge('2026-01-05 11:30', '2026-01-05 13:10', '60MIN', FAKE_END, '2026-01-05 14:00').state, 'closed')

    def test_open_auction_and_misaligned_labels_unknown(self):
        for label in ['2026-01-05 09:25', '2026-01-05 09:30', '2026-01-05 09:32']:
            self.assertEqual(self.judge(label, '2026-01-05 10:00', '5MIN', FAKE_END).state, 'unknown')

    def test_close_and_next_trading_day_need_source_evidence(self):
        for freq in FREQS:
            end = '2026-01-09 15:00'
            self.assertEqual(self.judge(end, '2026-01-10 12:00', freq, FAKE_END).state, 'unknown')
            next_label = dt('2026-01-12 09:30') + timedelta(minutes=int(freq[:-3]))
            self.assertEqual(self.judge(end, '2026-01-12 09:30', freq, FAKE_END,
                                        next_label.strftime('%Y-%m-%d %H:%M')).state, 'closed')

    def test_future_successor_cannot_prove_completion(self):
        self.assertEqual(self.judge('2026-01-05 09:31', '2026-01-05 09:31', contract=FAKE_END, successor='2026-01-06 09:32').state, 'unknown')

    def test_unknown_calendar_and_unverified_reference_are_unknown(self):
        for contract, calendar in [(FAKE_END, TradingCalendar()), (replace(FAKE_END, verification_reference=None), CAL)]:
            result = bar_state('2026-01-05 09:31', '1MIN', observed_at=dt('2026-01-05 10:00'),
                               contract=contract, calendar=calendar, successor='2026-01-05 09:32')
            self.assertEqual(result.state, 'unknown')


class TestFreshness(unittest.TestCase):
    def seed(self, quality, labels, now, freq='1MIN', **kwargs):
        quality.request(CODE, freq, dt(now))
        quality.observe(CODE, freq, frame(labels, freq), dt(now), **kwargs)
        return quality.snapshot(CODE, freq, dt(now))

    def test_records_all_required_times_and_source_status(self):
        q = state()
        snap = self.seed(q, ['2026-01-05 09:31', '2026-01-05 09:32'], '2026-01-05 09:31:30')
        self.assertEqual(snap.freshness, 'fresh')
        self.assertFalse(snap.is_stale)
        self.assertEqual(snap.latest_bar_time, '2026-01-05 09:32:00')
        self.assertEqual(snap.last_request_at, snap.last_success_at)
        self.assertEqual(snap.source_status, 'ok')

    def test_all_periods_fresh_then_stale_after_trading_deadline(self):
        for freq in FREQS:
            q = state()
            end = dt('2026-01-05 09:30') + timedelta(minutes=int(freq[:-3]))
            snap = self.seed(q, [end.strftime('%Y-%m-%d %H:%M')], '2026-01-05 09:30:30', freq)
            self.assertEqual(snap.freshness, 'fresh')
            snap = q.snapshot(CODE, freq, dt('2026-01-05 10:40'))
            self.assertEqual(snap.freshness, 'stale')

    def test_repeated_success_old_bars_does_not_make_data_fresh(self):
        q = state()
        self.seed(q, ['2026-01-05 09:31'], '2026-01-05 09:31')
        snap = self.seed(q, ['2026-01-05 09:31'], '2026-01-05 10:00')
        self.assertEqual(snap.source_status, 'ok')
        self.assertEqual(snap.freshness, 'stale')

    def test_same_bar_revision_not_a_new_bar_and_history_not_overwritten(self):
        q = state()
        self.seed(q, ['2026-01-05 09:32'], '2026-01-05 09:31:10')
        old = q.history(CODE, '1MIN')
        q.observe(CODE, '1MIN', frame(['2026-01-05 09:32'], close=21.), dt('2026-01-05 09:31:20'))
        snap = q.snapshot(CODE, '1MIN', dt('2026-01-05 09:31:20'))
        self.assertEqual(snap.bars[0].revision, 2)
        self.assertEqual(snap.bars[0].state, 'forming')
        self.assertEqual(q.history(CODE, '1MIN')[0], old[0])
        self.assertEqual(len(q.history(CODE, '1MIN')), 2)

    def test_unchanged_replies_do_not_refresh_progress_or_append_revisions(self):
        q = state(request_timeout_seconds=10000)
        self.seed(q, ['2026-01-05 09:31'], '2026-01-05 09:30:30')
        old = q.history(CODE, '1MIN')
        self.seed(q, ['2026-01-05 09:31'], '2026-01-05 09:30:40')
        self.assertEqual(q.history(CODE, '1MIN'), old)
        self.assertEqual(q.snapshot(CODE, '1MIN', dt('2026-01-05 09:35')).freshness, 'stale')

    def test_closed_source_revision_requires_review(self):
        q = state()
        self.seed(q, ['2026-01-05 09:31', '2026-01-05 09:32'], '2026-01-05 09:31:30')
        old = q.history(CODE, '1MIN')
        q.observe(CODE, '1MIN', frame(['2026-01-05 09:31'], close=21.), dt('2026-01-05 09:31:40'))
        snap = q.snapshot(CODE, '1MIN', dt('2026-01-05 09:31:40'))
        self.assertEqual(snap.freshness, 'unknown')
        self.assertEqual(snap.bars[0].state, 'unknown')
        self.assertEqual(q.history(CODE, '1MIN')[0], old[0])

    def test_disconnect_and_empty_are_unavailable_without_erasing_data(self):
        for status in ['error', 'disconnected', 'empty']:
            q = state()
            self.seed(q, ['2026-01-05 09:31'], '2026-01-05 09:31')
            q.request(CODE, '1MIN', dt('2026-01-05 09:32'))
            q.failure(CODE, '1MIN', dt('2026-01-05 09:32'), 'synthetic failure', status)
            snap = q.snapshot(CODE, '1MIN', dt('2026-01-05 09:32'))
            self.assertEqual(snap.freshness, 'unavailable')
            self.assertEqual(snap.last_error, 'synthetic failure')
            self.assertEqual(snap.last_success_at, dt('2026-01-05 09:31').isoformat())
            self.assertEqual(len(snap.bars), 1)

    def test_recovery_retains_last_error_but_can_be_fresh(self):
        q = state()
        q.failure(CODE, '1MIN', dt('2026-01-05 09:30'), 'old disconnect')
        snap = self.seed(q, ['2026-01-05 09:32'], '2026-01-05 09:31')
        self.assertEqual((snap.freshness, snap.source_status), ('fresh', 'ok'))
        self.assertEqual(snap.last_error, 'old disconnect')

    def test_lunch_and_overnight_and_weekend_do_not_age_completed_bars(self):
        for now, labels, later in [
            ('2026-01-05 11:30', ['2026-01-05 11:29', '2026-01-05 11:30'], '2026-01-05 12:59'),
            ('2026-01-09 15:00', ['2026-01-09 14:59', '2026-01-09 15:00'], '2026-01-12 09:29')]:
            q = state()
            self.seed(q, labels, now)
            snap = q.snapshot(CODE, '1MIN', dt(later))
            self.assertEqual(snap.freshness, 'fresh')
            self.assertEqual(snap.bars[0].state, 'closed')

    def test_lunch_resume_and_next_day_old_bar_become_stale(self):
        for now, label, later in [
            ('2026-01-05 11:30', '2026-01-05 11:30', '2026-01-05 13:04'),
            ('2026-01-05 15:00', '2026-01-05 15:00', '2026-01-06 09:34')]:
            q = state()
            self.seed(q, [label], now)
            self.assertEqual(q.snapshot(CODE, '1MIN', dt(later)).freshness, 'stale')

    def test_no_success_and_cache_only_never_fresh(self):
        q = state()
        self.assertEqual(q.snapshot(CODE, '1MIN', dt('2026-01-05 09:30')).freshness, 'unavailable')
        snap = self.seed(q, ['2026-01-05 09:31'], '2026-01-05 09:31', cache_only=True)
        self.assertIsNone(snap.last_success_at)
        self.assertEqual(snap.freshness, 'unknown')
        self.assertEqual(snap.bars[0].state, 'unknown')

    def test_missing_calendar_or_unknown_pytdx_semantics_never_fresh(self):
        for q in [MarketDataState(contract=FAKE_END), state(PYTDX_CONTRACT)]:
            snap = self.seed(q, ['2026-01-05 09:31'], '2026-01-05 09:31')
            self.assertEqual(snap.freshness, 'unknown')

    def test_pytdx_unknown_can_still_be_detectably_stale(self):
        q = state(PYTDX_CONTRACT)
        snap = self.seed(q, ['2026-01-05 09:31'], '2026-01-05 10:00')
        self.assertEqual(snap.freshness, 'stale')

    def test_expired_forming_becomes_unknown_not_closed_without_fetch(self):
        q = state()
        self.seed(q, ['2026-01-05 09:32'], '2026-01-05 09:31')
        self.assertEqual(q.snapshot(CODE, '1MIN', dt('2026-01-05 09:32')).bars[0].state, 'unknown')

    def test_future_labels_and_clock_rollback_never_fresh(self):
        q = state()
        snap = self.seed(q, ['2026-01-06 09:31'], '2026-01-05 09:31')
        self.assertEqual(snap.freshness, 'unknown')
        self.assertEqual(q.snapshot(CODE, '1MIN', dt('2026-01-05 09:30')).freshness, 'unknown')

    def test_naive_observation_clock_rejected(self):
        with self.assertRaises(ValueError):
            state().snapshot(CODE, '1MIN', datetime(2026, 1, 5, 9, 30))

    def test_instruments_and_periods_isolated(self):
        q = state()
        self.seed(q, ['2026-01-05 09:31'], '2026-01-05 09:31')
        self.assertEqual(q.snapshot('000001.SZ', '1MIN', dt('2026-01-05 09:31')).source_status, 'not_requested')
        self.assertEqual(q.snapshot(CODE, '5MIN', dt('2026-01-05 09:31')).source_status, 'not_requested')


class TestCalendar(unittest.TestCase):
    def test_cache_missing_malformed_and_conflicts_are_unknown(self):
        with TemporaryDirectory() as directory:
            p = Path(directory)
            self.assertEqual(TradingCalendar.from_cache(p).phase(dt('2026-01-05 10:00')), 'unknown')
            (p / 'trade_cal_20260105_20260106.json').write_text(json.dumps({'open_days': ['20260105']}))
            (p / 'trade_cal_20260105_20260107.json').write_text(json.dumps({'open_days': ['20260106']}))
            (p / 'trade_cal_20260101_20260131.json').write_text('broken')
            cal = TradingCalendar.from_cache(p)
            self.assertEqual(cal.phase(dt('2026-01-05 10:00')), 'unknown')
            self.assertEqual(cal.phase(dt('2026-01-06 10:00')), 'unknown')

    def test_explicit_holiday_not_weekday_guessed_open(self):
        cal = TradingCalendar({date(2026, 1, 5): False})
        self.assertEqual(cal.phase(dt('2026-01-05 10:00')), 'closed_day')
        self.assertEqual(cal.active_seconds(dt('2026-01-05 09:30'), dt('2026-01-05 15:00')), 0)
        self.assertIsNone(cal.active_seconds(dt('2026-01-05 15:00'), dt('2026-01-06 10:00')))


class TestQualitySignalGate(unittest.TestCase):
    def setUp(self):
        self.q = state()
        self.now = dt('2026-01-05 09:31:30')
        self.q.request(CODE, '1MIN', self.now)
        self.q.observe(CODE, '1MIN', frame(['2026-01-05 09:31', '2026-01-05 09:32']), self.now)
        self.snapshot = self.q.snapshot(CODE, '1MIN', self.now)

    def classify(self, name='CROSS_GC', snap=None, **kwargs):
        opts = dict(market_state=self.snapshot if snap is None else snap, observed_at=self.now,
                    bar_time=dt('2026-01-05 09:31'), instrument=CODE, timeframe='1MIN', scope='pool',
                    strategy_revision=STRATEGY_REVISION, calculation_profile=DEFAULT_PROFILE,
                    validation_reference=VALIDATION_REFERENCE)
        opts.update(kwargs)
        return classify_market_signal(name, **opts)

    def test_verified_closed_fresh_and_causal_cross_only(self):
        for name in ['CROSS_GC', 'CROSS_DC']:
            record = self.classify(name)
            self.assertEqual(record.status, 'confirmed')
            self.assertFalse(record.can_use_for_trade_plan)
        self.assertEqual(self.classify(validation_reference=None).status, 'candidate')

    def test_stale_unavailable_unknown_missing_or_forming_block_upgrade(self):
        for freshness in ['stale', 'unavailable', 'unknown']:
            record = self.classify(snap=replace(self.snapshot, freshness=freshness))
            self.assertEqual(record.status, 'candidate')
        self.assertEqual(self.classify(market_state=None).status, 'candidate')
        self.assertEqual(self.classify(bar_time=dt('2026-01-05 09:32')).status, 'candidate')

    def test_mismatched_security_period_bar_or_observation_cannot_confirm(self):
        for opts in [dict(instrument='000001.SZ'), dict(timeframe='5MIN'),
                     dict(bar_time=dt('2026-01-05 10:31')), dict(observed_at=self.now + timedelta(seconds=1))]:
            self.assertEqual(self.classify(**opts).status, 'candidate')

    def test_bs_and_jb_js_keep_original_restrictions(self):
        for name in ['BUY_B', 'SELL_S', 'BUY_JB', 'SELL_JS']:
            self.assertEqual(self.classify(name).status, 'candidate')
        self.assertEqual(self.classify('BOTTOM_FISHING').status, 'invalid')

    def test_confirmed_observation_not_overwritten_on_stale_or_revision(self):
        first = self.classify()
        later = self.classify(snap=replace(self.snapshot, freshness='stale'))
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            journal.record(first)
            original = (Path(directory) / 'observations.jsonl').read_bytes()
            with self.assertRaises(ConfirmedEventConflict):
                journal.record(later)
            self.assertEqual((Path(directory) / 'observations.jsonl').read_bytes(), original)
            self.assertEqual(SignalJournal(directory).records()[0], first)

    def test_raw_candidates_revisions_persist_with_quality_in_journal(self):
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            sig = signal(ts_code=CODE, bar_time='2026-01-05 09:31')
            record_latest_signal(journal, sig, observed_at=self.now, timeframe='1MIN', scope='pool', market_state=self.snapshot)
            sig.buy_b = False
            record_latest_signal(journal, sig, observed_at=self.now, timeframe='1MIN', scope='pool',
                                 market_state=replace(self.snapshot, freshness='stale'))
            records = SignalJournal(directory).records()
            self.assertEqual([r.active for r in records], [True, False])
            self.assertEqual(records[-1].payload['market_data']['freshness'], 'stale')


class TestFetcherQualityIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt('2026-01-05 09:31:30')
        self.f = Fetcher([CODE], cache_dir=self.temp.name, clock=lambda: self.now, quality_calendar=CAL)
        self.f._ensure_connected = Mock(return_value=True)
        self.f._fetch_realtime_quotes = Mock(return_value=[])

    def tick(self, raw):
        self.f._api.get_security_bars = Mock(return_value=raw)
        self.f._last_min_bar_fetch = 0
        with redirect_stdout(StringIO()):
            self.f._tick_once()

    def raw(self):
        return [dict(datetime='2026-01-05 09:31', open=20., close=20., high=21., low=19., vol=1000., amount=20000.)]

    def test_real_fetch_tick_and_atomic_snapshot_attach_unknown_pytdx_state(self):
        self.tick(self.raw())
        bars, snap = self.f.get_minute_snapshot(CODE)
        self.assertEqual(snap.freshness, 'unknown')
        self.assertEqual(snap.bars[0].state, 'unknown')
        self.assertEqual(snap.last_success_at, self.now.isoformat())
        bars.loc[0, 'close'] = 999
        self.assertEqual(self.f.get_minute_bars(CODE).iloc[0].close, 20.)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_transport_exception_keeps_history_and_marks_unavailable(self):
        self.tick(self.raw())
        old = self.f.get_minute_bars(CODE)
        self.f._api.get_security_bars = Mock(side_effect=ConnectionError('offline disconnect'))
        self.f._last_min_bar_fetch = 0
        with redirect_stdout(StringIO()):
            self.f._tick_once()
        self.assertIs(self.f.get_minute_bars(CODE), old)
        snap = self.f.get_market_status(CODE)
        self.assertEqual(snap.freshness, 'unavailable')
        self.assertIn('offline disconnect', snap.last_error)
        self.assertFalse(self.f._connected)

    def test_connection_failure_and_empty_response_do_not_refresh_success(self):
        self.tick(self.raw())
        old_time = self.f.get_market_status(CODE).last_success_at
        self.now += timedelta(minutes=1)
        self.tick(None)
        self.assertEqual(self.f.get_market_status(CODE).last_success_at, old_time)
        self.assertEqual(self.f.get_market_status(CODE).freshness, 'unavailable')
        self.f._ensure_connected = Mock(return_value=False)
        self.f._tick_once()
        self.assertEqual(self.f.get_market_status(CODE).source_status, 'disconnected')

    def test_same_bar_revision_retains_old_sidecar_observation(self):
        self.tick(self.raw())
        old = self.f._quality.history(CODE, '1MIN')
        revised = self.raw()
        revised[0]['vol'] = 1200.
        self.now += timedelta(seconds=10)
        self.tick(revised)
        self.assertEqual(self.f.get_market_status(CODE).bars[0].revision, 2)
        self.assertEqual(self.f._quality.history(CODE, '1MIN')[0], old[0])

    def test_disk_restore_is_unknown_and_does_not_touch_original_csv(self):
        path = Path(self.temp.name) / '600000_SH_1MIN.csv'
        frame(['2026-01-05 09:31']).to_csv(path, index=False)
        original = path.read_bytes()
        with redirect_stdout(StringIO()):
            self.f._load_all_disk_cache()
        snap = self.f.get_market_status(CODE)
        self.assertEqual(snap.source_status, 'cache_only')
        self.assertIsNone(snap.last_success_at)
        self.assertEqual(snap.bars[0].state, 'unknown')
        self.assertEqual(path.read_bytes(), original)

    def test_real_scan_keeps_observations_but_suppresses_bad_quality_alerts(self):
        # Compile actual main methods, avoiding main import/threads/network.
        tree = ast.parse((ROOT / 'main.py').read_text())
        app_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'QuantAlertApp')
        app_class.body = [n for n in app_class.body if isinstance(n, ast.FunctionDef)
                          and n.name in {'_scan_once', '_signal_key', '_pass_cooldown'}]
        ns = dict(datetime=datetime, timedelta=timedelta, record_latest_signal=record_latest_signal,
                  _load_tickers=lambda _: [SimpleNamespace(ts_code=CODE, display='offline')],
                  AlertItem=lambda **kw: SimpleNamespace(**kw))
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), app_class], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), 'main.py (quality integration)', 'exec'), ns)
        self.tick(self.raw())
        for freshness in ['fresh', 'stale', 'unavailable', 'unknown']:
            app = ns['QuantAlertApp'].__new__(ns['QuantAlertApp'])
            snap = replace(self.f.get_market_status(CODE), freshness=freshness)
            app.data_source = SimpleNamespace(get_minute_snapshot=lambda **_: (frame(['2026-01-05 09:31']), snap))
            app.config = SimpleNamespace(pools=[SimpleNamespace(pool_id='p', display_name='p', tickers_file='unused')],
                                         min_freq='1MIN', min_bars_required=1, signal_cooldown_minutes=0)
            app.strategy = SimpleNamespace(latest_signal=lambda **_: signal(ts_code=CODE, bar_time='2026-01-05 09:31'))
            app._ticker_display, app._last_seen_bar_time, app._last_sent_at = {}, {}, {}
            app._sent_cache = set()
            app._strategy_revision = STRATEGY_REVISION
            app._log = lambda _: None
            with TemporaryDirectory() as directory:
                app.signal_journal = SignalJournal(directory)
                alerts, _ = app._scan_once()
                self.assertEqual(len(alerts), int(freshness == 'fresh'))
                records = app.signal_journal.records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].status, 'candidate')
                self.assertEqual(records[0].payload['market_data']['freshness'], freshness)


if __name__ == '__main__':
    unittest.main()
