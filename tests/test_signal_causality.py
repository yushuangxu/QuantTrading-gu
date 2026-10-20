"""Offline prefix replay of the unchanged strategy.

Run: python -B -m unittest discover -s tests -p 'test_signal_*.py' -v
Export evidence: python -B tests/test_signal_causality.py --export docs/phases/P0-02_evidence
Contract tests intentionally FAIL when current production outputs repaint.
Only strategy/numpy/pandas are imported; no main, dotenv, data source or pusher.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import itertools
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from strategy import KdjMacdStrategy

SIGNALS = ['BUY_B', 'SELL_S', 'BOTTOM_FISHING', 'TOP_ESCAPE', 'BUY_JB', 'SELL_JS']
BASE = ['DIF', 'DEA', 'MACD', 'K', 'D', 'J', 'CCI', 'CROSS_GC', 'CROSS_DC']
DIVERGENCES = ['BOTTOM_DIV', 'TOP_DIV', 'STRONG_BOTTOM', 'STRONG_TOP',
               'TWO_ROUND_BOTTOM_DIV', 'INNER_BOTTOM_DIV',
               'TWO_ROUND_TOP_DIV', 'INNER_TOP_DIV', 'LINE_BOTTOM', 'LINE_TOP']


def fixed_bars(n=320, seed=1):
    """Synthetic completed bars; UTC timestamps are labels, not an exchange calendar.

    Fixed PCG64 generator, no external file or current date dependency.
    Price/volume units are arbitrary but consistent; not a market realism model.
    """
    close = 100 + np.cumsum(np.random.Generator(np.random.PCG64(seed)).normal(0, 2, n))
    return pd.DataFrame({
        'ts_code': 'SYNTHETIC',
        'time': pd.date_range('2026-01-05 00:00:00', periods=n, freq='min', tz='UTC').astype(str),
        'open': close.copy(),
        'high': close + .5,
        'low': close - .5,
        'close': close, 'vol': np.full(n, 10000.), 'amount': close * 10000.,
    })


def diagnostics(out, clear_wrap=False):
    """Observe unexported operands, using original helpers and original equations.

    Does not replace strategy code. clear_wrap is a test-only reference for isolating
    roll[0], not a proposed strategy or production fix.
    """
    s = KdjMacdStrategy()
    dif, dea, macd, close = [out[c].to_numpy(float) for c in ['DIF', 'DEA', 'MACD', 'close']]
    gc, dc = s._cross_np(dif, dea), s._cross_np(dea, dif)
    result = {'CROSS_GC': gc, 'CROSS_DC': dc}
    for name, event in [('BOTTOM', gc), ('TOP', dc)]:
        lag = np.roll(event, 1)
        if clear_wrap:
            lag[0] = False
        age = s._barslast(lag).astype(float)
        ref_c, ref_d = s._ref_series(close, age + 1), s._ref_series(dif, age + 1)
        result['LAG_' + name] = lag
        result['LINE_' + name] = ((ref_c > close) & (dif > ref_d) & gc if name == 'BOTTOM'
                                  else (ref_c < close) & (ref_d > dif) & dc)
    for name, trigger, origin, extreme in [('BA', dc, gc, s._hhvbars), ('BB', gc, dc, s._llvbars)]:
        periods = s._barslast(origin).astype(float) + 1
        offsets = extreme(macd, periods)
        last = np.arange(len(out)) == len(out) - 1
        mask = s._backset(trigger | last, offsets + 1)
        result[name + '_EVENT_ONLY'] = s._backset(trigger, offsets + 1)
        result[name] = mask
        result[name + '_TRIGGER'] = trigger | last
        result[name + '_EXTREME_INDEX'] = np.arange(len(out)) - offsets
        starts = (np.roll(mask, 1) == 0) & mask
        if clear_wrap:
            starts[0] = mask[0]
        result[name + '_START'] = starts
        result[name + '_AGE'] = s._barslast(starts)
    return pd.DataFrame(result, index=out.index)


def enriched(bars):
    out = KdjMacdStrategy().compute(bars)
    return pd.concat([out, diagnostics(out)], axis=1)


def different(a, b):
    if pd.api.types.is_bool_dtype(a.dtype):
        return a.to_numpy() != b.to_numpy()
    if pd.api.types.is_numeric_dtype(a.dtype):
        return ~np.isclose(a.to_numpy(float), b.to_numpy(float), rtol=1e-12, atol=1e-12, equal_nan=True)
    return a.to_numpy() != b.to_numpy()


@lru_cache(maxsize=1)
def replay():
    bars = fixed_bars()
    # Every row of every output prefix is retained, not just latest signals.
    snapshots = {n: enriched(bars.iloc[:n].copy()) for n in range(1, len(bars) + 1)}
    return bars, snapshots


@lru_cache(maxsize=1)
def companion_replay():
    bars = fixed_bars(240, seed=0)
    return bars, {n: enriched(bars.iloc[:n].copy()) for n in range(1, len(bars) + 1)}


def differences(snapshots, mode):
    rows = []
    final = snapshots[max(snapshots)]
    for n, current in snapshots.items():
        if mode == 'full':
            reference = final.iloc[:n]
            observed = current
        else:
            if n == 1:
                continue
            reference = current.iloc[:-1]
            observed = snapshots[n - 1]
        for col in observed.columns:
            if col in ['ts_code', 'time', 'open', 'high', 'low', 'close', 'vol', 'amount']:
                continue
            for i in np.flatnonzero(different(observed[col], reference[col])):
                rows.append({'observed_prefix': n if mode == 'full' else n - 1,
                             'reference_prefix': len(final) if mode == 'full' else n,
                             'bar_index': int(i), 'bar_time': current['time'].iloc[i], 'column': col,
                             'observed': observed[col].iloc[i], 'reference': reference[col].iloc[i]})
    return pd.DataFrame(rows)


def timing_table(snapshots):
    """Retain withdrawn as well as final labels; visibility is not confirmation.

    The last-row backset trigger can expose a *provisional* extreme immediately.
    A final label need not be first observed later to be future-dependent.
    """
    final = snapshots[max(snapshots)]
    rows = []
    for col in SIGNALS + DIVERGENCES:
        ever = sorted({int(i) for e in snapshots.values() for i in np.flatnonzero(e[col].to_numpy(bool))})
        for i in ever:
            visible = [n for n, e in snapshots.items() if n > i and bool(e[col].iloc[i])]
            first = min(visible)
            final_value = bool(final[col].iloc[i])
            changes = [n for n in range(i + 2, len(final) + 1)
                       if bool(snapshots[n][col].iloc[i]) != bool(snapshots[n - 1][col].iloc[i])]
            stable = max(changes) if changes else i + 1
            branch = ('BB' if 'BOTTOM' in col else 'BA') if col.startswith('INNER_') else None
            events = []
            if branch:
                event_col = 'CROSS_GC' if branch == 'BB' else 'CROSS_DC'
                events = [int(j) for j in np.flatnonzero(final[event_col].to_numpy(bool))
                          if j >= i and final[branch + '_EXTREME_INDEX'].iloc[j] == i]
            event_time = final.time.iloc[events[0]] if events else ''
            rows.append({'column': col, 'bar_index': i,
                         'first_cross_confirmation_of_extremum': event_time,
                         'historical_annotation_time': final.time.iloc[i],
                         'present_in_full': final_value,
                         'first_visible_prefix': first, 'first_visible_time': final.time.iloc[first - 1],
                         'stable_from_prefix_in_sample': stable,
                         'confirmation_time': 'undefined by original strategy; visibility is provisional',
                         'last_change_prefix': max(changes) if changes else '',
                         'real_time_same_bar': bool(snapshots[i + 1][col].iloc[i]),
                         'meets_default_150_bar_gate': i + 1 >= 150,
                         'pivot_time': (final.time.iloc[i] if col.startswith('INNER_') else ''),
                         'pivot_meaning': ('MACD backset extremum; not necessarily price extremum'
                                           if col.startswith('INNER_') else 'composite/no unique pivot'),
                         'appears_after_bar': first > i + 1,
                         'revised_after_bar': bool(changes),
                         'retrospective': bool(changes) or first > i + 1})
    return pd.DataFrame(rows)


def export_evidence(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    bars, snapshots = replay()
    bars.to_csv(directory / 'sample.csv', index=False, float_format='%.17g')
    companion_bars, companion_snapshots = companion_replay()
    fixed_bars(320, seed=0).to_csv(directory / 'companion_sample.csv', index=False, float_format='%.17g')
    pd.concat([e.assign(prefix_length=n, observed_at=companion_bars.time.iloc[n - 1])
               for n, e in companion_snapshots.items()], ignore_index=True).to_csv(
                   directory / 'companion_snapshots.csv.gz', index=False, float_format='%.17g',
                   compression={'method': 'gzip', 'mtime': 0})
    differences(companion_snapshots, 'full').to_csv(directory / 'companion_full_differences.csv', index=False)
    snapshots[len(bars)].to_csv(directory / 'full.csv', index=False, float_format='%.17g')
    pd.concat([e.assign(prefix_length=n, observed_at=bars.time.iloc[n - 1])
               for n, e in snapshots.items()], ignore_index=True).to_csv(
                   directory / 'snapshots.csv.gz', index=False, float_format='%.17g',
                   compression={'method': 'gzip', 'mtime': 0})
    counts = {}
    for mode in ['full', 'adjacent']:
        diff = differences(snapshots, mode)
        diff.to_csv(directory / (mode + '_differences.csv'), index=False)
        counts[mode] = diff.groupby('column').size().astype(int).to_dict() if len(diff) else {}
    timing_table(snapshots).to_csv(directory / 'signal_timing.csv', index=False)
    revision, before, after = revision_case()
    before.to_csv(directory / 'intrabar_before.csv', index=False, float_format='%.17g')
    after.to_csv(directory / 'intrabar_after.csv', index=False, float_format='%.17g')
    summary = {'seed': 1, 'bars': len(bars), 'prefixes': len(snapshots),
               'snapshot_rows': sum(map(len, snapshots.values())),
               'companion_prefixes': len(companion_snapshots),
               'companion_snapshot_rows': sum(map(len, companion_snapshots.values())),
               'difference_cells': counts,
               'signals_full': {c: int(snapshots[len(bars)][c].sum()) for c in SIGNALS + DIVERGENCES},
               'intrabar': revision, 'strategy_sha256': hashlib.sha256((ROOT / 'strategy.py').read_bytes()).hexdigest(),
               'python': sys.version, 'numpy': np.__version__, 'pandas': pd.__version__}
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    return summary


@lru_cache(maxsize=1)
def revision_case():
    # Identical timestamps, only final close/high/low revised; previous bars frozen.
    # Deterministic search over a finite fixture, not random retries or market data.
    bars, snapshots = replay()
    for n in range(150, len(bars) + 1):
        before = snapshots[n]
        for delta in [-15., 15.]:
            revised = bars.iloc[:n].copy()
            revised.loc[n - 1, 'close'] += delta
            revised.loc[n - 1, 'high'] = max(revised.loc[n - 1, 'high'], revised.loc[n - 1, 'close'] + .5)
            revised.loc[n - 1, 'low'] = min(revised.loc[n - 1, 'low'], revised.loc[n - 1, 'close'] - .5)
            revised.loc[n - 1, 'amount'] = revised.loc[n - 1, 'close'] * revised.loc[n - 1, 'vol']
            after = enriched(revised)
            changed = [c for c in SIGNALS if before[c].iloc[-1] != after[c].iloc[-1]]
            if changed:
                return {'prefix': n, 'delta': delta, 'time': bars.time.iloc[n - 1],
                        'changed_last_signals': changed}, before, after
    raise AssertionError('Fixture did not exercise any intrabar signal revision')


class TestPrefixCausality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars, cls.snapshots = replay()
        cls.final = cls.snapshots[len(cls.bars)]

    def assert_prefix_columns(self, columns):
        for n, e in self.snapshots.items():
            for col in columns:
                idx = np.flatnonzero(different(e[col], self.final[col].iloc[:n]))
                if len(idx):
                    i = int(idx[0])
                    self.fail(f'{col}: prefix={n}, bar_index={i}, time={e.time.iloc[i]}, '
                              f'then={e[col].iloc[i]}, full={self.final[col].iloc[i]}')

    def test_macd_causal(self):
        self.assert_prefix_columns(['DIF', 'DEA', 'MACD'])

    def test_kdj_causal(self):
        self.assert_prefix_columns(['K', 'D', 'J'])

    def test_cci_causal(self):
        self.assert_prefix_columns(['CCI'])

    def test_crosses_causal_and_nonvacuous(self):
        self.assertGreater(int(self.final.CROSS_GC.sum()), 0)
        self.assertGreater(int(self.final.CROSS_DC.sum()), 0)
        self.assert_prefix_columns(['CROSS_GC', 'CROSS_DC'])

    def test_area_and_line_divergences_causal(self):
        self.assertGreater(int(self.final.TWO_ROUND_BOTTOM_DIV.sum()), 0)
        self.assertGreater(int(enriched(fixed_bars(240, seed=0)).TWO_ROUND_TOP_DIV.sum()), 0)
        self.assert_prefix_columns(['TWO_ROUND_BOTTOM_DIV', 'TWO_ROUND_TOP_DIV', 'LINE_BOTTOM', 'LINE_TOP'])

    def test_inner_divergences_do_not_repaint(self):
        self.assert_prefix_columns(['INNER_BOTTOM_DIV', 'INNER_TOP_DIV'])

    def test_composite_divergences_do_not_repaint(self):
        self.assert_prefix_columns(['BOTTOM_DIV', 'TOP_DIV'])

    def test_buy_b_does_not_repaint_after_default_warmup(self):
        for n, e in self.snapshots.items():
            if n >= 150:
                bad = np.flatnonzero(e.BUY_B.to_numpy()[149:] != self.final.BUY_B.to_numpy()[149:n])
                if len(bad):
                    self.fail(f'BUY_B prefix={n}, historical index={bad[0] + 149}')

    def test_sell_s_does_not_repaint_after_default_warmup(self):
        for n, e in self.snapshots.items():
            if n >= 150:
                bad = np.flatnonzero(e.SELL_S.to_numpy()[149:] != self.final.SELL_S.to_numpy()[149:n])
                if len(bad):
                    self.fail(f'SELL_S prefix={n}, historical index={bad[0] + 149}')

    def test_jb_js_and_wave_outputs_are_prefix_causal(self):
        self.assertGreater(int(self.final.BUY_JB.sum()), 0)
        companion, snapshots = companion_replay()
        full = snapshots[len(companion)]
        self.assertGreater(int(full.SELL_JS.sum()), 0)
        self.assert_prefix_columns(['MACD_ZIG', 'WAVE_PEAK', 'WAVE_TROUGH', 'BUY_JB', 'SELL_JS'])
        for n, out in snapshots.items():
            for col in ['MACD_ZIG', 'WAVE_PEAK', 'WAVE_TROUGH', 'BUY_JB', 'SELL_JS',
                        'TWO_ROUND_BOTTOM_DIV', 'TWO_ROUND_TOP_DIV', 'LINE_BOTTOM', 'LINE_TOP']:
                self.assertFalse(different(out[col], full[col].iloc[:n]).any(), (n, col))

    def test_raw_backset_does_not_change_past(self):
        s = KdjMacdStrategy()
        old = s._backset(np.array([False, False]), np.array([1., 1.]))
        new = s._backset(np.array([False, False, True]), np.array([1., 1., 3.]))
        np.testing.assert_array_equal(old, new[:2], err_msg='Future event backfills past markers')

    def test_all_original_outputs_saved_and_input_not_mutated(self):
        before = fixed_bars()
        pd.testing.assert_frame_equal(before, self.bars)
        self.assertEqual(len(self.snapshots), len(self.bars))
        original_columns = set(KdjMacdStrategy().compute(self.bars.iloc[:1]).columns)
        for n, e in self.snapshots.items():
            self.assertEqual(len(e), n)
            self.assertTrue(original_columns.issubset(e.columns))

    def test_intrabar_revision_changes_current_not_completed_indicators(self):
        info, before, after = revision_case()
        self.assertGreaterEqual(info['prefix'], 150)
        self.assertEqual(before.time.tolist(), after.time.tolist())
        for col in BASE:
            self.assertFalse(different(before[col].iloc[:-1], after[col].iloc[:-1]).any(), col)
        self.assertTrue(any(different(before[c].iloc[-1:], after[c].iloc[-1:]).any() for c in BASE[:7]))
        self.assertTrue(info['changed_last_signals'])
        a = KdjMacdStrategy().latest_signal('SYNTHETIC', before)
        b = KdjMacdStrategy().latest_signal('SYNTHETIC', after)
        self.assertEqual(a.bar_time, b.bar_time)
        self.assertNotEqual(a, b)

    def test_original_roll_first_element_is_not_future_event(self):
        gc = np.array([False, False, True])
        lag = np.roll(gc, 1)  # Exactly the operation in strategy.py:412/417.
        self.assertFalse(bool(lag[0]), 'np.roll sends the final cross into the first row')

    def test_wrap_fix_reference_effect_is_measured_not_assumed(self):
        # Raw lag corruption is real, but verify exported line impact separately.
        for e in self.snapshots.values():
            reference = diagnostics(e, clear_wrap=True)
            for col in ['LINE_BOTTOM', 'LINE_TOP']:
                np.testing.assert_array_equal(e[col], reference[col])

    def test_retrospective_annotation_is_not_same_as_realtime(self):
        table = timing_table(self.snapshots)
        revised = table[table.revised_after_bar]
        self.assertGreater(len(revised), 0)
        withdrawn = revised[~revised.present_in_full]
        self.assertGreater(len(withdrawn), 0)
        self.assertTrue((withdrawn.stable_from_prefix_in_sample > withdrawn.bar_index + 1).all())
        # Measure actual timing; do not assume first visibility is delayed.
        self.assertTrue(revised.retrospective.all())


class TestReachability(unittest.TestCase):
    def test_actual_cross_and_all_divergence_components_exercised(self):
        out = pd.concat([enriched(fixed_bars(240, seed=0)),
                         enriched(fixed_bars(320, seed=1))], ignore_index=True)
        for col in ['CROSS_GC', 'CROSS_DC', 'TWO_ROUND_BOTTOM_DIV',
                    'TWO_ROUND_TOP_DIV', 'INNER_BOTTOM_DIV', 'INNER_TOP_DIV']:
            self.assertGreater(int(out[col].sum()), 0, col)
        self.assertTrue((out.loc[out.TWO_ROUND_BOTTOM_DIV, 'DIF'] >
                         out.loc[out.TWO_ROUND_BOTTOM_DIV, 'DEA']).all())
        self.assertTrue((out.loc[out.TWO_ROUND_TOP_DIV, 'DIF'] <
                         out.loc[out.TWO_ROUND_TOP_DIV, 'DEA']).all())

    def test_bottom_contradiction_exhaustive_sign_states(self):
        # All 5x5 finite DIF/DEA sign/order combinations, both cross histories,
        # with oversold gates forced TRUE to isolate the real contradiction.
        exercised = 0
        for dif, dea, prev_above in itertools.product([-2., -1., 0., 1., 2.], repeat=3):
            previous_dif = 1. if prev_above > 0 else -1.
            gc = KdjMacdStrategy._cross_np(np.array([previous_dif, dif]), np.array([0., dea]))[-1]
            area_bottom = gc and dif < 0
            strong_bottom = area_bottom and True  # hypothetical inner_bottom=True
            candidate = strong_bottom and dif < 0 and dea < 0 and dif < dea
            exercised += int(strong_bottom)
            self.assertFalse(candidate, (dif, dea, prev_above))
        self.assertGreater(exercised, 0, 'must cover hypothetical strong-bottom=True')

    def test_top_contradiction_exhaustive_sign_states(self):
        exercised = 0
        for dif, dea, prev_above in itertools.product([-2., -1., 0., 1., 2.], repeat=3):
            previous_dif = 1. if prev_above > 0 else -1.
            dc = KdjMacdStrategy._cross_np(np.array([0., dea]), np.array([previous_dif, dif]))[-1]
            area_top = dc and dif > 0
            strong_top = area_top and True  # hypothetical inner_top=True
            candidate = strong_top and dif > 0 and dea > 0 and dif > dea
            exercised += int(strong_top)
            self.assertFalse(candidate, (dif, dea, prev_above))
        self.assertGreater(exercised, 0, 'must cover hypothetical strong-top=True')

    def test_strong_components_are_already_disjoint_on_every_prefix(self):
        # Strong signals never occurring is NOT alone a universal proof.
        # The sign separation below measures why real component fixtures do not
        # provide an end-to-end strong=True example (see report for algebra).
        _, snapshots = replay()
        for e in snapshots.values():
            self.assertTrue((e.loc[e.INNER_BOTTOM_DIV, 'MACD'] <= 0).all())
            self.assertTrue((e.loc[e.INNER_TOP_DIV, 'MACD'] >= 0).all())
            self.assertTrue((e.loc[e.TWO_ROUND_BOTTOM_DIV, 'MACD'] > 0).all())
            self.assertTrue((e.loc[e.TWO_ROUND_TOP_DIV, 'MACD'] < 0).all())
            for col in ['STRONG_BOTTOM', 'STRONG_TOP', 'BOTTOM_FISHING', 'TOP_ESCAPE']:
                self.assertFalse(e[col].any(), (len(e), col))

    def test_original_six_signal_equations_match_outputs(self):
        for seed in [0, 1]:
            e = enriched(fixed_bars(320, seed))
            expected = {
                'BUY_B_RAW': e.BOTTOM_DIV & (e.CCI < -120) & (e.J < 20),
                'SELL_S_RAW': e.TOP_DIV & (e.CCI > 120) & (e.J > 80),
                'BOTTOM_FISHING_RAW': e.STRONG_BOTTOM & (e.CCI < -120) & (e.J < 20) &
                                      (e.DIF < 0) & (e.DEA < 0) & (e.DIF < e.DEA),
                'TOP_ESCAPE_RAW': e.STRONG_TOP & (e.CCI > 120) & (e.J > 80) &
                                  (e.DIF > 0) & (e.DEA > 0) & (e.DIF > e.DEA),
                'BUY_JB_RAW': e.BIG_GRE & e.WAVE_TROUGH & (e.DIF < 0) & (e.DEA < 0) &
                              (e.CCI < -120) & (e.J < 15),
                'SELL_JS_RAW': e.BIG_RED & e.WAVE_PEAK & (e.DIF > 0) & (e.DEA > 0) &
                               (e.CCI > 120) & (e.J > 85),
            }
            for col, values in expected.items():
                np.testing.assert_array_equal(e[col], values)
                np.testing.assert_array_equal(e[col], e[col.removesuffix('_RAW')])

class TestZigSemantics(unittest.TestCase):
    def test_initial_downward_move_is_recognized(self):
        _, _, troughs = KdjMacdStrategy._zig_pivots(np.array([100., 90., 80.]), 5)
        self.assertTrue(troughs.any(), '20% initial fall is completely unrecognized')

    def test_rising_run_has_no_confirmed_interior_peak(self):
        _, peaks, _ = KdjMacdStrategy._zig_pivots(np.array([100., 110., 120.]), 5)
        self.assertFalse(peaks[1], '110 is marked peak before any downward reversal')

    def test_falling_run_has_no_confirmed_interior_trough(self):
        _, _, troughs = KdjMacdStrategy._zig_pivots(np.array([100., 110., 100., 90., 80.]), 5)
        self.assertFalse(troughs[2:4].any(), '100 and 90 remain marked troughs during continued fall')

    def test_zig_is_append_only_not_a_future_function(self):
        for values in [[100., 110., 100., 90., 120.], [0., -.01, -.02, .01, -.03],
                       [-.02, -.01, 0., .01, .02], [100., 90., 80.]]:
            full = KdjMacdStrategy._zig_pivots(np.array(values), 5)
            for n in range(1, len(values) + 1):
                prefix = KdjMacdStrategy._zig_pivots(np.array(values[:n]), 5)
                for actual, final in zip(prefix, full):
                    np.testing.assert_allclose(actual, final[:n], equal_nan=True)

    def test_zero_crossing_sensitivity_and_direction_asymmetry_measured(self):
        # No assertion that 5% is financially reasonable; measure behavior only.
        a = KdjMacdStrategy._zig_pivots(np.array([-.01, -.009, .001]), 5)
        b = KdjMacdStrategy._zig_pivots(np.array([.01, .009, -.001]), 5)
        self.assertEqual(np.flatnonzero(a[1]).tolist(), [1, 2])
        self.assertFalse(b[1].any())
        self.assertFalse(b[2].any())
        # Offsetting an oscillator changes marks although increments are identical.
        small = KdjMacdStrategy._zig_pivots(np.array([-.000001, .000001]), 5)
        shifted = KdjMacdStrategy._zig_pivots(np.array([.999999, 1.000001]), 5)
        self.assertTrue(small[1][-1])
        self.assertFalse(shifted[1][-1])

    def test_jb_js_can_mark_continuing_macd_run_without_turn(self):
        counts = {'BUY_JB': 0, 'SELL_JS': 0}
        for seed in [0, 1]:
            e = enriched(fixed_bars(320, seed))
            m = e.MACD.to_numpy()
            for col, sign in [('BUY_JB', -1), ('SELL_JS', 1)]:
                for i in np.flatnonzero(e[col].to_numpy()):
                    if 0 < i < len(e) - 1 and sign * m[i - 1] < sign * m[i] < sign * m[i + 1]:
                        counts[col] += 1
        self.assertGreater(counts['BUY_JB'], 0)
        self.assertGreater(counts['SELL_JS'], 0)

class TestIndicatorReference(unittest.TestCase):
    def test_required_output_columns_and_insufficient_warmup(self):
        output = KdjMacdStrategy().compute(fixed_bars(50))
        for column in ['K', 'D', 'J', 'DIF', 'DEA', 'MACD', 'BUY', 'SELL']:
            self.assertIn(column, output.columns)
        bars = fixed_bars(2)
        before = bars.copy(deep=True)
        self.assertIsNone(KdjMacdStrategy().latest_signal('SYNTHETIC', bars, min_bars_required=35))
        pd.testing.assert_frame_equal(bars, before)

    def test_macd_independent_recursive_reference(self):
        e = enriched(fixed_bars(80))
        def ema(values, span):
            result = [float(values[0])]
            for value in values[1:]:
                result.append(2 / (span + 1) * value + (1 - 2 / (span + 1)) * result[-1])
            return np.array(result)
        dif = ema(e.close.to_numpy(), 12) - ema(e.close.to_numpy(), 26)
        dea = ema(dif, 9)
        for col, expected in [('DIF', dif), ('DEA', dea), ('MACD', 2 * (dif - dea))]:
            np.testing.assert_allclose(e[col], expected, rtol=1e-12, atol=1e-12)

    def test_cci_independent_window_reference(self):
        e = enriched(fixed_bars(80))
        tp = (e.high.to_numpy() + e.low.to_numpy() + e.close.to_numpy()) / 3
        expected = np.full(len(e), np.nan)
        for i in range(13, len(e)):
            window = tp[i - 13:i + 1]
            mean = sum(window) / 14
            dev = sum(abs(window - mean)) / 14
            expected[i] = (tp[i] - mean) / (.015 * dev) if dev else np.nan
        np.testing.assert_allclose(e.CCI, expected, rtol=1e-11, atol=1e-10, equal_nan=True)

    def test_kdj_independent_first_valid_rsv_reference(self):
        e = enriched(fixed_bars(80))
        k, d = np.full(len(e), np.nan), np.full(len(e), np.nan)
        previous_k = previous_d = None
        for i in range(8, len(e)):
            hh = max(e.high.iloc[i - 8:i + 1])
            ll = min(e.low.iloc[i - 8:i + 1])
            rsv = (e.close.iloc[i] - ll) / (hh - ll) * 100
            previous_k = rsv if previous_k is None else rsv / 3 + previous_k * 2 / 3
            previous_d = previous_k if previous_d is None else previous_k / 3 + previous_d * 2 / 3
            k[i], d[i] = previous_k, previous_d
        for col, expected in [('K', k), ('D', d), ('J', 3 * k - 2 * d)]:
            np.testing.assert_allclose(e[col], expected, rtol=1e-12, atol=1e-12, equal_nan=True)

    def test_cross_equal_boundary_and_first_row(self):
        s = KdjMacdStrategy()
        a, b = np.array([1., 1., 2., 1., 0.]), np.ones(5)
        np.testing.assert_array_equal(s._cross_np(a, b), [False, False, True, False, False])
        np.testing.assert_array_equal(s._cross_np(b, a), [False, False, False, False, True])
        np.testing.assert_array_equal(s._cross(pd.Series(a), pd.Series(b)), s._cross_np(a, b))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--export', type=Path)
    args, remaining = parser.parse_known_args()
    if args.export:
        print(json.dumps(export_evidence(args.export), ensure_ascii=False, indent=2))
    else:
        unittest.main(argv=[sys.argv[0]] + remaining)
