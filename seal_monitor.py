#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seal_monitor.py —— 涨停/跌停封单骤减/开板预警监控（集成版）
数据源：pytdx 直连通达信行情主站
通知：企业微信机器人 webhook（markdown 消息）

与 QuantTrading-gu 共用 .env 配置中的：
  - TICKERS_1_FILE（股票池文件）
  - WEWORK_1_WEBHOOK_URL（企业微信 webhook）
  - SEAL_MONITOR_ENABLED（是否启用，默认 true）
  - SEAL_POLL_INTERVAL（轮询间隔秒，默认 2）
"""

import os
import sys
import time
import logging
import signal
from datetime import datetime, time as dtime
from threading import Thread, Event

import requests
from pytdx.hq import TdxHq_API

from shared_data_fetcher import SharedDataFetcher

# ---------- 代码 → 名字映射 ----------
_CODE_NAME_MAP: dict[str, str] = {}

def _normalize_code(code: str) -> str:
    """统一代码格式：去掉 .SZ/.SH 后缀（pytdx 返回不带后缀）"""
    return code.split(".")[0].upper()


def _load_code_name_map():
    """从 tickers_1.txt 读取代码→名字映射"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for fname in ("tickers_1.txt", "tickers_2.txt"):
        fpath = os.path.join(base_dir, fname)
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                # 去掉行首编号前缀
                if len(raw) > 1 and raw[0].isdigit() and raw[1] == "|":
                    raw = raw[2:].strip()
                parts = raw.split("#", 1)
                code_part = parts[0].strip()
                name_part = parts[1].strip() if len(parts) > 1 else ""
                code = code_part.split()[0].strip().upper() if code_part else ""
                # 统一格式：去掉后缀
                code = _normalize_code(code)
                if code and name_part:
                    _CODE_NAME_MAP[code] = name_part


def _get_name(code: str) -> str:
    """根据代码获取股票名字"""
    return _CODE_NAME_MAP.get(_normalize_code(code), code)


# ---------- 日志 ----------
log = logging.getLogger("seal_monitor")
# 独立文件 + stderr（模块加载时配置，无论从 main.py 还是独立启动都生效）
from pathlib import Path
_log_dir = Path(__file__).resolve().parent / "logs"
_log_dir.mkdir(exist_ok=True)
_log_path = _log_dir / "seal_monitor.log"
log.setLevel(logging.INFO)
_fh = logging.FileHandler(_log_path, encoding="utf-8")
_fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setLevel(logging.WARNING)  # stderr 只记录警告和错误
_sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_sh)

# 模块加载时立即读取股票池名字
_load_code_name_map()

# ---------- 全局状态 ----------
STOP_EVENT = Event()

def handle_sig(signum, frame):
    log.info("收到退出信号，准备停止...")
    STOP_EVENT.set()

# Signal ownership belongs to the process entrypoint, never module import.


def code_to_market(code: str) -> int:
    """沪市 6/9/5 开头 -> 1；深市 0/3 开头 -> 0"""
    if code.startswith(("6", "9", "5")):
        return 1
    if code.startswith(("0", "3")):
        return 0
    return 2


def calc_limit(pre_close: float, name: str, code: str):
    """根据昨收与板块计算涨停/跌停价"""
    st = bool(name) and "ST" in name.upper()
    if st:
        up = round(pre_close * 1.05, 2)
        down = round(pre_close * 0.95, 2)
    elif code.startswith(("30", "68")):
        up = round(pre_close * 1.2, 2)
        down = round(pre_close * 0.8, 2)
    else:
        up = round(pre_close * 1.1, 2)
        down = round(pre_close * 0.9, 2)
    return up, down


# ---------- 涨跌停价格缓存 ----------
_LIMIT_CACHE = {"date": "", "data": {}, "source": ""}
_LIMIT_ERROR_LOGGED = False


def _local_limit_prices(codes, pre_close_map):
    result = {}
    for code in codes:
        pre_close = pre_close_map.get(code, 0)
        if not pre_close:
            continue
        name = code
        up, down = calc_limit(pre_close, name, code)
        result[code] = (up, down)
    return result


