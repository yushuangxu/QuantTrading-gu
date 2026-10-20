"""Offline trust policy tests. No main import, .env, live cache or network.

Run: python -B -m unittest discover -s tests -p 'test_signal_policy.py' -v
The AST integration harness compiles only the existing class methods, bypassing
all main module imports and __init__, which would start network/monitor threads.
"""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from signal_policy import (
    STRATEGY_REVISION, DEFAULT_PROFILE, VALIDATION_REFERENCE, SignalJournal, SignalRecord,
    ConfirmedEventConflict, append_record, classify_signal, format_signal_notice, record_latest_signal,
)

T0 = datetime(2026, 1, 5, 2, 30, tzinfo=timezone.utc)


def record(name='BUY_B', **changes):
    args = dict(observed_at=T0 + timedelta(minutes=2), bar_time=T0, bar_closed=True,
                confirmed_at=T0 + timedelta(minutes=1), instrument='SYNTHETIC', timeframe='1MIN',
                strategy_revision=STRATEGY_REVISION, calculation_profile=DEFAULT_PROFILE,
                validation_reference=VALIDATION_REFERENCE, payload={'MACD': -1.5, 'CCI': -130.})
    args.update(changes)
    return classify_signal(name, **args)


def signal(**changes):
    args = dict(ts_code='SYNTHETIC', bar_time='2026-01-05 10:30:00', close=80.,
                j=10., dif=-1., dea=-.5, macd=-1., cci=-150.,
                buy_b=True, sell_s=False, bottom_fishing=False, top_escape=False,
                buy_jb=False, sell_js=False)
    args.update(changes)
    return SimpleNamespace(**args)


