"""Offline synthetic index data. No real quotes, ports, cache or notifications."""
from datetime import date, datetime, timedelta
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from index_market import (
    INDICES, SOURCE, ENDPOINT, FIELDS, IndexSettings, IndexDataError,
    EastmoneyIndexSource, SohuIndexSource, SOHU_SOURCE, SOHU_ENDPOINT,
    TencentIndexSource, TENCENT_SOURCE, TENCENT_ENDPOINT,
    IndexMarketService, analyze_daily, daily_states,
    moving_averages, ma_direction, normalize_daily, trend_state,
)
from market_data_state import TradingCalendar, SHANGHAI

ROOT = Path(__file__).resolve().parents[1]


def fixture(count=140, descending=False):
    # Weekdays describe this explicit SIMULATED calendar, not a market fallback.
    dates = pd.bdate_range('2026-01-05', periods=count)
    rows = []
    for i, day in enumerate(dates):
        close = 1000.-i if descending else 1000.+i
        rows.append(dict(date=day.date().isoformat(), open=close-.5, high=close+1,
                         low=close-1, close=close, volume=100000., amount=200000000.))
    start, end = dates[0].date(), dates[-1].date()+timedelta(days=10)
    opens = {d.date() for d in dates}
    days = {start+timedelta(days=i): start+timedelta(days=i) in opens
            for i in range((end-start).days+1)}
    cal = TradingCalendar(days)
    now = datetime.combine(dates[-1].date(), datetime.min.time(), SHANGHAI).replace(hour=14, minute=50)
    return rows, cal, now


class TestIndexData(unittest.TestCase):
    def test_verified_index_map_is_explicit_not_stock_code_heuristic(self):
        self.assertEqual([(x.code, x.secid, x.exchange) for x in INDICES], [
            ('000001.SH', '1.000001', 'SH'), ('000300.SH', '1.000300', 'SH'),
            ('399006.SZ', '0.399006', 'SZ'), ('000852.SH', '1.000852', 'SH')])

    def test_source_uses_daily_unadjusted_fields_and_bounded_timeout(self):
        rows, _, now = fixture()
        response = Mock()
        response.json.return_value = {'data': {'code': '000001', 'market': 1, 'name': '上证指数',
            'klines': ['2026-01-05,100,102,103,99,500,60000,4,2,2,0']}}
        get = Mock(return_value=response)
        source = EastmoneyIndexSource(get=get)
        result = source.fetch(INDICES[0], now)
        self.assertEqual(result[0], dict(date='2026-01-05', open=100., high=103., low=99.,
                                        close=102., volume=500., amount=60000.))
        args, kwargs = get.call_args
        self.assertEqual(args, (ENDPOINT,))
        self.assertEqual(kwargs['params']['klt'], '101')
        self.assertEqual(kwargs['params']['fqt'], '0')
        self.assertEqual(kwargs['timeout'], (3., 8.))
        self.assertEqual(kwargs['params']['secid'], '1.000001')
        response.close.assert_called_once()

    def test_source_rejects_identity_error_empty_and_bad_fields(self):
        _, _, now = fixture()
        for data in [None, {'code':'000001','market':0,'name':'上证指数'},
                     {'code':'000001','market':1,'name':'平安银行'},
                     {'code':'000001','market':1,'name':'上证指数','klines':[]},
                     {'code':'000001','market':1,'name':'上证指数','klines':['a,b']}]:
            with self.subTest(data=data):
                response = Mock(); response.json.return_value = {'data': data}
                with self.assertRaises(IndexDataError):
                    EastmoneyIndexSource(get=Mock(return_value=response)).fetch(INDICES[0], now)
                response.close.assert_called_once()

    def test_source_http_failure_closes_response(self):
        response = Mock(); response.raise_for_status.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            EastmoneyIndexSource(get=Mock(return_value=response)).fetch(INDICES[0], fixture()[2])
        response.close.assert_called_once()

    def test_duplicate_sorting_is_idempotent_and_does_not_mutate(self):
        rows, _, _ = fixture(5)
        raw = rows[::-1]+[dict(rows[0])]
        before = json.dumps(raw)
        self.assertEqual(normalize_daily(raw), rows)
        self.assertEqual(json.dumps(raw), before)
        self.assertEqual(normalize_daily(normalize_daily(raw)), rows)

    def test_conflicting_duplicate_rejected(self):
        rows, _, _ = fixture(5)
        with self.assertRaises(IndexDataError):
            normalize_daily(rows+[{**rows[0], 'amount': 10}])

    def test_invalid_values_missing_fields_and_minute_dates_rejected(self):
        row = fixture(1)[0][0]
        for key, value in [('date','2026-01-05 15:00:00'), ('date','2026-02-30'),
                           ('volume',-1),('close',float('nan')),('high',0),('amount',True),
                           ('close',20000),('open',None),('low',float('inf'))]:
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                normalize_daily([{**row,key:value}])
        with self.assertRaises(IndexDataError):
            normalize_daily([{k:v for k,v in row.items() if k != 'amount'}])
        with self.assertRaises(IndexDataError):
            normalize_daily([])

    def test_settings_cannot_disable_history_warmup(self):
        for kwargs in [dict(min_history=60),dict(slope_bars=0),dict(read_timeout=0)]:
            with self.assertRaises(ValueError): IndexSettings(**kwargs)