def fetch_limit_prices(codes, pre_close_map=None):
    """获取精确涨跌停价（腾讯 qt.gtimg.cn + 本地计算冗余）"""
    global _LIMIT_CACHE, _LIMIT_ERROR_LOGGED
    today = datetime.now().strftime("%Y%m%d")

    if _LIMIT_CACHE["date"] != today:
        _LIMIT_ERROR_LOGGED = False

    if _LIMIT_CACHE["date"] == today and _LIMIT_CACHE["data"]:
        return _LIMIT_CACHE["data"]

    fresh = {}
    sh_codes = [c for c in codes if c.startswith(("6", "9", "5"))]
    sz_codes = [c for c in codes if c.startswith(("0", "3"))]

    for group, prefix in [(sh_codes, "sh"), (sz_codes, "sz")]:
        if not group:
            continue
        symbols = ",".join(f"{prefix}{c}" for c in group)
        url = f"http://qt.gtimg.cn/q={symbols}"
        try:
            r = requests.get(url, timeout=5)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) < 35:
                    continue
                code = parts[2]
                try:
                    high_limit = float(parts[5])
                    low_limit = float(parts[6])
                    if high_limit > 0 and low_limit > 0:
                        fresh[code] = (high_limit, low_limit)
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            log.warning("获取涨跌停价失败: %s", e)

    if fresh:
        _LIMIT_CACHE = {"date": today, "data": fresh, "source": "tencent"}
        log.info("涨跌停价更新(%s): %s", today,
                 {k: (round(v[0], 2), round(v[1], 2)) for k, v in fresh.items()})
    else:
        if not _LIMIT_ERROR_LOGGED:
            log.error("涨跌停价腾讯接口获取完全失败，回退本地计算! codes=%s", codes)
            _LIMIT_ERROR_LOGGED = True
        if pre_close_map:
            fresh = _local_limit_prices(codes, pre_close_map)
            _LIMIT_CACHE = {"date": today, "data": fresh, "source": "local"}
            log.info("涨跌停价本地计算(%s): %s", today,
                     {k: (round(v[0], 2), round(v[1], 2)) for k, v in fresh.items()})
        else:
            _LIMIT_CACHE = {"date": today, "data": {}, "source": "local"}

    return _LIMIT_CACHE["data"]


# ---------- 行情连接 ----------
class Quoter:
    def __init__(self):
        self.api = TdxHq_API()
        self.connected = False
        self.connect()

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

    def connect(self):
        """连接行情主站（固定列表，绕过 best_ip 的 bug）"""
        import os, sys
        devnull = open(os.devnull, 'w')
        saved_stderr = sys.stderr
        sys.stderr = devnull
        try:
            for h, p in self.HOSTS:
                try:
                    if self.api.connect(h, p):
                        log.info("连接行情主站 %s:%s", h, p)
                        self.connected = True
                        return True
                except Exception:
                    continue
            log.error("所有行情主站连接失败")
            self.connected = False
            return False
        finally:
            sys.stderr = saved_stderr
            devnull.close()

    def reconnect(self):
        try:
            self.api.disconnect()
        except Exception:
            pass
        for _ in range(3):
            try:
                if self.connect():
                    return True
            except Exception as e:
                log.warning("重连失败: %s", e)
            time.sleep(3)
        return False

    def quotes(self, codes):
        pairs = [(code_to_market(c), c) for c in codes]
        try:
            result = self.api.get_security_quotes(pairs)
            return result if result is not None else []
        except Exception as e:
            log.warning("get_security_quotes 异常: %s", e)
            return []

    def close(self):
        try:
            self.api.disconnect()
        except Exception:
            pass


# ---------- 交易时段 ----------
def in_trading():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


# ---------- 企微推送 ----------
def push_wecom(webhook, title, content):
    if not webhook:
        return
    payload = {"msgtype": "markdown", "markdown": {"content": f"## {title}\n{content}"}}
    try:
        r = requests.post(webhook, json=payload, timeout=5)
        if r.status_code == 200 and r.json().get("errcode") == 0:
            log.info("企微推送成功: %s", title)
        else:
            log.warning("企微推送异常: %s", r.text)
    except Exception as e:
        log.warning("企微推送失败: %s", e)


