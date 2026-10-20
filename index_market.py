"""Daily index data and descriptive trends, isolated from minute bars/signals.

No work at import time. Tencent is the explicitly selected daily source;
legacy Eastmoney caches remain identifiable. No automatic source switching.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import tempfile
from threading import Lock

import pandas as pd

from market_data_state import TradingCalendar, SHANGHAI, aware


SOURCE = 'eastmoney/index-daily'
ENDPOINT = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
DOC_URL = 'https://akshare.akfamily.xyz/data/index/index.html'
FIELDS = ('date', 'open', 'high', 'low', 'close', 'volume', 'amount')
MA_WINDOWS = (5, 10, 20, 60)
SOHU_SOURCE = 'sohu/index-daily'
SOHU_ENDPOINT = 'https://q.stock.sohu.com/hisHq'
TENCENT_SOURCE = 'tencent/index-daily'
TENCENT_ENDPOINT = 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
SOURCE_INFO = {
    SOURCE: {'source_url': ENDPOINT, 'doc_url': DOC_URL, 'source_label': '东方财富 · 指数日线'},
    SOHU_SOURCE: {'source_url': SOHU_ENDPOINT, 'doc_url': 'https://q.stock.sohu.com/zs/000001/lshq.shtml',
                  'source_label': '搜狐证券 · 指数日线'},
    TENCENT_SOURCE: {'source_url': TENCENT_ENDPOINT, 'doc_url': 'https://gu.qq.com/sh000001/zs',
                     'source_label': '腾讯财经 · 指数日线'},
}


@dataclass(frozen=True)
class IndexSpec:
    code: str
    name: str
    secid: str
    exchange: str


# All four (including the returned market/name) were probed on 2026-10-19.
INDICES = (
    IndexSpec('000001.SH', '上证指数', '1.000001', 'SH'),
    IndexSpec('000300.SH', '沪深300', '1.000300', 'SH'),
    IndexSpec('399006.SZ', '创业板指', '0.399006', 'SZ'),
    IndexSpec('000852.SH', '中证1000', '1.000852', 'SH'),
)


@dataclass(frozen=True)
class IndexSettings:
    min_history: int = 120
    slope_bars: int = 5
    history_days: int = 550
    refresh_seconds: int = 300
    rest_seconds: int = 21600
    retry_seconds: int = 60
    connect_timeout: float = 3.
    read_timeout: float = 8.

    def __post_init__(self):
        if self.min_history < 120 or self.slope_bars < 1:
            raise ValueError('指数预热至少120根，斜率窗口必须为正')
        if min(self.history_days, self.refresh_seconds, self.rest_seconds,
               self.retry_seconds, self.connect_timeout, self.read_timeout) <= 0:
            raise ValueError('指数缓存和请求参数必须为正')


class IndexDataError(ValueError):
    pass


def normalize_daily(rows):
    """Strict daily schema; exact duplicates collapse, conflicting ones fail."""
    if not isinstance(rows, list) or not rows:
        raise IndexDataError('日线数据为空')
    clean = {}
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != set(FIELDS):
            raise IndexDataError('日线字段不完整或频率/格式错误')
        label = raw['date']
        if not isinstance(label, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', label):
            raise IndexDataError('必须是日线交易日期，不能使用分钟/周线时间戳')
        date.fromisoformat(label)
        row = {'date': label}
        for key in FIELDS[1:]:
            if isinstance(raw[key], bool):
                raise IndexDataError(f'{key}不能是布尔值')
            try:
                value = float(raw[key])
            except (ValueError, TypeError) as exc:
                raise IndexDataError(f'{key}不是有效数值') from exc
            if not math.isfinite(value) or value < 0 or (key not in {'volume', 'amount'} and value == 0):
                raise IndexDataError(f'{key}为非法数值')
            row[key] = value
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            raise IndexDataError('OHLC高低价冲突')
        if label in clean and clean[label] != row:
            raise IndexDataError(f'{label}存在同日冲突数据')
        clean[label] = row
    return [clean[key] for key in sorted(clean)]


class EastmoneyIndexSource:
    """Small bounded adapter for the endpoint used by AkShare, not its unbounded wrapper."""
    source_id = SOURCE
    def __init__(self, settings=None, get=None):
        self.settings = settings or IndexSettings()
        self._get = get

    def fetch(self, spec, now):
        if self._get is None:
            import requests
            get = requests.get
        else:
            get = self._get
        response = get(ENDPOINT, params={
            'secid': spec.secid, 'klt': '101', 'fqt': '0',
            'fields1': 'f1,f2,f3,f4,f5,f6',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
            'beg': (now.date() - timedelta(days=self.settings.history_days)).strftime('%Y%m%d'),
            'end': now.strftime('%Y%m%d'),
        }, timeout=(self.settings.connect_timeout, self.settings.read_timeout))
        try:
            response.raise_for_status()
            data = response.json().get('data')
        finally:
            response.close()
        if not isinstance(data, dict):
            raise IndexDataError('指数接口无数据')
        market, code = spec.secid.split('.')
        if (data.get('code') != code or str(data.get('market')) != market
                or data.get('name') != spec.name):
            raise IndexDataError('指数代码、市场或名称与已核验映射不符')
        rows = []
        for line in data.get('klines') or []:
            parts = line.split(',')
            if len(parts) != 11:
                raise IndexDataError('指数日线响应列数异常')
            # f51=date,f52=open,f53=close,f54=high,f55=low,f56=volume,f57=amount
            rows.append(dict(zip(FIELDS, [parts[0], parts[1], parts[3], parts[4],
                                          parts[2], parts[5], parts[6]])))
        return normalize_daily(rows)


class SohuIndexSource:
    """Explicit zs_ index namespace; daily volume=lots, amount=10,000 CNY.

    Units verified against Sohu's index-history table and real 2026-10-20
    responses. Never treat a stock cn_ response or missing amount as an index.
    """
    source_id = SOHU_SOURCE

    def __init__(self, settings=None, get=None):
        self.settings = settings or IndexSettings()
        self._get = get

    def fetch(self, spec, now):
        if spec not in INDICES:
            raise IndexDataError('未核验的指数映射')
        symbol = 'zs_'+spec.code.split('.')[0]
        if self._get is None:
            import requests
            get = requests.get
        else:
            get = self._get
        begin = now.date()-timedelta(days=self.settings.history_days)
        response = get(SOHU_ENDPOINT, params={'code':symbol,'start':begin.strftime('%Y%m%d'),
            'end':now.strftime('%Y%m%d'),'stat':'1','order':'D','period':'d','rt':'json'},
            timeout=(self.settings.connect_timeout,self.settings.read_timeout))
        try:
            response.raise_for_status()
            # This endpoint returns GBK-compatible JSON including Chinese stat labels.
            data = json.loads(response.content.decode('gb18030'))
        finally:
            response.close()
        if (not isinstance(data,list) or len(data)!=1 or not isinstance(data[0],dict)
                or type(data[0].get('status')) is not int or data[0]['status']!=0
                or data[0].get('code')!=symbol):
            raise IndexDataError('搜狐指数响应身份或状态不符')
        hq=data[0].get('hq')
        if not isinstance(hq,list) or not hq:
            raise IndexDataError('搜狐指数日线为空')
        rows=[]
        for parts in hq:
            if not isinstance(parts,list) or len(parts)!=10:
                raise IndexDataError('搜狐指数日线列数异常')
            if isinstance(parts[8],bool):
                raise IndexDataError('成交额不能是布尔值')
            try:
                amount = float(Decimal(str(parts[8]))*10000)
            except Exception as exc:
                raise IndexDataError('搜狐指数成交额缺失或无效') from exc
            rows.append(dict(date=parts[0],open=parts[1],close=parts[2],low=parts[5],
                             high=parts[6],volume=parts[7],amount=amount))
        rows=normalize_daily(rows)
        if any(not begin<=date.fromisoformat(row['date'])<=now.date() for row in rows):
            raise IndexDataError('搜狐日线日期超出请求范围')
        return rows


class TencentIndexSource:
    """Verified 11-column unadjusted INDEX day rows, not six-column stock bars.

    day[5]=lots, day[8]=10,000 CNY. Verified against 120 Sohu sessions with
    publisher-labelled units; six-column amount-as-volume wrappers are unsafe.
    Only the four explicit index identities and their ZS quote metadata qualify.
    """
    source_id = TENCENT_SOURCE

    def __init__(self, settings=None, get=None):
        self.settings = settings or IndexSettings()
        self._get = get

    def fetch(self, spec, now):
        if spec not in INDICES:
            raise IndexDataError('未核验的指数映射')
        symbol=spec.exchange.lower()+spec.code.split('.')[0]
        begin=now.date()-timedelta(days=self.settings.history_days)
        if self._get is None:
            import requests
            get=requests.get
        else:
            get=self._get
        response=get(TENCENT_ENDPOINT,params={'_var':'index_daily',
            'param':f'{symbol},day,{begin.isoformat()},{now.date().isoformat()},640,qfq'},
            timeout=(self.settings.connect_timeout,self.settings.read_timeout))
        try:
            response.raise_for_status()
            text=response.content.decode('utf-8').strip()
            if text.startswith('index_daily='):
                text=text[len('index_daily='):].rstrip(';')
            data=json.loads(text)
        finally:
            response.close()
        if not isinstance(data,dict) or type(data.get('code')) is not int or data['code']!=0:
            raise IndexDataError('腾讯指数日线响应状态异常')
        node=data.get('data',{}).get(symbol)
        if not isinstance(node,dict):
            raise IndexDataError('腾讯指数响应代码不符')
        quote=node.get('qt',{}).get(symbol)
        if (not isinstance(quote,list) or len(quote)<62 or quote[1]!=spec.name
                or quote[2]!=spec.code.split('.')[0] or quote[61]!='ZS'
                or str(quote[0])!=('1' if spec.exchange=='SH' else '51')):
            raise IndexDataError('腾讯代码、市场、指数名称或ZS类型不符')
        # Do not accept qfqday or synthesize a daily row from the quote block.
        raw=node.get('day')
        if not isinstance(raw,list) or not raw:
            raise IndexDataError('腾讯缺少未复权指数日线')
        rows=[]
        for parts in raw:
            if not isinstance(parts,list) or len(parts)!=11:
                raise IndexDataError('腾讯日线缺独立量额字段或响应列数变化')
            if isinstance(parts[8],bool):raise IndexDataError('成交额不能是布尔值')
            try:amount=float(Decimal(str(parts[8]))*10000)
            except Exception as exc:raise IndexDataError('腾讯指数成交额无效') from exc
            rows.append(dict(date=parts[0],open=parts[1],close=parts[2],high=parts[3],
                             low=parts[4],volume=parts[5],amount=amount))
        rows=normalize_daily(rows)
        # The endpoint may return its full 640-row window despite begin. Keep
        # valid older history; never truncate it or accept future sessions.
        if any(date.fromisoformat(row['date'])>now.date() for row in rows):
            raise IndexDataError('腾讯日线包含未来日期')
        return rows


def daily_states(rows, fetched_at, now, calendar):
    """Conservative completion: daily date + observed later trading-day witness.

    A past clock time alone never closes the endpoint's final row. 'closed'
    denotes a completed session suitable for daily analysis, not an assurance
    that the vendor can never correct historical prices.
    """
    fetched_at, now = aware(fetched_at), aware(now)
    eligible = [r for r in rows if date.fromisoformat(r['date']) <= min(fetched_at.date(), now.date())]
    result = []
    for i, row in enumerate(eligible):
        day = date.fromisoformat(row['date'])
        known = calendar.days.get(day)
        successor = next((r for r in eligible[i + 1:]
                          if calendar.days.get(date.fromisoformat(r['date'])) is True
                          and (r['date'] < fetched_at.date().isoformat()
                               or fetched_at.time() >= time(9, 30))
                          and (r['date'] < now.date().isoformat() or now.time() >= time(9, 30))), None)
        if fetched_at > now or known is not True:
            state, reason = 'unknown', '日历缺失/非交易日或获取时间异常'
        elif day == now.date() and time(9, 30) <= now.time() < time(15):
            state, reason = 'forming', '当日临时日线（含午休），尚未收盘'
        elif successor and day < now.date():
            state, reason = 'closed', '明确日频响应中已观察到后续交易日记录'
        else:
            state, reason = 'unknown', '末根无最终完成标志，等待后续交易日佐证'
        result.append({**row, 'state': state, 'state_reason': reason})
    return result


def moving_averages(closes):
    series = pd.Series(closes, dtype=float)
    return {f'ma{n}': [None if pd.isna(v) else float(v)
                        for v in series.rolling(n, min_periods=n).mean()]
            for n in MA_WINDOWS}


def ma_direction(values, bars=5):
    if len(values) <= bars or values[-1] is None or values[-1-bars] is None:
        return {'direction': 'unknown', 'delta_per_bar': None}
    delta = (values[-1] - values[-1-bars]) / bars
    return {'direction': 'up' if delta > 0 else 'down' if delta < 0 else 'flat',
            'delta_per_bar': delta}


def trend_state(close, ma20, ma60, dir20, dir60, sufficient):
    if not sufficient or ma20 is None or ma60 is None or 'unknown' in (dir20, dir60):
        return '数据不足'
    if close > ma20 > ma60 and dir20 == dir60 == 'up':
        return '偏强'
    if close < ma20 < ma60 and dir20 == dir60 == 'down':
        return '偏弱'
    return '震荡'


def analyze_daily(rows, fetched_at, now, calendar, settings=None):
    settings = settings or IndexSettings()
    rows = normalize_daily(rows)
    states = daily_states(rows, fetched_at, now, calendar)
    closed = [r for r in states if r['state'] == 'closed']
    warnings = []
    if len(states) != len(rows):
        warnings.append('返回中含未来日期，已排除，不参与分析')
    if any(calendar.days.get(date.fromisoformat(r['date'])) is False for r in states):
        warnings.append('日线含已知非交易日记录，正式趋势停用')
    # Only a contiguous, explicitly covered suffix is usable: do not silently
    # bridge missing sessions or compress unknown days into a 60-bar MA.
    if closed:
        start = date.fromisoformat(closed[max(0, len(closed)-settings.min_history)]['date'])
        end = date.fromisoformat(closed[-1]['date'])
        labels = {r['date'] for r in closed}
        for offset in range((end-start).days + 1):
            day = start + timedelta(days=offset)
            opened = calendar.days.get(day)
            if opened is None or (opened and day.isoformat() not in labels):
                warnings.append('近期日线有缺失交易日或日历覆盖不足，正式趋势停用')
                break
    ma = moving_averages([r['close'] for r in closed])
    direction = {f'ma{n}': ma_direction(ma[f'ma{n}'], settings.slope_bars) for n in (20, 60)}
    sufficient = len(closed) >= max(settings.min_history, 60+settings.slope_bars) and not warnings
    latest = closed[-1] if closed else None
    result = {
        'trend': trend_state(latest['close'] if latest else None,
                             ma['ma20'][-1] if closed else None, ma['ma60'][-1] if closed else None,
                             direction['ma20']['direction'], direction['ma60']['direction'], sufficient),
        'analysis_date': latest['date'] if latest else None,
        'analysis_bar_state': 'closed' if latest else 'unknown',
        'closed_count': len(closed), 'required_history': settings.min_history,
        'close': latest['close'] if latest else None,
        'change_pct': (closed[-1]['close']/closed[-2]['close']-1)*100 if len(closed) > 1 else None,
        'amount': latest['amount'] if latest else None,
        'ma': {key: values[-1] if closed else None for key, values in ma.items()},
        'directions': direction, 'slope_bars': settings.slope_bars,
        'latest': states[-1] if states else None,
        'latest_change_pct': (states[-1]['close']/states[-2]['close']-1)*100 if len(states) > 1 else None,
        'chart': [{'date': r['date'], 'close': r['close'],
                   **{key: values[i] for key, values in ma.items()}}
                  for i, r in enumerate(closed)][-120:],
        'warnings': warnings,
    }
    if not sufficient:
        warnings.append(f'有效已完成日线{len(closed)}根，需至少{settings.min_history}根且近期连续')
    return result


class IndexMarketService:
    """Lazy per-index cache. Web requests only; no timers or scanner threads."""
    def __init__(self, cache_dir, calendar_dir, source=None, settings=None, clock=None):
        self.settings = settings or IndexSettings()
        self.source = source or TencentIndexSource(self.settings)
        self.cache_dir, self.calendar_dir = Path(cache_dir), Path(calendar_dir)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self._memory, self._attempts, self._errors = {}, {}, {}
        self._locks = {spec.code: Lock() for spec in INDICES}

    def _path(self, spec):
        return self.cache_dir / (spec.code + '.json')

    def _load(self, spec):
        path = self._path(spec)
        if not path.exists():
            return None
        packet = json.loads(path.read_text(encoding='utf-8'))
        self._validate_packet(spec, packet)
        return packet

    def _validate_packet(self, spec, packet):
        if (not isinstance(packet, dict) or packet.get('schema') != 1 or packet.get('source') not in SOURCE_INFO
                or packet.get('source_url') != SOURCE_INFO[packet['source']]['source_url']
                or packet.get('frequency') != '1D' or packet.get('index') != asdict(spec)
                or packet.get('adjustment') != 'index-unadjusted'
                or packet.get('units') != {'volume': '手', 'amount': '元'}):
            raise IndexDataError('指数缓存身份、来源、频率或单位不符')
        aware(packet['fetched_at'])
        normalize_daily(packet['rows'])

    def _save(self, spec, packet):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_dir,
                                             prefix='.'+spec.code, suffix='.tmp', delete=False) as stream:
                tmp = Path(stream.name)
                json.dump(packet, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            self._validate_packet(spec, json.loads(tmp.read_text(encoding='utf-8')))
            os.replace(tmp, self._path(spec))
        finally:
            if tmp is not None and tmp.exists():
                tmp.unlink()

    @staticmethod
    def _period(now, calendar):
        return (now.date(), calendar.phase(now))

    def _due(self, packet, now, calendar):
        fetched = aware(packet['fetched_at'])
        age = (now-fetched).total_seconds()
        ttl = (self.settings.refresh_seconds if calendar.phase(now) in {'trading', 'lunch', 'unknown'}
               else self.settings.rest_seconds)
        return age < 0 or age >= ttl or self._period(now, calendar) != self._period(fetched, calendar)

    def get_index(self, code):
        spec = next((s for s in INDICES if s.code == code), None)
        if spec is None:
            raise KeyError(code)
        # Separate locks and endpoint calls keep one failed index from locking
        # the other indices or any original scanner/notification resources.
        with self._locks[code]:
            now = aware(self.clock())
            calendar = TradingCalendar.from_cache(self.calendar_dir)
            packet = self._memory.get(code)
            if packet is None:
                try:
                    packet = self._load(spec)
                    if packet:
                        self._memory[code] = packet
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    self._errors[code] = f'指数缓存不可用: {exc}'
            last = self._attempts.get(code)
            retry_ok = last is None or (now-last).total_seconds() >= self.settings.retry_seconds or now < last
            if (packet is None or self._due(packet, now, calendar)) and retry_ok:
                self._attempts[code] = now
                try:
                    rows = normalize_daily(self.source.fetch(spec, now))
                    if any(date.fromisoformat(r['date']) > now.date() for r in rows):
                        raise IndexDataError('数据源返回未来日线')
                    source_id=getattr(type(self.source),'source_id',SOURCE)
                    if source_id not in SOURCE_INFO:
                        raise IndexDataError('未登记的指数数据源')
                    packet = {'schema': 1, 'source': source_id, 'source_url': SOURCE_INFO[source_id]['source_url'],
                              'index': asdict(spec), 'frequency': '1D', 'adjustment': 'index-unadjusted',
                              'units': {'volume': '手', 'amount': '元'},
                              'fetched_at': aware(self.clock()).isoformat(), 'rows': rows}
                    self._memory[code] = packet
                    self._errors.pop(code, None)
                    try:
                        self._save(spec, packet)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        self._errors[code] = f'取得有效数据，但指数缓存保存失败: {exc}'
                except Exception as exc:
                    self._errors[code] = f'指数获取失败: {type(exc).__name__}: {exc}'
            now = aware(self.clock())
            source_id=packet['source'] if packet else getattr(type(self.source),'source_id',SOURCE)
            base = {**asdict(spec), 'source': source_id, **SOURCE_INFO.get(source_id,SOURCE_INFO[SOURCE]),
                    'observed_at': now.isoformat(), 'fetched_at': packet['fetched_at'] if packet else None,
                    'last_request_at': self._attempts[code].isoformat() if code in self._attempts else None,
                    'error': self._errors.get(code), 'units': {'volume': '手', 'amount': '元'}}
            if packet is None:
                return {**base, 'availability': 'unavailable', 'trend': '数据不足', 'chart': [],
                        'latest': None, 'warnings': ['无有效指数日线，不使用合成数据替代']}
            result = analyze_daily(packet['rows'], packet['fetched_at'], now, calendar, self.settings)
            # Fresh acquisition does not prove a current market date. Require
            # calendar coverage back to the latest row and expected session.
            latest = date.fromisoformat(result['latest']['date']) if result['latest'] else None
            expected = None
            if calendar.days.get(now.date()) is not None:
                for offset in range(31):
                    day = now.date()-timedelta(days=offset)
                    if day == now.date() and now.time() < time(9, 30):
                        continue
                    if day not in calendar.days:
                        break
                    if calendar.days[day]:
                        expected = day
                        break
            availability = 'fresh'
            if self._errors.get(code) or self._due(packet, now, calendar) or (expected and (latest is None or latest < expected)):
                availability = 'stale'
            elif expected is None or aware(packet['fetched_at']) > now:
                availability = 'unknown'
            if availability != 'fresh':
                result['warnings'].append('行情不是已验证的新鲜数据；趋势仅供对应历史日期参考')
            return {**base, **result, 'availability': availability}