class TestIndexAnalysis(unittest.TestCase):
    def test_ma_all_four_windows_independent_arithmetic(self):
        data = list(range(1,131))
        ma = moving_averages(data)
        for n in [5,10,20,60]:
            self.assertEqual(ma[f'ma{n}'][:n-1], [None]*(n-1))
            for i in range(n-1,len(data)):
                self.assertEqual(ma[f'ma{n}'][i], sum(data[i-n+1:i+1])/n)

    def test_directions_and_strong_weak_sideways_rules(self):
        self.assertEqual(ma_direction([1,2,3,4,5,6]), {'direction':'up','delta_per_bar':1.})
        self.assertEqual(ma_direction([6,5,4,3,2,1])['direction'], 'down')
        self.assertEqual(ma_direction([2]*6)['direction'], 'flat')
        self.assertEqual(ma_direction([None,2])['direction'], 'unknown')
        self.assertEqual(trend_state(110,105,100,'up','up',True),'偏强')
        self.assertEqual(trend_state(90,95,100,'down','down',True),'偏弱')
        for args in [(100,100,100,'flat','flat',True),(110,105,100,'up','down',True)]:
            self.assertEqual(trend_state(*args),'震荡')
        self.assertEqual(trend_state(110,105,100,'up','up',False),'数据不足')

    def test_real_analysis_up_down_flat(self):
        for descending, expected in [(False,'偏强'),(True,'偏弱')]:
            rows, cal, now = fixture(descending=descending)
            result = analyze_daily(rows,now,now,cal)
            self.assertEqual(result['trend'],expected)
            self.assertEqual(result['closed_count'],139)
            self.assertEqual(result['analysis_date'],rows[-2]['date'])
            self.assertAlmostEqual(result['change_pct'],(rows[-2]['close']/rows[-3]['close']-1)*100)
        rows, cal, now = fixture()
        for row in rows: row.update(open=1000,close=1000,high=1001,low=999)
        self.assertEqual(analyze_daily(rows,now,now,cal)['trend'],'震荡')

    def test_short_history_no_formal_trend_even_if_ma60_exists(self):
        rows, cal, now = fixture(100)
        result = analyze_daily(rows,now,now,cal)
        self.assertEqual(result['trend'],'数据不足')
        self.assertIsNotNone(result['ma']['ma60'])
        self.assertIn('99根',result['warnings'][-1])

    def test_intraday_is_excluded_from_formal_ma_and_trend(self):
        rows, cal, now = fixture()
        before = analyze_daily(rows,now,now,cal)
        rows[-1].update(close=2000,high=2001,amount=1e12)
        after = analyze_daily(rows,now,now,cal)
        for key in ['trend','ma','directions','chart','close','change_pct','amount']:
            self.assertEqual(before[key],after[key])
        self.assertEqual(after['latest']['state'],'forming')
        self.assertEqual(after['latest']['close'],2000)

    def test_lunch_close_weekend_and_cross_day(self):
        rows, cal, now = fixture()
        for hour, minute, expected in [(9,30,'forming'),(11,30,'forming'),(12,30,'forming'),
                                       (15,0,'unknown'),(16,0,'unknown'),(8,0,'unknown')]:
            t = now.replace(hour=hour,minute=minute)
            result = daily_states(rows,t,t,cal)
            self.assertEqual(result[-1]['state'],expected)
            if hour >= 9: self.assertEqual(result[-2]['state'],'closed')
        # Rest days don't make the source's last unknown bar magically final.
        t = now+timedelta(days=2)
        self.assertEqual(daily_states(rows,now,t,cal)[-1]['state'],'unknown')
        # A real later daily record is the completion witness, not clock passage.
        self.assertEqual(daily_states(rows[:-1],now,now,cal)[-1]['state'],'unknown')

    def test_missing_calendar_future_dates_and_clock_rollback_fail_safe(self):
        rows, cal, now = fixture()
        self.assertTrue(all(r['state']=='unknown' for r in daily_states(rows,now,now,TradingCalendar())))
        self.assertEqual(analyze_daily(rows,now,now,TradingCalendar())['trend'],'数据不足')
        self.assertTrue(all(r['state']=='unknown' for r in daily_states(rows,now+timedelta(hours=1),now,cal)))
        result = analyze_daily(rows,now,now-timedelta(days=10),cal)
        self.assertTrue(all(r['date'] <= (now-timedelta(days=10)).date().isoformat() for r in result['chart']))
        self.assertEqual(result['trend'],'数据不足')

    def test_missing_session_or_weekly_sample_is_not_120_daily_sessions(self):
        rows, cal, now = fixture(160)
        result = analyze_daily(rows[:80]+rows[81:],now,now,cal)
        self.assertEqual(result['trend'],'数据不足')
        self.assertTrue(any('缺失交易日' in w for w in result['warnings']))
        self.assertEqual(analyze_daily(rows[::5],now,now,cal)['trend'],'数据不足')

    def test_prefix_causality_and_no_future_values_in_formal_mas(self):
        rows, cal, now = fixture(150)
        for count in (125,130,140,150):
            at = datetime.fromisoformat(rows[count-1]['date']).replace(tzinfo=SHANGHAI,hour=14)
            result = analyze_daily(rows[:count],at,at,cal)
            expected = sum(r['close'] for r in rows[count-61:count-1])/60
            self.assertEqual(result['ma']['ma60'], expected)
            self.assertEqual(result['analysis_date'],rows[count-2]['date'])


