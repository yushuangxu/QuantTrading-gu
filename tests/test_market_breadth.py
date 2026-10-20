"""Offline all-A statistics/source/cache/API tests; no real quote connections."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from market_data_state import TradingCalendar, SHANGHAI
from market_breadth import (BreadthError, calculate_breadth, normalize_quotes,
                           security_identity, distribution_index, a_share_exchange)
from breadth_data import (BreadthSettings, EastmoneyBreadthSource, BreadthService,
                          SOURCE, ENDPOINT, atomic_json, _quote)

NOW=datetime(2026,10,19,14,50,tzinfo=SHANGHAI)
DAY=NOW.date().isoformat()
CAL=TradingCalendar({(NOW-timedelta(days=i)).date(): i in {0,3} for i in range(10)})


def quote(code='600000',exchange='SH',change=1.,**kw):
    return {'code':code,'exchange':exchange,'security_type':'A_STOCK','name':'离线样本',
            'price':100+change if change is not None else 100.,'previous_close':100.,
            'change_pct':change,'volume':100.,'amount':1000.,'suspended':None,
            'quote_at':NOW.isoformat(),**kw}


def packet(rows=None):
    rows=deepcopy(rows if rows is not None else [quote(),quote('000001','SZ',-1),quote('920001','BJ',0)])
    return {'schema':1,'source':SOURCE,'source_url':ENDPOINT,'scope':['SH','SZ','BJ'],
            'amount_unit':'CNY','change_unit':'percent','requested_at':(NOW-timedelta(seconds=1)).isoformat(),
            'fetched_at':NOW.isoformat(),'provider_total':len(rows),'rows':rows}


def benchmark(rows=None):
    return {'trade_date':DAY,'source':'offline/independent-universe',
            'verification_reference':'TEST ONLY: explicit dated constituent list',
            'identities':[security_identity(row) for row in (rows or packet()['rows'])]}


def raw_quote(code='600000',market=1):
    return {'f12':code,'f13':market,'f14':'离线样本','f2':101,'f18':100,
            'f3':1,'f5':100,'f6':1000,'f124':int(NOW.timestamp())}


class TestBreadthPure(unittest.TestCase):
    def test_a_identity_requires_explicit_market_and_type(self):
        self.assertEqual(security_identity(quote()),'600000.SH')
        self.assertEqual(security_identity(quote('920001','BJ')),'920001.BJ')
        for value in ('600000.SH','600000','000001.SZ'):
            row=quote();row['code']=value
            if value!='600000':
                with self.assertRaises(BreadthError):security_identity(row)
        with self.assertRaises(BreadthError):security_identity({**quote(),'exchange':None})
        with self.assertRaises(BreadthError):security_identity({**quote(),'exchange':'SZ'})

    def test_funds_b_shares_index_cdr_and_legacy_bj_not_blindly_admitted(self):
        rows=packet()['rows']+[quote('510300','SH',security_type='ETF'),quote('900001','SH',security_type='B_STOCK'),
             quote('000001','SH',security_type='INDEX'),quote('159915','SZ',security_type='FUND'),
             quote('689009','SH'),quote('430001','BJ')]
        result=calculate_breadth(packet(rows),CAL)
        self.assertEqual(result['quality']['a_share_count'],3)
        self.assertEqual(result['quality']['non_a_filtered'],6)
        self.assertEqual(result['stats']['observed_amount_cny'],3000)

    def test_st_and_new_listing_are_not_name_filtered(self):
        p=packet();p['rows'][0]['name']='*ST离线';p['rows'][1]['name']='N离线'
        self.assertEqual(calculate_breadth(p,CAL)['stats']['valid_count'],3)

    def test_up_down_flat_denominator_and_direction_ratio(self):
        d=calculate_breadth(packet(),CAL)
        self.assertEqual((d['stats']['up'],d['stats']['down'],d['stats']['flat']),(1,1,1))
        self.assertAlmostEqual(d['stats']['advance_pct'],100/3)
        self.assertEqual(d['stats']['up_down_ratio'],1)
        self.assertEqual(d['stats']['up_down_text'],'1 : 1')
        self.assertEqual(sum(x['count'] for x in d['direction_distribution']),3)

    def test_bin_boundaries_are_mutually_exclusive_and_thresholds_strict(self):
        values=[-6,-5,-3.1,-3,-.01,0,.01,3,3.01,5,5.01]
        rows=[]
        for i,value in enumerate(values):
            ex=['SH','SZ','BJ'][i%3];prefix={'SH':'600','SZ':'000','BJ':'920'}[ex]
            rows.append(quote(prefix+f'{100+i:03}',ex,value))
        d=calculate_breadth(packet(rows),CAL)
        self.assertEqual([x['count'] for x in d['change_distribution']],[1,2,2,1,2,2,1])
        self.assertEqual(sum(x['count'] for x in d['change_distribution']),len(values))
        self.assertEqual([d['stats'][k] for k in ('up_gt3','up_gt5','down_lt_neg3','down_lt_neg5')],[3,1,3,1])
        self.assertEqual(distribution_index(-5),1);self.assertEqual(distribution_index(-3),2)

    def test_no_trade_and_explicit_suspension_are_not_flat(self):
        p=packet(packet()['rows']+[quote('600001',change=0,volume=0,amount=0),
                   quote('600002',change=0,suspended=True,amount=0)])
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['stats']['flat'],1)
        self.assertEqual(d['quality']['excluded'],{'no_trade':1,'suspended':1})
        self.assertEqual(d['stats']['valid_count'],3)

    def test_halt_after_trading_keeps_dated_amount_but_not_percentage(self):
        p=packet(packet()['rows']+[quote('600001',suspended=True,amount=500)])
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['stats']['observed_amount_cny'],3500)
        self.assertEqual(d['stats']['valid_count'],3)

    def test_old_halt_counters_not_summed_as_today(self):
        p=packet(packet()['rows']+[quote('600001',suspended=True,amount=9999,
                   quote_at=(NOW-timedelta(days=3)).isoformat())])
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['stats']['observed_amount_cny'],3000)
        self.assertEqual(d['quality']['amount_missing_count'],1)
        self.assertIsNone(d['stats']['all_a_amount_cny'])

    def test_missing_previous_close_and_change_separate_not_flat(self):
        rows=packet()['rows']+[quote('600001',previous_close=0),quote('600002',change=None)]
        d=calculate_breadth(packet(rows),CAL)
        self.assertEqual(d['quality']['excluded'],{'no_previous_close':1,'missing_change':1})
        self.assertEqual(d['stats']['valid_count'],3)
        self.assertEqual(d['stats']['flat'],1)
        # Valid amount is independent of percentage eligibility.
        self.assertEqual(d['stats']['observed_amount_cny'],5000)

    def test_missing_amount_and_activity_conflict_degrade_coverage(self):
        rows=packet()['rows']+[quote('600001',amount=None),quote('600002',volume=0)]
        d=calculate_breadth(packet(rows),CAL)
        self.assertEqual(d['quality']['amount_missing_count'],1)
        self.assertEqual(d['quality']['excluded'],{'missing_activity':1,'activity_conflict':1})
        self.assertIsNone(d['stats']['all_a_amount_cny'])

    def test_percentage_consistency_does_not_silently_fix_bad_values(self):
        p=packet();p['rows'][0]['change_pct']=.01
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['quality']['excluded'],{'change_conflict':1})
        self.assertEqual(d['stats']['up'],0)

    def test_exact_duplicates_dedup_before_amount_conflict_rejected(self):
        rows=packet()['rows'];rows.append(deepcopy(rows[0]))
        d=calculate_breadth(packet(rows),CAL)
        self.assertEqual(d['stats']['observed_amount_cny'],3000)
        self.assertEqual(d['quality']['exact_duplicates'],1)
        rows[-1]['amount']=1001
        with self.assertRaises(BreadthError):calculate_breadth(packet(rows),CAL)

    def test_amount_units_convert_explicitly_and_unknown_rejected(self):
        p=packet();p['amount_unit']='CNY_10K'
        self.assertEqual(calculate_breadth(p,CAL)['stats']['observed_amount_cny'],30000000)
        p['amount_unit']='unknown'
        with self.assertRaises(BreadthError):calculate_breadth(p,CAL)
        p=packet();p['change_unit']='fraction'
        with self.assertRaises(BreadthError):calculate_breadth(p,CAL)

    def test_invalid_numeric_values_are_not_zero_filled(self):
        for key,value in [('amount',-1),('amount',True),('change_pct',float('nan')),('price',float('inf'))]:
            p=packet();p['rows'][0][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(BreadthError):calculate_breadth(p,CAL)

    def test_dates_missing_mixed_future_and_preopen_are_rejected(self):
        for stamp in [None,(NOW-timedelta(days=3)).isoformat(),(NOW+timedelta(seconds=1)).isoformat(),
                      NOW.replace(hour=9,minute=0).isoformat()]:
            p=packet();p['rows'][0]['quote_at']=stamp
            with self.subTest(stamp=stamp),self.assertRaises(BreadthError):calculate_breadth(p,CAL)
        with self.assertRaises(BreadthError):calculate_breadth(packet(),TradingCalendar())

    def test_old_zero_trade_timestamp_not_joined_to_live_distribution(self):
        p=packet(packet()['rows']+[quote('600001',change=0,volume=0,amount=0,
                   quote_at=(NOW-timedelta(days=3)).isoformat())])
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['trade_date'],DAY)
        self.assertEqual(d['quality']['excluded']['no_trade'],1)

    def test_time_skew_and_collection_duration_fail_closed(self):
        p=packet();p['rows'][0]['quote_at']=(NOW-timedelta(seconds=31)).isoformat()
        with self.assertRaises(BreadthError):calculate_breadth(p,CAL)
        p=packet();p['requested_at']=(NOW-timedelta(seconds=21)).isoformat()
        with self.assertRaises(BreadthError):calculate_breadth(p,CAL)
        p=packet();p['requested_at']=(NOW+timedelta(seconds=1)).isoformat()
        with self.assertRaises(BreadthError):calculate_breadth(p,CAL)

    def test_no_independent_universe_cannot_claim_100_percent(self):
        d=calculate_breadth(packet(),CAL)
        self.assertEqual(d['quality']['provider_pagination_pct'],100)
        self.assertIsNone(d['coverage']['quote_coverage_pct'])
        self.assertFalse(d['coverage']['verified_complete'])
        self.assertEqual(d['availability'],'degraded')
        self.assertIsNone(d['stats']['all_a_amount_cny'])

    def test_verified_universe_coverage_distinct_from_effective_coverage(self):
        p=packet();p['rows'][2].update(volume=0,amount=0)
        d=calculate_breadth(p,CAL,universe=benchmark())
        self.assertEqual(d['coverage']['quote_coverage_pct'],100)
        self.assertAlmostEqual(d['coverage']['valid_change_coverage_pct'],200/3)
        self.assertEqual(d['stats']['all_a_amount_cny'],2000)
        universe=benchmark();universe['identities'].append('600999.SH')
        d=calculate_breadth(packet(),CAL,universe=universe)
        self.assertEqual(d['coverage']['quote_coverage_pct'],75)
        self.assertEqual(d['coverage']['missing_count'],1)
        self.assertIsNone(d['stats']['all_a_amount_cny'])

    def test_outdated_or_invalid_benchmark_not_implicitly_trusted(self):
        u=benchmark();u['trade_date']='2026-10-16'
        self.assertIsNone(calculate_breadth(packet(),CAL,universe=u)['coverage']['quote_coverage_pct'])
        u=benchmark();u['verification_reference']=''
        with self.assertRaises(BreadthError):calculate_breadth(packet(),CAL,universe=u)

    def test_partial_page_or_missing_exchange_not_published(self):
        p=packet();p['provider_total']=100
        with self.assertRaises(BreadthError):calculate_breadth(p,CAL)
        with self.assertRaises(BreadthError):calculate_breadth(packet(packet()['rows'][:2]),CAL)

    def test_zero_denominator_and_no_decliners_never_emit_infinity(self):
        p=packet()
        for row in p['rows']:row.update(price=101,change_pct=1)
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['stats']['advance_pct'],100)
        self.assertIsNone(d['stats']['up_down_ratio'])
        p=packet()
        for row in p['rows']:row['previous_close']=None
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['stats']['valid_count'],0)
        self.assertIsNone(d['stats']['advance_pct'])
        json.dumps(d,allow_nan=False)

    def test_after_close_is_not_automatically_final(self):
        p=packet();p['fetched_at']=NOW.replace(hour=16).isoformat();p['requested_at']=p['fetched_at']
        d=calculate_breadth(p,CAL)
        self.assertEqual(d['completion_state'],'unknown')
        self.assertEqual(d['snapshot_kind'],'unknown')
        self.assertIsNone(d['stats']['amount_change_pct'])

    def test_pure_calculation_does_not_mutate_input(self):
        p=packet();before=deepcopy(p);calculate_breadth(p,CAL)
        self.assertEqual(p,before)


class TestExactAShareIdentity(unittest.TestCase):
    """Official identity evidence; OHLC/amount here remain synthetic/offline."""

    def test_exact_code_matches_archived_szse_a_share_evidence(self):
        root=Path(__file__).resolve().parents[1]
        fixture=json.loads((root/'tests/fixtures/szse_302132_identity.json').read_text(encoding='utf-8'))
        archived=(root/fixture['archive']).read_bytes()
        self.assertEqual(hashlib.sha256(archived).hexdigest(),fixture['archive_sha256'])
        matches=[r for r in json.loads(archived) if r['A股代码']=='302132']
        self.assertEqual(len(matches),1)
        self.assertEqual({k:matches[0][k] for k in fixture['row']},fixture['row'])
        self.assertEqual(a_share_exchange(fixture['row']['A股代码']),'SZ')
        self.assertEqual(security_identity(quote('302132','SZ')),'302132.SZ')

    def test_exact_exception_does_not_authorize_entire_302_range(self):
        for suffix in range(1000):
            code=f'302{suffix:03}'
            if code=='302132':continue
            with self.subTest(code=code):
                self.assertIsNone(a_share_exchange(code))
                self.assertIsNone(security_identity(quote(code,'SZ')))
                self.assertEqual(_quote(raw_quote(code,0))['security_type'],'OTHER')

    def test_source_fetch_normalization_keeps_exact_stock_in_statistics(self):
        raw=[raw_quote(),raw_quote('302132',0),raw_quote('300001',0),raw_quote('920001',0)]
        response=Mock();response.json.return_value={'data':{'total':len(raw),'diff':raw}}
        source=EastmoneyBreadthSource(get=Mock(return_value=response),clock=lambda:NOW)
        p=source.fetch()
        row=next(r for r in p['rows'] if r['code']=='302132')
        self.assertEqual((row['exchange'],row['security_type']),('SZ','A_STOCK'))
        result=calculate_breadth(p,CAL)
        self.assertEqual(result['quality']['by_exchange'],{'SH':1,'SZ':2,'BJ':1})
        self.assertEqual(result['stats']['valid_count'],4)
        self.assertEqual(result['stats']['up'],4)
        self.assertEqual(result['stats']['observed_amount_cny'],4000)
        self.assertEqual(result['quality']['non_a_filtered'],0)
        source._get.assert_called_once();response.close.assert_called_once()

    def test_wrong_markets_rejected_without_silently_relabeling(self):
        for exchange in ('SH','BJ',None,'SZSE'):
            with self.subTest(exchange=exchange),self.assertRaises(BreadthError):
                security_identity(quote('302132',exchange))
        for code,market in (('302132',1),('600000',0),('300001',1),('920001',1)):
            with self.subTest(code=code,market=market),self.assertRaises(BreadthError):
                _quote(raw_quote(code,market))
        for market in (None,2,False,'0'):
            with self.subTest(market=market),self.assertRaises(BreadthError):
                _quote(raw_quote('302132',market))

    def test_security_type_still_required_and_non_stock_types_excluded(self):
        for security_type in ('ETF','FUND','B_STOCK','INDEX','CDR','OTHER',None):
            with self.subTest(security_type=security_type):
                self.assertIsNone(security_identity(quote('302132','SZ',security_type=security_type)))
        for code,market in (('510300',1),('159915',0),('900901',1),('200002',0),
                            ('399006',0),('689009',1),('430001',0)):
            with self.subTest(code=code):
                row=_quote(raw_quote(code,market))
                self.assertEqual(row['security_type'],'OTHER')
                self.assertIsNone(security_identity(row))
        # SSE index 000001 conflicts with SZ stock code: never counted as A.
        with self.assertRaises(BreadthError):_quote(raw_quote('000001',1))

    def test_existing_board_examples_keep_their_original_identity(self):
        for code,exchange,market in (('600000','SH',1),('601318','SH',1),('603000','SH',1),
                ('605001','SH',1),('688001','SH',1),('000001','SZ',0),('001001','SZ',0),
                ('002001','SZ',0),('003001','SZ',0),('300001','SZ',0),('301001','SZ',0),('920001','BJ',0)):
            with self.subTest(code=code):
                self.assertEqual(a_share_exchange(code),exchange)
                self.assertEqual(security_identity(_quote(raw_quote(code,market))),code+'.'+exchange)

    def test_new_identity_keeps_dedup_amount_and_conflict_checks(self):
        row=quote('302132','SZ',2,amount=1500)
        rows=packet()['rows']+[row,deepcopy(row)]
        result=calculate_breadth(packet(rows),CAL)
        self.assertEqual(result['quality']['exact_duplicates'],1)
        self.assertEqual(result['stats']['valid_count'],4)
        self.assertEqual((result['stats']['up'],result['stats']['down'],result['stats']['flat']),(2,1,1))
        self.assertEqual(result['stats']['observed_amount_cny'],4500)
        rows[-1]['amount']=1600
        with self.assertRaisesRegex(BreadthError,'重复修订冲突'):
            calculate_breadth(packet(rows),CAL)

    def test_exact_identity_does_not_fabricate_universe_coverage(self):
        rows=packet()['rows']+[quote('302132','SZ')]
        result=calculate_breadth(packet(rows),CAL)
        self.assertIsNone(result['coverage']['quote_coverage_pct'])
        self.assertFalse(result['coverage']['verified_complete'])
        self.assertIsNone(result['stats']['all_a_amount_cny'])
        self.assertEqual(result['availability'],'degraded')
        # Existing explicit benchmark seam shares the same identity guard.
        universe=benchmark(rows);universe['identities'].append('600999.SH')
        checked=calculate_breadth(packet(rows),CAL,universe=universe)
        self.assertEqual(checked['coverage']['quote_coverage_pct'],80)
        self.assertFalse(checked['coverage']['verified_complete'])


class TestBreadthSource(unittest.TestCase):
    def source(self, pages, **settings):
        def get(url,params,timeout):
            response=Mock();response.json.return_value=pages[int(params['pn'])]
            return response
        return EastmoneyBreadthSource(BreadthSettings(**settings),get=Mock(side_effect=get),clock=lambda:NOW)

    def test_current_bj_mapping_and_volume_amount_are_not_confused(self):
        row=_quote(raw_quote('920992',0))
        self.assertEqual(row['exchange'],'BJ');self.assertEqual(row['amount'],1000)
        self.assertEqual(row['volume'],100);self.assertIsNone(row['suspended'])

    def test_bounded_pagination_and_explicit_timeout(self):
        raw=[raw_quote(),raw_quote('000001',0),raw_quote('920001',0)]
        source=self.source({1:{'data':{'total':3,'diff':raw[:2]}},2:{'data':{'total':3,'diff':raw[2:]}}},page_size=2)
        p=source.fetch();self.assertEqual(p['provider_total'],3)
        self.assertEqual(len(p['rows']),3);self.assertEqual(len(p['pages']),2)
        for call in source._get.call_args_list:
            self.assertEqual(call.kwargs['timeout'],(2.,5.))
            self.assertEqual(call.kwargs['params']['fid'],'f12')
        self.assertEqual(calculate_breadth(p,CAL)['stats']['valid_count'],3)

    def test_count_change_empty_and_truncated_page_rejected(self):
        for page in [{'total':4,'diff':[raw_quote('920001',0)]},
                     {'total':3,'diff':[]}, {'total':3,'diff':[raw_quote('920001',0)]*2}]:
            with self.subTest(page=page):
                source=self.source({1:{'data':{'total':3,'diff':[raw_quote(),raw_quote('000001',0)]}},2:{'data':page}},page_size=2)
                with self.assertRaises(BreadthError):source.fetch()

    def test_page_budget_stops_before_mass_requests(self):
        source=self.source({1:{'data':{'total':1000,'diff':[raw_quote()]}}},max_pages=2)
        with self.assertRaises(BreadthError):source.fetch()
        self.assertEqual(source._get.call_count,1)

    def test_missing_date_not_invented_from_request_time(self):
        raw=raw_quote();raw.pop('f124')
        source=self.source({1:{'data':{'total':3,'diff':[raw,raw_quote('000001',0),raw_quote('920001',0)]}}})
        p=source.fetch();self.assertIsNone(p['rows'][0]['quote_at'])
        with self.assertRaises(BreadthError):calculate_breadth(p,CAL)

    def test_http_failure_closes_response_and_does_not_retry(self):
        response=Mock();response.raise_for_status.side_effect=OSError('offline')
        source=EastmoneyBreadthSource(get=Mock(return_value=response),clock=lambda:NOW)
        with self.assertRaises(OSError):source.fetch()
        response.close.assert_called_once();source._get.assert_called_once()

    def test_invalid_source_fields_and_excessive_concurrency_fail(self):
        with self.assertRaises(BreadthError):_quote(raw_quote('920001',2))
        with self.assertRaises(ValueError):BreadthSettings(workers=100)
        with self.assertRaises(ValueError):BreadthSettings(read_timeout=0)

    def test_deadline_exceeded_is_not_a_successful_snapshot(self):
        source=self.source({1:{'data':{'total':1,'diff':[raw_quote()]}}})
        values=iter([0,0,21]);source.monotonic=lambda:next(values)
        with self.assertRaises(BreadthError):source.fetch()


class TestBreadthCacheAndAPI(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);calendar=self.root/'calendar';calendar.mkdir()
        (calendar/'trade_cal_20261001_20261031.json').write_text(json.dumps({'open_days':['20261016','20261019']}))
        self.now=NOW;self.p=packet();self.source=Mock(fetch=Mock(side_effect=lambda:deepcopy(self.p)))
        self.service=BreadthService(self.root/'breadth',calendar,source=self.source,clock=lambda:self.now)

    def test_cache_reuse_and_restart_no_repeated_market_download(self):
        d=self.service.get_snapshot();self.assertEqual(d['availability'],'degraded')
        self.service.get_snapshot();self.source.fetch.assert_called_once()
        other=BreadthService(self.root/'breadth',self.root/'calendar',source=self.source,clock=lambda:self.now)
        self.assertEqual(other.get_snapshot()['stats'],d['stats']);self.source.fetch.assert_called_once()
        self.assertFalse((self.root/'cache').exists());self.assertFalse((self.root/'index_daily').exists())

    def test_expired_fetch_failure_retains_old_snapshot_and_cooldown(self):
        first=self.service.get_snapshot();path=self.root/'breadth/latest.json';before=path.read_bytes()
        self.now+=timedelta(seconds=180);self.source.fetch.side_effect=OSError('offline')
        d=self.service.get_snapshot();self.assertEqual(d['availability'],'stale')
        self.assertEqual(d['stats'],first['stats']);self.assertEqual(path.read_bytes(),before)
        self.assertEqual(d['fetched_at'],first['fetched_at'])
        self.service.get_snapshot();self.assertEqual(self.source.fetch.call_count,2)
        self.now+=timedelta(seconds=180);self.service.get_snapshot();self.assertEqual(self.source.fetch.call_count,3)

    def test_first_failure_unavailable_and_failure_cooldown_survives_restart(self):
        self.source.fetch.side_effect=OSError('offline')
        d=self.service.get_snapshot();self.assertIsNone(d['stats']);self.assertEqual(d['availability'],'unavailable')
        self.assertFalse((self.root/'breadth/latest.json').exists())
        other=BreadthService(self.root/'breadth',self.root/'calendar',source=self.source,clock=lambda:self.now)
        other.get_snapshot();self.assertEqual(self.source.fetch.call_count,1)

    def test_bad_data_cannot_replace_existing_valid_cache(self):
        self.service.get_snapshot();path=self.root/'breadth/latest.json';before=path.read_bytes()
        self.now+=timedelta(seconds=180);self.p['provider_total']=999
        d=self.service.get_snapshot();self.assertEqual(d['availability'],'stale')
        self.assertEqual(before,path.read_bytes());self.assertIn('分页',d['error'])

    def test_atomic_replace_failure_preserves_previous_file(self):
        self.service.get_snapshot();path=self.root/'breadth/latest.json';before=path.read_bytes()
        with patch('breadth_data.os.replace',side_effect=OSError('disk')):
            with self.assertRaises(OSError):atomic_json(path,{'replacement':'bad'})
        self.assertEqual(before,path.read_bytes());self.assertFalse(list(path.parent.glob('*.tmp')))

    def test_unknown_calendar_never_uses_weekday_fallback(self):
        self.service.calendar_dir=self.root/'missing'
        d=self.service.get_snapshot();self.assertIsNone(d['stats']);self.assertIn('日历',d['error'])

    def test_concurrent_refresh_does_not_start_second_scan(self):
        self.service._lock.acquire()
        try:
            d=self.service.get_snapshot();self.assertEqual(d['availability'],'loading')
            self.source.fetch.assert_not_called()
        finally:self.service._lock.release()

    def test_recent_request_returning_old_trading_day_is_stale(self):
        for row in self.p['rows']:row['quote_at']=(NOW-timedelta(days=3)).isoformat()
        d=self.service.get_snapshot();self.assertEqual(d['availability'],'stale')
        self.assertEqual(d['trade_date'],'2026-10-16')

    def test_rest_does_not_mark_last_session_stale_just_because_older(self):
        self.now=NOW+timedelta(days=1)
        self.p['requested_at']=self.now.isoformat();self.p['fetched_at']=self.now.isoformat()
        for row in self.p['rows']:row['quote_at']=NOW.replace(hour=15).isoformat()
        d=self.service.get_snapshot();self.assertEqual(d['freshness'],'fresh')
        self.assertEqual(d['completion_state'],'unknown')

    def test_phase_transition_refresh_and_lunch_cache_reuse(self):
        self.now=NOW.replace(hour=11,minute=29)
        def update():
            p=packet();p['requested_at']=self.now.isoformat();p['fetched_at']=self.now.isoformat()
            for row in p['rows']:row['quote_at']=self.now.isoformat()
            return p
        self.source.fetch.side_effect=update;self.service.get_snapshot()
        self.now=self.now.replace(minute=30);self.service.get_snapshot()
        self.assertEqual(self.source.fetch.call_count,2)
        self.now+=timedelta(minutes=10);self.service.get_snapshot();self.assertEqual(self.source.fetch.call_count,2)

    def test_api_reuses_service_and_keeps_four_index_routes(self):
        from webapp import app as web
        from webapp.dashboard import application
        from werkzeug.test import Client
        from werkzeug.wrappers import Response
        old=web.app.extensions.get('market_breadth')
        web.app.extensions['market_breadth']=self.service
        try:
            client=Client(application,Response)
            d=client.get('/api/market/breadth');self.assertEqual(d.status_code,200)
            self.assertEqual(d.json['stats']['up'],1)
            page=client.get('/').get_data(as_text=True)
            for text in ('market/breadth','市场广度','breadthDistribution','chartStage','analysisRows'):
                self.assertIn(text,page)
            self.assertEqual(len(client.get('/api/market/indices').json['indices']),4)
            self.assertEqual(client.get('/api/market/index/not-an-index').status_code,404)
            self.assertNotIn('breadth.js',web.app.test_client().get('/').get_data(as_text=True))
        finally:
            if old is None:web.app.extensions.pop('market_breadth',None)
            else:web.app.extensions['market_breadth']=old

    def test_unavailable_api_keeps_null_values_not_synthetic_zeros(self):
        self.source.fetch.side_effect=OSError('offline')
        from webapp import app as web
        with patch.object(web,'get_breadth_service',return_value=self.service):
            d=web.app.test_client().get('/api/market/breadth').json
            self.assertEqual(d['availability'],'unavailable');self.assertIsNone(d['stats'])
            self.assertIsNone(d['coverage']['quote_coverage_pct'])


if __name__=='__main__':unittest.main()
