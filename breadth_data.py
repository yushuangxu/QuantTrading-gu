"""Bounded batch source and isolated breadth cache. No background scan at import.

No automatic source switching: Tencent/Sina probes are not mixed with Eastmoney
rows. The installed AkShare wrapper was inspected; its unbounded retry/paging
is deliberately not used on Flask request threads.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, time
import json
import math
import os
from pathlib import Path
import tempfile
from threading import Lock
import time as clock_time

from market_data_state import TradingCalendar, SHANGHAI, aware
from market_breadth import (BreadthError, SCOPE, a_share_exchange,
                           security_identity, calculate_breadth)
import re

ENDPOINT = 'https://82.push2.eastmoney.com/api/qt/clist/get'
SOURCE = 'eastmoney/all-a-batch'
FILTER = 'm:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048'
FIELDS = 'f2,f3,f5,f6,f12,f13,f14,f18,f124'


@dataclass(frozen=True)
class BreadthSettings:
    page_size: int = 100
    max_pages: int = 80
    workers: int = 4
    connect_timeout: float = 2.
    read_timeout: float = 5.
    collection_seconds: float = 20.
    quote_skew_seconds: float = 30.
    trading_ttl: int = 180
    lunch_ttl: int = 900
    after_close_ttl: int = 1800
    rest_ttl: int = 21600
    failure_cooldown: int = 180
    quote_stale_seconds: int = 180

    def __post_init__(self):
        if any(not math.isfinite(float(v)) or v <= 0 for v in asdict(self).values()):
            raise ValueError('广度请求和缓存配置必须为有限正值')
        if any(type(getattr(self,k)) is not int for k in ('page_size','max_pages','workers')):
            raise ValueError('分页参数必须为整数')
        if self.page_size > 100 or self.max_pages > 100 or self.workers > 4:
            raise ValueError('禁止扩大为逐股请求或无界并发')


def _quote(raw):
    if not isinstance(raw,dict):
        raise BreadthError('来源行情行格式异常')
    code,market = raw.get('f12'),raw.get('f13')
    if type(market) is not int or market not in (0,1) or not isinstance(code,str) or not re.fullmatch(r'\d{6}',code):
        raise BreadthError('来源证券代码/市场未核验')
    actual = a_share_exchange(code)
    exchange = 'SH' if market==1 else 'BJ' if actual=='BJ' else 'SZ'
    stock_type = 'A_STOCK' if actual is not None else 'OTHER'
    # Keep the source market authoritative: never silently correct a conflict
    # to the exchange inferred from the code. Reuse the pure identity check.
    security_identity({'code':code,'exchange':exchange,'security_type':stock_type})
    epoch = raw.get('f124')
    if epoch in (None,'-',0):
        stamp = None
    else:
        if isinstance(epoch,bool) or not isinstance(epoch,(int,float)) or not math.isfinite(epoch):
            raise BreadthError('来源f124时间戳异常')
        stamp = datetime.fromtimestamp(epoch,SHANGHAI).isoformat()
    return {'code':code,'exchange':exchange,'security_type':stock_type,'name':raw.get('f14'),
            'price':raw.get('f2'),'previous_close':raw.get('f18'),'change_pct':raw.get('f3'),
            'volume':raw.get('f5'),'amount':raw.get('f6'),'quote_at':stamp,
            'suspended':None}  # No verified suspension flag in this response.


class EastmoneyBreadthSource:
    def __init__(self, settings=None, get=None, clock=None, monotonic=None):
        self.settings = settings or BreadthSettings()
        self._get = get
        self.clock = clock or (lambda:datetime.now(SHANGHAI))
        self.monotonic = monotonic or clock_time.monotonic

    def _page(self, page, deadline):
        if self.monotonic() >= deadline:
            raise BreadthError('批量取数超过采集时限')
        get = self._get
        if get is None:
            import requests
            get = requests.get
        started = aware(self.clock())
        response = get(ENDPOINT,params={'pn':str(page),'pz':str(self.settings.page_size),
            'po':'1','np':'1','fltt':'2','invt':'2','fid':'f12','fs':FILTER,'fields':FIELDS},
            timeout=(self.settings.connect_timeout,self.settings.read_timeout))
        try:
            response.raise_for_status()
            body = response.json()
        finally:
            response.close()
        data = body.get('data') if isinstance(body,dict) else None
        if not isinstance(data,dict) or type(data.get('total')) is not int or data['total']<=0:
            raise BreadthError('批量接口没有有效总数')
        rows = data.get('diff')
        if not isinstance(rows,list) or not rows:
            raise BreadthError('批量接口空页或响应格式变化')
        fetched = aware(self.clock())
        return data['total'], rows, {'page':page,'row_count':len(rows),
                'requested_at':started.isoformat(),'fetched_at':fetched.isoformat()}

    def fetch(self):
        started = aware(self.clock())
        deadline = self.monotonic()+self.settings.collection_seconds
        total,first,meta = self._page(1,deadline)
        page_size = len(first)
        if page_size > self.settings.page_size:
            raise BreadthError('接口分页大小与请求协议不符')
        count = math.ceil(total/page_size)
        if count > self.settings.max_pages:
            raise BreadthError('声明总数超过有限分页预算',{'provider_total':total,'collected_rows':len(first)})
        batches,metas = {1:first},[meta]
        if count > 1:
            # At most four finite workers. Each starts only before deadline;
            # in-flight requests retain their own explicit network timeout.
            with ThreadPoolExecutor(max_workers=self.settings.workers) as pool:
                futures = {page:pool.submit(self._page,page,deadline) for page in range(2,count+1)}
                try:
                    for page,future in futures.items():
                        current,rows,page_meta = future.result()
                        if current != total:
                            raise BreadthError('分页期间来源总数变化，拒绝混合快照')
                        expected = page_size if page < count else total-page_size*(count-1)
                        if len(rows) != expected:
                            raise BreadthError('分页被截断或数据缺页')
                        batches[page]=rows;metas.append(page_meta)
                except Exception as exc:
                    for future in futures.values(): future.cancel()
                    raise BreadthError(f'全A批量采集失败: {type(exc).__name__}: {exc}',
                        {'provider_total':total,'collected_rows':sum(map(len,batches.values())),
                         'completed_pages':len(batches),'expected_pages':count}) from exc
        fetched = aware(self.clock())
        if self.monotonic()>deadline or (fetched-started).total_seconds()>self.settings.collection_seconds:
            raise BreadthError('采集耗时超限，不发布跨时点数据',{'provider_total':total})
        raw = [row for page in sorted(batches) for row in batches[page]]
        return {'schema':1,'source':SOURCE,'source_url':ENDPOINT,'scope':list(SCOPE),
                'amount_unit':'CNY','volume_unit':'lot','change_unit':'percent',
                'date_basis':'f124来源更新时间日期；非交易所日终认证',
                'requested_at':started.isoformat(),'fetched_at':fetched.isoformat(),
                'provider_total':total,'pages':metas,'rows':[_quote(row) for row in raw]}


def atomic_json(path, payload):
    path = Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                prefix='.'+path.name+'.',suffix='.tmp',delete=False) as stream:
            tmp=Path(stream.name);json.dump(payload,stream,ensure_ascii=False,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        json.loads(tmp.read_text(encoding='utf-8'))
        os.replace(tmp,path)
    finally:
        if tmp is not None and tmp.exists(): tmp.unlink()


class BreadthService:
    def __init__(self, cache_dir, calendar_dir, *, source=None, settings=None, clock=None, universe=None):
        self.settings = settings or BreadthSettings()
        self.clock = clock or (lambda:datetime.now(SHANGHAI))
        self.source = source or EastmoneyBreadthSource(self.settings,clock=self.clock)
        self.cache_dir,self.calendar_dir = Path(cache_dir),Path(calendar_dir)
        self.universe = universe  # explicit independently verified provider seam; production=None
        self._packet=None;self._attempt=None;self._error=None;self._failure_details={}
        self._lock=Lock();self._loaded=False

    def _calculate(self, packet, calendar):
        if packet.get('source') != SOURCE or packet.get('source_url') != ENDPOINT:
            raise BreadthError('缓存来源身份不符')
        return calculate_breadth(packet,calendar,universe=self.universe,
            max_collection_seconds=self.settings.collection_seconds,
            max_quote_skew_seconds=self.settings.quote_skew_seconds)

    def _load(self, calendar):
        self._loaded=True
        try:
            path=self.cache_dir/'latest.json'
            if path.exists():
                packet=json.loads(path.read_text(encoding='utf-8'))
                self._calculate(packet,calendar);self._packet=packet
            path=self.cache_dir/'request_status.json'
            if path.exists():
                state=json.loads(path.read_text(encoding='utf-8'))
                if state.get('source')!=SOURCE: raise BreadthError('请求状态来源不符')
                self._attempt=aware(state['last_request_at'])
                self._error=state.get('error');self._failure_details=state.get('details',{})
        except (OSError,ValueError,TypeError,KeyError,AttributeError) as exc:
            self._error=f'广度缓存不可用: {exc}'

    def _due(self, now, calendar):
        if self._packet is None: return True
        fetched=aware(self._packet['fetched_at'])
        phase=calendar.phase(now)
        ttl={'trading':self.settings.trading_ttl,'lunch':self.settings.lunch_ttl,
             'after_close':self.settings.after_close_ttl,'unknown':self.settings.failure_cooldown}.get(phase,self.settings.rest_ttl)
        return (now<fetched or (now-fetched).total_seconds()>=ttl or
                (now.date(),phase)!=(fetched.date(),calendar.phase(fetched)))

    def _freshness(self, result, now, calendar):
        if self._due(now,calendar) or self._error: return 'stale'
        trade_date=datetime.fromisoformat(result['trade_date']).date()
        expected=None
        for offset in range(31):
            day=now.date()-timedelta(days=offset)
            if calendar.days.get(day) is None: return 'unknown'
            if day==now.date() and now.time()<time(9,30): continue
            if calendar.days[day]: expected=day;break
        if expected is None: return 'unknown'
        if trade_date<expected: return 'stale'
        if trade_date>expected: return 'unknown'
        newest=aware(result['quote_time_min'])
        target=now
        phase=calendar.phase(now)
        if phase=='lunch': target=now.replace(hour=11,minute=30,second=0,microsecond=0)
        elif trade_date!=now.date() or phase in {'after_close','closed_day','preopen'}:
            target=datetime.combine(trade_date,time(15),SHANGHAI)
        if (target-newest).total_seconds()>self.settings.quote_stale_seconds: return 'stale'
        return 'fresh'

    def get_snapshot(self):
        # Do not queue a second batch on concurrent HTTP refresh. A quick
        # nonblocking status response avoids holding any original scanner lock.
        if not self._lock.acquire(blocking=False):
            return {'availability':'loading','stats':None,'source':SOURCE,'source_url':ENDPOINT,
                    'error':None,'warnings':['已有一次广度批量获取在进行，请稍后刷新'],
                    'coverage':{'expected_scope':list(SCOPE),'quote_coverage_pct':None}}
        try:
            now=aware(self.clock());calendar=TradingCalendar.from_cache(self.calendar_dir)
            if not self._loaded: self._load(calendar)
            retry_ok=(self._error is None or self._attempt is None or now<self._attempt
                      or (now-self._attempt).total_seconds()>=self.settings.failure_cooldown)
            if self._due(now,calendar) and retry_ok:
                self._attempt=now
                try:
                    packet=self.source.fetch()
                    result=self._calculate(packet,calendar)
                    if aware(packet['fetched_at'])>aware(self.clock()):
                        raise BreadthError('获取时间超前，拒绝未来缓存')
                    self._packet=packet;self._error=None;self._failure_details={}
                    try: atomic_json(self.cache_dir/'latest.json',packet)
                    except (OSError,ValueError) as exc: self._error=f'广度获取有效，但缓存保存失败: {exc}'
                except Exception as exc:
                    self._error=f'{type(exc).__name__}: {exc}'
                    self._failure_details=getattr(exc,'details',{})
                try:
                    atomic_json(self.cache_dir/'request_status.json',{'source':SOURCE,
                        'last_request_at':self._attempt.isoformat(),'error':self._error,'details':self._failure_details})
                except (OSError,ValueError) as exc:
                    self._error=(self._error+'；' if self._error else '')+f'请求状态保存失败: {exc}'
            now=aware(self.clock())
            base={'source':SOURCE,'source_url':ENDPOINT,'last_request_at':self._attempt.isoformat() if self._attempt else None,
                  'next_retry_at':(self._attempt+timedelta(seconds=self.settings.failure_cooldown)).isoformat()
                      if self._error and self._attempt else None,
                  'error':self._error,'failure_details':self._failure_details,'observed_at':now.isoformat()}
            if self._packet is None:
                return {**base,'availability':'unavailable','freshness':'unavailable','stats':None,
                    'fetched_at':None,'trade_date':None,'completion_state':'unknown',
                    'coverage':{'expected_scope':list(SCOPE),'quote_coverage_pct':None,'reference_count':None,
                                'verified_complete':False,'reason':'没有可验证的完整全A快照'},
                    'warnings':['全A批量快照未通过获取/校验；不展示部分页统计，不使用合成行情']}
            try:
                result=self._calculate(self._packet,calendar)
                if aware(self._packet['fetched_at'])>now:
                    raise BreadthError('缓存获取时间在未来，当前不能发布其中统计')
            except (ValueError,TypeError,KeyError,AttributeError) as exc:
                return {**base,'availability':'unavailable','stats':None,'trade_date':None,
                        'warnings':[f'已有快照目前无法核验: {exc}'],'coverage':{'quote_coverage_pct':None}}
            freshness=self._freshness(result,now,calendar)
            if freshness!='fresh':
                result['availability']=freshness
                result['warnings'].append('仅为对应日期/时点的历史或未验证样本，不代表当前全A市场')
            return {**result,**base,'freshness':freshness}
        finally:
            self._lock.release()
