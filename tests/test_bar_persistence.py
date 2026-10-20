"""P0-04-3 offline disk/lifecycle tests: temp CSVs, no real threads or servers.

Actual fetcher/main/seal methods are AST-loaded excluding import side effects.
Simulated interleavings run synchronously inside a write hook, not background
tasks. Never read, migrate or delete runtime/cache or a real notification config.
"""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from test_bar_cache_merge import Fetcher, bars, ROOT
from bar_cache_merge import BAR_COLUMNS, BarCacheValidationError
from test_market_data_state import CAL, dt


class TestBarPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tick_time = 0.
        self.f = Fetcher(['600000.SH'], cache_dir=self.temp.name, quality_calendar=CAL,
                         clock=lambda: dt('2026-01-05 10:30'), monotonic=lambda: self.tick_time)
        self.f._api.disconnect = Mock()
        self.f._ensure_connected = Mock(return_value=True)
        self.f._fetch_realtime_quotes = Mock(return_value=[])
        self.code = '600000.SH'
        self.path = self.f._cache_path(self.code)
        self.globals = Fetcher._save_disk_cache.__globals__

    def tick(self, frame):
        self.f._fetch_minute_bars = Mock(return_value=frame)
        self.f._last_min_bar_fetch = 0
        with redirect_stdout(StringIO()):
            self.f._tick_once()

    def flush(self, **kwargs):
        with redirect_stdout(StringIO()):
            return self.f.flush_pending(force=True, **kwargs)

    def status(self):
        return self.f.get_persistence_status(self.code)

    def initial_saved(self):
        self.tick(bars(20))
        self.assertTrue(self.flush())
        return self.path.read_bytes()

    def test_atomic_save_roundtrip_eight_columns_and_all_values(self):
        original = bars(20)
        self.tick(original)
        self.assertTrue(self.flush())
        restored = self.f._load_disk_cache(self.code)
        pd.testing.assert_frame_equal(original, restored, check_dtype=False, check_exact=True)
        self.assertEqual(pd.read_csv(self.path).columns.tolist(), BAR_COLUMNS)
        self.assertFalse(self.status()['dirty'])
        self.assertEqual(list(Path(self.temp.name).glob('*.tmp')), [])

    def test_write_interruption_keeps_original_valid_csv(self):
        original = self.initial_saved()
        new = bars(1, start=19)
        new['vol'] += 10
        self.tick(new)
        def interrupted(frame, stream, **kwargs):
            stream.write('half-written invalid CSV')
            raise OSError('injected write interruption')
        with patch.object(pd.DataFrame, 'to_csv', interrupted):
            self.assertFalse(self.flush())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.f._load_disk_cache(self.code)), 20)
        self.assertTrue(self.status()['dirty'])

    def test_replace_failure_keeps_original_and_pending_then_retry_succeeds(self):
        original = self.initial_saved()
        self.tick(bars(1, start=20))
        with patch.object(self.globals['os'], 'replace', side_effect=OSError('replace denied')):
            self.assertFalse(self.flush())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.status()['dirty'])
        self.assertIn('replace denied', self.status()['last_error'])
        self.assertTrue(self.flush())
        self.assertFalse(self.status()['dirty'])
        self.assertEqual(len(self.f._load_disk_cache(self.code)), 21)

    def test_temporary_file_is_in_same_directory_and_complete_before_replace(self):
        self.tick(bars(5))
        real_replace = self.globals['os'].replace
        def inspect(source, target):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(target), self.path)
            self.assertTrue(Path(source).name.startswith('.' + self.path.name))
            self.assertEqual(pd.read_csv(source).columns.tolist(), BAR_COLUMNS)
            self.assertEqual(len(pd.read_csv(source)), 5)
            real_replace(source, target)
        with patch.object(self.globals['os'], 'replace', side_effect=inspect):
            self.assertTrue(self.flush())

    def test_validation_failure_never_replaces_original(self):
        original = self.initial_saved()
        bad = bars(1)
        bad['close'] = float('nan')
        with self.assertRaises(BarCacheValidationError):
            self.f._save_disk_cache(self.code, bad)
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_serialization_is_detected_before_replacement(self):
        original = self.initial_saved()
        self.tick(bars(1, start=20))
        def corrupt(frame, stream, **kwargs):
            stream.write('not,the,expected,columns\n1,2,3,4\n')
        with patch.object(pd.DataFrame, 'to_csv', corrupt):
            self.assertFalse(self.flush())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.status()['dirty'])

    def test_file_fsync_failure_keeps_old_file_and_pending(self):
        original = self.initial_saved()
        self.tick(bars(1, start=20))
        with patch.object(self.globals['os'], 'fsync', side_effect=OSError('fsync failed')):
            self.assertFalse(self.flush())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.status()['dirty'])

    def test_directory_sync_failure_reports_unacknowledged_valid_replacement(self):
        self.initial_saved()
        self.tick(bars(1, start=20))
        real_sync = self.globals['os'].fsync
        calls = []
        def sync(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError('directory sync failed')
            real_sync(descriptor)
        with patch.object(self.globals['os'], 'fsync', side_effect=sync):
            self.assertFalse(self.flush())
        self.assertEqual(len(self.f._load_disk_cache(self.code)), 21)
        self.assertTrue(self.status()['dirty'])
        self.assertIn('directory sync failed', self.status()['last_error'])

    def test_orphan_temp_is_not_loaded_as_formal_cache(self):
        self.initial_saved()
        orphan = Path(self.temp.name) / ('.' + self.path.name + '.orphan.tmp')
        orphan.write_text('broken CSV', encoding='utf-8')
        self.assertEqual(len(self.f._load_disk_cache(self.code)), 20)
        self.path.unlink()  # Only this test's generated CSV.
        self.assertTrue(self.f._load_disk_cache(self.code).empty)
        self.assertTrue(orphan.exists())

    def test_every_same_bar_ohlcv_or_amount_revision_is_dirty(self):
        replacements = dict(open=20.05, high=21., low=19., close=20.05, vol=2000., amount=90000.)
        for column, value in replacements.items():
            with self.subTest(column=column):
                self.tick(bars(20))
                self.assertTrue(self.flush())
                revised = bars(1, start=19)
                revised[column] = value
                version = self.status()['version']
                self.tick(revised)
                self.assertTrue(self.status()['dirty'])
                self.assertEqual(self.status()['version'], version + 1)
                self.assertEqual(self.f._bar_count_since_save[self.code], 0)
                self.tick_time += 30
                self.assertTrue(self.f.flush_pending())
                self.assertEqual(self.f._load_disk_cache(self.code).iloc[-1][column], value)

    def test_identical_batch_does_not_dirty_or_write_even_with_numeric_dtype_change(self):
        self.initial_saved()
        version = self.status()['version']
        repeated = bars(20)
        repeated['vol'] = repeated['vol'].astype(int)
        self.tick(repeated)
        self.assertEqual(self.status()['version'], version)
        self.assertFalse(self.status()['dirty'])
        with patch.object(self.f, '_save_disk_cache') as writer:
            self.assertTrue(self.flush())
            writer.assert_not_called()

    def test_first_small_change_waits_for_cadence_and_failures_are_paced(self):
        self.tick(bars(1))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.f.flush_pending())
        self.assertFalse(self.path.exists())
        self.tick_time = 30
        with patch.object(self.f, '_save_disk_cache', side_effect=OSError('retry later')) as writer:
            with redirect_stdout(StringIO()):
                self.assertFalse(self.f.flush_pending())
                self.tick_time = 31
                self.assertTrue(self.f.flush_pending())  # No attempt due; dirty remains.
            self.assertEqual(writer.call_count, 1)
            self.assertTrue(self.status()['dirty'])
        self.tick_time = 60
        self.assertTrue(self.f.flush_pending())
        self.assertFalse(self.status()['dirty'])

    def test_update_during_save_is_not_acknowledged_as_saved(self):
        self.tick(bars(20))
        captured = self.status()['version']
        real_save = self.f._save_disk_cache
        revised = bars(1, start=19)
        revised['amount'] += 500.
        def interleaved(code, snapshot):
            self.tick(revised)  # No thread: real merge/update while I/O is outside memory lock.
            real_save(code, snapshot)
        with patch.object(self.f, '_save_disk_cache', side_effect=interleaved):
            self.assertTrue(self.flush())
        self.assertEqual(self.status()['saved_version'], captured)
        self.assertTrue(self.status()['dirty'])
        self.assertNotEqual(self.f._load_disk_cache(self.code).iloc[-1].amount, revised.iloc[-1].amount)
        self.assertTrue(self.flush())
        self.assertFalse(self.status()['dirty'])
        self.assertEqual(self.f._load_disk_cache(self.code).iloc[-1].amount, revised.iloc[-1].amount)

    def test_new_bars_during_save_preserve_new_pending_count(self):
        self.tick(bars(20))
        real_save = self.f._save_disk_cache
        def interleaved(code, snapshot):
            self.tick(bars(2, start=20))
            real_save(code, snapshot)
        with patch.object(self.f, '_save_disk_cache', side_effect=interleaved):
            self.assertTrue(self.flush())
        self.assertEqual(self.f._bar_count_since_save[self.code], 2)
        self.assertTrue(self.status()['dirty'])
        self.assertTrue(self.flush())
        self.assertEqual(len(self.f._load_disk_cache(self.code)), 22)

    def test_long_history_is_persisted_in_full_after_200_bar_refresh(self):
        self.tick(bars(3000))
        self.tick(bars(200, start=2900))
        self.assertTrue(self.flush())
        restored = self.f._load_disk_cache(self.code)
        self.assertEqual(len(restored), 3100)
        self.assertEqual(restored.iloc[0].time, bars(1).iloc[0].time)

    def test_restore_does_not_migrate_or_dirty_disk_bytes(self):
        old = bars(50)
        old['time'] = old['time'].str.slice(0, 16)
        old.to_csv(self.path, index=False)
        content = self.path.read_bytes()
        with redirect_stdout(StringIO()):
            self.f._load_all_disk_cache()
        self.assertFalse(self.status()['dirty'])
        self.assertTrue(self.flush())
        self.assertEqual(self.path.read_bytes(), content)

    def test_stop_flushes_pending_disconnects_and_is_idempotent(self):
        self.tick(bars(2))
        fetch = Mock()
        fetch.is_alive.return_value = False
        saver = Mock()
        saver.is_alive.return_value = False
        self.f._thread, self.f._save_thread = fetch, saver
        self.assertTrue(self.f.stop())
        self.assertTrue(self.f._stop_event.is_set())
        self.assertFalse(self.f._running)
        self.assertEqual(len(self.f._load_disk_cache(self.code)), 2)
        fetch.join.assert_called_once()
        saver.join.assert_called_once()
        self.f._api.disconnect.assert_called_once()
        self.assertTrue(self.f.stop())
        self.f._api.disconnect.assert_called_once()

    def test_stop_failure_reports_reason_and_keeps_pending_for_retry(self):
        self.tick(bars(2))
        output = StringIO()
        with patch.object(self.f, '_save_disk_cache', side_effect=OSError('disk full')), redirect_stdout(output):
            self.assertFalse(self.f.stop())
        self.assertIn('disk full', output.getvalue())
        self.assertIn('最终保存未完成', output.getvalue())
        self.assertTrue(self.status()['dirty'])
        self.assertTrue(self.f.stop())

    def test_stop_thread_timeout_is_not_claimed_success(self):
        self.tick(bars(2))
        self.f._thread = Mock()
        self.f._thread.is_alive.return_value = True
        output = StringIO()
        with redirect_stdout(output):
            self.assertFalse(self.f.stop(timeout=0))
        self.assertIn('行情线程退出超时', output.getvalue())
        self.f._api.disconnect.assert_not_called()

    def test_stop_disconnect_error_is_reported(self):
        self.f._api.disconnect.side_effect = OSError('disconnect failed')
        with redirect_stdout(StringIO()) as output:
            self.assertFalse(self.f.stop())
        self.assertIn('disconnect failed', output.getvalue())

    def test_stop_cannot_hang_on_a_busy_writer_lock(self):
        self.tick(bars(2))
        lock = Mock()
        lock.acquire.return_value = False
        self.f._save_lock = lock
        with redirect_stdout(StringIO()) as output:
            self.assertFalse(self.f.stop(timeout=0))
        self.assertIn('锁超时', output.getvalue())
        self.assertTrue(self.status()['dirty'])

    def test_stop_suppresses_further_tick_requests(self):
        self.assertTrue(self.f.stop())
        self.f._fetch_minute_bars = Mock(side_effect=AssertionError('must not request after stop'))
        self.f._tick_once()
        self.f._fetch_minute_bars.assert_not_called()

    def test_quality_sidecar_is_not_changed_by_persistence(self):
        self.tick(bars(2))
        before = self.f.get_market_status(self.code)
        self.assertTrue(self.flush())
        self.assertEqual(self.f.get_market_status(self.code), before)

    def test_unexpected_final_flush_exception_still_closes_connection(self):
        self.tick(bars(2))
        with patch.object(self.f, 'flush_pending', side_effect=RuntimeError('unexpected flush error')), redirect_stdout(StringIO()) as output:
            self.assertFalse(self.f.stop())
        self.assertIn('unexpected flush error', output.getvalue())
        self.f._api.disconnect.assert_called_once()
        self.assertTrue(self.status()['dirty'])

    def test_stop_does_not_claim_success_when_new_version_remains_pending(self):
        self.tick(bars(2))
        real_save = self.f._save_disk_cache
        def interleaved(code, snapshot):
            revised = bars(2)
            revised['amount'] += 100
            with self.f._lock:
                self.f._track_bar_count(code, revised)
                self.f._minute_bars[code] = revised
            real_save(code, snapshot)
        with patch.object(self.f, '_save_disk_cache', side_effect=interleaved), redirect_stdout(StringIO()) as output:
            self.assertFalse(self.f.stop())
        self.assertIn('仍有待保存版本', output.getvalue())
        self.assertTrue(self.status()['dirty'])

    def test_overlapping_flush_cannot_overwrite_inflight_snapshot(self):
        self.tick(bars(2))
        save = self.f._save_disk_cache
        calls = []
        def overlap(code, snapshot):
            calls.append(code)
            self.assertFalse(self.f.flush_pending(force=True, timeout=0))
            save(code, snapshot)
        with patch.object(self.f, '_save_disk_cache', side_effect=overlap), redirect_stdout(StringIO()):
            self.assertTrue(self.flush())
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.status()['dirty'])

    def test_save_loop_uses_interruptible_wait_and_flushes_revision(self):
        self.initial_saved()
        revised = bars(1, start=19)
        revised['vol'] += 100
        self.tick(revised)
        self.tick_time = 30
        self.f._save_thread_running = True
        self.f._stop_event = SimpleNamespace(wait=Mock(side_effect=[False, True]))
        self.f._save_loop()  # Bounded synchronous loop; no background thread.
        self.assertFalse(self.status()['dirty'])
        self.assertEqual(self.f._load_disk_cache(self.code).iloc[-1].vol, revised.iloc[-1].vol)
        self.assertEqual(self.f._stop_event.wait.call_count, 2)


