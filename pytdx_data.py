from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from pytdx.hq import TdxHq_API


def _suppress_stderr_connect(fn):
    """装饰器：调用 fn 时抑制 stderr（绕过 pytdx best_ip 的噪音）"""
    def wrapper(*args, **kwargs):
        import sys, os
        devnull = open(os.devnull, 'w')
        saved = sys.stderr
        sys.stderr = devnull
        try:
            return fn(*args, **kwargs)
        finally:
            sys.stderr = saved
            devnull.close()
    return wrapper


class PytdxDataSource:
    """
    使用 pytdx 直连通达信行情主站作为数据源（替代 akshare）。
    支持 1/5/15/30/60 分钟 K线。
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

    def __init__(self) -> None:
        self._cache: Dict[str, pd.DataFrame] = {}
        self.cache_dir = Path(__file__).resolve().parent / "runtime" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._api = TdxHq_API()
        self._connected = False
        self._lock = threading.Lock()

    # ---------- 频率/市场映射 ----------

    @staticmethod
    def _freq_to_category(freq: str) -> int:
        """pytdx category: 0=5MIN, 1=15MIN, 2=30MIN, 3=60MIN, 8=1MIN"""
        mapping = {"1MIN": 8, "5MIN": 0, "15MIN": 1, "30MIN": 2, "60MIN": 3}
        return mapping.get(freq.upper(), 8)

    @staticmethod
    def _code_to_market(ts_code: str) -> int:
        """000021.SZ -> 0 (深市), 600183.SH -> 1 (沪市)"""
        if ts_code.endswith(".SZ"):
            return 0
        if ts_code.endswith(".SH"):
            return 1
        code = ts_code.split(".")[0]
        if code.startswith(("6", "9", "5")):
            return 1
        return 0

    @staticmethod
    def _code_to_tdx(ts_code: str) -> str:
        """000021.SZ -> 000021"""
        return ts_code.split(".")[0]

    # ---------- 连接管理 ----------

    def connect(self) -> bool:
        """连接行情主站（遍历公共列表，best_ip 有 bug 已绕过）"""
        for h, p in self.HOSTS:
            try:
                if self._api.connect(h, p):
                    self._connected = True
                    return True
            except Exception:
                continue
        self._connected = False
        return False

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

    # ---------- 缓存 ----------

    def _cache_key(self, ts_code: str, freq: str) -> str:
        return f"{ts_code.upper()}::{freq.upper()}"

    def _cache_path(self, ts_code: str, freq: str) -> Path:
        safe_code = ts_code.replace(".", "_").upper()
        safe_freq = freq.replace(".", "_").upper()
        return self.cache_dir / f"{safe_code}_{safe_freq}.csv"

    def _load_disk_cache(self, ts_code: str, freq: str) -> pd.DataFrame:
        path = self._cache_path(ts_code, freq)
        if not path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_csv(path, dtype={"ts_code": str})
            required = {"ts_code", "time", "open", "high", "low", "close", "vol", "amount"}
            if not df.empty and required.issubset(df.columns):
                return df[["ts_code", "time", "open", "high", "low", "close", "vol", "amount"]]
        except Exception as e:
            print(f"加载本地缓存失败 ({ts_code}, {freq}): {e}")
        return pd.DataFrame()

    def _save_disk_cache(self, ts_code: str, freq: str, df: pd.DataFrame) -> None:
        path = self._cache_path(ts_code, freq)
        try:
            df.to_csv(path, index=False)
        except Exception as e:
            print(f"保存本地缓存失败 ({ts_code}, {freq}): {e}")

    # ---------- 数据获取 ----------

    def _fetch_bars(self, ts_code: str, freq: str, start: int, count: int) -> List[dict]:
        """从 TDX 获取原始 K线数据"""
        with self._lock:
            if not self._ensure_connected():
                return []
            market = self._code_to_market(ts_code)
            code = self._code_to_tdx(ts_code)
            category = self._freq_to_category(freq)
            try:
                bars = self._api.get_security_bars(category, market, code, start, count)
                return bars if bars else []
            except Exception as e:
                print(f"获取K线失败 ({ts_code}, start={start}, count={count}): {e}")
                # 尝试重连一次
                if self.reconnect():
                    try:
                        bars = self._api.get_security_bars(category, market, code, start, count)
                        return bars if bars else []
                    except Exception:
                        pass
                return []

    @staticmethod
    def _bars_to_df(bars: List[dict], ts_code: str) -> pd.DataFrame:
        """将 TDX 原始 K线转换为标准 DataFrame（按时间升序）"""
        if not bars:
            return pd.DataFrame()
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
        df = df.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
        return df

    # ---------- 公开接口 ----------

    def load_history(self, ts_code: str, freq: str) -> None:
        """
        加载历史 K线（启动时调用一次）。
        分页获取最多 800 根（TDX 单次上限），与磁盘缓存合并。
        """
        cache_key = self._cache_key(ts_code, freq)
        cached = self._load_disk_cache(ts_code, freq)

        all_bars: List[dict] = []
        start = 0
        batch = 200
        max_batches = 4  # 最多 800 根

        for _ in range(max_batches):
            bars = self._fetch_bars(ts_code, freq, start, batch)
            if not bars:
                break
            all_bars.extend(bars)
            start += batch
            if len(bars) < batch:
                break  # 没有更多数据了

        if not all_bars and cached.empty:
            print(f"无历史数据: {ts_code}")
            return

        df = self._bars_to_df(all_bars, ts_code) if all_bars else pd.DataFrame()

        # 与磁盘缓存合并去重
        if not cached.empty:
            if df.empty:
                df = cached
            else:
                df = pd.concat([cached, df]).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)

        self._cache[cache_key] = df
        self._save_disk_cache(ts_code, freq, df)
        print(f"加载历史数据: {ts_code} {freq} {len(df)} 根K线")

    def fetch_rt_min(self, ts_code: str, freq: str) -> pd.DataFrame:
        """
        获取最新 K线（轮询时调用）。
        拉取最近 200 根，与内存+磁盘缓存合并去重。
        """
        cache_key = self._cache_key(ts_code, freq)

        bars = self._fetch_bars(ts_code, freq, 0, 200)

        if not bars:
            # 拉取失败，返回缓存
            cached = self._cache.get(cache_key, pd.DataFrame())
            return cached if not cached.empty else self._load_disk_cache(ts_code, freq)

        df = self._bars_to_df(bars, ts_code)

        # 与内存缓存合并
        mem_cache = self._cache.get(cache_key, pd.DataFrame())
        if mem_cache.empty:
            mem_cache = self._load_disk_cache(ts_code, freq)
            if not mem_cache.empty:
                self._cache[cache_key] = mem_cache

        if not mem_cache.empty:
            combined = pd.concat([mem_cache, df]).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
        else:
            combined = df

        self._cache[cache_key] = combined
        self._save_disk_cache(ts_code, freq, combined)
        return combined