class TestSignalClassification(unittest.TestCase):
    def test_backset_never_confirmed_even_closed_with_confirmation(self):
        for name in ['BUY_B', 'SELL_S', 'BUY', 'SELL', 'BUY_B_RAW', 'BOTTOM_DIV',
                     'TOP_DIV', 'INNER_BOTTOM_DIV', 'INNER_TOP_DIV', 'BACKSET']:
            with self.subTest(name=name):
                item = record(name)
                self.assertEqual(item.status, 'candidate')
                self.assertIn('重绘', item.reason)
                self.assertFalse(item.can_use_for_trade_plan)

    def test_historical_marker_stays_retrospective_even_with_verified_cross(self):
        for name in ['BACKSET', 'BUY_B', 'INNER_TOP_DIV', 'CROSS_GC']:
            item = record(name, record_kind='historical_marker', extremum_at=T0,
                          marker_appeared_at=T0 + timedelta(minutes=1))
            self.assertEqual(item.status, 'retrospective')
            self.assertFalse(item.can_use_for_trade_plan)

    def test_unclosed_bar_cannot_confirm_any_cross(self):
        for name in ['CROSS_GC', 'CROSS_DC']:
            self.assertEqual(record(name, bar_closed=False).status, 'candidate')

    def test_unknown_bar_state_not_treated_as_truthy(self):
        for state in [None, 'True', 'False', 1, 0]:
            with self.subTest(state=state):
                item = record('CROSS_GC', bar_closed=state)
                self.assertEqual(item.status, 'candidate')
                self.assertIsNone(item.bar_closed)

    def test_invalid_signals_and_raw_aliases(self):
        for name in ['BOTTOM_FISHING', 'TOP_ESCAPE', 'STRONG_BOTTOM', 'STRONG_TOP',
                     'BOTTOM_FISHING_RAW', 'TOP_ESCAPE_RAW']:
            for active in [True, False]:
                item = record(name, active=active, record_kind='historical_marker')
                self.assertEqual(item.status, 'invalid')
                self.assertFalse(item.can_use_for_trade_plan)
                self.assertEqual(item.active, active)

    def test_jb_js_are_momentum_candidates_not_confirmed_reversals(self):
        for name in ['BUY_JB', 'SELL_JS', 'BUY_JB_RAW', 'WAVE_PEAK', 'WAVE_TROUGH']:
            item = record(name)
            self.assertEqual(item.status, 'candidate')
            self.assertIn('不代表已确认趋势反转', item.reason)
            self.assertFalse(item.can_use_for_trade_plan)

    def test_area_line_divergences_are_not_trade_confirmation(self):
        for name in ['TWO_ROUND_BOTTOM_DIV', 'TWO_ROUND_TOP_DIV', 'LINE_BOTTOM', 'LINE_TOP']:
            self.assertEqual(record(name).status, 'candidate')
            self.assertFalse(record(name).can_use_for_trade_plan)

    def test_confirmed_cross_requires_exact_validation_scope(self):
        for name in ['CROSS_GC', 'CROSS_DC']:
            item = record(name)
            self.assertEqual(item.status, 'confirmed')
            self.assertFalse(item.can_use_for_trade_plan, 'technical fact is not strategy approval')
            for change in [dict(source_strategy='Other'), dict(strategy_revision='changed'),
                           dict(calculation_profile='custom-periods'), dict(validation_reference=None)]:
                with self.subTest(name=name, change=change):
                    self.assertEqual(record(name, **change).status, 'candidate')

    def test_missing_and_naive_times_are_conservative(self):
        for change in [dict(confirmed_at=None), dict(observed_at=None), dict(bar_time=None),
                       dict(confirmed_at='not a timestamp'), dict(confirmed_at=T0.replace(tzinfo=None)),
                       dict(observed_at='2026-01-05 10:30'), dict(instrument=''), dict(timeframe='')]:
            with self.subTest(change=change):
                self.assertEqual(record('CROSS_GC', **change).status, 'candidate')

    def test_temporal_order_does_not_confirm_future_or_backdated_events(self):
        for change in [dict(confirmed_at=T0 - timedelta(minutes=1)),
                       dict(confirmed_at=T0 + timedelta(minutes=3)),
                       dict(bar_time=T0 + timedelta(minutes=3))]:
            self.assertEqual(record('CROSS_DC', **change).status, 'candidate')

    def test_false_conditions_are_observations_not_confirmed_events(self):
        item = record('CROSS_GC', active=False)
        self.assertEqual(item.status, 'candidate')
        self.assertFalse(item.active)
        self.assertIn('False', item.reason)

    def test_unknown_signal_and_indicator_snapshots_deny_trade_plans(self):
        for name in ['MACD', 'KDJ', 'CCI', 'SOME_NEW_BUY']:
            self.assertEqual(record(name).status, 'candidate')
            self.assertFalse(record(name).can_use_for_trade_plan)

    def test_time_meanings_are_separate_and_timezone_normalized(self):
        item = record('BACKSET', record_kind='historical_marker', extremum_at=T0,
                      marker_appeared_at=T0 + timedelta(seconds=30))
        self.assertEqual(item.extremum_at, T0.isoformat())
        self.assertNotEqual(item.extremum_at, item.marker_appeared_at)
        self.assertNotEqual(item.marker_appeared_at, item.confirmed_at)
        self.assertNotEqual(item.confirmed_at, item.observed_at)
        local = datetime.fromisoformat('2026-01-05T10:30:00+08:00')
        self.assertEqual(record(bar_time=local).bar_time, T0.isoformat())

    def test_records_and_nested_payload_are_immutable_snapshots(self):
        payload = {'nested': {'values': [1, 2]}}
        item = record(payload=payload)
        payload['nested']['values'][0] = 10
        decoded = item.payload
        decoded['nested']['values'][0] = 20
        self.assertEqual(item.payload['nested']['values'], [1, 2])
        with self.assertRaises(FrozenInstanceError):
            item.active = False

    def test_bad_values_do_not_become_active_by_python_truthiness(self):
        for bad in ['False', None, 1, float('nan')]:
            with self.assertRaises(TypeError):
                record(active=bad)
        with self.assertRaises(ValueError):
            record(payload={'MACD': float('nan')})

    def test_required_output_fields_and_json_roundtrip(self):
        item = record()
        self.assertTrue({'signal_type', 'source_strategy', 'observed_at', 'bar_time', 'bar_closed',
                         'status', 'reason', 'can_use_for_trade_plan'}.issubset(item.to_dict()))
        self.assertEqual(item, SignalRecord.from_dict(json.loads(json.dumps(item.to_dict()))))