def sohu_response(spec, rows):
    payload=[{'status':0,'code':'zs_'+spec.code.split('.')[0],
              'hq':[[r['date'],str(r['open']),str(r['close']),'0','0%',str(r['low']),
                     str(r['high']),str(r['volume']),str(r['amount']/10000),'-'] for r in rows],
              'stat':['累计']}]
    response=Mock();response.content=json.dumps(payload,ensure_ascii=False).encode('gb18030')
    return response,payload


def tencent_response(spec, rows):
    quote=['']*62
    quote[0]='1' if spec.exchange=='SH' else '51'
    quote[1]=spec.name;quote[2]=spec.code.split('.')[0];quote[61]='ZS'
    symbol=spec.exchange.lower()+quote[2]
    node={'day':[[r['date'],str(r['open']),str(r['close']),str(r['high']),str(r['low']),
                  str(r['volume']),{},'0',str(r['amount']/10000),'0','0'] for r in rows],
          'qt':{symbol:quote}}
    payload={'code':0,'data':{symbol:node}}
    response=Mock();response.content=('index_daily='+json.dumps(payload,ensure_ascii=False)).encode('utf-8')
    return response,payload,node,quote


class TestTencentIndexSource(unittest.TestCase):
    def test_four_index_identity_and_separate_volume_amount(self):
        rows,_,now=fixture()
        for spec in INDICES:
            with self.subTest(code=spec.code):
                response,_,_,_=tencent_response(spec,rows);get=Mock(return_value=response)
                self.assertEqual(TencentIndexSource(get=get).fetch(spec,now),rows)
                args,kw=get.call_args
                self.assertEqual(args,(TENCENT_ENDPOINT,))
                self.assertTrue(kw['params']['param'].startswith(spec.exchange.lower()+spec.code[:6]+',day,'))
                self.assertEqual(kw['timeout'],(3.,8.));response.close.assert_called_once()

    def test_stock_metadata_wrong_market_name_and_code_rejected(self):
        rows,_,now=fixture()
        for col,value in ((0,'51'),(1,'平安银行'),(2,'000300'),(61,'GP-A')):
            response,payload,_,q=tencent_response(INDICES[0],rows);q[col]=value
            response.content=('index_daily='+json.dumps(payload)).encode()
            with self.subTest(col=col),self.assertRaises(IndexDataError):
                TencentIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)

    def test_six_column_and_qfqday_only_never_used_as_index_amount(self):
        rows,_,now=fixture()
        for adjusted in (False,True):
            response,payload,node,_=tencent_response(INDICES[0],rows)
            if adjusted:node['qfqday']=node.pop('day')
            else:node['day']=[r[:6] for r in node['day']]
            response.content=('index_daily='+json.dumps(payload)).encode()
            with self.assertRaises(IndexDataError):
                TencentIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)

    def test_amount_missing_negative_nonfinite_and_future_dates_rejected(self):
        rows,_,now=fixture()
        for col,value in ((8,None),(8,'-'),(8,'NaN'),(8,'-1'),(8,True),
                          (0,(now.date()+timedelta(days=1)).isoformat())):
            response,payload,node,_=tencent_response(INDICES[0],rows);node['day'][0][col]=value
            response.content=('index_daily='+json.dumps(payload)).encode()
            with self.subTest(col=col,value=value),self.assertRaises(ValueError):
                TencentIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)

    def test_valid_extra_older_history_is_preserved(self):
        rows,_,now=fixture();older={**rows[0],'date':'2020-01-02'}
        response,_,_,_=tencent_response(INDICES[0],[older]+rows)
        result=TencentIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)
        self.assertEqual(len(result),len(rows)+1)
        self.assertEqual(result[0]['date'],'2020-01-02')

    def test_tencent_service_and_flask_api_reuse_real_contract_without_network(self):
        rows,cal,now=fixture()
        with TemporaryDirectory() as directory:
            root=Path(directory);calendar=root/'calendar';calendar.mkdir()
            start,end=min(cal.days),max(cal.days)
            (calendar/f'trade_cal_{start:%Y%m%d}_{end:%Y%m%d}.json').write_text(json.dumps(
                {'open_days':[f'{d:%Y%m%d}' for d,v in cal.days.items() if v]}))
            response,_,_,_=tencent_response(INDICES[0],rows);get=Mock(return_value=response)
            service=IndexMarketService(root/'daily',calendar,source=TencentIndexSource(get=get),clock=lambda:now)
            from webapp import app as web
            previous=web.app.extensions.get('index_market')
            try:
                web.app.extensions['index_market']=service
                result=web.app.test_client().get('/api/market/index/000001.SH')
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.json['source'],TENCENT_SOURCE)
                self.assertEqual(result.json['latest']['state'],'forming')
                self.assertEqual(result.json['analysis_date'],rows[-2]['date'])
                restored=IndexMarketService(root/'daily',calendar,source=TencentIndexSource(get=get),clock=lambda:now)
                self.assertEqual(restored.get_index('000001.SH')['ma'],result.json['ma'])
                get.assert_called_once()
            finally:
                if previous is None:web.app.extensions.pop('index_market',None)
                else:web.app.extensions['index_market']=previous


