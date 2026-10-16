from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv

# Monkey-patch pytdx best_ip（绕过 ping 噪音和 bug）
import pytdx.util.best_ip as _bip
if hasattr(_bip, 'select_best_ip'):
    _bip.select_best_ip = lambda _type='stock': {'ip': '115.238.90.165', 'port': 7709}

from email_push import EmailPusher
from market_hours import AshareSessionGuard
from seal_monitor import SealMonitor
from shared_data_fetcher import SharedDataFetcher
from strategy import KdjMacdStrategy, SignalResult
from tg_push import TelegramPusher
from wecom_push import WeComPusher


@dataclass(frozen=True)
class AlertItem:
    signal: SignalResult
    side: str
    label: str
    pool_id: str


@dataclass(frozen=True)
class TickerEntry:
    ts_code: str
    display: str


@dataclass(frozen=True)
class PoolConfig:
    pool_id: str
    display_name: str
    tickers_file: str
    wecom_webhook_url: str


@dataclass(frozen=True)
class AppConfig:
    pools: List[PoolConfig]
    poll_interval_per_minutes: int
    min_freq: str
    min_bars_required: int
    signal_cooldown_minutes: int
    heartbeat_interval_minutes: int
    enable_trading_hours: bool
    enable_email: bool
    enable_telegram: bool
    enable_wecom: bool
    subject_prefix: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    smtp_use_ssl: bool
    mail_from: str
    mail_to: List[str]
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_send_interval_seconds: int
    wecom_send_interval_seconds: int
    # 数据获取频率（秒）
    fetch_interval_seconds: float = 1.0
    # 封单监控配置
    seal_monitor_enabled: bool = False
    seal_poll_interval: int = 2
    seal_drop_pct_per_sec: int = 10
    seal_min_drop_amount: int = 200
    seal_near_limit_pct: float = 1.5
    seal_buy_amount_per_sec: int = 800


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_tickers(file_path: str) -> List[TickerEntry]:
    tickers: List[TickerEntry] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            # 去掉行首的编号前缀（如 "1|"、"2|"）
            if len(raw) > 1 and raw[0].isdigit() and raw[1] == "|":
                raw = raw[2:].strip()
            if not raw:
                continue
            code_part = raw.split("#", 1)[0].strip()
            if not code_part:
                continue
            ts_code = code_part.split()[0].strip().upper()
            if not ts_code:
                continue
            tickers.append(TickerEntry(ts_code=ts_code, display=raw))

    seen: Set[str] = set()
    out: List[TickerEntry] = []
    for item in tickers:
        if item.ts_code not in seen:
            out.append(item)
            seen.add(item.ts_code)
    return out


