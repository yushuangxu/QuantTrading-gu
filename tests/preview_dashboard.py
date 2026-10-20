"""Bounded, explicit synthetic UI preview; no production cache/quotes/notices.

Manual only, not discovered by unittest. All data is marked synthetic in-page.
Both ports mount the existing Flask app. Test-only /__preview__/<scenario>
controls never exist in the production application.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import math
from pathlib import Path
import select
import sys
import time
from wsgiref.simple_server import make_server

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from index_market import INDICES, SOURCE, analyze_daily
from market_data_state import SHANGHAI, TradingCalendar
from webapp import app as web
from webapp.dashboard import application


def samples(count=160):
    dates = pd.bdate_range(end='2026-10-19', periods=160)
    labels = {day.date() for day in dates}
    start,end = dates[0].date(),dates[-1].date()
    calendar = TradingCalendar({start+timedelta(days=i):start+timedelta(days=i) in labels
                                for i in range((end-start).days+1)})
    now = datetime(2026,10,19,14,50,tzinfo=SHANGHAI)
    result = {}
    for j,spec in enumerate(INDICES):
        rows = []
        for i,day in enumerate(dates):
            # Explicit analytic curves, not sampled or claimed market prices.
            base,step,wave = [(3240,3.0,25),(4200,0.1,40),(2980,-1.6,24),(6050,4,60)][j]
            close = base+step*i+wave*math.sin(i/7)+wave*.35*math.cos(i/3)
            rows.append({'date':day.date().isoformat(),'open':close-4,'high':close+12,
                         'low':close-13,'close':close,'volume':100000+i*200,
                         'amount':(4000+j*300+i*5)*1e8})
        data = analyze_daily(rows[-count:],now,now,calendar)
        data.update(code=spec.code,name=spec.name,source=SOURCE,availability='fresh',
                    fetched_at=now.isoformat(),observed_at=now.isoformat(),error=None)
        result[spec.code] = data
    return result


class PreviewService:
    scenario = 'normal'

    def __init__(self):
        self.data = samples()
        self.short_data = samples(36)

    def get_index(self, code):
        d = deepcopy((self.short_data if self.scenario == 'short' else self.data)[code])
        if self.scenario == 'unavailable':
            return {'code':code,'name':d['name'],'source':SOURCE,'availability':'unavailable',
                    'trend':'数据不足','chart':[],'warnings':['离线测试：未获得有效数据，不使用合成行情替代生产行情。'],
                    'error':'离线故障场景：模拟数据源请求超时。','fetched_at':None,'latest':None}
        if self.scenario == 'short':
            d['warnings'].append('离线测试：有效历史35根，正式分析需至少120根。')
        elif code == '000852.SH' or self.scenario == 'stale':
            d['availability'] = 'stale'
            d['error'] = '离线测试：最近获取失败，保留此前历史快照。'
            d['warnings'].append('趋势仅对应所示历史日期，不代表当前行情。')
        return d


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=600)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 900:
        parser.error('Preview must stop within 900 seconds')
    fixture = PreviewService()
    web.app.extensions['index_market'] = fixture
    web.check_daemon = lambda:False
    web.get_today_log = lambda:None
    @web.app.context_processor
    def preview_context():
        return {'dashboard_preview':True}

    def preview(environ,start_response):
        prefix = '/__preview__/'
        if environ.get('PATH_INFO','').startswith(prefix):
            scenario = environ['PATH_INFO'][len(prefix):]
            if scenario not in {'normal','unavailable','short','stale'}:
                start_response('404 Not Found',[]);return [b'unknown fixture']
            fixture.scenario = scenario
            start_response('303 See Other',[('Location','/'),('Content-Length','0')])
            return [b'']
        return application(environ,start_response)

    servers = []
    try:
        servers.append(make_server('127.0.0.1',8090,web.app))
        servers.append(make_server('127.0.0.1',8091,preview))
        print(f'SYNTHETIC_UI_PREVIEW_READY ports=8090,8091 expires={args.seconds}s',flush=True)
        deadline = time.monotonic()+args.seconds
        while time.monotonic()<deadline:
            for server in select.select(servers,[],[],.25)[0]:
                server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers: server.server_close()


if __name__ == '__main__':
    main()