class TestSohuIndexSource(unittest.TestCase):
    def test_explicit_four_index_namespaces_daily_fields_and_timeouts(self):
        rows,_,now=fixture()
        for spec in INDICES:
            with self.subTest(code=spec.code):
                response,_=sohu_response(spec,rows[::-1]);get=Mock(return_value=response)
                self.assertEqual(SohuIndexSource(get=get).fetch(spec,now),rows)
                args,kw=get.call_args
                self.assertEqual(args,(SOHU_ENDPOINT,))
                self.assertEqual(kw['params']['code'],'zs_'+spec.code.split('.')[0])
                self.assertEqual(kw['params']['period'],'d')
                self.assertEqual(kw['timeout'],(3.,8.))
                response.close.assert_called_once()

    def test_gb18030_and_amount_conversion_are_explicit_not_volume_rename(self):
        rows,_,now=fixture();response,payload=sohu_response(INDICES[0],rows)
        payload[0]['hq'][0][8]='123.4567'
        response.content=json.dumps(payload,ensure_ascii=False).encode('gb18030')
        result=SohuIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)
        self.assertEqual(result[0]['amount'],1234567)
        self.assertEqual(result[0]['volume'],100000)

    def test_stock_namespace_wrong_index_status_and_shape_rejected(self):
        rows,_,now=fixture()
        for key,value in [('code','cn_000001'),('code','zs_000300'),('status',False),
                          ('status',1),('hq',[]),('hq',[['2026-01-05',1,2,3,4,5]])]:
            response,payload=sohu_response(INDICES[0],rows);payload[0][key]=value
            response.content=json.dumps(payload).encode('gb18030')
            with self.subTest(key=key,value=value),self.assertRaises(IndexDataError):
                SohuIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)
            response.close.assert_called_once()

    def test_missing_invalid_amount_or_bad_ohlc_never_zero_filled(self):
        rows,_,now=fixture()
        for col,value in [(8,None),(8,'-'),(8,'NaN'),(8,'Infinity'),(8,'-1'),(8,True),
                          (6,'0'),(0,'2026-01-05 15:00:00')]:
            response,payload=sohu_response(INDICES[0],rows);payload[0]['hq'][0][col]=value
            response.content=json.dumps(payload).encode('gb18030')
            with self.subTest(col=col,value=value),self.assertRaises(ValueError):
                SohuIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)

    def test_conflicting_duplicates_and_future_dates_fail(self):
        rows,_,now=fixture()
        for extra in ({**rows[-1],'amount':1},
                      {**rows[-1],'date':(now.date()+timedelta(days=1)).isoformat()}):
            response,_=sohu_response(INDICES[0],rows+[extra])
            with self.assertRaises(IndexDataError):
                SohuIndexSource(get=Mock(return_value=response)).fetch(INDICES[0],now)

    def test_http_error_and_bad_json_close_response_without_retry(self):
        _,_,now=fixture()
        for fail in (False,True):
            response=Mock();response.content=b'not json'
            if fail:response.raise_for_status.side_effect=OSError('offline')
            get=Mock(return_value=response)
            with self.assertRaises((OSError,ValueError)):
                SohuIndexSource(get=get).fetch(INDICES[0],now)
            response.close.assert_called_once();get.assert_called_once()