# ---------- 核心处理 ----------
def process(q, cfg, poll, states, webhook):
    code = q["code"]
    name = _get_name(code)  # 从股票池映射获取名字
    price = q.get("price", 0)
    pre_close = q.get("last_close", 0)
    if not pre_close:
        return

    limit_prices = _LIMIT_CACHE.get("data", {})
    calc_up, calc_down = calc_limit(pre_close, name, code)
    if code in limit_prices:
        up, down = limit_prices[code]
        if not (0.3 < up / calc_up < 1.7 and 0.3 < down / calc_down < 1.7):
            up, down = calc_up, calc_down
    else:
        up, down = calc_up, calc_down

    ask1 = q.get("ask1", 0)
    bid1 = q.get("bid1", 0)
    ask_vol1 = q.get("ask_vol1", 0)
    bid_vol1 = q.get("bid_vol1", 0)

    state = "none"
    seal_vol = 0
    if abs(price - up) < 0.001:
        state = "up"
        # 涨停封单 = 买一量（压在涨停价的买单）
        seal_vol = bid_vol1 if abs(bid1 - up) < 0.001 else 0
    elif abs(price - down) < 0.001:
        state = "down"
        # 跌停封单 = 卖一量（压在跌停价的卖单）
        seal_vol = ask_vol1 if abs(ask1 - down) < 0.001 else 0

    amount = q.get("amount", 0)
    log.info(
        "%s(%s) 价:%.2f 昨收:%.2f 涨跌:%.2f%% 涨停:%.2f 跌停:%.2f "
        "封单:%d 买一量:%d 卖一量:%d 成交额:%d万",
        name, code, price, pre_close,
        (price - pre_close) / pre_close * 100 if pre_close else 0,
        up, down, int(seal_vol), int(bid_vol1), int(ask_vol1), int(amount / 10000),
    )

    st = states.setdefault(code, {
        "state": "none", "prev_seal": 0,
        "alerted": False, "name": name,
        "last_amount": 0, "near_alerted": False,
        "open_alerted": False,
        "_last_push": {},  # msg_type -> datetime
    })

    # ---------- 消息过滤：同一股票同一类型 60秒内只发一次 ----------
    COOLDOWN_SECONDS = 60

    def _can_push(msg_type: str) -> bool:
        now = datetime.now()
        last = st["_last_push"].get(msg_type)
        if last is None or (now - last).total_seconds() >= COOLDOWN_SECONDS:
            st["_last_push"][msg_type] = now
            return True
        return False

    if state == "none":
        if st["state"] != "none":
            log.info("%s(%s) 已离开涨停/跌停", name, code)
        st["state"] = "none"
        st["peak"] = 0
        st["alerted"] = False
        st["open_alerted"] = False

        # 逼近封板监测
        near_pct = cfg.get("near_limit_pct", 1.5)
        buy_amount_sec = cfg.get("buy_amount_per_sec", 800) * 10000
        dist_up = (up - price) / up * 100 if up else 999
        buy_rate = 0
        if st["last_amount"] > 0 and amount > 0:
            buy_rate = (amount - st["last_amount"]) / max(poll, 0.5)
        st["last_amount"] = amount
        if dist_up <= near_pct and buy_rate >= buy_amount_sec and not st["near_alerted"]:
            title = f"🚀 {name}({code}) 大量买单快速逼近涨停板"
            content = (
                f"> **{name}** `{code}`\n"
                f"> 现价：{price}（距涨停 {dist_up:.2f}%）\n"
                f"> 每秒成交额增量：**{buy_rate / 10000:.0f}** 万元\n"
                f"> 时间：{datetime.now().strftime('%H:%M:%S')}\n"
                f"> 买单强劲，关注封板！"
            )
            log.warning("逼近涨停预警 %s: 距涨停%.2f%% 每秒买额%d万", code, dist_up, int(buy_rate / 10000))
            if _can_push("near"):
                push_wecom(webhook, title, content)
            st["near_alerted"] = True
        if dist_up > near_pct * 2:
            st["near_alerted"] = False
        return

    # 进入封板
    if st["state"] != state:
        st["state"] = state
        st["prev_seal"] = seal_vol
        st["alerted"] = False
        st["near_alerted"] = False
        st["open_alerted"] = False
        direction = "涨停" if state == "up" else "跌停"
        log.info("%s(%s) %s封板，初始封单 %s 手",
                 name, code, direction, seal_vol)
        # 封板瞬间推送通知
        title = f"🔒 {name}({code}) {direction}封板！"
        content = (
            f"> **{name}** `{code}`\n"
            f"> 状态：{direction}封板\n"
            f"> 封单：**{int(seal_vol)}** 手\n"
            f"> 时间：{datetime.now().strftime('%H:%M:%S')}\n"
            f"> 封板成功，关注封单变化！"
        )
        if _can_push("sealed"):
            push_wecom(webhook, title, content)
        return

    # 封单减少检测（减少 >= threshold_pct% 触发，不再要求金额阈值）
    drop_pct_per_sec = cfg.get("seal_drop_pct_per_sec", 10)
    prev_seal = st["prev_seal"]
    prev_seal_amount = st.get("prev_seal_amount", 0)
    # 当前封单金额（万元）= 封单手数 × 100 × 涨停价/跌停价
    seal_amount = seal_vol * 100 * (up if state == "up" else down) / 10000 if seal_vol > 0 else 0
    if prev_seal > 0 and seal_vol > 0:
        drop = prev_seal - seal_vol
        drop_pct = drop / prev_seal * 100
        threshold_pct = drop_pct_per_sec * poll
        # 计算每秒封单金额减少（万元），仅用于消息展示
        drop_amount_per_sec = (prev_seal_amount - seal_amount) / poll if poll > 0 else 0
        if drop_pct >= threshold_pct and not st["alerted"]:
            direction = "涨停" if state == "up" else "跌停"
            title = f"⚠️ {name}({code}) {direction}封单骤减，疑似开板"
            content = (
                f"> **{name}** `{code}`\n"
                f"> 状态：{direction}中\n"
                f"> 上次封单：**{int(prev_seal)}** 手（{prev_seal_amount:.0f}万元）\n"
                f"> 当前封单：**{int(seal_vol)}** 手（{seal_amount:.0f}万元）\n"
                f"> {poll}秒内减少：**{drop_pct:.0f}%**（{prev_seal_amount - seal_amount:.0f}万元）\n"
                f"> 每秒流失：**{drop_amount_per_sec:.0f}** 万元\n"
                f"> 时间：{datetime.now().strftime('%H:%M:%S')}\n"
                f"> 封单快速流失，注意可能开板！"
            )
            log.warning("封单骤减 %s: %d→%d手 (-%.0f%%) 金额:%.0f→%.0f万", code, prev_seal, seal_vol, drop_pct, prev_seal_amount, seal_amount)
            if _can_push("drop"):
                push_wecom(webhook, title, content)
            st["alerted"] = True
    st["prev_seal"] = seal_vol
    st["prev_seal_amount"] = seal_amount

    # 开板直接预警（最高优先级，独立于 alerted 状态）
    if seal_vol == 0 and prev_seal > 0 and not st.get("open_alerted", False):
        direction = "涨停" if state == "up" else "跌停"
        title = f"🚨 {name}({code}) 已开板！"
        content = (
            f"> **{name}** `{code}`\n"
            f"> {direction}封单已耗尽，疑似开板\n"
            f"> 时间：{datetime.now().strftime('%H:%M:%S')}"
        )
        log.warning("开板预警 %s", code)
        if _can_push("open"):
            push_wecom(webhook, title, content)
        st["open_alerted"] = True
        # 开板后重置封单骤减状态，以便再次封板后能重新检测
        st["alerted"] = False