def _load_pool_configs(enable_wecom: bool) -> List[PoolConfig]:
    pools_by_index: Dict[int, PoolConfig] = {}
    env_items = dict(os.environ)
    pool_indexes: Set[int] = set()

    for key, raw_value in env_items.items():
        value = raw_value.strip()
        if key.startswith("TICKERS_") and key.endswith("_FILE"):
            middle = key[len("TICKERS_"):-len("_FILE")]
            if middle.isdigit() and value:
                pool_indexes.add(int(middle))
        elif key.startswith("WEWORK_") and key.endswith("_WEBHOOK_URL"):
            middle = key[len("WEWORK_"):-len("_WEBHOOK_URL")]
            if middle.isdigit() and value:
                pool_indexes.add(int(middle))

    for index in sorted(pool_indexes):
        tickers_file = os.getenv(f"TICKERS_{index}_FILE", "").strip()
        wecom_webhook_url = os.getenv(f"WEWORK_{index}_WEBHOOK_URL", "").strip()
        display_name = os.getenv(f"POOL_{index}_NAME", "").strip() or f"pool_{index}"

        if not tickers_file:
            raise ValueError(f"TICKERS_{index}_FILE is required when configuring pool {index}")

        if enable_wecom and not wecom_webhook_url:
            raise ValueError(
                f"WEWORK_{index}_WEBHOOK_URL is required when ENABLE_WECOM=true and pool {index} is configured"
            )

        pools_by_index[index] = PoolConfig(
            pool_id=f"pool_{index}",
            display_name=display_name,
            tickers_file=tickers_file,
            wecom_webhook_url=wecom_webhook_url,
        )

    if pools_by_index:
        return [pools_by_index[index] for index in sorted(pools_by_index)]

    fallback_tickers_1 = os.getenv("TICKERS_1_FILE", "tickers_1.txt").strip()
    fallback_tickers_2 = os.getenv("TICKERS_2_FILE", "tickers_2.txt").strip()
    fallback_wework_1 = os.getenv("WEWORK_1_WEBHOOK_URL", "").strip()
    fallback_wework_2 = os.getenv("WEWORK_2_WEBHOOK_URL", "").strip()
    fallback_name_1 = os.getenv("POOL_1_NAME", "").strip() or "pool_1"
    fallback_name_2 = os.getenv("POOL_2_NAME", "").strip() or "pool_2"

    pools: List[PoolConfig] = [
        PoolConfig(
            pool_id="pool_1",
            display_name=fallback_name_1,
            tickers_file=fallback_tickers_1,
            wecom_webhook_url=fallback_wework_1,
        )
    ]

    if fallback_tickers_2:
        pools.append(
            PoolConfig(
                pool_id="pool_2",
                display_name=fallback_name_2,
                tickers_file=fallback_tickers_2,
                wecom_webhook_url=fallback_wework_2,
            )
        )

    if enable_wecom and not fallback_wework_1:
        raise ValueError("WEWORK_1_WEBHOOK_URL is required when ENABLE_WECOM=true")
    if enable_wecom and fallback_tickers_2 and not fallback_wework_2:
        raise ValueError("WEWORK_2_WEBHOOK_URL is required when ENABLE_WECOM=true and TICKERS_2_FILE is configured")

    return pools


