"""Offline readiness checks: actual main methods + existing Flask WSGI routes.

Fixtures live exclusively in TemporaryDirectory. No daemon, socket, notification
or production cache is opened. Flask must be available in the test environment.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from test_market_data_state import CODE, CAL, dt, frame, state
from test_signal_policy import signal
from signal_policy import SignalJournal, STRATEGY_REVISION, record_latest_signal, format_signal_notice
from strategy import KdjMacdStrategy
from test_signal_causality import fixed_bars

ROOT = Path(__file__).resolve().parents[1]


class TestRuntimeReadiness(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Execute actual loader/scanner/content methods, excluding main imports
        # and __init__ (which would connect and start monitor threads).
        tree = ast.parse((ROOT / 'main.py').read_text())
        body = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == '_load_tickers':
                body.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == 'TickerEntry':
                body.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == 'QuantAlertApp':
                node.body = [n for n in node.body if isinstance(n, ast.FunctionDef)
                             and n.name in {'_scan_once', '_signal_key', '_pass_cooldown', '_build_email_content'}]
                body.append(node)
        mod = ModuleType('offline_readiness_main')
        mod.__dict__.update(dataclass=dataclass, datetime=datetime, timedelta=timedelta,
                            record_latest_signal=record_latest_signal, format_signal_notice=format_signal_notice,
                            AlertItem=lambda **kw: SimpleNamespace(**kw))
        with patch.dict(sys.modules, {mod.__name__: mod}):
            module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + body, type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), 'main.py (readiness methods)', 'exec'), mod.__dict__)
        self.loader = mod._load_tickers
        self.app = mod.QuantAlertApp.__new__(mod.QuantAlertApp)
        self.app.config = SimpleNamespace(pools=[], min_freq='1MIN', min_bars_required=1, signal_cooldown_minutes=0)
        self.app._ticker_display, self.app._last_seen_bar_time, self.app._last_sent_at = {}, {}, {}
        self.app._sent_cache = set()
        self.app._strategy_revision = STRATEGY_REVISION
        self.app.signal_journal = SignalJournal(self.root / 'signals')
        self.lines = []
        self.app._log = lambda text: self.lines.append(f'[2026-01-05 10:30:20] {text}')
        self.frames, self.signals = {}, {}
        # Use actual production PyTDX-unknown contract, not the fake verified one.
        from market_data_state import PYTDX_CONTRACT
        self.quality = state(PYTDX_CONTRACT)
        for code in [CODE, '000001.SZ']:
            for freq in ['1MIN', '5MIN']:
                batch = frame(['2026-01-05 10:30:00'], freq)
                batch['ts_code'] = code
                batch.attrs['ts_code'] = code
                for second, vol in [(10, 1000.), (20, 1100.)]:
                    batch['vol'] = vol
                    now = dt('2026-01-05 10:30') + timedelta(seconds=second)
                    self.quality.request(code, freq, now)
                    self.quality.observe(code, freq, batch, now)
                self.frames[(code, freq)] = batch.copy(deep=True)
                self.signals[code] = signal(ts_code=code, close=20.)
        self.app.data_source = SimpleNamespace(get_minute_snapshot=lambda ts_code:
            (self.frames[(ts_code, self.app.config.min_freq)].copy(deep=True),
             self.quality.snapshot(ts_code, self.app.config.min_freq, dt('2026-01-05 10:30:20'))))
        self.app.strategy = SimpleNamespace(latest_signal=lambda ts_code, **_: self.signals[ts_code])

    def pool(self, name='pool1', text=None):
        path = self.root / f'{name}.txt'
        path.write_text(text if text is not None else f'{CODE} # 离线样本\n', encoding='utf-8')
        item = SimpleNamespace(pool_id=name, display_name=name, tickers_file=str(path))
        self.app.config.pools.append(item)
        return path

    def test_real_pool_loader_scans_all_stocks_and_deduplicates_file_entries(self):
        path = self.pool(text=f'# comment\n1|{CODE} # 样本甲\n{CODE} # duplicate\n000001.sz # 样本乙\n')
        tickers = self.loader(str(path))
        self.assertEqual([t.ts_code for t in tickers], [CODE, '000001.SZ'])
        alerts, new = self.app._scan_once()
        self.assertTrue(new)
        self.assertEqual({a.signal.ts_code for a in alerts}, {CODE, '000001.SZ'})
        self.assertEqual(len(self.app.signal_journal.records()), 2)

    def test_empty_pool_no_spurious_observations(self):
        self.pool(text='# empty\n')
        self.assertEqual(self.app._scan_once(), ([], False))
        self.assertEqual(self.app.signal_journal.records(), ())

    def test_six_raw_signal_fields_preserved_but_invalid_branches_not_notified(self):
        self.pool()
        attrs = {'buy_b': ('BUY_B', 'BUY'), 'sell_s': ('SELL_S', 'SELL'),
                 'buy_jb': ('BUY_JB', 'BUY_JB'), 'sell_js': ('SELL_JS', 'SELL_JS'),
                 'bottom_fishing': ('BOTTOM_FISHING', None), 'top_escape': ('TOP_ESCAPE', None)}
        for attr, (policy_name, notice) in attrs.items():
            with self.subTest(attr=attr):
                raw = signal(ts_code=CODE, close=20., **{key: key == attr for key in attrs})
                original = dict(vars(raw))
                self.signals[CODE] = raw
                alerts, _ = self.app._scan_once()
                self.assertEqual([a.side for a in alerts], [notice] if notice else [])
                latest = [r for r in self.app.signal_journal.records() if r.signal_type == policy_name][-1]
                self.assertTrue(latest.active)
                self.assertEqual(latest.status, 'candidate' if notice else 'invalid')
                self.assertFalse(latest.can_use_for_trade_plan)
                self.assertEqual(vars(raw), original)
        # These forced booleans test routing only, not natural reachability.

    def test_real_strategy_output_remains_unchanged_through_actual_scanner(self):
        self.pool()
        bars = fixed_bars(160)
        times = list(pd.date_range('2026-01-05 09:31', periods=120, freq='min'))
        times += list(pd.date_range('2026-01-05 13:01', periods=40, freq='min'))
        bars['time'] = [t.strftime('%Y-%m-%d %H:%M:%S') for t in times]
        bars['ts_code'] = CODE
        bars.attrs = {'ts_code': CODE, 'freq': '1MIN'}
        from market_data_state import MarketDataState
        quality = MarketDataState(calendar=CAL)
        for seconds, vol in [(10, 10000.), (20, 10100.)]:
            bars.loc[len(bars) - 1, 'vol'] = vol
            now = dt('2026-01-05 13:40') + timedelta(seconds=seconds)
            quality.request(CODE, '1MIN', now)
            quality.observe(CODE, '1MIN', bars, now)
        snap = quality.snapshot(CODE, '1MIN', now)
        self.assertEqual(snap.observation_freshness, 'fresh')
        self.app.data_source = SimpleNamespace(get_minute_snapshot=lambda **_: (bars.copy(deep=True), snap))
        self.app.strategy = KdjMacdStrategy()
        self.app.config.min_bars_required = 150
        expected = self.app.strategy.latest_signal(CODE, bars, 150)
        frozen = bars.copy(deep=True)
        self.app._scan_once()
        self.assertEqual(self.app.strategy.latest_signal(CODE, bars, 150), expected)
        pd.testing.assert_frame_equal(bars, frozen)
        line = next(s for s in self.lines if 'DIF=' in s)
        for text, attr in [('B', 'buy_b'), ('S', 'sell_s'), ('JB', 'buy_jb'), ('JS', 'sell_js'),
                           ('底', 'bottom_fishing'), ('顶', 'top_escape')]:
            self.assertIn(f'{text}={getattr(expected, attr)}', line)

    def test_pool_period_and_instrument_journal_and_notification_isolation(self):
        self.pool('pool1', f'{CODE}\n000001.SZ\n')
        self.pool('pool2', f'{CODE}\n')
        first, _ = self.app._scan_once()
        self.assertEqual(len(first), 3)
        for a in first:
            self.app._sent_cache.add(self.app._signal_key(a.signal, a.side, a.pool_id))
        self.assertEqual(self.app._scan_once()[0], [])
        self.app.config.min_freq = '5MIN'
        second, _ = self.app._scan_once()
        self.assertEqual(len(second), 3)
        identities = {(r.instrument, r.timeframe, r.scope) for r in self.app.signal_journal.records()}
        self.assertEqual(len(identities), 6)
        self.assertEqual({r.status for r in self.app.signal_journal.records()}, {'candidate'})

    def test_jsonl_byte_prefix_preserved_with_repeat_and_true_false_withdrawal(self):
        self.pool()
        alerts, _ = self.app._scan_once()
        self.assertEqual(len(alerts), 1)
        self.app._sent_cache.add(self.app._signal_key(alerts[0].signal, alerts[0].side, alerts[0].pool_id))
        path = self.root / 'signals' / 'observations.jsonl'
        first = path.read_bytes()
        self.assertEqual(self.app._scan_once()[0], [])
        self.signals[CODE].buy_b = False
        self.assertEqual(self.app._scan_once()[0], [])
        self.assertTrue(path.read_bytes().startswith(first))
        self.assertEqual([r.active for r in SignalJournal(path.parent).records()], [True, True, False])

    def load_flask(self):
        # Normal existing module execution: __main__ server block never runs.
        spec = importlib.util.spec_from_file_location('offline_existing_webapp', ROOT / 'webapp' / 'app.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
        module.app.testing = True
        # Explicit filesystem-backed template directory; no new template/page.
        module.app.template_folder = str(ROOT / 'webapp' / 'templates')
        return module

    def test_existing_flask_page_and_readonly_apis_accept_real_scan_log_format(self):
        self.pool()
        alerts, _ = self.app._scan_once()
        self.assertIn('candidate', self.app._build_email_content(alerts))
        log = self.root / 'sample.log'
        log.write_text('\n'.join(self.lines), encoding='utf-8')
        web = self.load_flask()
        pool2 = self.root / 'pool2.txt'
        pool2.write_text('', encoding='utf-8')
        with patch.object(web, 'get_today_log', return_value=str(log)), \
             patch.object(web, 'check_daemon', return_value=False), \
             patch.object(web, 'TICKERS_1', str(self.root / 'pool1.txt')), \
             patch.object(web, 'TICKERS_2', str(pool2)):
            client = web.app.test_client()
            self.assertEqual(client.get('/').status_code, 200)
            self.assertIn(b'html', client.get('/').data.lower())
            self.assertEqual(client.get('/api/status').status_code, 200)
            self.assertEqual(client.get('/api/pools').json['pool1'][0]['code'], CODE)
            self.assertEqual(len(client.get('/api/signals').json['signals']), 1)
            result = client.get(f'/api/stock/{CODE}/bars')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['count'], 1)
            self.assertEqual(result.json['signals'][0]['label'], 'B')
            self.assertEqual(client.get(f'/api/stock/{CODE}/latest').json['bar']['close'], 20.)
            self.assertTrue(client.get('/api/log/tail').json['lines'])

    def test_existing_flask_empty_log_paths_remain_readable(self):
        web = self.load_flask()
        with patch.object(web, 'get_today_log', return_value=None), patch.object(web, 'check_daemon', return_value=False):
            client = web.app.test_client()
            for route in ['/', '/api/status', '/api/signals', '/api/log/tail',
                          f'/api/stock/{CODE}/bars', f'/api/stock/{CODE}/latest']:
                self.assertEqual(client.get(route).status_code, 200)
            self.assertEqual(client.get('/api/signals').json['signals'], [])
            self.assertEqual(client.get(f'/api/stock/{CODE}/bars').json['bars'], [])


if __name__ == '__main__':
    unittest.main()
