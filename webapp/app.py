#!/usr/bin/env python3
"""股票监控 Web 管理面板 - 后端 API"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from threading import Lock
from datetime import datetime, date
from flask import Flask, render_template, jsonify, request

# Support both `python -m webapp.app` and the existing script entrypoint.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from index_market import INDICES, IndexMarketService

app = Flask(__name__)

BASE_PATH = os.environ.get("QUANT_BASE_PATH", str(PROJECT_ROOT))
LOGS_PATH = os.path.join(BASE_PATH, "logs")
WEBAPP_PATH = os.path.join(BASE_PATH, "webapp")
TICKERS_1 = os.path.join(BASE_PATH, "tickers_1.txt")
TICKERS_2 = os.path.join(BASE_PATH, "tickers_2.txt")
_index_service_lock = Lock()
_breadth_service_lock = Lock()


def get_index_service():
    # Construct lazily: importing/starting Flask performs no market requests,
    # cache writes, daemon launches or scans. Injectable for offline WSGI tests.
    service = app.extensions.get('index_market')
    if service is None:
        with _index_service_lock:
            service = app.extensions.get('index_market')
            if service is None:
                service = IndexMarketService(Path(BASE_PATH) / 'runtime' / 'index_daily',
                                             Path(BASE_PATH) / 'runtime' / 'calendar')
                app.extensions['index_market'] = service
    return service


def get_breadth_service():
    # No requests, scans or filesystem writes at Web startup.
    with _breadth_service_lock:
        service = app.extensions.get('market_breadth')
        if service is None:
            from breadth_data import BreadthService
            service = BreadthService(Path(BASE_PATH) / 'runtime' / 'market_breadth',
                                     Path(BASE_PATH) / 'runtime' / 'calendar')
            app.extensions['market_breadth'] = service
        return service

# ===================== 工具函数 =====================

def read_tickers(filepath):
    """读取股票池文件，返回 [(code, name), ...]"""
    tickers = []
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"^(\d{6}\.(?:SH|SZ))\s*#?(.*)", line)
                if match:
                    tickers.append((match.group(1), match.group(2).strip() or match.group(1)))
    return tickers


def write_tickers(filepath, tickers):
    """写回股票池文件"""
    with open(filepath, "w", encoding="utf-8") as f:
        for code, name in tickers:
            f.write(f"{code} # {name}\n")


def backup_file(filepath):
    """备份文件"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{filepath}.bak_{ts}"
    shutil.copy2(filepath, bak)
    return bak


