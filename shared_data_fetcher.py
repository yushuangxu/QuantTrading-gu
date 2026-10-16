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
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from pytdx.hq import TdxHq_API


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
                 cache_dir: Optional[str] = None, save_interval_bars: int = 60):
        self.ts_codes = ts_codes
        self.freq = freq
        self.fetch_interval = fetch_interval
        self._api = TdxHq_API()
        self._connected = False
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

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

    # ---------- 磁盘持久化 ----------

    def _cache_path(self, ts_code: str) -> Path:
        safe_code = ts_code.replace(".", "_").upper()
        safe_freq = self.freq.upper()
        return self.cache_dir / f"{safe_code}_{safe_freq}.csv"

    def _save_disk_cache(self, ts_code: str, df: pd.DataFrame) -> None:
        path = self._cache_path(ts_code)
        try:
            df.to_csv(path, index=False)
        except Exception as e:
            print(f"保存缓存失败 ({ts_code}): {e}")

    def _load_disk_cache(self, ts_code: str) -> pd.DataFrame:
        path = self._cache_path(ts_code)
        if not path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_csv(path, dtype={"ts_code": str})
            required = {"ts_code", "time", "open", "high", "low", "close", "vol", "amount"}
            if not df.empty and required.issubset(df.columns):
                return df[["ts_code", "time", "open", "high", "low", "close", "vol", "amount"]]
        except Exception as e:
            print(f"加载缓存失败 ({ts_code}): {e}")
        return pd.DataFrame()

    def _load_all_disk_cache(self) -> None:
        """启动时加载所有标的磁盘缓存到内存"""
        loaded = 0
        for ts_code in self.ts_codes:
            df = self._load_disk_cache(ts_code)
            if not df.empty:
                self._minute_bars[ts_code] = df
                self._last_bar_time[ts_code] = df.iloc[-1]["time"]
                self._bar_count_since_save[ts_code] = 0
                loaded += 1
        if loaded > 0:
            print(f"从磁盘缓存恢复了 {loaded}/{len(self.ts_codes)} 只标的的历史数据")

    def _save_loop(self):
        """后台保存线程：每 30 秒检查一次，有新增 K 线就保存"""
        while self._save_thread_running:
            time.sleep(30)
            with self._lock:
                for ts_code, df in self._minute_bars.items():
                    cnt = self._bar_count_since_save.get(ts_code, 0)
                    if cnt >= self._save_interval_bars and not df.empty:
                        self._save_disk_cache(ts_code, df)
                        self._bar_count_since_save[ts_code] = 0

    def _track_bar_count(self, ts_code: str, new_df: pd.DataFrame) -> None:
        """追踪新增 K 线数"""
        old_df = self._minute_bars.get(ts_code)
        if old_df is None:
            self._bar_count_since_save[ts_code] = self._bar_count_since_save.get(ts_code, 0) + len(new_df)
        else:
            old_times = set(old_df["time"])
            new_count = sum(1 for t in new_df["time"] if t not in old_times)
            self._bar_count_since_save[ts_code] = self._bar_count_since_save.get(ts_code, 0) + new_count

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
        return mapping.get(freq.upper(), 8)

    def _fetch_minute_bars(self, ts_code: str) -> Optional[pd.DataFrame]:
        """获取单只标的分时K线"""
        market = self._code_to_market(ts_code)
        code = self._code_to_tdx(ts_code)
        category = self._freq_to_category(self.freq)
        try:
            bars = self._api.get_security_bars(category, market, code, 0, 200)
            if not bars:
                return None
            rows = []
            for b in bars:
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
            # 过滤无效 Bar（pytdx 收盘时 vol/amount 可能为极小数如 5.8e-39）
            df = df[(df["vol"] > 1) & (df["amount"] > 1)].reset_index(drop=True)
            df = df.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
            return df
        except Exception as e:
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
        with self._lock:
            if not self._ensure_connected():
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
                    try:
                        df = self._fetch_minute_bars(ts_code)
                        if df is not None and not df.empty:
                            self._track_bar_count(ts_code, df)
                            self._minute_bars[ts_code] = df
                            self._last_bar_time[ts_code] = df.iloc[-1]["time"]
                    except Exception as e:
                        print(f"获取 {ts_code} K线异常: {e}")
                self._last_min_bar_fetch = now

            self._last_fetch_at = time.time()

    def _run(self):
        """后台循环：按 fetch_interval 间隔获取"""
        while self._running:
            self._tick_once()
            time.sleep(self.fetch_interval)

    def start(self):
        """启动后台获取线程 + 加载磁盘缓存 + 启动保存线程"""
        # 启动前先加载磁盘缓存
        self._load_all_disk_cache()

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="shared-data-fetcher")
        self._thread.start()

        # 启动后台保存线程
        self._save_thread_running = True
        self._save_thread = threading.Thread(target=self._save_loop, daemon=True, name="data-save-thread")
        self._save_thread.start()

    def stop(self):
        """停止后台获取线程 + 最终保存 + 停止保存线程"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

        # 最终保存一次
        self._save_thread_running = False
        if self._save_thread:
            self._save_thread.join(timeout=5)
        with self._lock:
            for ts_code, df in self._minute_bars.items():
                if not df.empty:
                    self._save_disk_cache(ts_code, df)

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()
