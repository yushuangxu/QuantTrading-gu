#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shared_data_filler.py —— 统一数据获取器（每秒获取，分时K线 + 实时行情共用）
使用 pytdx 直连通达信行情主站，单次连接、每秒获取所有数据。
支持磁盘持久化：定期将内存数据同步到 CSV，重启时自动加载历史缓存。
"""

from __future__ import annotations

import threading
import time
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from pytdx.hq import TdxHq_API
from bar_cache_merge import (
    BarCacheValidationError, merge_minute_bars, normalize_minute_bars,
    security_code, minute_frequency, BAR_COLUMNS,
)
from market_data_state import MarketDataState, TradingCalendar, SHANGHAI


class SharedDataFetcher:
    """
    统一数据获取器：每秒获取一次所有标的分时K线 + 实时行情，存入共享状态。
    所有监控模块从共享状态读取，不单独连接行情主站。
    """

    HOSTS = [
        ("115.238.90.165", 7709),
        ("119.147.212.81", 7709),
        ("119.147.212.82", 7709),
        ("203.197.225.188", 7709),
        ("218.75.126.9", 7709),
        ("114.80.63.12", 7709),
        ("218.108.98.244", 7709),
        ("180.153.18.170", 7709),
        ("114.80.63.11", 7709),
        ("123.125.108.23", 7709),
    ]

    def __init__(self, ts_codes: List[str], freq: str = "1MIN", fetch_interval: float = 1.0,
                 cache_dir: Optional[str] = None, save_interval_bars: int = 60,
                 clock=None, quality_calendar=None, save_interval_seconds: float = 30.0,
                 monotonic=None):
        if save_interval_seconds <= 0 or save_interval_bars <= 0:
            raise ValueError('Save intervals must be positive')
        self.ts_codes = [security_code(code) for code in ts_codes]
        self.freq = minute_frequency(freq)
        self.fetch_interval = fetch_interval
        self._api = TdxHq_API()
        self._connected = False
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._save_lock = threading.Lock()
        self._monotonic = monotonic or time.monotonic
        self._save_interval_seconds = save_interval_seconds
        self._data_version = {}
        self._saved_version = {}
        self._dirty_since = {}
        self._last_save_attempt = {}
        self._last_save_error = {}
        self._stop_errors = []
        self._disconnected_after_stop = False

        # 磁盘持久化
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path(__file__).resolve().parent / "runtime" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._save_interval_bars = save_interval_bars  # 每积累多少根K线保存一次
        self._bar_count_since_save: Dict[str, int] = {}  # ts_code -> 自上次保存以来的新增K线数
        self._save_thread: Optional[threading.Thread] = None
        self._save_thread_running = False

        # 共享状态
        self._minute_bars: Dict[str, pd.DataFrame] = {}  # ts_code -> DataFrame
        self._realtime_quotes: Dict[str, dict] = {}       # code -> quote dict
        self._last_bar_time: Dict[str, str] = {}          # ts_code -> last bar time
        self._last_fetch_at: float = 0
        self._last_min_bar_fetch: float = 0               # 上次获取分钟K线的时间
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._quality = MarketDataState(calendar=quality_calendar if quality_calendar is not None else
            TradingCalendar.from_cache(Path(__file__).resolve().parent / 'runtime' / 'calendar'))
        self._bar_errors = {}

    # ---------- 磁盘持久化 ----------

    def _cache_path(self, ts_code: str) -> Path:
        safe_code = security_code(ts_code).replace(".", "_")
        safe_freq = minute_frequency(self.freq)
        return self.cache_dir / f"{safe_code}_{safe_freq}.csv"

    def _save_disk_cache(self, ts_code: str, df: pd.DataFrame) -> None:
        """Atomic eight-column snapshot. Caller owns the save lock.

        Never open the destination for writing. An error after replace (e.g.
        directory fsync) is an unacknowledged save, not permission to roll back
        or delete the newly valid destination.
        """
        path = self._cache_path(ts_code)
        temporary = None
        try:
            valid = normalize_minute_bars(df, ts_code=ts_code, freq=self.freq)
            if valid.empty:
                raise BarCacheValidationError('Refusing an empty persistence snapshot')
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                    prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                valid.to_csv(stream, index=False, columns=BAR_COLUMNS, float_format='%.17g')
                stream.flush()
                os.fsync(stream.fileno())
            check = pd.read_csv(temporary, dtype={'ts_code': str}, float_precision='round_trip')
            check = normalize_minute_bars(check, ts_code=ts_code, freq=self.freq, declared_freq=self.freq)
            pd.testing.assert_frame_equal(valid, check, check_dtype=False, check_exact=True)
            os.replace(temporary, path)
            temporary = None
            if os.name == 'posix':
                descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    print(f'清理临时缓存失败 ({temporary.name}): {exc}', flush=True)

    @staticmethod
    def _acquire_before(lock, deadline):
        acquired = lock.acquire() if deadline is None else lock.acquire(timeout=max(0., deadline - time.monotonic()))
        if not acquired:
            raise TimeoutError('等待缓存/保存锁超时；不能确认最终保存完成')

    def get_persistence_status(self, ts_code):
        with self._lock:
            return dict(version=self._data_version.get(ts_code, 0),
                        saved_version=self._saved_version.get(ts_code, 0),
                        dirty=ts_code in self._dirty_since,
                        last_error=self._last_save_error.get(ts_code))

    def flush_pending(self, *, force=False, timeout=None) -> bool:
        """Serialize writers; snapshot under memory lock, write outside it.

        Save acknowledgement applies ONLY to the captured version. New data can
        arrive while writing and remains dirty. Failure never clears pending.
        """
        deadline = None if timeout is None else time.monotonic() + max(0., timeout)
        try:
            self._acquire_before(self._save_lock, deadline)
        except TimeoutError as exc:
            print(f'保存缓存失败: {exc}', flush=True)
            return False
        try:
            self._acquire_before(self._lock, deadline)
            try:
                now = self._monotonic()
                pending = []
                for code, since in self._dirty_since.items():
                    count = self._bar_count_since_save.get(code, 0)
                    due = count >= self._save_interval_bars or now - since >= self._save_interval_seconds
                    paced = now - self._last_save_attempt.get(code, float('-inf')) >= self._save_interval_seconds
                    if force or (due and paced):
                        pending.append((code, self._minute_bars[code].copy(deep=True), self._data_version[code], count))
                        self._last_save_attempt[code] = now
            finally:
                self._lock.release()
            ok = True
            for code, frame, version, count in pending:
                try:
                    self._save_disk_cache(code, frame)
                except Exception as exc:
                    self._acquire_before(self._lock, deadline)
                    try:
                        self._last_save_error[code] = str(exc)
                    finally:
                        self._lock.release()
                    print(f'保存缓存失败 ({code}, version={version}): {exc}；保留待保存状态', flush=True)
                    ok = False
                    continue
                self._acquire_before(self._lock, deadline)
                try:
                    self._saved_version[code] = version
                    self._last_save_error.pop(code, None)
                    self._bar_count_since_save[code] = max(0, self._bar_count_since_save.get(code, 0) - count)
                    if self._data_version[code] == version:
                        self._dirty_since.pop(code, None)
                finally:
                    self._lock.release()
            return ok
        except Exception as exc:
            print(f'保存缓存失败: {exc}；未确认的数据仍待保存', flush=True)
            return False
        finally:
            self._save_lock.release()

    def _load_disk_cache(self, ts_code: str) -> pd.DataFrame:
        path = self._cache_path(ts_code)
        if not path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_csv(path, dtype={"ts_code": str}, float_precision='round_trip')
        except Exception as e:
            raise BarCacheValidationError(f"加载缓存失败 ({ts_code}, {self.freq}): {e}") from e
        # Legacy eight-column CSV period is explicitly bound by the requested
        # cache filename, never inferred from spacing. No disk data is rewritten.
        return normalize_minute_bars(df, ts_code=ts_code, freq=self.freq, declared_freq=self.freq)

    def _load_all_disk_cache(self) -> None:
        """启动时加载所有标的磁盘缓存到内存"""
        loaded = 0
        for ts_code in self.ts_codes:
            df = self._load_disk_cache(ts_code)
            if not df.empty:
                merged = merge_minute_bars(
                    self._minute_bars.get(ts_code, pd.DataFrame()), df,
                    ts_code=ts_code, freq=self.freq,
                )
                self._minute_bars[ts_code] = merged
                self._last_bar_time[ts_code] = merged.iloc[-1]["time"]
                self._bar_count_since_save[ts_code] = 0
                self._data_version.setdefault(ts_code, 0)
                self._saved_version.setdefault(ts_code, 0)
                self._quality.observe(ts_code, self.freq, merged, self._clock(), cache_only=True)
                loaded += 1
        if loaded > 0:
            print(f"从磁盘缓存恢复了 {loaded}/{len(self.ts_codes)} 只标的的历史数据")

    def _save_loop(self):
        """Interruptible periodic dirty flush, including same-bar revisions."""
        while self._save_thread_running and not self._stop_event.wait(self._save_interval_seconds):
            self.flush_pending()

    def _track_bar_count(self, ts_code: str, new_df: pd.DataFrame) -> None:
        """追踪新增 K 线数"""
        old_df = self._minute_bars.get(ts_code)
        if old_df is None:
            self._bar_count_since_save[ts_code] = self._bar_count_since_save.get(ts_code, 0) + len(new_df)
            changed = not new_df.empty
        else:
            normalized = normalize_minute_bars(old_df, ts_code=ts_code, freq=self.freq)
            old_times = set(normalized["time"])
            new_count = sum(1 for t in new_df["time"] if t not in old_times)
            self._bar_count_since_save[ts_code] = self._bar_count_since_save.get(ts_code, 0) + new_count
            changed = len(normalized) != len(new_df) or not normalized.eq(new_df).all().all()
        if changed:
            self._data_version[ts_code] = self._data_version.get(ts_code, 0) + 1
            self._dirty_since.setdefault(ts_code, self._monotonic())

    # ---------- 连接管理 ----------

    def connect(self) -> bool:
        """连接行情主站"""
        import os, sys
        devnull = open(os.devnull, 'w')
        saved_stderr = sys.stderr
        sys.stderr = devnull
        try:
            for h, p in self.HOSTS:
                try:
                    if self._api.connect(h, p):
                        self._connected = True
                        self._disconnected_after_stop = False
                        return True
                except Exception:
                    continue
            self._connected = False
            return False
        finally:
            sys.stderr = saved_stderr
            devnull.close()

    def reconnect(self) -> bool:
        try:
            self._api.disconnect()
        except Exception:
            pass
        self._connected = False
        return self.connect()

    def _ensure_connected(self) -> bool:
        if self._connected:
            return True
        return self.reconnect()

    # ---------- 数据获取 ----------

    @staticmethod
    def _code_to_market(ts_code: str) -> int:
        ts_code = security_code(ts_code)
        if ts_code.endswith(".SZ"):
            return 0
        if ts_code.endswith(".SH"):
            return 1
        if ts_code.endswith(".BJ"):
            return 2
        code = ts_code.split(".")[0]
        if code.startswith(("6", "9", "5")):
            return 1
        if code.startswith("920"):
            return 2
        return 0

    @staticmethod
    def _code_to_tdx(ts_code: str) -> str:
        return ts_code.split(".")[0]

    @staticmethod
    def _freq_to_category(freq: str) -> int:
        """pytdx category: 0=5MIN, 1=15MIN, 2=30MIN, 3=60MIN, 8=1MIN"""
        mapping = {"1MIN": 8, "5MIN": 0, "15MIN": 1, "30MIN": 2, "60MIN": 3}
        return mapping[minute_frequency(freq)]

    def _fetch_minute_bars(self, ts_code: str) -> Optional[pd.DataFrame]:
        """获取单只标的分时K线"""
        market = self._code_to_market(ts_code)
        code = self._code_to_tdx(ts_code)
        category = self._freq_to_category(self.freq)
        try:
            bars = self._api.get_security_bars(category, market, code, 0, 200)
            if not bars:
                # pytdx default error mode returns None rather than raising.
                # Empty replies cannot establish either connectivity or freshness.
                self._connected = False
                return None
            rows = []
            for b in bars:
                # If a producer supplies identity metadata, never discard a
                # conflict while assigning the requested series to raw rows.
                if "ts_code" in b and security_code(b["ts_code"]) != security_code(ts_code):
                    raise BarCacheValidationError("TDX batch contains a different security")
                if "code" in b and b["code"] != code:
                    raise BarCacheValidationError("TDX batch contains a different code")
                if "market" in b and b["market"] != market:
                    raise BarCacheValidationError("TDX batch contains a different market")
                if "freq" in b and minute_frequency(b["freq"]) != self.freq:
                    raise BarCacheValidationError("TDX batch contains a different frequency")
                dt = b.get("datetime")
                if not dt:
                    dt = f"{b['year']}-{b['month']:02d}-{b['day']:02d} {b['hour']:02d}:{b['minute']:02d}"
                rows.append({
                    "ts_code": ts_code,
                    "time": dt,
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "vol": float(b.get("vol", 0)),
                    "amount": float(b.get("amount", 0)),
                })

            df = pd.DataFrame(rows)
            df = normalize_minute_bars(df, ts_code=ts_code, freq=self.freq, declared_freq=self.freq)
            # 过滤无效 Bar（pytdx 收盘时 vol/amount 可能为极小数如 5.8e-39）
            df = df[(df["vol"] > 1) & (df["amount"] > 1)].reset_index(drop=True)
            return df
        except Exception as e:
            self._bar_errors[ts_code] = str(e)
            if not isinstance(e, (BarCacheValidationError, ValueError, KeyError, TypeError)):
                self._connected = False
            print(f"获取K线失败 ({ts_code}): {e}")
            return None

    def _fetch_realtime_quotes(self) -> List[dict]:
        """获取所有标的实时行情"""
        pairs = []
        for ts_code in self.ts_codes:
            market = self._code_to_market(ts_code)
            code = self._code_to_tdx(ts_code)
            pairs.append((market, code))
        try:
            result = self._api.get_security_quotes(pairs)
            return result if result else []
        except Exception as e:
            print(f"获取实时行情失败: {e}")
            return []

    # ---------- 获取接口（供外部读取） ----------

    def get_minute_bars(self, ts_code: str) -> pd.DataFrame:
        """获取最新分时K线"""
        return self._minute_bars.get(ts_code, pd.DataFrame())

    def get_minute_snapshot(self, ts_code: str):
        """Atomic copy + quality sidecar; never add metadata columns to CSVs."""
        with self._lock:
            return (self._minute_bars.get(ts_code, pd.DataFrame()).copy(deep=True),
                    self._quality.snapshot(ts_code, self.freq, self._clock()))

    def get_market_status(self, ts_code: str):
        with self._lock:
            return self._quality.snapshot(ts_code, self.freq, self._clock())

    def get_realtime_quote(self, code: str) -> Optional[dict]:
        """获取最新实时行情（code 不带 .SZ/.SH）"""
        return self._realtime_quotes.get(code)

    def get_all_realtime_quotes(self) -> Dict[str, dict]:
        """获取所有实时行情"""
        return dict(self._realtime_quotes)

    def get_last_bar_time(self, ts_code: str) -> str:
        return self._last_bar_time.get(ts_code, "")

    # ---------- 后台循环 ----------

    def _tick_once(self):
        """单次获取所有数据"""
        if self._stop_event.is_set():
            return
        with self._lock:
            if not self._ensure_connected():
                for ts_code in self.ts_codes:
                    self._quality.request(ts_code, self.freq, self._clock())
                    self._quality.failure(ts_code, self.freq, self._clock(), 'TDX connection unavailable', 'disconnected')
                return

            # 1. 实时快照 — 每秒获取
            try:
                quotes = self._fetch_realtime_quotes()
                for q in quotes:
                    code = q.get("code", "")
                    if code:
                        self._realtime_quotes[code] = q
            except Exception as e:
                print(f"获取实时行情异常: {e}")

            # 2. 分钟K线 — 每60秒获取一次
            now = time.time()
            if now - self._last_min_bar_fetch >= 60:
                for ts_code in self.ts_codes:
                    self._quality.request(ts_code, self.freq, self._clock())
                    self._bar_errors.pop(ts_code, None)
                    try:
                        df = self._fetch_minute_bars(ts_code)
                        if df is not None and len(df) > 0:
                            # Stage and validate the entire union before changing
                            # any existing history, latest time or save counter.
                            merged = merge_minute_bars(
                                self._minute_bars.get(ts_code, pd.DataFrame()), df,
                                ts_code=ts_code, freq=self.freq,
                            )
                            self._quality.observe(ts_code, self.freq, df, self._clock())
                            self._track_bar_count(ts_code, merged)
                            self._minute_bars[ts_code] = merged
                            self._last_bar_time[ts_code] = merged.iloc[-1]["time"]
                        else:
                            error = self._bar_errors.get(ts_code)
                            self._quality.failure(ts_code, self.freq, self._clock(),
                                                  error or 'Empty/filtered TDX response',
                                                  'error' if error else 'empty')
                    except Exception as e:
                        self._quality.failure(ts_code, self.freq, self._clock(), str(e))
                        print(f"获取 {ts_code} K线异常: {e}")
                self._last_min_bar_fetch = now

            self._last_fetch_at = time.time()

    def _run(self):
        """后台循环：按 fetch_interval 间隔获取"""
        while self._running and not self._stop_event.is_set():
            self._tick_once()
            self._stop_event.wait(self.fetch_interval)

    def start(self):
        """启动后台获取线程 + 加载磁盘缓存 + 启动保存线程"""
        # 启动前先加载磁盘缓存
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('Fetcher already running')
        self._load_all_disk_cache()
        self._stop_event.clear()

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="shared-data-fetcher")
        self._thread.start()

        # 启动后台保存线程
        self._save_thread_running = True
        self._save_thread = threading.Thread(target=self._save_loop, daemon=True, name="data-save-thread")
        self._save_thread.start()

    def stop(self, timeout=10.0) -> bool:
        """Bound thread/lock waits; report incomplete shutdown, never fake success."""
        self._running = False
        self._save_thread_running = False
        self._stop_event.set()
        deadline = time.monotonic() + max(0., timeout)
        errors = []
        for thread, name in [(self._thread, '行情线程'), (self._save_thread, '保存线程')]:
            if thread is not None:
                try:
                    thread.join(timeout=max(0., deadline - time.monotonic()))
                    if thread.is_alive():
                        errors.append(f'{name}退出超时，可能仍有未持久化的更新')
                except Exception as exc:
                    errors.append(f'{name}停止失败: {exc}')
        try:
            if not self.flush_pending(force=True, timeout=max(0., deadline - time.monotonic())):
                errors.append('最终保存未完成，请检查保存错误与待保存状态')
            self._acquire_before(self._lock, deadline)
            try:
                if self._dirty_since:
                    errors.append('最终保存后仍有待保存版本: ' + ','.join(sorted(self._dirty_since)))
            finally:
                self._lock.release()
        except Exception as exc:
            errors.append(f'最终保存检查异常: {exc}')
        try:
            fetch_alive = self._thread is not None and self._thread.is_alive()
        except Exception as exc:
            fetch_alive = True
            errors.append(f'无法确认行情线程是否停止: {exc}')
        if not fetch_alive and not self._disconnected_after_stop:
            try:
                self._api.disconnect()
                self._connected = False
                self._disconnected_after_stop = True
            except Exception as exc:
                errors.append(f'关闭行情连接失败: {exc}')
        elif fetch_alive:
            errors.append('行情线程仍在使用连接，未并发强关；退出未完成')
        self._stop_errors = errors
        for error in errors:
            print(f'停止行情获取器失败: {error}', flush=True)
        return not errors

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()
