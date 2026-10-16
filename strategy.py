from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SignalResult:
    ts_code: str
    bar_time: str
    close: float
    j: float
    dif: float
    dea: float
    macd: float
    cci: float
    buy_b: bool
    sell_s: bool
    bottom_fishing: bool
    top_escape: bool
    buy_jb: bool
    sell_js: bool

    @property
    def buy(self) -> bool:
        return self.buy_b

    @property
    def sell(self) -> bool:
        return self.sell_s

    @property
    def has_alert(self) -> bool:
        return (
            self.buy_b
            or self.sell_s
            or self.bottom_fishing
            or self.top_escape
            or self.buy_jb
            or self.sell_js
        )


class KdjMacdStrategy:
    """
    按通达信公式移植的分时 MACD 背离 + CCI + KDJ + 子浪策略。

    买入B := 综合底背离 AND CCI < -120 AND J < 20
    卖出S := 综合顶背离 AND CCI > 120 AND J > 80
    抄底 := 强背离 AND CCI < -120 AND J < 20 AND DIF < 0 AND DEA < 0 AND DIF < DEA
    逃顶 := 强顶背离_新 AND CCI > 120 AND J > 80 AND DIF > 0 AND DEA > 0 AND DIF > DEA
    """
    def __init__(
        self,
        buy_j_threshold: float = 20.0,
        sell_j_threshold: float = 80.0,
        cci_buy_threshold: float = -120.0,
        cci_sell_threshold: float = 120.0,
        macd_short: int = 12,
        macd_long: int = 26,
        macd_mid: int = 9,
        cci_period: int = 14,
        kdj_n: int = 9,
        kdj_m1: int = 3,
        kdj_m2: int = 3,
        big_ratio: float = 0.75,
        mid_ratio: float = 0.3,
        zig_r: int = 5,
        kinetic_window: int = 60,
        signal_filter_period: int = 0,
        turn_buy_j_threshold: float = 15.0,
        turn_sell_j_threshold: float = 85.0,
    ) -> None:
        self.buy_j_threshold = buy_j_threshold
        self.sell_j_threshold = sell_j_threshold
        self.cci_buy_threshold = cci_buy_threshold
        self.cci_sell_threshold = cci_sell_threshold
        self.macd_short = macd_short
        self.macd_long = macd_long
        self.macd_mid = macd_mid
        self.cci_period = cci_period
        self.kdj_n = kdj_n
        self.kdj_m1 = kdj_m1
        self.kdj_m2 = kdj_m2
        self.big_ratio = big_ratio
        self.mid_ratio = mid_ratio
        self.zig_r = zig_r
        self.kinetic_window = kinetic_window
        self.signal_filter_period = signal_filter_period
        self.turn_buy_j_threshold = turn_buy_j_threshold
        self.turn_sell_j_threshold = turn_sell_j_threshold

    @staticmethod
    def _sma_cn(x: pd.Series, n: int, m: int = 1) -> pd.Series:
        if n <= 0:
            raise ValueError("n must be > 0")
        if m <= 0 or m > n:
            raise ValueError("m must satisfy 1 <= m <= n")

        x = x.astype(float)
        out = pd.Series(index=x.index, dtype="float64")
        prev = np.nan
        alpha = m / n
        for i, value in enumerate(x.to_numpy(dtype=float)):
            if np.isnan(value):
                out.iat[i] = prev
                continue
            if np.isnan(prev):
                prev = value
            else:
                prev = alpha * value + (1.0 - alpha) * prev
            out.iat[i] = prev
        return out

    @staticmethod
    def _ema(x: pd.Series, span: int) -> pd.Series:
        return x.astype(float).ewm(span=span, adjust=False).mean()

    @staticmethod
    def _cross(a: pd.Series, b: pd.Series) -> pd.Series:
        a_prev = a.shift(1)
        b_prev = b.shift(1)
        return (a > b) & (a_prev <= b_prev)

    @staticmethod
    def _filter_cn(cond: np.ndarray, n: int) -> np.ndarray:
        if n <= 0:
            return cond.astype(bool)
        out = np.zeros(len(cond), dtype=bool)
        cooldown = 0
        for i, flag in enumerate(cond):
            if cooldown > 0:
                cooldown -= 1
                continue
            if bool(flag):
                out[i] = True
                cooldown = n
        return out

    @staticmethod
    def _cross_np(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        a_prev = np.roll(a, 1)
        b_prev = np.roll(b, 1)
        a_prev[0] = np.nan
        b_prev[0] = np.nan
        return (a > b) & (a_prev <= b_prev)

    @staticmethod
    def _barslast(cond: np.ndarray) -> np.ndarray:
        n = len(cond)
        out = np.zeros(n, dtype=int)
        last_true = -1
        for i in range(n):
            if bool(cond[i]):
                last_true = i
                out[i] = 0
            elif last_true >= 0:
                out[i] = i - last_true
            else:
                out[i] = i + 1
        return out

    @staticmethod
    def _valuewhen(cond: np.ndarray, values: np.ndarray) -> np.ndarray:
        n = len(cond)
        out = np.full(n, np.nan, dtype=float)
        last_val = np.nan
        for i in range(n):
            if bool(cond[i]):
                last_val = float(values[i])
            out[i] = last_val
        return out

    @staticmethod
    def _ref_series(values: np.ndarray, offsets: np.ndarray) -> np.ndarray:
        n = len(values)
        out = np.full(n, np.nan, dtype=float)
        for i in range(n):
            off = offsets[i]
            if np.isnan(off):
                continue
            j = i - int(off)
            if 0 <= j < n:
                out[i] = float(values[j])
        return out

    @staticmethod
    def _rolling_extreme(arr: np.ndarray, periods: np.ndarray, mode: str) -> np.ndarray:
        n = len(arr)
        out = np.full(n, np.nan, dtype=float)
        for i in range(n):
            p_val = periods[i]
            if np.isnan(p_val):
                continue
            p = int(p_val)
            if p <= 0:
                continue
            start = max(0, i - p + 1)
            window = arr[start : i + 1]
            if len(window) == 0:
                continue
            out[i] = float(np.min(window) if mode == "min" else np.max(window))
        return out

    @staticmethod
    def _sum_if(cond: np.ndarray, values: np.ndarray, periods: np.ndarray) -> np.ndarray:
        n = len(values)
        out = np.full(n, np.nan, dtype=float)
        for i in range(n):
            p = int(periods[i]) if not np.isnan(periods[i]) else 0
            if p <= 0:
                continue
            start = max(0, i - p + 1)
            mask = cond[start : i + 1]
            out[i] = float(np.sum(values[start : i + 1][mask]))
        return out

    @staticmethod
    def _sumbars(cond: np.ndarray, n_th: int) -> np.ndarray:
        out = np.full(len(cond), np.nan, dtype=float)
        true_idx = np.flatnonzero(cond)
        for i in range(len(cond)):
            pos = true_idx[true_idx <= i]
            if len(pos) >= n_th:
                out[i] = float(i - pos[-n_th] + 1)
        return out

    @staticmethod
    def _hhvbars(arr: np.ndarray, periods: np.ndarray) -> np.ndarray:
        out = np.full(len(arr), np.nan, dtype=float)
        for i in range(len(arr)):
            p = int(periods[i]) if not np.isnan(periods[i]) else 0
            if p <= 0:
                continue
            start = max(0, i - p + 1)
            window = arr[start : i + 1]
            idx = int(np.nanargmax(window))
            out[i] = float(len(window) - 1 - idx)
        return out

    @staticmethod
    def _llvbars(arr: np.ndarray, periods: np.ndarray) -> np.ndarray:
        out = np.full(len(arr), np.nan, dtype=float)
        for i in range(len(arr)):
            p = int(periods[i]) if not np.isnan(periods[i]) else 0
            if p <= 0:
                continue
            start = max(0, i - p + 1)
            window = arr[start : i + 1]
            idx = int(np.nanargmin(window))
            out[i] = float(len(window) - 1 - idx)
        return out

    @staticmethod
    def _backset(cond: np.ndarray, n_bars: np.ndarray) -> np.ndarray:
        out = np.zeros(len(cond), dtype=bool)
        for i, flag in enumerate(cond):
            if not flag:
                continue
            n = int(n_bars[i]) if not np.isnan(n_bars[i]) else 0
            if n <= 0:
                continue
            start = max(0, i - n + 1)
            out[start : i + 1] = True
        return out

    @staticmethod
    def _zig_pivots(series: np.ndarray, threshold_pct: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = len(series)
        zig = np.full(n, np.nan, dtype=float)
        peaks = np.zeros(n, dtype=bool)
        troughs = np.zeros(n, dtype=bool)
        if n == 0:
            return zig, peaks, troughs

        threshold = abs(float(threshold_pct)) / 100.0
        if threshold <= 0:
            threshold = 0.05

        valid = np.isfinite(series)
        first_idx = int(np.flatnonzero(valid)[0]) if np.any(valid) else 0
        last_pivot_idx = first_idx
        last_pivot_val = float(series[first_idx])
        zig[last_pivot_idx] = last_pivot_val
        candidate_idx = last_pivot_idx
        candidate_val = last_pivot_val
        trend = 0

        for i in range(first_idx + 1, n):
            if not np.isfinite(series[i]):
                continue
            val = float(series[i])

            if trend == 0:
                if val >= candidate_val:
                    candidate_idx = i
                    candidate_val = val
                if candidate_val != 0 and (candidate_val - last_pivot_val) / abs(candidate_val) >= threshold:
                    trend = 1
                    last_pivot_idx = candidate_idx
                    last_pivot_val = candidate_val
                    zig[last_pivot_idx] = last_pivot_val
                    peaks[last_pivot_idx] = True
                elif last_pivot_val != 0 and (last_pivot_val - candidate_val) / abs(last_pivot_val) >= threshold:
                    trend = -1
                    last_pivot_idx = candidate_idx
                    last_pivot_val = candidate_val
                    zig[last_pivot_idx] = last_pivot_val
                    troughs[last_pivot_idx] = True
                continue

            if trend > 0:
                if val >= last_pivot_val:
                    last_pivot_idx = i
                    last_pivot_val = val
                    zig[last_pivot_idx] = last_pivot_val
                    peaks[last_pivot_idx] = True
                elif last_pivot_val != 0 and (last_pivot_val - val) / abs(last_pivot_val) >= threshold:
                    trend = -1
                    last_pivot_idx = i
                    last_pivot_val = val
                    zig[last_pivot_idx] = last_pivot_val
                    troughs[last_pivot_idx] = True
            else:
                if val <= last_pivot_val:
                    last_pivot_idx = i
                    last_pivot_val = val
                    zig[last_pivot_idx] = last_pivot_val
                    troughs[last_pivot_idx] = True
                elif last_pivot_val != 0 and (val - last_pivot_val) / abs(last_pivot_val) >= threshold:
                    trend = 1
                    last_pivot_idx = i
                    last_pivot_val = val
                    zig[last_pivot_idx] = last_pivot_val
                    peaks[last_pivot_idx] = True

        return zig, peaks, troughs

    def _avedev(self, x: pd.Series, n: int) -> pd.Series:
        return x.rolling(n, min_periods=n).apply(
            lambda values: float(np.mean(np.abs(values - np.mean(values)))),
            raw=True,
        )

    def _cci(self, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        typ = (high + low + close) / 3.0
        ma = typ.rolling(self.cci_period, min_periods=self.cci_period).mean()
        avedev = self._avedev(typ, self.cci_period)
        return (typ - ma) / (0.015 * avedev.replace(0, np.nan))

    def _compute_divergence(self, df: pd.DataFrame, dif: np.ndarray, dea: np.ndarray, macd: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        close = df["close"].astype(float).to_numpy(dtype=float)
        high = df["high"].astype(float).to_numpy(dtype=float)
        low = df["low"].astype(float).to_numpy(dtype=float)

        cross_gc = self._cross_np(dif, dea)
        cross_dc = self._cross_np(dea, dif)

        jcd = cross_gc & (dif < 0)
        jc = cross_gc
        sc = cross_dc
        jt = self._sumbars(jcd, 2)
        st = self._barslast(sc).astype(float)
        l1 = self._rolling_extreme(low, st + 1, "min")
        l2 = self._ref_series(l1, jt - 1)

        mt1 = self._barslast(macd < 0)
        mt2 = self._barslast(macd > 0)
        red_area = self._sum_if(macd > 0, macd, mt1 + 1)
        green_area = self._sum_if(macd < 0, macd, mt2 + 1)

        jc_pos1 = self._sumbars(jc, 1)
        jc_pos2 = self._sumbars(jc, 2)
        green_area_1 = self._ref_series(green_area, jc_pos1)
        green_area_2 = self._ref_series(green_area, jc_pos2)
        area_bottom = jcd & (l1 < l2) & (green_area_1 > green_area_2)

        jct = self._barslast(jc).astype(float)
        h1 = self._rolling_extreme(high, jct + 1, "max")
        jcd2 = sc & (dif > 0)
        jt2 = self._sumbars(jcd2, 2)
        h2 = self._ref_series(h1, jt2 - 1)
        dead_cross_1 = self._sumbars(sc, 1)
        dead_cross_2 = self._sumbars(sc, 2)
        red_area_1 = self._ref_series(red_area, dead_cross_1)
        red_area_2 = self._ref_series(red_area, dead_cross_2)
        area_top = sc & (h1 > h2) & (red_area_1 < red_area_2)

        bn = self._barslast(cross_gc).astype(float)
        ba = self._backset(cross_dc | (np.arange(len(df), dtype=float) == len(df) - 1), self._hhvbars(macd, bn + 1) + 1)
        bn2 = self._barslast((np.roll(ba, 1) == 0) & (ba == 1)).astype(float)
        da = self._ref_series(macd, bn2)
        cc = self._ref_series(close, bn2)
        prev_cc = self._ref_series(cc, np.ones(len(df)))
        prev_da = self._ref_series(da, np.ones(len(df)))
        ba_cross = self._cross_np(ba.astype(float), np.full(len(df), 0.5))
        bar_top = ba_cross & (cc > prev_cc) & (da < prev_da)

        bm = self._barslast(cross_dc).astype(float)
        bb = self._backset(cross_gc | (np.arange(len(df), dtype=float) == len(df) - 1), self._llvbars(macd, bm + 1) + 1)
        bn1 = self._barslast((np.roll(bb, 1) == 0) & (bb == 1)).astype(float)
        xiao = self._ref_series(macd, bn1)
        cc1 = self._ref_series(close, bn1)
        prev_cc1 = self._ref_series(cc1, np.ones(len(df)))
        prev_xiao = self._ref_series(xiao, np.ones(len(df)))
        bb_cross = self._cross_np(bb.astype(float), np.full(len(df), 0.5))
        bar_bottom = bb_cross & (cc1 < prev_cc1) & (xiao > prev_xiao)

        bla1 = self._barslast(np.roll(cross_gc, 1)).astype(float)
        ref_close1 = self._ref_series(close, bla1 + 1)
        ref_dif1 = self._ref_series(dif, bla1 + 1)
        line_bottom = (ref_close1 > close) & (dif > ref_dif1) & cross_gc

        bla2 = self._barslast(np.roll(cross_dc, 1)).astype(float)
        ref_close2 = self._ref_series(close, bla2 + 1)
        ref_dif2 = self._ref_series(dif, bla2 + 1)
        line_top = (ref_close2 < close) & (ref_dif2 > dif) & cross_dc

        total_bottom = area_bottom | bar_bottom | line_bottom
        total_top = area_top | bar_top | line_top
        strong_bottom = area_bottom & bar_bottom
        strong_top = area_top & bar_top

        return total_bottom, total_top, strong_bottom, strong_top, area_bottom, bar_bottom, area_top, bar_top

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        required_columns = {"high", "low", "close"}
        if not required_columns.issubset(df.columns):
            missing = required_columns.difference(df.columns)
            raise ValueError(f"missing columns: {sorted(missing)}")

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        high_arr = high.to_numpy(dtype=float)
        low_arr = low.to_numpy(dtype=float)
        amount_arr = df["amount"].astype(float).to_numpy(dtype=float) if "amount" in df.columns else np.full(len(df), np.nan)
        vol_arr = df["vol"].astype(float).to_numpy(dtype=float) if "vol" in df.columns else np.full(len(df), np.nan)

        dif = self._ema(close, self.macd_short) - self._ema(close, self.macd_long)
        dea = self._ema(dif, self.macd_mid)
        macd = 2.0 * (dif - dea)
        cci = self._cci(high, low, close)

        vol_safe = pd.Series(vol_arr, index=df.index).replace(0, np.nan)
        jj = pd.Series(amount_arr, index=df.index) / vol_safe / 100.0
        jj = jj.where(jj.notna(), close)

        llv_l = low.rolling(self.kdj_n, min_periods=self.kdj_n).min()
        hhv_h = high.rolling(self.kdj_n, min_periods=self.kdj_n).max()
        denom = (hhv_h - llv_l).replace(0, np.nan)
        rsv = (close - llv_l) / denom * 100.0
        k = self._sma_cn(rsv, self.kdj_m1, 1)
        d = self._sma_cn(k, self.kdj_m2, 1)
        j = 3 * k - 2 * d

        (
            total_bottom,
            total_top,
            strong_bottom,
            strong_top,
            two_round_bottom,
            inner_bottom,
            two_round_top,
            inner_top,
        ) = self._compute_divergence(
            df,
            dif.to_numpy(dtype=float),
            dea.to_numpy(dtype=float),
            macd.to_numpy(dtype=float),
        )

        red_val = np.where(macd.to_numpy(dtype=float) > 0, macd.to_numpy(dtype=float), 0.0)
        gre_val = np.where(macd.to_numpy(dtype=float) < 0, macd.to_numpy(dtype=float), 0.0)
        red_max_144 = pd.Series(red_val, index=df.index).rolling(self.kinetic_window, min_periods=1).max().to_numpy(dtype=float)
        gre_min_144 = pd.Series(gre_val, index=df.index).rolling(self.kinetic_window, min_periods=1).min().to_numpy(dtype=float)

        macd_arr = macd.to_numpy(dtype=float)
        dif_arr = dif.to_numpy(dtype=float)
        dea_arr = dea.to_numpy(dtype=float)

        is_red = macd_arr > 0
        is_gre = macd_arr < 0
        red_wave_start = self._barslast(is_gre) + 1
        gre_wave_start = self._barslast(is_red) + 1
        curr_red_peak = self._rolling_extreme(macd_arr, red_wave_start.astype(float), "max")
        curr_gre_trough = self._rolling_extreme(macd_arr, gre_wave_start.astype(float), "min")

        big_red = is_red & (curr_red_peak >= red_max_144 * self.big_ratio)
        mid_red = is_red & (curr_red_peak < red_max_144 * self.big_ratio) & (curr_red_peak >= red_max_144 * self.mid_ratio)
        small_red = is_red & (curr_red_peak < red_max_144 * self.mid_ratio)

        big_gre = is_gre & (curr_gre_trough <= gre_min_144 * self.big_ratio)
        mid_gre = is_gre & (curr_gre_trough > gre_min_144 * self.big_ratio) & (curr_gre_trough <= gre_min_144 * self.mid_ratio)
        small_gre = is_gre & (curr_gre_trough > gre_min_144 * self.mid_ratio)

        macd_zig, wave_peak, wave_trough = self._zig_pivots(macd_arr, self.zig_r)

        cci_arr = cci.to_numpy(dtype=float)
        j_arr = j.to_numpy(dtype=float)

        buy_b_raw = total_bottom & (cci_arr < self.cci_buy_threshold) & (j_arr < 20)
        sell_s_raw = total_top & (cci_arr > self.cci_sell_threshold) & (j_arr > 80)

        bottom_fishing_raw = strong_bottom & (cci_arr < self.cci_buy_threshold) & (j_arr < 20) & (dif_arr < 0) & (dea_arr < 0) & (dif_arr < dea_arr)
        top_escape_raw = strong_top & (cci_arr > self.cci_sell_threshold) & (j_arr > 80) & (dif_arr > 0) & (dea_arr > 0) & (dif_arr > dea_arr)

        big_gre = is_gre & (curr_gre_trough <= gre_min_144 * self.big_ratio)
        big_red = is_red & (curr_red_peak >= red_max_144 * self.big_ratio)
        buy_jb_raw = big_gre & wave_trough & (dif_arr < 0) & (dea_arr < 0) & (cci_arr < self.cci_buy_threshold) & (j_arr < 15)
        sell_js_raw = big_red & wave_peak & (dif_arr > 0) & (dea_arr > 0) & (cci_arr > self.cci_sell_threshold) & (j_arr > 85)

        buy_b = self._filter_cn(buy_b_raw, self.signal_filter_period)
        sell_s = self._filter_cn(sell_s_raw, self.signal_filter_period)
        bottom_fishing = self._filter_cn(bottom_fishing_raw, self.signal_filter_period)
        top_escape = self._filter_cn(top_escape_raw, self.signal_filter_period)
        buy_jb = self._filter_cn(buy_jb_raw, self.signal_filter_period)
        sell_js = self._filter_cn(sell_js_raw, self.signal_filter_period)

        out = df.copy()
        out["K"] = k
        out["D"] = d
        out["J"] = j
        out["DIF"] = dif
        out["DEA"] = dea
        out["MACD"] = macd
        out["CCI"] = cci
        out["IS_RED"] = is_red
        out["IS_GRE"] = is_gre
        out["RED_MAX_144"] = red_max_144
        out["GRE_MIN_144"] = gre_min_144
        out["CURRENT_RED_PEAK"] = curr_red_peak
        out["CURRENT_GRE_TROUGH"] = curr_gre_trough
        out["MACD_ZIG"] = macd_zig
        out["WAVE_PEAK"] = wave_peak
        out["WAVE_TROUGH"] = wave_trough
        out["BIG_RED"] = big_red
        out["MID_RED"] = mid_red
        out["SMALL_RED"] = small_red
        out["BIG_GRE"] = big_gre
        out["MID_GRE"] = mid_gre
        out["SMALL_GRE"] = small_gre
        out["BOTTOM_DIV"] = total_bottom
        out["TOP_DIV"] = total_top
        out["STRONG_BOTTOM"] = strong_bottom
        out["STRONG_TOP"] = strong_top
        out["TWO_ROUND_BOTTOM_DIV"] = two_round_bottom
        out["INNER_BOTTOM_DIV"] = inner_bottom
        out["TWO_ROUND_TOP_DIV"] = two_round_top
        out["INNER_TOP_DIV"] = inner_top
        out["BUY_B_RAW"] = buy_b_raw
        out["SELL_S_RAW"] = sell_s_raw
        out["BOTTOM_FISHING_RAW"] = bottom_fishing_raw
        out["TOP_ESCAPE_RAW"] = top_escape_raw
        out["BUY_JB_RAW"] = buy_jb_raw
        out["SELL_JS_RAW"] = sell_js_raw

        out["BUY_B"] = buy_b
        out["SELL_S"] = sell_s
        out["BOTTOM_FISHING"] = bottom_fishing
        out["TOP_ESCAPE"] = top_escape
        out["BUY_JB"] = buy_jb
        out["SELL_JS"] = sell_js
        out["BUY"] = buy_b
        out["SELL"] = sell_s
        return out

    def latest_signal(self, ts_code: str, df: pd.DataFrame, min_bars_required: int = 150) -> Optional[SignalResult]:
        if df is None or df.empty:
            return None
        if len(df) < min_bars_required:
            return None

        enriched = self.compute(df)
        last = enriched.iloc[-1]
        return SignalResult(
            ts_code=ts_code,
            bar_time=str(last.get("time", "")),
            close=float(last["close"]),
            j=float(last["J"]),
            dif=float(last["DIF"]),
            dea=float(last["DEA"]),
            macd=float(last["MACD"]),
            cci=float(last["CCI"]) if pd.notna(last["CCI"]) else float("nan"),
            buy_b=bool(last["BUY_B"]),
            sell_s=bool(last["SELL_S"]),
            bottom_fishing=bool(last["BOTTOM_FISHING"]),
            top_escape=bool(last["TOP_ESCAPE"]),
            buy_jb=bool(last["BUY_JB"]),
            sell_js=bool(last["SELL_JS"]),
        )