def _build_config() -> AppConfig:
    enable_email = _env_bool("ENABLE_EMAIL", True)
    enable_telegram = _env_bool("ENABLE_TELEGRAM", False)
    enable_wecom = _env_bool("ENABLE_WECOM", False)

    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_use_ssl = _env_bool("SMTP_USE_SSL", True)
    mail_to = [x.strip() for x in os.getenv("MAIL_TO", "").split(",") if x.strip()]
    mail_from = os.getenv("MAIL_FROM", smtp_user).strip() or smtp_user

    if enable_email:
        if not mail_to:
            raise ValueError("MAIL_TO is required when ENABLE_EMAIL=true")
        if not smtp_user or not smtp_pass or not smtp_host:
            raise ValueError("SMTP_HOST / SMTP_USER / SMTP_PASS are required when ENABLE_EMAIL=true")

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    telegram_send_interval_seconds = int(os.getenv("TELEGRAM_SEND_INTERVAL_SECONDS", "30"))
    if enable_telegram and (not telegram_bot_token or not telegram_chat_id):
        raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are required when ENABLE_TELEGRAM=true")

    pools = _load_pool_configs(enable_wecom=enable_wecom)
    wecom_send_interval_seconds = int(os.getenv("WEWORK_SEND_INTERVAL_SECONDS", "30"))

    # 数据获取频率（秒）
    fetch_interval_seconds = float(os.getenv("FETCH_INTERVAL_SECONDS", "1"))

    # 封单监控配置
    seal_monitor_enabled = _env_bool("SEAL_MONITOR_ENABLED", False)
    seal_poll_interval = int(os.getenv("SEAL_POLL_INTERVAL", "2"))
    seal_drop_pct_per_sec = int(os.getenv("SEAL_DROP_PCT_PER_SEC", "10"))
    seal_min_drop_amount = int(os.getenv("SEAL_MIN_DROP_AMOUNT", "200"))
    seal_near_limit_pct = float(os.getenv("SEAL_NEAR_LIMIT_PCT", "1.5"))
    seal_buy_amount_per_sec = int(os.getenv("SEAL_BUY_AMOUNT_PER_SEC", "800"))

    poll_per_minutes_raw = os.getenv("POLL_INTERVAL_PER_MINUTES", "").strip()
    if poll_per_minutes_raw:
        poll_per_minutes = int(poll_per_minutes_raw)
    else:
        old_poll_minutes = int(os.getenv("POLL_INTERVAL_MINUTES", "1"))
        if old_poll_minutes <= 0:
            raise ValueError("POLL_INTERVAL_MINUTES must be > 0")
        poll_per_minutes = max(1, int(60 / old_poll_minutes))
    if poll_per_minutes <= 0:
        raise ValueError("POLL_INTERVAL_PER_MINUTES must be > 0")

    min_freq = os.getenv("MIN_FREQ", "5MIN").strip().upper()
    if min_freq not in {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}:
        raise ValueError("MIN_FREQ must be one of: 1MIN,5MIN,15MIN,30MIN,60MIN")

    return AppConfig(
        pools=pools,
        poll_interval_per_minutes=poll_per_minutes,
        min_freq=min_freq,
        min_bars_required=int(os.getenv("MIN_BARS_REQUIRED", "150")),
        signal_cooldown_minutes=int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "60")),
        heartbeat_interval_minutes=int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "5")),
        enable_trading_hours=_env_bool("ENABLE_TRADING_HOURS", True),
        enable_email=enable_email,
        enable_telegram=enable_telegram,
        enable_wecom=enable_wecom,
        subject_prefix=os.getenv("SUBJECT_PREFIX", "[QuantAlert]").strip() or "[QuantAlert]",
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        smtp_use_ssl=smtp_use_ssl,
        mail_from=mail_from,
        mail_to=mail_to,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_send_interval_seconds=telegram_send_interval_seconds,
        wecom_send_interval_seconds=wecom_send_interval_seconds,
        seal_monitor_enabled=seal_monitor_enabled,
        seal_poll_interval=seal_poll_interval,
        seal_drop_pct_per_sec=seal_drop_pct_per_sec,
        seal_min_drop_amount=seal_min_drop_amount,
        seal_near_limit_pct=seal_near_limit_pct,
        seal_buy_amount_per_sec=seal_buy_amount_per_sec,
        fetch_interval_seconds=fetch_interval_seconds,
    )


class QuantAlertApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.strategy = KdjMacdStrategy()
        self.email_pusher: EmailPusher | None = None
        self.telegram_pusher: TelegramPusher | None = None
        self.wecom_pushers: Dict[str, WeComPusher] = {}
        self.seal_monitor: SealMonitor | None = None

        if self.config.enable_email:
            self.email_pusher = EmailPusher(
                smtp_host=config.smtp_host,
                smtp_port=config.smtp_port,
                smtp_user=config.smtp_user,
                smtp_pass=config.smtp_pass,
                mail_from=config.mail_from,
                mail_to=config.mail_to,
                use_ssl=config.smtp_use_ssl,
            )
        if self.config.enable_telegram:
            self.telegram_pusher = TelegramPusher(
                bot_token=config.telegram_bot_token,
                chat_id=config.telegram_chat_id,
                send_interval=config.telegram_send_interval_seconds,
            )
        if self.config.enable_wecom:
            for pool in self.config.pools:
                if not pool.wecom_webhook_url:
                    self._log(f"未配置 {pool.display_name} 的企业微信 webhook，{pool.tickers_file} 产生的信号将不会推送到企业微信")
                    continue
                self.wecom_pushers[pool.pool_id] = WeComPusher(
                    webhook_url=pool.wecom_webhook_url,
                    send_interval=config.wecom_send_interval_seconds,
                )

        # 加载所有tickers
        tickers = self._load_all_tickers()

        # 启动统一数据获取器（每秒获取）
        self.data_source = SharedDataFetcher(
            ts_codes=[t.ts_code for t in tickers],
            freq=config.min_freq,
            fetch_interval=config.fetch_interval_seconds,
        )
        self.data_source.connect()
        self.data_source.start()
        self._log(f"统一数据获取器已启动，每秒获取 {len(tickers)} 只标的")

        # 启动封单监控（共用股票池 + webhook）
        if self.config.seal_monitor_enabled:
            seal_symbols = []
            for pool in self.config.pools:
                pool_tickers = _load_tickers(pool.tickers_file)
                for t in pool_tickers:
                    code = t.ts_code.split(".")[0]
                    if code not in seal_symbols:
                        seal_symbols.append(code)
            seal_webhook = self.config.pools[0].wecom_webhook_url if self.config.pools else ""
            if seal_symbols and seal_webhook:
                # 获取股票池文件路径（用于动态重载）
                seal_tickers_file = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    self.config.pools[0].tickers_file
                )
                self.seal_monitor = SealMonitor(
                    symbols=seal_symbols,
                    webhook=seal_webhook,
                    data_fetcher=self.data_source,
                    poll_interval=self.config.seal_poll_interval,
                    seal_drop_pct_per_sec=self.config.seal_drop_pct_per_sec,
                    seal_min_drop_amount=self.config.seal_min_drop_amount,
                    near_limit_pct=self.config.seal_near_limit_pct,
                    buy_amount_per_sec=self.config.seal_buy_amount_per_sec,
                    tickers_file=seal_tickers_file,
                )
                self.seal_monitor.start()
                self._log(f"封单监控已启动，共用股票池 {len(seal_symbols)} 只，webhook={seal_webhook[:40]}...")
            else:
                self._log("封单监控未启动：股票池或 webhook 为空")

        self.session_guard: AshareSessionGuard | None = None
        if config.enable_trading_hours:
            self.session_guard = AshareSessionGuard()
        self._sent_cache: Set[Tuple[str, str, str, str]] = set()
        self._last_sent_at: Dict[Tuple[str, str, str], datetime] = {}
        self._last_seen_bar_time: Dict[str, str] = {}
        self._ticker_display: Dict[Tuple[str, str], str] = {}

    def _log(self, text: str) -> None:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text}", flush=True)

    def _signal_key(self, signal: SignalResult, side: str, pool_id: str) -> Tuple[str, str, str, str]:
        return signal.ts_code, signal.bar_time, side, pool_id

    def _pass_cooldown(self, signal: SignalResult, side: str, pool_id: str) -> bool:
        cooldown_minutes = self.config.signal_cooldown_minutes
        if cooldown_minutes <= 0:
            return True
        key = (signal.ts_code, side, pool_id)
        now = datetime.now()
        last = self._last_sent_at.get(key)
        if last is None:
            return True
        return (now - last) >= timedelta(minutes=cooldown_minutes)

    def _scan_once(self) -> Tuple[List[AlertItem], bool]:
        alerts: List[AlertItem] = []
        saw_new_bar = False
        loaded_any = False

        for pool in self.config.pools:
            tickers = _load_tickers(pool.tickers_file)
            if not tickers:
                self._log(f"未读取到标的，请检查 {pool.tickers_file}")
                continue
            loaded_any = True

            for ticker in tickers:
                ts_code = ticker.ts_code
                self._ticker_display[(pool.pool_id, ts_code)] = ticker.display
                try:
                    min_df = self.data_source.get_minute_bars(ts_code=ts_code)
                    if min_df is None or min_df.empty:
                        self._log(f"{ts_code}: 无分时数据")
                        continue

                    last_bar = min_df.iloc[-1]
                    bar_time = str(last_bar.get("time", ""))
                    open_px = float(last_bar.get("open", 0.0))
                    high_px = float(last_bar.get("high", 0.0))
                    low_px = float(last_bar.get("low", 0.0))
                    close_px = float(last_bar.get("close", 0.0))
                    vol = float(last_bar.get("vol", 0.0))
                    bar_key = f"{pool.pool_id}::{ts_code}"
                    is_new_bar = self._last_seen_bar_time.get(bar_key) != bar_time
                    self._last_seen_bar_time[bar_key] = bar_time
                    if is_new_bar:
                        saw_new_bar = True
                    self._log(
                        f"[{pool.display_name}] {ts_code} BAR time={bar_time} open={open_px:.3f} high={high_px:.3f} "
                        f"low={low_px:.3f} close={close_px:.3f} vol={vol:.0f}"
                    )

                    signal = self.strategy.latest_signal(
                        ts_code=ts_code, df=min_df, min_bars_required=self.config.min_bars_required
                    )
                    if signal is None:
                        self._log(f"[{pool.display_name}] {ts_code}: K线不足（当前 {len(min_df)} < {self.config.min_bars_required}）")
                        continue

                    self._log(
                        f"[{pool.display_name}] {ts_code} {signal.bar_time} close={signal.close:.3f} "
                        f"J={signal.j:.3f} CCI={signal.cci:.2f} "
                        f"DIF={signal.dif:.4f} DEA={signal.dea:.4f} MACD={signal.macd:.4f} "
                        f"B={signal.buy_b} S={signal.sell_s} JB={signal.buy_jb} JS={signal.sell_js} "
                        f"底={signal.bottom_fishing} 顶={signal.top_escape}"
                    )

                    candidates: List[Tuple[str, str, bool]] = [
                        ("BOTTOM", "底", signal.bottom_fishing),
                        ("BUY", "B", signal.buy_b and not signal.bottom_fishing and not signal.buy_jb),
                        ("BUY_JB", "JB", signal.buy_jb and not signal.bottom_fishing),
                        ("TOP", "顶", signal.top_escape),
                        ("SELL", "S", signal.sell_s and not signal.top_escape and not signal.sell_js),
                        ("SELL_JS", "JS", signal.sell_js and not signal.top_escape),
                    ]
                    for side, label, active in candidates:
                        if not active:
                            continue
                        if self._signal_key(signal, side, pool.pool_id) in self._sent_cache:
                            continue
                        if not self._pass_cooldown(signal, side, pool.pool_id):
                            continue
                        alerts.append(AlertItem(signal=signal, side=side, label=label, pool_id=pool.pool_id))
                except Exception as exc:
                    self._log(f"[{pool.display_name}] {ts_code}: 扫描失败 -> {exc}")

        if not loaded_any:
            return [], False
        return alerts, saw_new_bar

    def _build_email_content(self, alerts: List[AlertItem]) -> str:
        push_time = datetime.now().astimezone().strftime("%H:%M:%S")
        lines: List[str] = [f"推送时间：{push_time}"]

        label_map = {
            "BOTTOM": "底",
            "BUY": "B",
            "BUY_JB": "JB",
            "TOP": "顶",
            "SELL": "S",
            "SELL_JS": "JS",
        }

        for item in alerts:
            label = self._ticker_display.get((item.pool_id, item.signal.ts_code), item.signal.ts_code)
            lines.append(f"{label}  {label_map.get(item.side, item.label)}")

        return "\n".join(lines).strip() + "\n"

    def _load_all_tickers(self) -> List[TickerEntry]:
        tickers: List[TickerEntry] = []
        for pool in self.config.pools:
            loaded_tickers = _load_tickers(pool.tickers_file)
            if not loaded_tickers:
                self._log(f"未读取到标的，请检查 {pool.tickers_file}")
            tickers.extend(loaded_tickers)
        return tickers

    def _push_alerts(self, alerts: List[AlertItem], subject: str, content: str, pool_id: str) -> bool:
        sent_any = False
        if self.config.enable_email and self.email_pusher is not None:
            self.email_pusher.push(subject=subject, content=content)
            sent_any = True
            self._log(f"邮件已发送: {subject}")

        if self.config.enable_telegram and self.telegram_pusher is not None:
            self.telegram_pusher.push(subject=subject, content=content)
            sent_any = True
            self._log(f"Telegram已发送: {subject}")

        if self.config.enable_wecom:
            wecom_pusher = self.wecom_pushers.get(pool_id)
            if wecom_pusher is not None:
                wecom_pusher.push(subject=subject, content=content)
                sent_any = True
                self._log(f"企业微信已发送[{pool_id}]: {subject}")

        return sent_any

    def run_forever(self) -> None:
        self._log(
            f"后台启动，轮询频率: 每分钟 {self.config.poll_interval_per_minutes} 次，"
            f"分时周期={self.config.min_freq}，最少K线={self.config.min_bars_required}"
        )
        self._log(
            f"通知通道: email={self.config.enable_email}, telegram={self.config.enable_telegram}, wecom={self.config.enable_wecom}"
        )
        if self.config.enable_trading_hours:
            self._log(
                "交易时段控制: 已开启（连续竞价 09:30-11:30、13:00-15:00；"
                "集合竞价 09:15-09:30 及收盘后不抓取）"
            )
        else:
            self._log("交易时段控制: 已关闭（全天轮询）")

        tickers = self._load_all_tickers()
        pool_summary = "，".join(f"{pool.display_name}={pool.tickers_file}" for pool in self.config.pools)
        self._log(
            f"监控 {len(tickers)} 个标的，分时周期={self.config.min_freq}，票池配置: {pool_summary}"
        )

        last_heartbeat_at: datetime = datetime.now()
        fetch_session_active = False
        poll_sleep_seconds = 60.0 / self.config.poll_interval_per_minutes

        while True:
            now = datetime.now()
            in_fetch_window = True
            if self.session_guard is not None:
                self.session_guard.refresh_if_needed(now)
                in_fetch_window = self.session_guard.should_fetch(now)
                if in_fetch_window != fetch_session_active:
                    fetch_session_active = in_fetch_window
                    if in_fetch_window:
                        self._log("进入交易时段，开始抓取分时数据")
                    else:
                        self._log(f"暂停抓取：{self.session_guard.status_message(now)}")

                if not in_fetch_window:
                    if self.config.heartbeat_interval_minutes > 0 and (
                        now - last_heartbeat_at
                    ) >= timedelta(minutes=self.config.heartbeat_interval_minutes):
                        self._log(f"--- 心跳检测正常（休市：{self.session_guard.status_message(now)}）---")
                        last_heartbeat_at = now
                    time.sleep(self.session_guard.seconds_until_fetch_window(now))
                    continue

            alerts, saw_new_bar = self._scan_once()

            if alerts:
                sent_any = False
                for pool in self.config.pools:
                    pool_alerts = [item for item in alerts if item.pool_id == pool.pool_id]
                    if not pool_alerts:
                        continue
                    counts = {
                        "底": sum(1 for x in pool_alerts if x.side == "BOTTOM"),
                        "B": sum(1 for x in pool_alerts if x.side == "BUY"),
                        "JB": sum(1 for x in pool_alerts if x.side == "BUY_JB"),
                        "顶": sum(1 for x in pool_alerts if x.side == "TOP"),
                        "S": sum(1 for x in pool_alerts if x.side == "SELL"),
                        "JS": sum(1 for x in pool_alerts if x.side == "SELL_JS"),
                    }
                    count_text = " ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
                    subject = f"{self.config.subject_prefix} [{pool.display_name}] {count_text}".strip()
                    content = self._build_email_content(pool_alerts)
                    sent_any = self._push_alerts(pool_alerts, subject, content, pool.pool_id) or sent_any

                if not sent_any:
                    self._log("未启用任何通知通道，本轮仅打印信号")

                for item in alerts:
                    self._sent_cache.add(self._signal_key(item.signal, item.side, item.pool_id))
                    self._last_sent_at[(item.signal.ts_code, item.side, item.pool_id)] = datetime.now()
            elif saw_new_bar:
                self._log("本轮无新信号，不发送通知")

            if self.config.heartbeat_interval_minutes > 0:
                now = datetime.now()
                if (now - last_heartbeat_at) >= timedelta(minutes=self.config.heartbeat_interval_minutes):
                    self._log("--- 心跳检测正常 ---")
                    last_heartbeat_at = now

            time.sleep(poll_sleep_seconds)


def main() -> int:
    load_dotenv(".env")
    config = _build_config()
    app = QuantAlertApp(config)
    try:
        app.run_forever()
    except KeyboardInterrupt:
        print("已手动停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
