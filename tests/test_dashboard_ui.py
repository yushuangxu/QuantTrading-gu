"""Presentation-only WSGI regression; never starts servers or market requests."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from werkzeug.test import Client
from werkzeug.wrappers import Response

from webapp import app as web
from webapp.dashboard import application

ROOT = Path(__file__).resolve().parents[1]


class TestDashboardUI(unittest.TestCase):
    def setUp(self):
        self.previous = web.app.extensions.get('index_market')
        self.addCleanup(self.restore)
        web.app.extensions.pop('index_market', None)
        self.client = Client(application, Response)

    def restore(self):
        if self.previous is None:
            web.app.extensions.pop('index_market', None)
        else:
            web.app.extensions['index_market'] = self.previous

    def test_original_root_layout_preserved_with_actual_index_source_label(self):
        old = web.app.test_client().get('/').get_data(as_text=True)
        self.assertIn('股票量化监控面板', old)
        self.assertNotIn('dashboard.css', old)
        self.assertEqual(hashlib.sha256((ROOT/'webapp/templates/index.html').read_bytes()).hexdigest(),
                         '6125ce7e31cda6841d03bd903de3cdd0a3b1cf02236a819c23ab417e409ac81c')
        self.assertIn('d.source_label',old)

    def test_new_root_uses_existing_flask_template_without_data_request(self):
        with patch('index_market.EastmoneyIndexSource.fetch', side_effect=AssertionError('no network')):
            result = self.client.get('/')
            self.assertEqual(result.status_code, 200)
            html = result.get_data(as_text=True)
            self.assertIn('dashboard.css', html)
            self.assertIn('dataQuality', html)
            self.assertNotIn('index_market', web.app.extensions)
            for text in ('上证指数', '沪深300', '创业板指', '中证1000', 'MA5', 'MA10', 'MA20', 'MA60'):
                self.assertIn(text, html)
            self.assertNotIn('离线合成样例 · 仅用于 UI 验收', html)

    def test_8091_proxy_forwards_the_existing_index_api_without_changing_payload(self):
        payload = {'code':'000001.SH','trend':'震荡','availability':'stale',
                   'ma':{'ma5':12.34},'latest':{'state':'unknown'},'warnings':['test history']}
        calls = []
        def get_index(code):
            calls.append(code)
            return payload
        web.app.extensions['index_market'] = SimpleNamespace(get_index=get_index)
        self.assertEqual(self.client.get('/api/market/index/000001.SH').json, payload)
        self.assertEqual(web.app.test_client().get('/api/market/index/000001.SH').json, payload)
        self.assertEqual(calls,['000001.SH','000001.SH'])

    def test_index_list_status_and_unknown_index_reuse_original_routes(self):
        self.assertEqual(len(self.client.get('/api/market/indices').json['indices']),4)
        self.assertEqual(self.client.get('/api/market/index/000001.SZ').status_code,404)
        with patch.object(web,'check_daemon',return_value=False),patch.object(web,'get_today_log',return_value=None):
            self.assertFalse(self.client.get('/api/status').json['daemon'])

    def test_static_assets_and_light_design_tokens(self):
        css = self.client.get('/static/dashboard.css')
        js = self.client.get('/static/dashboard.js')
        self.addCleanup(css.close); self.addCleanup(js.close)
        self.assertEqual(css.status_code,200); self.assertEqual(js.status_code,200)
        text = css.get_data(as_text=True).lower()
        for color in ('#1677ff','#f5f7fa','#262626','#8c8c8c','#f0f0f0','#cf1322','#389e0d'):
            self.assertIn(color,text)
        self.assertIn('width:220px',text)
        self.assertIn('color-scheme:light',text)
        self.assertNotIn('linear-gradient',text)
        self.assertNotIn('react',js.get_data(as_text=True).lower())

    def test_presentation_controls_and_quality_states_are_present(self):
        html = self.client.get('/').get_data(as_text=True)
        for control in ('indexSelect','trendFilter','sortOrder','refreshButton','changeSort','qualityAlert'):
            self.assertIn(f'id="{control}"',html)
        js = (ROOT/'webapp/static/dashboard.js').read_text()
        for state in ('fresh','stale','unknown','unavailable','forming','closed'):
            self.assertIn(state,js)
        self.assertIn('items.get(selected)',js)
        self.assertIn("d.chart.slice(-range)",js)
        self.assertNotIn('moving_averages(',js)
        self.assertNotIn('analyze_daily(',js)

    def test_wrapper_does_not_mutate_original_environ(self):
        from werkzeug.test import EnvironBuilder
        environ = EnvironBuilder(path='/').get_environ()
        response = Response.from_app(application,environ)
        self.assertEqual(response.status_code,200)
        self.assertEqual(environ['PATH_INFO'],'/')


if __name__ == '__main__':
    unittest.main()