class TestSignalHistory(unittest.TestCase):
    def test_withdrawal_appends_without_erasing_candidate(self):
        first = record()
        withdrawn = record(active=False, observed_at=T0 + timedelta(minutes=3))
        original = append_record((), first)
        later = append_record(original, withdrawn)
        self.assertEqual(len(original), 1)
        self.assertEqual(len(later), 2)
        self.assertTrue(later[0].active)
        self.assertFalse(later[1].active)
        self.assertEqual(first.key, withdrawn.key)

    def test_confirmed_event_cannot_be_silently_recomputed(self):
        first = record('CROSS_GC')
        history = append_record((), first)
        for change in [dict(active=False), dict(payload={'MACD': 999}),
                       dict(bar_closed=None), dict(confirmed_at=T0)]:
            with self.subTest(change=change):
                later = record('CROSS_GC', observed_at=T0 + timedelta(minutes=3), **change)
                with self.assertRaises(ConfirmedEventConflict):
                    append_record(history, later)
        self.assertEqual(history, (first,))

    def test_verified_real_crosses_remain_confirmed_after_future_append(self):
        from strategy import KdjMacdStrategy
        from test_signal_causality import fixed_bars
        strategy = KdjMacdStrategy()
        bars = fixed_bars(100)
        full = strategy.compute(bars)
        history = ()
        for n in range(2, 81):
            prefix = strategy.compute(bars.iloc[:n])
            flags = strategy._cross_np(prefix.DIF.to_numpy(), prefix.DEA.to_numpy())
            if flags[-1]:
                row = prefix.iloc[-1]
                when = datetime.fromisoformat(row.time)
                item = record('CROSS_GC', bar_time=when,
                              confirmed_at=when + timedelta(minutes=1),
                              observed_at=when + timedelta(minutes=1),
                              payload={'DIF': float(row.DIF), 'DEA': float(row.DEA)})
                self.assertEqual(item.status, 'confirmed')
                history = append_record(history, item)
                future = full.iloc[n - 1]
                later = record('CROSS_GC', bar_time=when,
                               confirmed_at=when + timedelta(minutes=1),
                               observed_at=when + timedelta(minutes=100),
                               payload={'DIF': float(future.DIF), 'DEA': float(future.DEA)})
                history = append_record(history, later)
        self.assertGreater(len(history), 0, 'must observe actual golden-cross events')
        self.assertTrue(all(r.status == 'confirmed' for r in history))

    def test_identical_confirmed_fact_can_be_observed_later(self):
        first = record('CROSS_DC')
        second = record('CROSS_DC', observed_at=T0 + timedelta(minutes=3))
        history = append_record(append_record((), first), second)
        self.assertEqual(history[0], first)
        self.assertEqual(history[1].status, 'confirmed')

    def test_history_distinguishes_instrument_period_pool_and_revision(self):
        first = record('CROSS_GC')
        history = (first,)
        for change in [dict(instrument='OTHER'), dict(timeframe='5MIN'), dict(scope='pool2'),
                       dict(strategy_revision='new-version')]:
            later = record('CROSS_GC', active=False, **change)
            history = append_record(history, later)
        self.assertEqual(len(history), 5)
        self.assertEqual(history[0], first)

    def test_live_and_retrospective_records_are_physically_separate(self):
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            live = record('CROSS_GC')
            marker = record('CROSS_GC', record_kind='historical_marker', extremum_at=T0,
                            marker_appeared_at=T0 + timedelta(minutes=1))
            journal.record(live)
            journal.record(marker)
            self.assertEqual(journal.records(), (live,))
            self.assertEqual(journal.records('historical_marker'), (marker,))
            self.assertTrue((Path(directory) / 'observations.jsonl').exists())
            self.assertTrue((Path(directory) / 'historical_markers.jsonl').exists())

    def test_withdrawal_survives_journal_restart(self):
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            journal.record(record())
            journal.record(record(active=False, observed_at=T0 + timedelta(minutes=3)))
            recovered = SignalJournal(directory)
            self.assertEqual([r.active for r in recovered.records()], [True, False])
            self.assertEqual(recovered.records(), journal.records())

    def test_confirmed_conflict_is_rejected_after_restart_without_disk_write(self):
        with TemporaryDirectory() as directory:
            SignalJournal(directory).record(record('CROSS_GC'))
            path = Path(directory) / 'observations.jsonl'
            before = path.read_bytes()
            recovered = SignalJournal(directory)
            with self.assertRaises(ConfirmedEventConflict):
                recovered.record(record('CROSS_GC', active=False, observed_at=T0 + timedelta(minutes=3)))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(recovered.records()), 1)

    def test_forged_confirmation_cannot_be_loaded_or_appended(self):
        forged = replace(record(), status='confirmed', can_use_for_trade_plan=True)
        with self.assertRaises(ValueError):
            append_record((), forged)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'observations.jsonl'
            path.write_text(json.dumps(forged.to_dict()) + '\n')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                SignalJournal(directory)
            self.assertEqual(path.read_bytes(), before)

    def test_corrupt_journal_is_not_silently_replaced(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'observations.jsonl'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                SignalJournal(directory)
            self.assertEqual(path.read_text(), '{broken')

    def test_backdated_or_unidentifiable_records_rejected(self):
        with self.assertRaises(ValueError):
            append_record((record(),), record(observed_at=T0 + timedelta(seconds=30)))
        for change in [dict(observed_at=None), dict(bar_time=None), dict(instrument=''), dict(timeframe='')]:
            with self.assertRaises(ValueError):
                append_record((), record(**change))


class TestLegacyPolicyIntegration(unittest.TestCase):
    def test_journal_write_failure_is_not_reported_as_recorded(self):
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            with patch.object(journal, 'record', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    record_latest_signal(journal, signal(), observed_at=T0 + timedelta(minutes=1),
                                         timeframe='1MIN', scope='pool1')
            self.assertEqual(journal.records(), ())

    def test_adapter_records_same_bar_withdrawal_without_mutating_signal(self):
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            first = signal()
            original = dict(vars(first))
            record_latest_signal(journal, first, observed_at=T0 + timedelta(minutes=1),
                                 timeframe='1MIN', scope='pool1')
            record_latest_signal(journal, signal(buy_b=False), observed_at=T0 + timedelta(minutes=2),
                                 timeframe='1MIN', scope='pool1')
            recovered = SignalJournal(directory).records()
            self.assertEqual([r.active for r in recovered], [True, False])
            self.assertTrue(all(r.status == 'candidate' and r.bar_closed is None for r in recovered))
            self.assertEqual(vars(first), original)
            self.assertEqual(recovered[0].bar_time, T0.isoformat())

    def test_later_bar_does_not_erase_an_earlier_observation(self):
        with TemporaryDirectory() as directory:
            journal = SignalJournal(directory)
            record_latest_signal(journal, signal(), observed_at=T0 + timedelta(minutes=1),
                                 timeframe='1MIN', scope='pool1')
            record_latest_signal(journal, signal(bar_time='2026-01-05 10:31', buy_b=False),
                                 observed_at=T0 + timedelta(minutes=2), timeframe='1MIN', scope='pool1')
            self.assertEqual(len(journal.records()), 1)
            self.assertTrue(journal.records()[0].active)

    def test_notice_labels_candidates_and_invalid_without_claiming_a_buy(self):
        self.assertIn('重绘风险', format_signal_notice('BUY'))
        self.assertIn('candidate', format_signal_notice('SELL'))
        self.assertIn('非已确认反转', format_signal_notice('BUY_JB'))
        self.assertIn('invalid', format_signal_notice('BOTTOM_FISHING'))

    def _isolated_scan_fixture(self, journal, *, with_quality):
        # Compile existing methods, not a copied implementation or main imports.
        source = ast.parse((ROOT / 'main.py').read_text())
        app_node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'QuantAlertApp')
        app_node.body = [n for n in app_node.body if isinstance(n, ast.FunctionDef)
                         and n.name in {'_scan_once', '_build_email_content', '_signal_key', '_pass_cooldown'}]
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                                 app_node], type_ignores=[])
        namespace = dict(datetime=datetime, timedelta=timedelta, record_latest_signal=record_latest_signal,
                         format_signal_notice=format_signal_notice,
                         _load_tickers=lambda _: [SimpleNamespace(ts_code='600000.SH', display='demo')],
                         AlertItem=lambda **kw: SimpleNamespace(**kw))
        exec(compile(ast.fix_missing_locations(module), 'main.py (isolated methods)', 'exec'), namespace)
        app = namespace['QuantAlertApp'].__new__(namespace['QuantAlertApp'])
        app.config = SimpleNamespace(pools=[SimpleNamespace(pool_id='pool1', display_name='pool', tickers_file='unused')],
                                     min_freq='1MIN', min_bars_required=150, signal_cooldown_minutes=0)
        import pandas as pd
        bars = pd.DataFrame([dict(ts_code='600000.SH', time='2026-01-05 10:30:00',
                                  open=80., high=81., low=79., close=80., vol=1000., amount=80000.)])
        bars.attrs = {'ts_code': '600000.SH', 'freq': '1MIN'}
        app.data_source = SimpleNamespace(get_minute_bars=lambda **_: bars)
        if with_quality:
            from market_data_state import MarketDataState, TradingCalendar
            quality = MarketDataState(calendar=TradingCalendar({T0.date(): True}))
            for seconds, vol in [(10, 1000.), (20, 1100.)]:
                bars['vol'] = vol
                quality.request('600000.SH', '1MIN', T0 + timedelta(seconds=seconds))
                quality.observe('600000.SH', '1MIN', bars, T0 + timedelta(seconds=seconds))
            snap = quality.snapshot('600000.SH', '1MIN', T0 + timedelta(seconds=20))
            self.assertEqual(snap.observation_freshness, 'fresh')
            self.assertEqual(snap.bars[-1].state, 'unknown')
            app.data_source = SimpleNamespace(get_minute_snapshot=lambda **_: (bars.copy(deep=True), snap))
        current = signal(ts_code='600000.SH')
        app.strategy = SimpleNamespace(latest_signal=lambda **_: current)
        app._ticker_display, app._last_seen_bar_time, app._last_sent_at = {}, {}, {}
        app._sent_cache = set()
        app._strategy_revision = STRATEGY_REVISION
        app._log = lambda _: None
        app.signal_journal = journal
        return app, current

    def test_scan_without_quality_evidence_records_candidate_but_does_not_notify(self):
        with TemporaryDirectory() as directory:
            app, current = self._isolated_scan_fixture(SignalJournal(directory), with_quality=False)
            alerts, _ = app._scan_once()
            self.assertEqual(alerts, [])
            records = app.signal_journal.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, 'candidate')
            self.assertIsNone(records[0].bar_closed)
            self.assertFalse(records[0].can_use_for_trade_plan)
            current.buy_b = False
            self.assertEqual(app._scan_once()[0], [])
            self.assertEqual([r.active for r in SignalJournal(directory).records()], [True, False])

    def test_scan_with_continuous_quality_evidence_notifies_only_candidate(self):
        with TemporaryDirectory() as directory:
            app, current = self._isolated_scan_fixture(SignalJournal(directory), with_quality=True)
            alerts, _ = app._scan_once()
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].side, 'BUY')
            self.assertIn('candidate', app._build_email_content(alerts))
            self.assertIn('重绘风险', app._build_email_content(alerts))
            self.assertEqual(len(app.signal_journal.records()), 1)
            saved = app.signal_journal.records()[0]
            self.assertEqual(saved.status, 'candidate')
            self.assertIsNone(saved.bar_closed)
            self.assertIsNone(saved.confirmed_at)
            self.assertFalse(saved.can_use_for_trade_plan)
            current.buy_b = False
            alerts, _ = app._scan_once()
            self.assertEqual(alerts, [])
            self.assertEqual([r.active for r in app.signal_journal.records()], [True, False])

    def test_classification_leaves_every_original_indicator_and_output_unchanged(self):
        import pandas as pd
        from strategy import KdjMacdStrategy
        from test_signal_causality import fixed_bars
        bars = fixed_bars(160)
        before = KdjMacdStrategy().compute(bars)
        frozen = before.copy(deep=True)
        for _, row in before.iterrows():
            for name in ['BUY_B', 'SELL_S', 'BUY_JB', 'SELL_JS']:
                classify_signal(name, active=bool(row[name]), observed_at=T0 + timedelta(hours=1),
                                bar_time=row.time, bar_closed=None,
                                payload={c: float(row[c]) for c in ['DIF', 'DEA', 'MACD']})
        pd.testing.assert_frame_equal(before, frozen, check_exact=True)
        pd.testing.assert_frame_equal(KdjMacdStrategy().compute(bars), frozen, check_exact=True)
        self.assertEqual(hashlib.sha256((ROOT / 'strategy.py').read_bytes()).hexdigest(), STRATEGY_REVISION)


if __name__ == '__main__':
    unittest.main()
