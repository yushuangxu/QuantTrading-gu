"""Synthetic offline cache tests. No live TDX, .env, real cache or push imports.

python -B -m unittest discover -s tests -p 'test_bar_cache_merge.py' -v
The installed test runtime lacks pytdx: an AST loader executes the real fetcher
module excluding ONLY its pytdx import, injecting an API sentinel that cannot
connect. Actual merge/load/tick methods are tested, not copied implementations.
"""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bar_cache_merge import (
    BAR_COLUMNS, BarCacheValidationError, merge_minute_bars, normalize_minute_bars,
)


def bars(n=10, start=0, code='600000.SH', freq='1MIN'):
    i = np.arange(start, start + n)
    close = 20. + i * .001 + np.sin(i / 10) * .1
    period = int(freq.removesuffix('MIN'))
    time = pd.Timestamp('2026-01-05 09:30') + pd.to_timedelta(i * period, unit='min')
    out = pd.DataFrame({'ts_code': code, 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'open': close.copy(), 'high': close + .1, 'low': close - .1,
                        'close': close, 'vol': 1000. + i, 'amount': (1000. + i) * close})
    out.attrs = {'ts_code': code, 'freq': freq}
    return out


def merge(old, new, code='600000.SH', freq='1MIN'):
    return merge_minute_bars(old, new, ts_code=code, freq=freq)


class OfflineTdx:
    def connect(self, *args, **kwargs):
        raise AssertionError('Real network calls are forbidden')

    def get_security_bars(self, *args, **kwargs):
        raise AssertionError('Use an explicit synthetic API result')

    def get_security_quotes(self, *args, **kwargs):
        raise AssertionError('Use an explicit synthetic API result')


def load_offline_fetcher():
    path = ROOT / 'shared_data_fetcher.py'
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.module == 'pytdx.hq')]
    namespace = {'__file__': str(path), '__name__': 'offline_shared_data_fetcher', 'TdxHq_API': OfflineTdx}
    exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), namespace)
    return namespace['SharedDataFetcher']


Fetcher = load_offline_fetcher()


