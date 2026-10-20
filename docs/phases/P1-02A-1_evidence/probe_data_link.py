"""Opt-in finite read-only diagnostic; never imported by regression or Flask.

Run from project root with its Python. Keeps default requests proxy/TLS policy.
No proxy values, exception messages, request headers or credentials are emitted.
Only public market/list response bodies and sanitized metadata may be saved.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
from threading import Lock
import time
import traceback
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import requests
from requests.utils import get_environ_proxies, select_proxy
from breadth_data import (ENDPOINT, FILTER, FIELDS, BreadthSettings,
                          EastmoneyBreadthSource, _quote, atomic_json)
from market_breadth import normalize_quotes, calculate_breadth, security_identity
from market_data_state import SHANGHAI, TradingCalendar

OUT = Path(__file__).resolve().parent
START = time.monotonic()
LIMIT_SECONDS = 180
LIMIT_REQUESTS = 80
LOCK = Lock()
RECORDS = []


def stamp():
    return datetime.now(SHANGHAI).isoformat()


def error_info(exc):
    seen, types, frames = set(), [], []
    def visit(error):
        if not isinstance(error, BaseException) or id(error) in seen:
            return
        seen.add(id(error)); types.append(type(error).__name__)
        if error.__traceback__:
            frames.extend(f.name for f in traceback.extract_tb(error.__traceback__))
        visit(error.__cause__); visit(error.__context__)
        visit(getattr(error, 'reason', None))
        for arg in error.args:
            visit(arg)
    visit(exc)
    return {'exception_types': list(dict.fromkeys(types)),
            'trace_functions': list(dict.fromkeys(frames))}


def environment():
    keys = ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY',
            'http_proxy','https_proxy','all_proxy','no_proxy',
            'REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE')
    effective = get_environ_proxies(ENDPOINT)
    with requests.Session() as session:
        merged = session.merge_environment_settings(ENDPOINT, {}, False, True, None)
    info = {'python': sys.version.split()[0], 'requests': requests.__version__,
            'environment': {k: {'present': k in os.environ, 'nonempty': bool(os.environ.get(k))} for k in keys},
            'urllib_proxy_keys': sorted(urllib.request.getproxies()),
            'effective_proxy_keys': sorted(effective),
            'selected_proxy_enabled': bool(select_proxy(ENDPOINT, merged['proxies'])),
            'session_trust_env': True, 'tls_verification_enabled': bool(merged['verify']),
            'proxy_policy_changed': False}
    try:
        import _scproxy
        info['mac_system_proxy_value_present'] = bool(_scproxy._get_proxies())
    except ImportError:
        info['mac_system_proxy_value_present'] = None
    return info


def request(method, url, *, label, **kwargs):
    with LOCK:
        if len(RECORDS) >= LIMIT_REQUESTS or time.monotonic()-START > LIMIT_SECONDS-8:
            raise RuntimeError('finite diagnostic budget exhausted')
        record = {'label': label, 'method': method, 'requested_at': stamp(),
                  'proxy_enabled': bool(select_proxy(url, get_environ_proxies(url)))}
        RECORDS.append(record)
    phases = Counter()
    # Observe function calls only: never inspect local variables or arguments.
    def profile(frame, event, arg):
        if event != 'call': return
        mod, name = frame.f_globals.get('__name__',''), frame.f_code.co_name
        if (mod, name) in {('socket','getaddrinfo'), ('http.client','_tunnel'),
                          ('http.client','_read_status'), ('ssl','wrap_socket'),
                          ('ssl','do_handshake'), ('urllib3.util.connection','create_connection')}:
            phases[mod+'.'+name] += 1
    started=time.monotonic(); old=sys.getprofile();sys.setprofile(profile)
    try:
        with requests.Session() as session:
            # Default trust_env=True, default certificate verification; no bypass.
            with session.request(method, url, timeout=(2,5), stream=True,
                                 allow_redirects=False, **kwargs) as response:
                record['status_code']=response.status_code
                response.raise_for_status()
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>5_000_000 or time.monotonic()-START>LIMIT_SECONDS:
                        raise RuntimeError('diagnostic body/time budget exceeded')
                    chunks.append(chunk)
                record['body_bytes']=size;record['http_success']=True
                return b''.join(chunks)
    except Exception as exc:
        record.update(error_info(exc));record['http_success']=False
        raise
    finally:
        sys.setprofile(old)
        record['finished_at']=stamp();record['elapsed_seconds']=round(time.monotonic()-started,4)
        record['observed_network_calls']=dict(phases)


def inspect_page(body):
    data=json.loads(body)['data'];rows=data['diff']
    normalized, quality=normalize_quotes([_quote(row) for row in rows], 'CNY')
    times=[r['quote_at'] for r in normalized if r['quote_at']]
    return {'total':data['total'],'rows':len(rows),'quality':quality,
            'by_exchange':dict(Counter(r['exchange'] for r in normalized)),
            'field_sets':sorted({','.join(sorted(r)) for r in rows}),
            'timestamp_min':min(times) if times else None,'timestamp_max':max(times) if times else None,
            'timestamp_distribution':dict(Counter(times)),
            'identities':[r['identity'] for r in normalized]}


def market_page(page, label):
    body=request('GET',ENDPOINT,label=label,params={'pn':str(page),'pz':'100','po':'1','np':'1',
        'fltt':'2','invt':'2','fid':'f12','fs':FILTER,'fields':FIELDS})
    summary=inspect_page(body)
    atomic_json(OUT/(label+'.json'),json.loads(body))
    return summary


def audit_saved_evidence():
    """Offline replay only. Does not mark these partial lists as a full universe."""
    report=json.loads((OUT/'diagnostic_results.json').read_text(encoding='utf-8'))
    for item in report['official_lists']:
        if isinstance(item.get('page_help'),dict):item['page_help'].pop('data',None)
    atomic_json(OUT/'diagnostic_summary.json',report)
    unsupported=[];counts={};all_ids=[];cdr=[]
    for label in ('sse_main','sse_star','szse_a'):
        path=OUT/(label+'.json')
        if not path.exists():continue
        data=json.loads(path.read_text(encoding='utf-8'))
        rows=data if label=='szse_a' else data['result']
        keys=('A股代码','A股简称','A股上市日期') if label=='szse_a' else ('A_STOCK_CODE','SEC_NAME_CN','LIST_DATE')
        exchange='SZ' if label=='szse_a' else 'SH'
        count=Counter()
        for row in rows:
            code,name,listed=[row.get(key) for key in keys]
            count['returned_rows']+=1
            if exchange=='SH' and isinstance(code,str) and code.startswith('689'):
                cdr.append(code);count['cdr_excluded']+=1;continue
            all_ids.append(str(code)+'.'+exchange)
            if security_identity({'code':code,'exchange':exchange,'security_type':'A_STOCK'}) is None:
                unsupported.append({'code':code,'exchange':exchange,'name':name,'listed_at':listed,'source':label})
            if 'ST' in str(name).upper():count['st_names']+=1
        counts[label]=dict(count)
    audit={'source_counts':counts,'partial_sh_sz_unique_a_codes':len(set(all_ids)),
           'duplicate_codes':len(all_ids)-len(set(all_ids)),
           'unsupported_official_a':unsupported,'excluded_cdr_codes':cdr,
           'bse_complete_list_verified':False,'all_a_coverage_pct':None,
           'independent_full_universe_verified':False,
           'note':'Official current list responses, not certified point-in-time suspension/universe archives. No quote snapshot to compare.'}
    atomic_json(OUT/'universe_audit.json',audit)
    return audit


def main():
    report={'started_at':stamp(),'budget':{'requests':LIMIT_REQUESTS,'seconds':LIMIT_SECONDS},
            'environment':environment(),'single_pages':[],'small_pages':[],
            'complete_collection':{'attempted':False,'accepted_snapshots':0},'official_lists':[]}
    singles=[]
    for i in range(3):
        try:
            summary=market_page(1,f'em_single_{i+1}');singles.append(summary)
            report['single_pages'].append({'ok':True,**summary})
        except Exception as exc:
            report['single_pages'].append({'ok':False,**error_info(exc)})
    if len(singles)==3:
        for page in (2,3):
            try:report['small_pages'].append({'ok':True,'page':page,**market_page(page,f'em_page_{page}')})
            except Exception as exc:
                report['small_pages'].append({'ok':False,'page':page,**error_info(exc)});break
    summaries=singles[-1:]+[p for p in report['small_pages'] if p['ok']]
    ids=[identity for p in summaries for identity in p['identities']]
    small_ok=(len(summaries)==3 and len({p['total'] for p in summaries})==1
              and all(p['rows']==100 for p in summaries) and len(ids)==len(set(ids)))
    report['small_stage_passed']=len(singles)==3 and small_ok
    if report['small_stage_passed']:
        class Response:
            def __init__(self, payload):self.payload=payload
            def raise_for_status(self):pass
            def json(self):return json.loads(self.payload)
            def close(self):pass
        def get(url,params,timeout):
            return Response(request('GET',url,label='em_full_'+params['pn'],params=params))
        report['complete_collection']['attempted']=True
        try:
            packet=EastmoneyBreadthSource(BreadthSettings(),get=get).fetch()
            atomic_json(OUT/'full_packet.json',packet)
            report['complete_collection']['fetched_rows']=len(packet['rows'])
            report['complete_collection']['collection_seconds']=(datetime.fromisoformat(packet['fetched_at'])-datetime.fromisoformat(packet['requested_at'])).total_seconds()
            result=calculate_breadth(packet,TradingCalendar.from_cache(ROOT/'runtime/calendar'))
            atomic_json(OUT/'full_analysis.json',result)
            report['complete_collection'].update(accepted_snapshots=1,quality=result['quality'],coverage=result['coverage'])
        except Exception as exc:
            report['complete_collection'].update(error_info(exc))
    # Independent exchange evidence: never substitute it with quote provider total.
    sh_params={'STOCK_TYPE':'1','sqlId':'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L',
        'COMPANY_STATUS':'2,4,5,7,8','type':'inParams','isPagination':'true',
        'pageHelp.cacheSize':'1','pageHelp.beginPage':'1','pageHelp.pageSize':'10000',
        'pageHelp.pageNo':'1','pageHelp.endPage':'1'}
    official=[('sse_main','GET','https://query.sse.com.cn/sseQuery/commonQuery.do',
               {'params':sh_params,'headers':{'Referer':'https://www.sse.com.cn/assortment/stock/list/share/'}}),
              ('sse_star','GET','https://query.sse.com.cn/sseQuery/commonQuery.do',
               {'params':{**sh_params,'STOCK_TYPE':'8'},'headers':{'Referer':'https://www.sse.com.cn/assortment/stock/list/share/'}}),
              ('szse_a','GET','https://www.szse.cn/api/report/ShowReport',
               {'params':{'SHOWTYPE':'xlsx','CATALOGID':'1110','TABKEY':'tab1'}}),
              ('bse_list','POST','https://www.bse.cn/nqxxController/nqxxCnzq.do',
               {'data':{'page':'0','typejb':'T','xxfcbj[]':'2','xxzqdm':'','sortfield':'xxzqdm','sorttype':'asc'}}),
              ('bse_mapping','GET','https://www.bse.cn/service/code_mapping.html',{})]
    for label,method,url,kwargs in official:
        item={'label':label,'url':url}
        try:
            body=request(method,url,label=label,**kwargs)
            if label.startswith('sse_'):
                data=json.loads(body);rows=data['result'];codes=[r['A_STOCK_CODE'] for r in rows]
                item.update(parsed_rows=len(rows),unique_codes=len(set(codes)),
                            prefix_counts=dict(Counter(c[:3] for c in codes)),
                            page_help={k:v for k,v in data.get('pageHelp',{}).items() if k!='data'})
                atomic_json(OUT/(label+'.json'),data)
            elif label=='szse_a':
                import pandas as pd
                df=pd.read_excel(BytesIO(body),dtype=str)
                codes=df['A股代码'].dropna().tolist()
                item.update(parsed_rows=len(df),unique_codes=len(set(codes)),
                            prefix_counts=dict(Counter(c[:3] for c in codes)))
                atomic_json(OUT/(label+'.json'),df.fillna('').to_dict('records'))
            else:
                # HTML/callback arrival alone does not establish a current list.
                item.update(body_received=True,list_completeness_verified=False)
            item['parse_success']=True
        except Exception as exc:
            item.update(parse_success=False,**error_info(exc))
        report['official_lists'].append(item)
    report.update(finished_at=stamp(),elapsed_seconds=round(time.monotonic()-START,4),requests=RECORDS,
                  independently_verified_full_a_universe=False)
    atomic_json(OUT/'diagnostic_results.json',report)
    print(json.dumps({'result_file':str(OUT/'diagnostic_results.json'),
                      'elapsed_seconds':report['elapsed_seconds'],
                      'request_count':len(RECORDS),
                      'single_successes':len(singles),
                      'complete_collection':report['complete_collection']},ensure_ascii=False))


if __name__=='__main__':main()