def check_daemon():
    """检查 quant 监控进程是否存活"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "python main.py"],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def get_today_log():
    """获取今日日志文件路径"""
    today = date.today().strftime("%Y-%m-%d")
    log_file = os.path.join(LOGS_PATH, f"{today}_quant_alert.log")
    if os.path.exists(log_file):
        return log_file
    return None


def parse_signals(log_file, limit=200):
    """解析日志中的交易信号"""
    signals = []
    if not log_file or not os.path.exists(log_file):
        return signals

    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue

        # 匹配信号行: [时间] [池名] CODE 时间 close=xx J=xx CCI=xx ... B=xx S=xx ...
        match = re.search(
            r"\[(.*?)\]\s+\[(.*?)\]\s+(\d{6}\.\w{2})\s+.*?"
            r"close=([\d.]+).*?"
            r"J=([\d.-]+).*?"
            r"B=(\w+).*?"
            r"S=(\w+).*?"
            r"JB=(\w+).*?"
            r"JS=(\w+).*?"
            r"底=(\w+).*?"
            r"顶=(\w+)",
            line
        )
        if match:
            ts, pool, code, close, j, b, s, jb, js, bottom, top = match.groups()
            buy_signal = b == "True" or jb == "True" or bottom == "True"
            sell_signal = s == "True" or js == "True" or top == "True"

            if buy_signal or sell_signal:
                sig_type = "买入" if buy_signal else "卖出"
                detail_parts = []
                if b == "True": detail_parts.append("B点")
                if s == "True": detail_parts.append("S点")
                if jb == "True": detail_parts.append("J买")
                if js == "True": detail_parts.append("J卖")
                if bottom == "True": detail_parts.append("底背离")
                if top == "True": detail_parts.append("顶背离")

                signals.append({
                    "time": ts,
                    "pool": pool,
                    "code": code,
                    "price": float(close),
                    "type": sig_type,
                    "detail": ", ".join(detail_parts),
                    "j": round(float(j), 1),
                })

    return list(reversed(signals))


# ===================== 页面路由 =====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    return render_template('dashboard.html', index_specs=[
        {'code': spec.code, 'name': spec.name} for spec in INDICES])


# ===================== API 路由 =====================

@app.route("/api/market/indices")
def api_market_indices():
    return jsonify({'indices': [{'code': spec.code, 'name': spec.name} for spec in INDICES]})


@app.route('/api/market/breadth')
def api_market_breadth():
    return jsonify(get_breadth_service().get_snapshot())


@app.route("/api/market/index/<code>")
def api_market_index(code):
    if code not in {spec.code for spec in INDICES}:
        return jsonify({'error': '不支持的指数代码'}), 404
    return jsonify(get_index_service().get_index(code))

@app.route("/api/status")
def api_status():
    daemon_alive = check_daemon()
    return jsonify({
        "daemon": daemon_alive,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": get_market_status(),
    })


def get_market_status():
    """获取当前市场状态（基于日志中的心跳信息）"""
    log_file = get_today_log()
    if not log_file:
        return "未知"
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            if "休市" in line:
                return "休市"
            if "收盘" in line:
                return "已收盘"
            if "暂停抓取" in line:
                if "午间" in line:
                    return "午间休市"
            if "心跳检测正常" in line:
                continue
        return "正常交易"
    except Exception:
        return "未知"


@app.route("/api/pools")
def api_pools():
    pool1 = read_tickers(TICKERS_1)
    pool2 = read_tickers(TICKERS_2)
    return jsonify({
        "pool1": [{"code": c, "name": n} for c, n in pool1],
        "pool2": [{"code": c, "name": n} for c, n in pool2],
    })


@app.route("/api/pool/<int:pool_id>/add", methods=["POST"])
def api_add_stock(pool_id):
    data = request.get_json()
    code = data.get("code", "").strip().upper()
    name = data.get("name", "").strip()

    if not code or not re.match(r"^\d{6}\.(SH|SZ)$", code):
        return jsonify({"ok": False, "error": "代码格式错误，如: 601869.SH"}), 400

    filepath = TICKERS_1 if pool_id == 1 else TICKERS_2
    if not os.path.exists(filepath):
        return jsonify({"ok": False, "error": f"股票池{pool_id}文件不存在"}), 404

    tickers = read_tickers(filepath)
    existing = [c for c, _ in tickers]
    if code in existing:
        return jsonify({"ok": False, "error": f"{code} 已在股票池{pool_id}中"}), 409

    backup_file(filepath)
    tickers.append((code, name or code))
    write_tickers(filepath, tickers)

    return jsonify({"ok": True, "pool_id": pool_id, "code": code, "name": name or code})


@app.route("/api/pool/<int:pool_id>/remove", methods=["POST"])
def api_remove_stock(pool_id):
    data = request.get_json()
    code = data.get("code", "").strip().upper()

    if not code:
        return jsonify({"ok": False, "error": "请提供股票代码"}), 400

    filepath = TICKERS_1 if pool_id == 1 else TICKERS_2
    tickers = read_tickers(filepath)
    new_tickers = [(c, n) for c, n in tickers if c != code]

    if len(new_tickers) == len(tickers):
        return jsonify({"ok": False, "error": f"{code} 不在股票池{pool_id}中"}), 404

    backup_file(filepath)
    write_tickers(filepath, new_tickers)

    return jsonify({"ok": True, "pool_id": pool_id, "code": code})


@app.route("/api/signals")
def api_signals():
    log_file = get_today_log()
    signals = parse_signals(log_file, limit=500)
    return jsonify({"signals": signals, "date": date.today().isoformat()})


@app.route("/api/log/tail")
def api_log_tail():
    """获取最新日志尾部"""
    log_file = get_today_log()
    if not log_file:
        return jsonify({"lines": [], "error": "今日日志不存在"})

    lines_tail = request.args.get("lines", 50, type=int)
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        return jsonify({
            "lines": [l.rstrip() for l in all_lines[-lines_tail:]],
            "file": os.path.basename(log_file),
        })
    except Exception as e:
        return jsonify({"lines": [], "error": str(e)})


# ===================== 股票K线 & 技术指标 =====================

def parse_bars(log_file, code, max_bars=120):
    """从日志解析分钟级别的 OHLCV 数据"""
    bars = []
    if not log_file or not os.path.exists(log_file):
        return bars

    code_upper = code.upper()
    bar_pattern = re.compile(
        r"\[(.*?)\]\s+\[.*?\]\s+" + re.escape(code_upper) +
        r"\s+BAR time=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2}"
        r"\s+open=([\d.]+)\s+high=([\d.]+)\s+low=([\d.]+)\s+close=([\d.]+)\s+vol=(\d+)"
    )

    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            m = bar_pattern.search(line)
            if m:
                log_ts, bar_time, o, h, l, c, v = m.groups()
                bars.append({
                    "time": bar_time,
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": int(v),
                })

    # 去重（同一分钟可能多次出现）
    seen = set()
    unique = []
    for b in bars:
        if b["time"] not in seen:
            seen.add(b["time"])
            unique.append(b)
    # 按时间排序，取最近 max_bars
    unique.sort(key=lambda x: x["time"])
    return unique[-max_bars:]


def calc_macd(closes, fast=12, slow=26, signal=9):
    """计算 MACD 指标"""
    if len(closes) < slow + signal:
        return {"dif": [], "dea": [], "macd": []}

    ema_fast = closes[0]
    ema_slow = closes[0]
    ema_signal = 0
    dif_list = []
    dea_list = []
    macd_list = []

    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    k_sig = 2.0 / (signal + 1)

    for i, c in enumerate(closes):
        ema_fast = c * k_fast + ema_fast * (1 - k_fast)
        ema_slow = c * k_slow + ema_slow * (1 - k_slow)
        dif = ema_fast - ema_slow
        dif_list.append(round(dif, 4))

        if i == slow - 1:
            ema_signal = dif
        elif i >= slow:
            ema_signal = dif * k_sig + ema_signal * (1 - k_sig)

        dea_list.append(round(ema_signal, 4) if i >= slow - 1 else None)
        macd_list.append(round((dif - ema_signal) * 2, 4) if i >= slow - 1 else None)

    return {
        "dif": dif_list,
        "dea": [d for d in dea_list if d is not None],
        "macd": [m for m in macd_list if m is not None],
        "start_idx": slow + signal - 2
    }


def calc_kdj(highs, lows, closes, n=9):
    """计算 KDJ 指标"""
    if len(closes) < n:
        return {"k": [], "d": [], "j": []}

    k_list = [50.0] * (n - 1)
    d_list = [50.0] * (n - 1)
    j_list = [50.0] * (n - 1)

    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = ((closes[i] - ll) / (hh - ll)) * 100 if hh != ll else 50

        prev_k = k_list[-1] if k_list else 50
        prev_d = d_list[-1] if d_list else 50

        k = prev_k * 2/3 + rsv * 1/3
        d = prev_d * 2/3 + k * 1/3
        j = 3 * k - 2 * d

        k_list.append(round(k, 2))
        d_list.append(round(d, 2))
        j_list.append(round(j, 2))

    return {"k": k_list, "d": d_list, "j": j_list}


def parse_signal_points(log_file, code):
    """解析所有信号点（买卖点、背离），返回 [(time_str, type, label), ...]"""
    code_upper = code.upper()
    ind_pattern = re.compile(
        r"\[(.*?)\]\s+\[.*?\]\s+" + re.escape(code_upper) +
        r"\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2}"
        r"\s+close=([\d.]+)\s+J=([\d.-]+)\s+CCI=([\d.-]+)"
        r"\s+DIF=([\d.-]+)\s+DEA=([\d.-]+)\s+MACD=([\d.-]+)"
        r"\s+B=(\w+)\s+S=(\w+)\s+JB=(\w+)\s+JS=(\w+)"
        r"\s+底=(\w+)\s+顶=(\w+)"
    )

    signals = []
    if not log_file or not os.path.exists(log_file):
        return signals

    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            m = ind_pattern.search(line)
            if not m:
                continue
            ts, bar_time, close, j, cci, dif, dea, macd, b, s, jb, js, bottom, top = m.groups()
            # 只保留有信号的时间点
            labels = []
            if b == "True": labels.append(("buy", "B", "B点"))
            if jb == "True": labels.append(("buy", "J买", "J买"))
            if bottom == "True": labels.append(("buy", "底", "底背离"))
            if s == "True": labels.append(("sell", "S", "S点"))
            if js == "True": labels.append(("sell", "J卖", "J卖"))
            if top == "True": labels.append(("sell", "顶", "顶背离"))

            for direction, short, full in labels:
                signals.append({
                    "time": bar_time,
                    "direction": direction,
                    "label": short,
                    "full": full,
                    "price": float(close),
                })

    return signals


def parse_indicators(log_file, code):
    """从日志解析已有的 MACD/KDJ 指标值（用于实时显示）"""
    code_upper = code.upper()
    ind_pattern = re.compile(
        r"\[(.*?)\]\s+\[.*?\]\s+" + re.escape(code_upper) +
        r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
        r"\s+close=([\d.]+)\s+J=([\d.-]+)\s+CCI=([\d.-]+)"
        r"\s+DIF=([\d.-]+)\s+DEA=([\d.-]+)\s+MACD=([\d.-]+)"
        r"\s+B=(\w+)\s+S=(\w+)\s+JB=(\w+)\s+JS=(\w+)"
        r"\s+底=(\w+)\s+顶=(\w+)"
    )

    latest = None
    if log_file and os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                m = ind_pattern.search(line)
                if m:
                    ts, close, j, cci, dif, dea, macd, b, s, jb, js, bottom, top = m.groups()
                    latest = {
                        "time": ts,
                        "close": float(close),
                        "j": float(j),
                        "cci": float(cci),
                        "dif": float(dif),
                        "dea": float(dea),
                        "macd": float(macd),
                        "b": b, "s": s, "jb": jb, "js": js,
                        "bottom": bottom, "top": top,
                    }

    return latest


@app.route("/api/stock/<code>/bars")
def api_stock_bars(code):
    """获取股票分钟 K 线 + 技术指标"""
    log_file = get_today_log()
    bars = parse_bars(log_file, code)

    if not bars:
        return jsonify({"bars": [], "indicators": None})

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]

    macd_data = calc_macd(closes)
    kdj_data = calc_kdj(highs, lows, closes)

    # 最新指标（从日志直接读）
    latest_ind = parse_indicators(log_file, code)
    # 买卖信号标注点
    signals = parse_signal_points(log_file, code)

    return jsonify({
        "code": code.upper(),
        "bars": bars,
        "macd": macd_data,
        "kdj": kdj_data,
        "indicators": latest_ind,
        "signals": signals,
        "count": len(bars),
    })


@app.route("/api/stock/<code>/latest")
def api_stock_latest(code):
    """获取股票最新价格和指标"""
    log_file = get_today_log()
    bars = parse_bars(log_file, code, max_bars=1)
    indicators = parse_indicators(log_file, code)
    return jsonify({
        "code": code.upper(),
        "bar": bars[-1] if bars else None,
        "indicators": indicators,
    })


if __name__ == "__main__":
    os.makedirs(LOGS_PATH, exist_ok=True)
    app.run(host="0.0.0.0", port=8090, debug=False)