class TestIndexCacheAndWeb(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows,self.cal,self.now = fixture()
        calendar_dir = self.root/'calendar'; calendar_dir.mkdir()
        start,end = min(self.cal.days),max(self.cal.days)
        (calendar_dir/f'trade_cal_{start:%Y%m%d}_{end:%Y%m%d}.json').write_text(json.dumps(
            {'open_days':[f'{d:%Y%m%d}' for d,v in self.cal.days.items() if v]}))
        self.source = Mock(); self.source.fetch.side_effect = lambda *_: self.rows
        self.service = IndexMarketService(self.root/'index_daily',calendar_dir,source=self.source,
                                           clock=lambda:self.now)

    def test_cache_reused_in_memory_and_after_new_service(self):
        result = self.service.get_index(INDICES[0].code)
        self.assertEqual(result['availability'],'fresh')
        self.assertEqual(result['trend'],'偏强')
        self.service.get_index(INDICES[0].code)
        self.source.fetch.assert_called_once()
        other = IndexMarketService(self.root/'index_daily', self.root/'calendar',source=self.source,clock=lambda:self.now)
        self.assertEqual(other.get_index(INDICES[0].code)['trend'],'偏强')
        self.source.fetch.assert_called_once()
        self.assertEqual(list(self.root.glob('cache/*')), [])

    def test_sohu_cache_metadata_restart_and_forming_bar_preserved(self):
        response,_=sohu_response(INDICES[0],self.rows);get=Mock(return_value=response)
        source=SohuIndexSource(get=get)
        service=IndexMarketService(self.root/'sohu_daily',self.root/'calendar',source=source,clock=lambda:self.now)
        data=service.get_index(INDICES[0].code)
        self.assertEqual(data['source'],SOHU_SOURCE)
        self.assertEqual(data['source_url'],SOHU_ENDPOINT)
        self.assertIn('搜狐',data['source_label'])
        self.assertEqual(data['latest']['state'],'forming')
        self.assertEqual(data['analysis_date'],self.rows[-2]['date'])
        restored=IndexMarketService(self.root/'sohu_daily',self.root/'calendar',source=source,clock=lambda:self.now)
        self.assertEqual(restored.get_index(INDICES[0].code)['ma'],data['ma'])
        get.assert_called_once()

    def test_legacy_eastmoney_cache_keeps_origin_when_sohu_refresh_fails(self):
        self.service.get_index(INDICES[0].code)
        path=self.service._path(INDICES[0]);before=path.read_bytes()
        self.now+=timedelta(minutes=5)
        source=SohuIndexSource(get=Mock(side_effect=OSError('sohu offline')))
        service=IndexMarketService(path.parent,self.root/'calendar',source=source,clock=lambda:self.now)
        data=service.get_index(INDICES[0].code)
        self.assertEqual(data['source'],SOURCE)
        self.assertEqual(data['source_url'],ENDPOINT)
        self.assertIn('东方财富',data['source_label'])
        self.assertEqual(data['availability'],'stale')
        self.assertEqual(path.read_bytes(),before)

    def test_default_source_is_explicit_tencent_not_auto_switch(self):
        service=IndexMarketService(self.root/'default_daily',self.root/'calendar',clock=lambda:self.now)
        self.assertIsInstance(service.source,TencentIndexSource)
        with patch.object(service.source,'fetch',side_effect=OSError('offline')) as fetch, \
             patch('index_market.EastmoneyIndexSource.fetch',side_effect=AssertionError('no fallback')):
            data=service.get_index(INDICES[0].code)
        self.assertEqual(data['availability'],'unavailable')
        self.assertEqual(data['source'],TENCENT_SOURCE);fetch.assert_called_once()

    def test_ttl_expiry_retries_and_same_day_revision_refresh(self):
        self.service.get_index(INDICES[0].code)
        self.now += timedelta(seconds=299)
        self.service.get_index(INDICES[0].code)
        self.assertEqual(self.source.fetch.call_count,1)
        self.now += timedelta(seconds=1)
        self.rows = [dict(r) for r in self.rows]
        self.rows[-1]['amount'] += 1000
        result = self.service.get_index(INDICES[0].code)
        self.assertEqual(self.source.fetch.call_count,2)
        self.assertEqual(result['latest']['amount'],200001000)

    def test_failure_preserves_cache_reports_stale_and_throttles_retry(self):
        self.service.get_index(INDICES[0].code)
        path = self.service._path(INDICES[0]); before = path.read_bytes()
        self.now += timedelta(minutes=5)
        self.source.fetch.side_effect = OSError('offline')
        result = self.service.get_index(INDICES[0].code)
        self.assertEqual(result['availability'],'stale')
        self.assertIn('offline', result['error'])
        self.assertEqual(before,path.read_bytes())
        self.service.get_index(INDICES[0].code)
        self.assertEqual(self.source.fetch.call_count,2)
        self.now += timedelta(minutes=1)
        self.service.get_index(INDICES[0].code)
        self.assertEqual(self.source.fetch.call_count,3)

    def test_first_failure_unavailable_without_synthetic_fallback(self):
        self.source.fetch.side_effect = TimeoutError('test timeout')
        result = self.service.get_index(INDICES[0].code)
        self.assertEqual(result['availability'],'unavailable')
        self.assertEqual(result['chart'],[])
        self.assertEqual(result['trend'],'数据不足')
        self.assertFalse((self.root/'index_daily').exists())

    def test_recent_fetch_old_market_date_is_not_fresh(self):
        self.rows = self.rows[:-3]
        result = self.service.get_index(INDICES[0].code)
        self.assertEqual(result['availability'],'stale')
        self.assertIsNotNone(result['fetched_at'])

    def test_invalid_future_batch_cannot_overwrite_cache(self):
        self.service.get_index(INDICES[0].code)
        path = self.service._path(INDICES[0]); before = path.read_bytes()
        self.rows = self.rows + [{**self.rows[-1], 'date':(self.now.date()+timedelta(days=1)).isoformat()}]
        self.now += timedelta(minutes=5)
        self.assertEqual(self.service.get_index(INDICES[0].code)['availability'],'stale')
        self.assertEqual(before,path.read_bytes())

    def test_cache_identity_frequency_and_corruption_cannot_be_used(self):
        self.service.get_index(INDICES[0].code)
        path = self.service._path(INDICES[0]); original = json.loads(path.read_text())
        for key,value in [('frequency','1MIN'),('source','pytdx'),('index',{}),('units',{})]:
            with self.subTest(key=key):
                packet = {**original,key:value}; path.write_text(json.dumps(packet))
                other = IndexMarketService(path.parent,self.root/'calendar',source=Mock(
                    fetch=Mock(side_effect=OSError('offline'))),clock=lambda:self.now)
                self.assertEqual(other.get_index(INDICES[0].code)['availability'],'unavailable')

    def test_non_object_or_truncated_cache_fails_closed_not_http500(self):
        path = self.service._path(INDICES[0]); path.parent.mkdir()
        for text in ('null', '[1,2]', '{"schema":'):
            path.write_text(text)
            other = IndexMarketService(path.parent,self.root/'calendar',source=Mock(
                fetch=Mock(side_effect=OSError('offline'))),clock=lambda:self.now)
            self.assertEqual(other.get_index(INDICES[0].code)['availability'],'unavailable')

    def test_one_index_failure_does_not_contaminate_other_index(self):
        def fetch(spec, now):
            if spec.code == INDICES[0].code:
                raise OSError('one index offline')
            return self.rows
        self.source.fetch.side_effect = fetch
        self.assertEqual(self.service.get_index(INDICES[0].code)['availability'],'unavailable')
        self.assertEqual(self.service.get_index(INDICES[1].code)['availability'],'fresh')

    def test_atomic_write_failure_preserves_existing_daily_cache(self):
        self.service.get_index(INDICES[0].code)
        path = self.service._path(INDICES[0]); before = path.read_bytes()
        self.now += timedelta(minutes=5)
        with patch('index_market.os.replace',side_effect=OSError('disk')):
            result = self.service.get_index(INDICES[0].code)
        self.assertIn('缓存保存失败',result['error'])
        self.assertEqual(before,path.read_bytes())
        self.assertFalse(list(path.parent.glob('*.tmp')))

    def test_missing_calendar_unknown_not_weekday_fallback(self):
        self.service.calendar_dir = self.root/'missing'
        result = self.service.get_index(INDICES[0].code)
        self.assertEqual(result['availability'],'unknown')
        self.assertEqual(result['trend'],'数据不足')

    def test_independent_index_keys_and_unknown_code(self):
        for spec in INDICES:
            self.assertEqual(self.service.get_index(spec.code)['code'],spec.code)
        self.assertEqual(self.source.fetch.call_count,4)
        self.assertEqual(len(list((self.root/'index_daily').glob('*.json'))),4)
        with self.assertRaises(KeyError): self.service.get_index('000001.SZ')

    def test_phase_crossing_refresh_and_rest_cache_not_wall_clock_stale(self):
        self.now = self.now.replace(hour=10,minute=0)
        self.service.get_index(INDICES[0].code)
        self.now = self.now.replace(hour=12,minute=0)
        self.service.get_index(INDICES[0].code)
        self.assertEqual(self.source.fetch.call_count,2)
        self.now = self.now.replace(hour=16)
        self.service.get_index(INDICES[0].code)
        self.now += timedelta(hours=1)
        self.assertEqual(self.service.get_index(INDICES[0].code)['availability'],'fresh')
        self.assertEqual(self.source.fetch.call_count,3)
        self.now += timedelta(days=1)
        self.service.get_index(INDICES[0].code)
        self.assertEqual(self.source.fetch.call_count,4)

    def load_web(self):
        spec = importlib.util.spec_from_file_location('p1_existing_web',ROOT/'webapp'/'app.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.app.template_folder = str(ROOT/'webapp'/'templates')
        module.app.testing = True
        return module

    def test_flask_original_page_renders_without_starting_network_or_scanner(self):
        with patch('index_market.EastmoneyIndexSource.fetch',side_effect=AssertionError('no network')):
            web = self.load_web()
            client = web.app.test_client()
            html = client.get('/').get_data(as_text=True)
            self.assertIn('大盘晴雨表',html)
            for old in ['sidebarContent','klineChart','stockContent','MACD','KDJ']:
                self.assertIn(old,html)
            self.assertNotIn('index_market',web.app.extensions)
            self.assertEqual(len(client.get('/api/market/indices').json['indices']),4)
            self.assertEqual(client.get('/api/market/index/000001.SZ').status_code,404)

    def test_flask_index_api_success_failure_and_legacy_readonly_routes(self):
        web = self.load_web(); web.app.extensions['index_market'] = self.service
        client = web.app.test_client()
        success = client.get('/api/market/index/000001.SH')
        self.assertEqual(success.status_code,200)
        self.assertEqual(success.json['trend'],'偏强')
        self.assertEqual(success.json['latest']['state'],'forming')
        self.assertEqual(success.json['analysis_bar_state'],'closed')
        self.source.fetch.side_effect = OSError('mock failure')
        failure = client.get('/api/market/index/399006.SZ')
        self.assertEqual(failure.status_code,200)
        self.assertEqual(failure.json['availability'],'unavailable')
        self.assertIn('mock failure',failure.json['error'])
        with patch.object(web,'check_daemon',return_value=False), patch.object(web,'get_today_log',return_value=None), \
             patch.object(web,'TICKERS_1',str(self.root/'p1')),patch.object(web,'TICKERS_2',str(self.root/'p2')):
            for route in ['/api/status','/api/pools','/api/signals','/api/log/tail',
                          '/api/stock/600000.SH/bars','/api/stock/600000.SH/latest']:
                self.assertEqual(client.get(route).status_code,200)


if __name__ == '__main__':
    unittest.main()