# ---------- 监控主类 ----------
class SealMonitor:
    """封单监控主类，设计为在独立线程中运行
    支持动态重载股票池：每 60 秒检查文件 mtime，变更后自动重新加载
    """

    def __init__(self, symbols, webhook, data_fetcher: SharedDataFetcher,
                 poll_interval=2, seal_drop_pct_per_sec=10, seal_min_drop_amount=200,
                 near_limit_pct=1.5, buy_amount_per_sec=800,
                 tickers_file=None):
        self.symbols = symbols
        self.webhook = webhook
        self.data_fetcher = data_fetcher
        self.poll_interval = poll_interval
        self.tickers_file = tickers_file  # 股票池文件路径（用于动态重载）
        self._ticker_mtime = 0  # 文件最后修改时间
        self.cfg = {
            "seal_drop_pct_per_sec": seal_drop_pct_per_sec,
            "seal_min_drop_amount": seal_min_drop_amount,
            "near_limit_pct": near_limit_pct,
            "buy_amount_per_sec": buy_amount_per_sec,
        }
        self.states = {}
        self._thread = None
        self._stop_event = Event()
        self._last_reload_check = 0

    def _maybe_reload_symbols(self):
        """每 60 秒检查股票池文件是否变更，变更则动态重载"""
        if not self.tickers_file:
            return
        now = time.time()
        if now - self._last_reload_check < 60:
            return
        self._last_reload_check = now
        try:
            mtime = os.path.getmtime(self.tickers_file)
            if mtime == self._ticker_mtime:
                return
            self._ticker_mtime = mtime
            # 重新读取股票池
            new_symbols = []
            with open(self.tickers_file, "r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    if len(raw) > 1 and raw[0].isdigit() and raw[1] == "|":
                        raw = raw[2:].strip()
                    code = raw.split("#")[0].strip().split()[0].strip().upper()
                    code = _normalize_code(code)
                    if code and code not in new_symbols:
                        new_symbols.append(code)
            if new_symbols and new_symbols != self.symbols:
                self.symbols = new_symbols
                # 同步更新名字映射
                _load_code_name_map()
                log.info("封单监控股票池已更新：%d 只", len(self.symbols))
        except Exception as e:
            log.warning("重载股票池失败: %s", e)

    def _run(self):
        log.info("封单监控线程启动，标的 %d 只：%s", len(self.symbols), self.symbols)
        while not self._stop_event.is_set():
            # 动态检查股票池变更
            self._maybe_reload_symbols()
            if not in_trading():
                self._stop_event.wait(30)
                continue
            try:
                quotes = self.data_fetcher.get_all_realtime_quotes()
                for code, q in quotes.items():
                    # 只处理股票池内的标的
                    if _normalize_code(code) not in self.symbols:
                        continue
                    try:
                        process(q, self.cfg, self.poll_interval, self.states, self.webhook)
                    except Exception as e:
                        log.warning("处理 %s 出错: %s", code, e)
            except Exception as e:
                log.warning("行情获取失败: %s", e)
            self._stop_event.wait(self.poll_interval)
        log.info("封单监控线程已停止")

    def start(self):
        self._thread = Thread(target=self._run, daemon=True, name="seal-monitor")
        self._thread.start()

    def stop(self, timeout=10):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.error('封单监控线程退出超时')
                return False
        return True


# ---------- 独立运行入口 ----------
def main():
    """独立运行封单监控（保留兼容性）"""
    from dotenv import load_dotenv
    load_dotenv(".env")

    # 读取股票池
    ticker_file = os.getenv("TICKERS_1_FILE", "tickers_1.txt")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ticker_path = os.path.join(base_dir, ticker_file)

    symbols = []
    if os.path.exists(ticker_path):
        with open(ticker_path, "r", encoding="utf-8") as f:
            symbols = [line.strip().split("#")[0].strip() for line in f
                       if line.strip() and not line.strip().startswith("#")]
        log.info("从 %s 读取股票池，%d 只", ticker_file, len(symbols))

    if not symbols:
        log.error("未设置股票池，退出")
        sys.exit(1)

    webhook = os.getenv("WEWORK_1_WEBHOOK_URL", "")
    poll = int(os.getenv("SEAL_POLL_INTERVAL", "2"))

    # 日志已在模块顶部配置（独立文件 + stderr）

    log.info("启动封单监控，标的 %d 只：%s", len(symbols), symbols)
    quoter = Quoter()
    states = {}
    cfg = {}
    last_beat = 0
    BEAT_INTERVAL = 300

    while not STOP_EVENT.is_set():
        if not in_trading():
            now_ts = time.time()
            if now_ts - last_beat >= BEAT_INTERVAL:
                try:
                    qs = quoter.quotes(symbols[:1])
                    log.info("心跳检测 OK，行情返回 %d 条", len(qs) if qs else 0)
                except Exception as e:
                    log.warning("心跳检测失败，尝试重连: %s", e)
                    quoter.reconnect()
                last_beat = now_ts
            time.sleep(30)
            continue
        try:
            qs = quoter.quotes(symbols)
            pre_close_map = {}
            if qs:
                for q in qs:
                    c = q.get("code", "")
                    pc = q.get("last_close", 0)
                    if c and pc:
                        pre_close_map[c] = pc
            fetch_limit_prices(symbols, pre_close_map)
            for q in qs:
                try:
                    process(q, cfg, poll, states, webhook)
                except Exception as e:
                    log.warning("处理 %s 出错: %s", q.get("code"), e)
        except Exception as e:
            log.warning("行情获取失败，尝试重连: %s", e)
            if not quoter.reconnect():
                log.error("重连失败，休眠后重试")
                time.sleep(10)
        time.sleep(poll)
    quoter.close()
    log.info("监控已停止")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    main()
