"""Opt-in, bounded, read-only candidate probes. No production source switch.

Preserve system proxies/TLS. Never infer a quote's date from acquisition time.
Imported diagnostic helpers do NOT execute their main/network workflow.
"""
from collections import Counter
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from breadth_data import atomic_json
from market_breadth import a_share_exchange
from market_data_state import SHANGHAI

OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('read_only_diagnostic',
    ROOT/'docs/phases/P1-02A-1_evidence/probe_data_link.py')
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)
diag.OUT=OUT;diag.LIMIT_REQUESTS=32;diag.LIMIT_SECONDS=120

TX='https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList'
SINA='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData'
COUNT='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount'


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):return None


def inspect(rows, source, total=None):
    ids=[];conflicts=[];unsupported=[];times=[];dated=[];fields=set();bad_pct=[]
    bad_amount=[];price_range_checks=[];exchanges=Counter();types=Counter()
    for row in rows:
        fields.update(row)
        symbol=row.get('code') if source=='tencent' else row.get('symbol')
        if not isinstance(symbol,str) or not re.fullmatch(r'(sh|sz|bj)\d{6}',symbol):
            unsupported.append(symbol);continue
        ex,code=symbol[:2].upper(),symbol[2:]
        if a_share_exchange(code)!=ex:
            unsupported.append(symbol);continue
        ids.append(code+'.'+ex);exchanges[ex]+=1
        types[str(row.get('stock_type','not_supplied'))]+=1
        if source=='sina':
            if row.get('code')!=code:conflicts.append(symbol)
            price,prev,pct=[number(row.get(k)) for k in ('trade','settlement','changepercent')]
            amount,volume,high,low=[number(row.get(k)) for k in ('amount','volume','high','low')]
            if price is not None and prev and pct is not None:
                if abs((price/prev-1)*100-pct)>.025:bad_pct.append(symbol)
            if amount is None or amount<0:bad_amount.append(symbol)
            if amount and volume and high and low:
                price_range_checks.append(low-.02<=amount/volume<=high+.02)
            if row.get('ticktime'):times.append(row['ticktime'])
        else:
            if number(row.get('turnover')) is None:bad_amount.append(symbol)
        # Only actual fields containing dates count as source date evidence.
        found=[(k,str(v)) for k,v in row.items() if isinstance(v,str) and
               re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?|\d{14}',v)]
        if found:dated.append({'symbol':symbol,'fields':found})
    duplicates=[k for k,v in Counter(ids).items() if v>1]
    return {'source':source,'returned_rows':len(rows),'provider_total':total,
            'recognized_unique_a_codes':len(set(ids)),'identities':ids,
            'exchange_counts':dict(exchanges),'security_type_values':dict(types),
            'unsupported_identities':unsupported,'identity_conflicts':conflicts,
            'duplicate_codes':duplicates,'field_names':sorted(fields),
            'time_only_min':min(times) if times else None,'time_only_max':max(times) if times else None,
            'time_only_distribution':dict(Counter(times)),
            'rows_with_explicit_date_fields':len(dated),'explicit_date_examples':dated[:3],
            'inconsistent_price_pct':bad_pct,'invalid_amount':bad_amount,
            'sina_amount_per_share_within_intraday_range':{
                'checked':len(price_range_checks),'passed':sum(price_range_checks)},
            'amount_unit':'CNY (AkShare documented, range cross-check)' if source=='sina' else 'not_verified',
            'full_source_date_proof':len(dated)==len(rows) and bool(rows)}


def page(source, page_no, label):
    if source=='tencent':
        params={'_appver':'11.17.0','board_code':'aStock','sort_type':'price','direct':'down',
                'offset':str((page_no-1)*200),'count':'200'}
        body=diag.request('GET',TX,label=label,params=params)
        parsed=json.loads(body);atomic_json(OUT/(label+'.json'),parsed)
        data=parsed['data'];rows=data['rank_list'];total=int(data['total'])
    else:
        params={'page':str(page_no),'num':'80','sort':'symbol','asc':'1','node':'hs_a','symbol':'','_s_r_a':'page'}
        body=diag.request('GET',SINA,label=label,params=params)
        parsed=json.loads(body);atomic_json(OUT/(label+'.json'),parsed)
        rows=parsed;total=None
    if not isinstance(rows,list) or not rows:raise ValueError('empty_or_invalid_page')
    return inspect(rows,source,total)


def batch_sample(source):
    codes='sh600000,sz302132,bj920000'
    if source=='tencent':
        body=diag.request('GET','https://qt.gtimg.cn/q='+codes,label='tencent_batch_sample')
    else:
        body=diag.request('GET','https://hq.sinajs.cn/list='+codes,label='sina_batch_sample',
                          headers={'Referer':'https://finance.sina.com.cn/'})
    text=body.decode('gb18030',errors='replace')
    atomic_json(OUT/(source+'_batch_sample.json'),{'raw_public_quote_response':text})
    records=[]
    pattern=r'(?:var\s+)?(?:v_|hq_str_)((?:sh|sz|bj)\d{6})\s*=\s*"([^"]*)"'
    for symbol,value in re.findall(pattern,text):
        parts=value.split('~' if source=='tencent' else ',')
        dates=[{'index':i,'value':v} for i,v in enumerate(parts)
               if re.fullmatch(r'\d{4}-\d{2}-\d{2}|\d{14}',v)]
        times=[{'index':i,'value':v} for i,v in enumerate(parts) if re.fullmatch(r'\d{2}:\d{2}:\d{2}',v)]
        records.append({'symbol':symbol,'field_count':len(parts),'date_fields':dates,'time_fields':times,
                        'nonempty':bool(value)})
    return {'records':records,'returned_records':len(records),'requested_symbols':codes.split(','),
            'mapping_status':'raw field positions observed; not a production adapter or complete universe'}


def main():
    report={'started_at':diag.stamp(),'budget':{'requests':32,'seconds':120,'concurrency':1},
            'environment':diag.environment(),'candidates':{},'complete_snapshots':0,
            'production_source_changed':False,'all_a_coverage_pct':None}
    try:
        for source in ('tencent','sina'):
            result={'single_pages':[],'small_pages':[],
                    'full_collection_attempted':False,'qualified_complete_snapshot':False}
            report['candidates'][source]=result
            for i in range(3):
                try:result['single_pages'].append({'ok':True,**page(source,1,f'{source}_single_{i+1}')})
                except Exception as exc:result['single_pages'].append({'ok':False,**diag.error_info(exc)})
            if all(p['ok'] for p in result['single_pages']):
                for pn in (2,3):
                    try:result['small_pages'].append({'ok':True,'page':pn,**page(source,pn,f'{source}_page_{pn}')})
                    except Exception as exc:
                        result['small_pages'].append({'ok':False,'page':pn,**diag.error_info(exc)});break
            if source=='sina' and len(result['small_pages'])==2 and all(p['ok'] for p in result['small_pages']):
                try:
                    raw=diag.request('GET',COUNT,label='sina_total_count',params={'node':'hs_a'})
                    data=json.loads(raw);atomic_json(OUT/'sina_total_count.json',data)
                    result['provider_count']=int(data)
                except Exception as exc:result['count_error']=diag.error_info(exc)
            # One batch per provider checks actual date fields and all three market symbols.
            try:result['same_provider_batch']=batch_sample(source)
            except Exception as exc:result['same_provider_batch']={'error':diag.error_info(exc)}
            sample=[p for p in result['single_pages'][-1:]+result['small_pages'] if p['ok']]
            ids=[i for p in sample for i in p['identities']]
            result['three_page_unique_a_codes']=len(set(ids))
            result['three_page_duplicates']=len(ids)-len(set(ids))
            result['field_sets_consistent']=len({tuple(p['field_names']) for p in sample})==1 if sample else None
            result['stop_reasons']=[]
            if len(sample)!=3:result['stop_reasons'].append('single_or_small_paging_not_stable')
            if sample and not all(p['full_source_date_proof'] for p in sample):
                result['stop_reasons'].append('rank_pages_lack_full_source_trade_date; client_date_not_substituted')
            if source=='tencent':result['stop_reasons'].append('rank_turnover_unit_and_explicit_previous_close_not_verified')
            if source=='sina' and 'provider_count' not in result:result['stop_reasons'].append('provider_page_count_not_verified')
            if result['three_page_duplicates']:result['stop_reasons'].append('cross_page_duplicate_identity')
            if any(p['unsupported_identities'] or p['identity_conflicts'] for p in sample):
                result['stop_reasons'].append('identity_conflicts_or_unsupported_codes')
            # Both statically inspected rank contracts lack date fields. No
            # full-fetch implementation is enabled by this diagnostic script.
            # If the schema changes unexpectedly, stop for review, not a crawl.
            if not result['stop_reasons']:
                result['stop_reasons'].append('unexpected_schema_change_requires_mapping_review_before_full_fetch')
    finally:
        report.update(finished_at=diag.stamp(),elapsed_seconds=round(time.monotonic()-diag.START,4),requests=diag.RECORDS)
        atomic_json(OUT/'diagnostic_results.json',report)
    print(json.dumps({'result_file':str(OUT/'diagnostic_results.json'),
                      'requests':len(diag.RECORDS),'elapsed_seconds':report['elapsed_seconds'],
                      'candidate_stop_reasons':{k:v['stop_reasons'] for k,v in report['candidates'].items()}},ensure_ascii=False))


if __name__=='__main__':main()