def isolated_app_class():
    tree = ast.parse((ROOT / 'main.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'QuantAlertApp')
    node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in {'shutdown', '__init__'}]
    namespace = dict(Event=Event, Path=Path, __file__=str(ROOT/'main.py'),
                     hashlib=__import__('hashlib'), SignalJournal=Mock(),
                     KdjMacdStrategy=Mock(), SharedDataFetcher=Mock())
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), 'main.py (shutdown methods)', 'exec'), namespace)
    return namespace['QuantAlertApp'], namespace


class TestShutdownIntegration(unittest.TestCase):
    def test_real_main_poll_loop_honors_stop_event_without_sleeping(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'QuantAlertApp')
        node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == 'run_forever']
        ns = {'datetime': SimpleNamespace(now=lambda: dt('2026-01-05 10:30'))}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'main.py (poll loop)', 'exec'), ns)
        app = ns['QuantAlertApp'].__new__(ns['QuantAlertApp'])
        app.config = SimpleNamespace(poll_interval_per_minutes=1, min_freq='1MIN', min_bars_required=150,
             enable_email=False, enable_telegram=False, enable_wecom=False, enable_trading_hours=False,
             pools=[], heartbeat_interval_minutes=0)
        app._stop_requested = Event()
        app._log = Mock()
        app._load_all_tickers = lambda: []
        app.session_guard = None
        def scan():
            app._stop_requested.set()
            return [], False
        app._scan_once = Mock(side_effect=scan)
        app.run_forever()
        app._scan_once.assert_called_once()

    def test_seal_stop_failure_still_attempts_shared_fetcher_final_save(self):
        cls, _ = isolated_app_class()
        app = cls.__new__(cls)
        app._stop_requested = Event()
        app._log = Mock()
        app.seal_monitor = SimpleNamespace(stop=Mock(side_effect=OSError('seal stop error')))
        app.data_source = SimpleNamespace(stop=Mock(return_value=True))
        self.assertFalse(app.shutdown())
        app.data_source.stop.assert_called_once()
        self.assertTrue(app._stop_requested.is_set())

    def test_partial_constructor_failure_cleans_started_source(self):
        cls, ns = isolated_app_class()
        source = SimpleNamespace(connect=Mock(), start=Mock(side_effect=RuntimeError('partial startup')),
                                 stop=Mock(return_value=True))
        ns['SharedDataFetcher'].return_value = source
        cls._load_all_tickers = lambda self: []
        cls._log = Mock()
        cfg = SimpleNamespace(enable_email=False, enable_telegram=False, enable_wecom=False,
                              min_freq='1MIN', fetch_interval_seconds=1)
        with self.assertRaisesRegex(RuntimeError, 'partial startup'):
            cls(cfg)
        source.stop.assert_called_once()

    def test_partial_startup_interrupt_cannot_hide_failed_final_save(self):
        cls, ns = isolated_app_class()
        source = SimpleNamespace(connect=Mock(), start=Mock(side_effect=KeyboardInterrupt()),
                                 stop=Mock(return_value=False))
        ns['SharedDataFetcher'].return_value = source
        cls._load_all_tickers = lambda self: []
        cls._log = Mock()
        cfg = SimpleNamespace(enable_email=False, enable_telegram=False, enable_wecom=False,
                              min_freq='1MIN', fetch_interval_seconds=1)
        with self.assertRaisesRegex(RuntimeError, '初始化中止且退出清理未完成') as context:
            cls(cfg)
        self.assertIsInstance(context.exception.__context__, KeyboardInterrupt)
        source.stop.assert_called_once()

    def run_main(self, *, shutdown=True, outcome='return'):
        tree = ast.parse((ROOT / 'main.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        handlers = {2: object(), 15: object()}
        originals = dict(handlers)
        def install(sig, callback):
            old = handlers[sig]
            handlers[sig] = callback
            return old
        app = SimpleNamespace(shutdown=Mock(return_value=shutdown))
        def factory(config, stop_event):
            app.event = stop_event
            def loop():
                if outcome == 'signal':
                    handlers[15](15, None)
                    self.assertTrue(stop_event.is_set())
                elif outcome == 'keyboard':
                    raise KeyboardInterrupt()
                elif outcome == 'exception':
                    raise RuntimeError('injected main error')
            app.run_forever = loop
            return app
        ns = dict(Event=Event, signal=SimpleNamespace(SIGINT=2, SIGTERM=15, signal=install),
                  load_dotenv=Mock(), _build_config=Mock(), QuantAlertApp=factory)
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'main.py (entry only)', 'exec'), ns)
        with redirect_stdout(StringIO()):
            if outcome == 'exception':
                with self.assertRaisesRegex(RuntimeError, 'injected main error'):
                    ns['main']()
                result = None
            else:
                result = ns['main']()
        self.assertEqual(handlers, originals)
        app.shutdown.assert_called_once()
        return result

    def test_main_finally_shutdown_on_return_keyboard_sigterm_and_error(self):
        for outcome in ['return', 'keyboard', 'signal', 'exception']:
            self.assertEqual(self.run_main(outcome=outcome), None if outcome == 'exception' else 0)

    def test_main_exit_code_nonzero_if_final_save_not_acknowledged(self):
        self.assertEqual(self.run_main(shutdown=False), 1)

    def test_seal_import_has_no_signal_registration_but_standalone_does(self):
        tree = ast.parse((ROOT / 'seal_monitor.py').read_text())
        registrations = lambda node: [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
            and n.func.value.id == 'signal' and n.func.attr == 'signal']
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.If) and '__name__' in ast.unparse(node.test):
                self.assertEqual(len(registrations(node)), 2)
            else:
                self.assertEqual(registrations(node), [])

    def test_seal_wait_is_interruptible_and_stop_reports_alive_thread(self):
        tree = ast.parse((ROOT / 'seal_monitor.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SealMonitor')
        node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in {'_run', 'stop'}]
        ns = dict(log=Mock(), in_trading=lambda: False)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'seal_monitor.py (lifecycle only)', 'exec'), ns)
        monitor = ns['SealMonitor'].__new__(ns['SealMonitor'])
        event = Event()
        def wake(timeout):
            event.set()
        monitor._stop_event = SimpleNamespace(is_set=event.is_set, wait=Mock(side_effect=wake), set=event.set)
        monitor.symbols = []
        monitor._maybe_reload_symbols = Mock()
        monitor._run()
        monitor._stop_event.wait.assert_called_once_with(30)
        monitor._thread = Mock()
        monitor._thread.is_alive.return_value = True
        self.assertFalse(monitor.stop(timeout=0))
        monitor._thread.is_alive.return_value = False
        self.assertTrue(monitor.stop())


if __name__ == '__main__':
    unittest.main()
