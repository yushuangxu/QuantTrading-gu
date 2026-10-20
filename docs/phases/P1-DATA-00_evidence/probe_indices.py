"""Finite opt-in index-history probes; no scanner, breadth calls or source switch."""
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from index_market import INDICES, IndexSettings, EastmoneyIndexSource, normalize_daily, analyze_daily
from breadth_data import atomic_json
from market_data_state import TradingCalendar

OUT=Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('bounded_index_diagnostic',ROOT/'docs/phases/P1-02A-1_evidence/probe_data_link.py')
d=importlib.util.module_from_spec(s);s.loader.exec_module(d)
d.LIMIT_REQUESTS=12;d.LIMIT_SECONDS=120


def main():
    now=d.datetime.now(d.SHANGHAI)
    cal=TradingCalendar.from_cache(ROOT/'runtime/calendar')
    result={'started_at':now.isoformat(),'environment':d.environment(),
            'prior_index_cache_files':[p.name for p in (ROOT/'runtime/index_daily').glob('*.json')],
            'probes':[],'complete_real_indices':[]}
    def get(url,params,timeout):
        label='eastmoney_'+params['secid']
        raw=d.request('GET',url,label=label,params=params)
        parsed=json.loads(raw);atomic_json(OUT/(label+'.json'),parsed)
        class Response:
            def raise_for_status(self):pass
            def json(self):return parsed
            def close(self):pass
        return Response()
    source=EastmoneyIndexSource(get=get)
    for spec in INDICES:
        item={'source':'eastmoney','code':spec.code}
        try:
            rows=source.fetch(spec,now)
            fetched=d.datetime.now(d.SHANGHAI)
            a=analyze_daily(rows,fetched,fetched,cal)
            item.update(rows=len(rows),first=rows[0],last=rows[-1],closed_count=a['closed_count'],
                        analysis_date=a['analysis_date'],trend=a['trend'],warnings=a['warnings'],ma=a['ma'])
            atomic_json(OUT/(spec.code+'_analysis.json'),a)
            if a['closed_count']>=120 and a['trend']!='数据不足':result['complete_real_indices'].append(spec.code)
        except Exception as exc:item.update(d.error_info(exc))
        result['probes'].append(item)
        if spec==INDICES[0] and not result['complete_real_indices']:break
    if not result['complete_real_indices']:
        # Only one index across candidates until a genuine OHLCV+amount source qualifies.
        url='https://finance.sina.com.cn/realstock/company/sh000001/hisdata/klc_kl.js'
        item={'source':'sina_compressed_daily','code':'000001.SH','url':url}
        try:
            raw=d.request('GET',url,label='sina_compressed_sh000001',params={'d':'2020_2_4'})
            text=raw.decode('utf-8');atomic_json(OUT/'sina_compressed_raw.json',{'public_response':text})
            match=re.search(r'^\s*var\s+KLC_KL_sh000001\s*=\s*"([^"]+)";',text)
            if not match:raise ValueError('compressed_daily_response_format')
            import py_mini_racer
            from akshare.stock.cons import hk_js_decode
            context=py_mini_racer.MiniRacer();context.eval(hk_js_decode)
            decoded=context.call('d',match.group(1))
            atomic_json(OUT/'sina_decoded_rows.json',decoded)
            item.update(rows=len(decoded),field_sets=sorted({','.join(sorted(r)) for r in decoded}),
                        first=decoded[0],last=decoded[-1],
                        missing_amount_rows=sum('amount' not in r for r in decoded))
        except Exception as exc:item.update(d.error_info(exc))
        result['probes'].append(item)
        url='https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
        item={'source':'tencent_daily','code':'000001.SH','url':url}
        try:
            raw=d.request('GET',url,label='tencent_daily_sh000001',params={
                'param':f'sh000001,day,2025-01-01,{now.date().isoformat()},640,qfq','_var':'index_daily'})
            text=raw.decode('utf-8');parsed=json.loads(text[text.find('=')+1:])
            atomic_json(OUT/'tencent_daily_raw.json',parsed)
            data=parsed['data']['sh000001'];rows=data['day']
            item.update(rows=len(rows),row_widths=sorted({len(r) for r in rows}),
                        metadata_keys=sorted(data),first=rows[0],last=rows[-1])
        except Exception as exc:item.update(d.error_info(exc))
        result['probes'].append(item)
        url='https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData'
        item={'source':'sina_daily_kline','code':'000001.SH','url':url}
        try:
            raw=d.request('GET',url,label='sina_daily_kline_sh000001',params={'symbol':'sh000001','scale':'240','ma':'no','datalen':'240'})
            text=raw.decode('utf-8');rows=json.loads(text[text.find('['):text.rfind(']')+1])
            atomic_json(OUT/'sina_kline_rows.json',rows)
            item.update(rows=len(rows),field_sets=sorted({','.join(sorted(r)) for r in rows}),
                        first=rows[0],last=rows[-1],missing_amount_rows=sum('amount' not in r for r in rows))
        except Exception as exc:item.update(d.error_info(exc))
        result['probes'].append(item)
    result.update(finished_at=d.stamp(),requests=d.RECORDS)
    atomic_json(OUT/'diagnostic_results.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('environment','requests')},ensure_ascii=False))


if __name__=='__main__':main()