class TestBarCacheMerge(unittest.TestCase):
    def test_3000_history_and_200_overlapping_bars_preserve_all_history(self):
        old, new = bars(3000), bars(200, start=2800)
        out = merge(old, new)
        self.assertEqual(len(out), 3000)
        pd.testing.assert_frame_equal(out, old)
        self.assertEqual(out.iloc[0].time, old.iloc[0].time)

    def test_3000_history_and_200_with_100_new_times_gives_3100(self):
        out = merge(bars(3000), bars(200, start=2900))
        self.assertEqual(len(out), 3100)
        pd.testing.assert_frame_equal(out, bars(3100))

    def test_append_new_times_and_fill_gap_without_truncation(self):
        old = pd.concat([bars(2), bars(2, start=6)], ignore_index=True)
        old.attrs = bars().attrs
        out = merge(old, bars(6, start=2))
        pd.testing.assert_frame_equal(out, bars(8))

    def test_revision_replaces_entire_ohlcv_and_amount_row(self):
        old, new = bars(10), bars(1, start=9)
        new.loc[0, ['open', 'high', 'low', 'close', 'vol', 'amount']] = [31., 33., 30., 32., 2345., 75040.]
        out = merge(old, new)
        self.assertEqual(len(out), 10)
        pd.testing.assert_series_equal(out.iloc[-1], new.iloc[0], check_names=False)
        pd.testing.assert_frame_equal(out.iloc[:-1].reset_index(drop=True), old.iloc[:-1].reset_index(drop=True))

    def test_minute_and_zero_second_formats_share_one_key(self):
        old, new = bars(10), bars(1, start=9)
        old['time'] = old.time.str.slice(0, 16)
        out = merge(old, new)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.time.str.endswith(':00').all())

    def test_datetime_input_normalized_to_string(self):
        old = bars()
        old['time'] = pd.to_datetime(old.time)
        out = merge(old, bars(2, start=9))
        pd.testing.assert_frame_equal(out, bars(11))

    def test_unsorted_inputs_result_in_ascending_time(self):
        old = bars(20).iloc[::-1].copy()
        new = bars(10, start=15).sample(frac=1, random_state=7)
        out = merge(old, new)
        pd.testing.assert_frame_equal(out, bars(25))

    def test_repeated_merge_is_idempotent(self):
        old, new = bars(100), bars(20, start=90)
        once = merge(old, new)
        twice = merge(once, new)
        pd.testing.assert_frame_equal(once, twice)
        self.assertFalse(twice.duplicated(['ts_code', 'time']).any())

    def test_identical_duplicates_in_a_batch_are_collapsed(self):
        original = bars(10)
        duplicates = pd.concat([original, original.iloc[[4]]], ignore_index=True)
        duplicates.attrs = original.attrs
        pd.testing.assert_frame_equal(merge(pd.DataFrame(), duplicates), original)

    def test_conflicting_duplicates_within_a_batch_are_rejected(self):
        first, second = bars(1), bars(1)
        second['vol'] += 1
        ambiguous = pd.concat([first, second], ignore_index=True)
        ambiguous.attrs = first.attrs
        with self.assertRaisesRegex(BarCacheValidationError, 'Conflicting duplicate'):
            merge(bars(10), ambiguous)
        with self.assertRaises(BarCacheValidationError):
            merge(ambiguous, bars(10))

    def test_inputs_and_attrs_not_modified_on_success(self):
        old, new = bars(10), bars(4, start=8)
        old['time'] = old.time.str.slice(0, 16)
        a, b = old.copy(deep=True), new.copy(deep=True)
        out = merge(old, new)
        pd.testing.assert_frame_equal(old, a)
        pd.testing.assert_frame_equal(new, b)
        self.assertEqual(old.attrs, a.attrs)
        out.loc[0, 'close'] = 999
        self.assertNotEqual(old.iloc[0].close, 999)

    def test_inputs_not_modified_on_error(self):
        old, new = bars(10), bars(2)
        new.loc[1, 'time'] = 'bad time'
        a, b = old.copy(deep=True), new.copy(deep=True)
        with self.assertRaises(BarCacheValidationError):
            merge(old, new)
        pd.testing.assert_frame_equal(old, a)
        pd.testing.assert_frame_equal(new, b)

    def test_different_securities_rejected(self):
        for new in [bars(2, code='000001.SZ'), bars(2, code='600000.SZ')]:
            with self.assertRaises(BarCacheValidationError):
                merge(bars(10), new)

    def test_mixed_security_rows_rejected_even_if_attrs_claim_target(self):
        new = bars(2)
        new.loc[1, 'ts_code'] = '000001.SZ'
        with self.assertRaises(BarCacheValidationError):
            merge(bars(10), new)

    def test_different_frequencies_rejected_even_with_same_timestamps(self):
        new = bars(2)
        new.attrs['freq'] = '5MIN'
        with self.assertRaisesRegex(BarCacheValidationError, 'frequency'):
            merge(bars(10), new)

    def test_period_row_metadata_cannot_override_conflicting_attrs(self):
        new = bars(2)
        new['freq'] = '1MIN'
        new.attrs['freq'] = '5MIN'
        with self.assertRaises(BarCacheValidationError):
            merge(bars(10), new)

    def test_explicit_frequency_column_is_supported_and_checked(self):
        new = bars(2)
        new.attrs = {}
        new['freq'] = ['1MIN', '1MIN']
        out = merge(pd.DataFrame(), new)
        self.assertEqual(out.attrs['freq'], '1MIN')
        self.assertEqual(out.columns.tolist(), BAR_COLUMNS)
        new.loc[1, 'freq'] = '5MIN'
        with self.assertRaises(BarCacheValidationError):
            merge(pd.DataFrame(), new)

    def test_missing_or_unknown_frequency_identity_is_rejected(self):
        new = bars(2)
        new.attrs = {}
        with self.assertRaisesRegex(BarCacheValidationError, 'no explicit frequency'):
            merge(bars(10), new)
        for freq in ['DAY', 'WEEK', '2MIN', '', None]:
            with self.assertRaises(BarCacheValidationError):
                merge(bars(10), bars(2), freq=freq)

    def test_unrecognized_security_identity_rejected(self):
        for code in ['600000', '600000.XX', '1|600000.SH', '', None]:
            with self.assertRaises(BarCacheValidationError):
                merge(bars(10), bars(2), code=code)
        new = bars(2)
        new.loc[1, 'ts_code'] = None
        with self.assertRaises(BarCacheValidationError):
            merge(bars(10), new)

    def test_unrecognized_or_non_aligned_time_not_silently_floored(self):
        for invalid in ['2026-02-30 09:30', '01/05/2026 09:30', '2026-01-05', None,
                        '2026-01-05 09:30:01', '2026-01-05 09:30:00.001',
                        '2026-01-05T09:30:00+08:00', pd.NaT, 1760000000]:
            with self.subTest(time=invalid):
                new = bars(2)
                # This is deliberately malformed input. Pandas 3 infers a
                # StringDtype and rejects an integer BEFORE our validator runs;
                # object preserves the intended mixed-type test on both 2/3.
                new['time'] = new['time'].astype(object)
                new.loc[1, 'time'] = invalid
                with self.assertRaises(BarCacheValidationError):
                    merge(bars(10), new)

    def test_malformed_numeric_row_rejects_whole_batch(self):
        for field, value in [('close', np.nan), ('vol', -1.), ('amount', np.inf),
                             ('high', 1.), ('open', 0.), ('close', 'bad'), ('vol', True)]:
            with self.subTest(field=field, value=value):
                new = bars(2)
                new[field] = new[field].astype(object)
                new.loc[1, field] = value
                with self.assertRaises(BarCacheValidationError):
                    merge(bars(10), new)

    def test_missing_or_extra_columns_are_not_silently_discarded(self):
        for new in [bars(2).drop(columns=['amount']), bars(2).assign(unknown_metadata='???')]:
            with self.assertRaises(BarCacheValidationError):
                merge(bars(10), new)

    def test_invalid_history_is_not_replaced_by_valid_recent_data(self):
        old = bars(3000)
        old.loc[0, 'time'] = 'bad historical label'
        frozen = old.copy(deep=True)
        with self.assertRaises(BarCacheValidationError):
            merge(old, bars(200, start=2800))
        pd.testing.assert_frame_equal(old, frozen)

    def test_empty_incoming_preserves_history_and_first_fetch_works(self):
        old = bars(10)
        pd.testing.assert_frame_equal(merge(old, pd.DataFrame()), old)
        pd.testing.assert_frame_equal(merge(pd.DataFrame(), old), old)
        self.assertEqual(len(merge(pd.DataFrame(), pd.DataFrame())), 0)

    def test_merge_keeps_original_indicator_warmup_origin(self):
        from strategy import KdjMacdStrategy
        combined = merge(bars(3000), bars(200, start=2900))
        strategy = KdjMacdStrategy()
        expected = strategy.compute(bars(3100))
        actual = strategy.compute(combined)
        pd.testing.assert_frame_equal(actual[['DIF', 'DEA', 'MACD']],
                                      expected[['DIF', 'DEA', 'MACD']], check_exact=True)
        self.assertEqual(combined.iloc[0].time, bars(3000).iloc[0].time)


class TestSharedFetcherMergeIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fetcher = Fetcher(['600000.SH'], freq='1MIN', cache_dir=self.temp.name)
        self.fetcher._ensure_connected = Mock(return_value=True)
        self.fetcher._fetch_realtime_quotes = Mock(return_value=[])

    def seed_memory(self, frame):
        self.fetcher._minute_bars['600000.SH'] = frame
        self.fetcher._last_bar_time['600000.SH'] = frame.iloc[-1].time
        self.fetcher._bar_count_since_save['600000.SH'] = 7

    def tick(self, frame):
        self.fetcher._fetch_minute_bars = Mock(return_value=frame)
        self.fetcher._last_min_bar_fetch = 0
        output = StringIO()
        with redirect_stdout(output):
            self.fetcher._tick_once()
        return output.getvalue()

    def test_real_tick_merges_and_counts_only_new_times(self):
        self.seed_memory(bars(3000))
        self.assertEqual(self.tick(bars(200, start=2900)), '')
        pd.testing.assert_frame_equal(self.fetcher.get_minute_bars('600000.SH'), bars(3100))
        self.assertEqual(self.fetcher._bar_count_since_save['600000.SH'], 107)
        self.assertEqual(self.fetcher.get_last_bar_time('600000.SH'), bars(3100).iloc[-1].time)
        self.tick(bars(200, start=2900))
        self.assertEqual(self.fetcher._bar_count_since_save['600000.SH'], 107)

    def test_tick_failure_preserves_history_latest_time_and_counter(self):
        bad_time = bars(2)
        bad_time.loc[1, 'time'] = 'bad time'
        no_period = bars(2)
        no_period.attrs = {}
        for bad in [bars(2, code='000001.SZ'), bars(2, freq='5MIN'), bars(2).drop(columns=['low']),
                    pd.DataFrame(index=range(2)), bad_time, no_period]:
            self.seed_memory(bars(3000))
            previous = self.fetcher.get_minute_bars('600000.SH')
            time = self.fetcher.get_last_bar_time('600000.SH')
            message = self.tick(bad)
            self.assertIn('异常', message)
            self.assertIs(self.fetcher.get_minute_bars('600000.SH'), previous)
            self.assertEqual(self.fetcher.get_last_bar_time('600000.SH'), time)
            self.assertEqual(self.fetcher._bar_count_since_save['600000.SH'], 7)

    def test_malformed_history_blocks_refresh_instead_of_replacing_it(self):
        original = bars(3000)
        original.loc[0, 'time'] = 'invalid historical time'
        self.seed_memory(original)
        message = self.tick(bars(200, start=2800))
        self.assertIn('异常', message)
        self.assertIs(self.fetcher.get_minute_bars('600000.SH'), original)
        self.assertEqual(len(original), 3000)
        self.assertEqual(self.fetcher._bar_count_since_save['600000.SH'], 7)

    def test_real_fetch_rejects_invalid_api_batch_and_keeps_existing_memory(self):
        self.seed_memory(bars(3000))
        original = self.fetcher.get_minute_bars('600000.SH')
        raw = {'datetime': '2026-01-05 09:30', 'open': 20., 'high': 21., 'low': 19.,
               'close': 20., 'vol': 100., 'amount': 2000.}
        for changes in [dict(datetime='unrecognized label'), dict(freq='5MIN'),
                        dict(ts_code='000001.SZ'), dict(code='000001'), dict(market=0)]:
            self.fetcher._api.get_security_bars = Mock(return_value=[dict(raw, **changes)])
            self.fetcher._last_min_bar_fetch = 0
            output = StringIO()
            with redirect_stdout(output):
                self.fetcher._tick_once()
            self.assertIn('获取K线失败', output.getvalue())
            self.assertIs(self.fetcher.get_minute_bars('600000.SH'), original)
            self.assertEqual(self.fetcher._bar_count_since_save['600000.SH'], 7)

    def test_same_time_revision_not_counted_as_new_even_with_format_change(self):
        old = bars(3000)
        old['time'] = old.time.str.slice(0, 16)
        self.seed_memory(old)
        new = bars(200, start=2800)
        new['vol'] += 100
        self.tick(new)
        self.assertEqual(len(self.fetcher.get_minute_bars('600000.SH')), 3000)
        self.assertEqual(self.fetcher._bar_count_since_save['600000.SH'], 7)
        self.assertEqual(self.fetcher.get_minute_bars('600000.SH').iloc[-1].vol, new.iloc[-1].vol)

    def test_no_result_keeps_history(self):
        self.seed_memory(bars(3000))
        original = self.fetcher.get_minute_bars('600000.SH')
        for incoming in [None, pd.DataFrame()]:
            self.tick(incoming)
            self.assertIs(self.fetcher.get_minute_bars('600000.SH'), original)

    def test_two_securities_update_separate_cache_entries(self):
        self.fetcher.ts_codes = ['600000.SH', '000001.SZ']
        self.seed_memory(bars(3000))
        other = bars(300, code='000001.SZ')
        self.fetcher._minute_bars['000001.SZ'] = other
        self.fetcher._fetch_minute_bars = Mock(side_effect=[bars(200, start=2900), bars(2, start=299, code='000001.SZ')])
        self.fetcher._tick_once()
        self.assertEqual(len(self.fetcher.get_minute_bars('600000.SH')), 3100)
        self.assertEqual(len(self.fetcher.get_minute_bars('000001.SZ')), 301)
        self.assertEqual(set(self.fetcher.get_minute_bars('000001.SZ').ts_code), {'000001.SZ'})

    def test_distinct_frequency_instances_are_isolated(self):
        other = Fetcher(['600000.SH'], freq='5MIN', cache_dir=self.temp.name)
        other._ensure_connected = Mock(return_value=True)
        other._fetch_realtime_quotes = Mock(return_value=[])
        other._minute_bars['600000.SH'] = bars(20, freq='5MIN')
        other._fetch_minute_bars = Mock(return_value=bars(2, start=19, freq='5MIN'))
        self.seed_memory(bars(3000))
        other._tick_once()
        self.assertEqual(len(other.get_minute_bars('600000.SH')), 21)
        self.assertEqual(len(self.fetcher.get_minute_bars('600000.SH')), 3000)
        self.assertNotEqual(other._cache_path('600000.SH'), self.fetcher._cache_path('600000.SH'))

    def test_legacy_eight_column_restore_then_refresh_keeps_3000(self):
        old = bars(3000)
        old['time'] = old.time.str.slice(0, 16)
        path = Path(self.temp.name) / '600000_SH_1MIN.csv'
        old.to_csv(path, index=False)
        original_bytes = path.read_bytes()
        with redirect_stdout(StringIO()):
            self.fetcher._load_all_disk_cache()
        restored = self.fetcher.get_minute_bars('600000.SH')
        self.assertEqual(restored.attrs['freq'], '1MIN')
        self.assertEqual(len(restored), 3000)
        self.tick(bars(200, start=2800))
        self.assertEqual(len(self.fetcher.get_minute_bars('600000.SH')), 3000)
        self.assertEqual(path.read_bytes(), original_bytes, 'memory merge must not rewrite CSV')

    def test_corrupt_restore_is_explicit_and_does_not_clear_memory_or_file(self):
        self.seed_memory(bars(3000))
        original = self.fetcher.get_minute_bars('600000.SH')
        path = Path(self.temp.name) / '600000_SH_1MIN.csv'
        path.write_text('ts_code,time\n600000.SH,bad time\n')
        original_bytes = path.read_bytes()
        with self.assertRaises(BarCacheValidationError):
            self.fetcher._load_all_disk_cache()
        self.assertIs(self.fetcher.get_minute_bars('600000.SH'), original)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_wrong_period_column_in_legacy_file_is_not_stripped(self):
        path = Path(self.temp.name) / '600000_SH_1MIN.csv'
        bars(2).assign(freq='5MIN').to_csv(path, index=False)
        with self.assertRaises(BarCacheValidationError):
            self.fetcher._load_disk_cache('600000.SH')

    def test_invalid_configuration_does_not_create_cache_or_api(self):
        for codes, freq in [(['600000'], '1MIN'), (['600000.SH'], 'DAY')]:
            with self.assertRaises(BarCacheValidationError):
                Fetcher(codes, freq=freq, cache_dir=Path(self.temp.name) / 'must-not-exist')
        self.assertFalse((Path(self.temp.name) / 'must-not-exist').exists())
        with self.assertRaises(BarCacheValidationError):
            self.fetcher._freq_to_category('DAY')

    def test_api_normalizes_and_tags_input_before_merge(self):
        raw = [{'datetime': '2026-01-05 09:30', 'open': 20., 'high': 21., 'low': 19.,
                'close': 20., 'vol': 100., 'amount': 2000.}]
        self.fetcher._api.get_security_bars = Mock(return_value=raw)
        out = self.fetcher._fetch_minute_bars('600000.SH')
        self.assertEqual(out.time.iloc[0], '2026-01-05 09:30:00')
        self.assertEqual(out.attrs['freq'], '1MIN')
        self.fetcher._api.get_security_bars.assert_called_once_with(8, 1, '600000', 0, 200)


if __name__ == '__main__':
    unittest.main()
