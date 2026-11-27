#!/opt/thinksmart-addons/venv/bin/python -B
"""Automated tests for Multi-City Weather Detail API (/api/home/weather/detail) & #wpage UI (2026-11-13)."""
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

HOME_PY = pathlib.Path('/usr/local/bin/thinksmart-home.py')
HOME_HTML = pathlib.Path('/usr/local/share/thinksmart-home/index.html')

spec = importlib.util.spec_from_file_location('ts_home', HOME_PY)
ts_home = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_home)


class WeatherDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_01_unit_multicity_detail_and_cooldown(self):
        w = ts_home.Weather()
        calls = []

        geo_db = {
            '北京': {'id': '101010100', 'name': '北京', 'adm1': '北京市', 'adm2': '北京', 'lat': '39.905', 'lon': '116.405'},
            '上海': {'id': '101020100', 'name': '上海', 'adm1': '上海市', 'adm2': '上海', 'lat': '31.231', 'lon': '121.472'},
            '广州': {'id': '101280101', 'name': '广州', 'adm1': '广东省', 'adm2': '广州', 'lat': '23.125', 'lon': '113.281'},
            '深圳': {'id': '101280601', 'name': '深圳', 'adm1': '广东省', 'adm2': '深圳', 'lat': '22.547', 'lon': '114.086'},
        }
        temp_by_id = {'101010100': '3', '101020100': '11', '101280101': '21', '101280601': '23'}

        async def fake_get(_session, _host, _key, path, params=None):
            calls.append((path, dict(params or {})))
            if path == '/geo/v2/city/lookup':
                q = (params or {}).get('location', '北京')
                return {'code': '200', 'location': [geo_db[q]]}
            loc_id = (params or {}).get('location', '101010100')
            if path == '/v7/weather/now':
                return {'code': '200', 'now': {'obsTime': '2026-11-13T00:35+08:00', 'temp': temp_by_id.get(loc_id, '0'), 'feelsLike': '-4', 'icon': '150', 'text': '晴', 'windDir': '东北风', 'windScale': '2', 'windSpeed': '6', 'humidity': '62', 'precip': '0.0', 'pressure': '902', 'vis': '19', 'cloud': '0', 'dew': '-8'}}
            if path == '/v7/weather/7d':
                return {'code': '200', 'daily': [
                    {'fxDate': f'2026-11-{13+i:02d}', 'tempMax': '12', 'tempMin': '-2', 'textDay': '晴', 'iconDay': '100', 'textNight': '多云', 'iconNight': '151', 'sunrise': '07:17', 'sunset': '17:16', 'moonrise': '11:05', 'moonset': '19:52', 'moonPhase': '蛾眉月', 'uvIndex': '3', 'humidity': '38', 'vis': '25', 'precip': '0.0', 'windDirDay': '西南风', 'windScaleDay': '1-3', 'windDirNight': '东风', 'windScaleNight': '1-3'}
                    for i in range(7)
                ]}
            if path == '/v7/weather/24h':
                return {'code': '200', 'hourly': [
                    {'fxTime': f'2026-11-13T{h:02d}:00+08:00', 'temp': str(h - 3), 'icon': '150', 'text': '晴', 'pop': '0', 'windDir': '东北风', 'windScale': '1-3'}
                    for h in range(24)
                ]}
            if path == '/v7/indices/1d':
                return {'code': '200', 'daily': [
                    {'type': '3', 'name': '穿衣指数', 'category': '较冷', 'text': '建议着厚外套加毛衣。'},
                    {'type': '9', 'name': '感冒指数', 'category': '较易发', 'text': '昼夜温差较大，注意增减衣物。'},
                    {'type': '5', 'name': '紫外线指数', 'category': '弱', 'text': '紫外线强度较弱。'},
                ]}
            if path.startswith('/airquality/v1/current/'):
                return {
                    'indexes': [{'code': 'cn-mee', 'aqi': 77, 'aqiDisplay': '77', 'category': '良', 'primaryPollutant': {'code': 'pm10', 'name': 'PM10'}, 'health': {'effect': '空气质量可接受', 'advice': {'generalPopulation': '一般人群可正常户外活动。', 'sensitivePopulation': '极少数敏感人群减少剧烈运动。'}}}],
                    'pollutants': [
                        {'code': 'pm2p5', 'name': 'PM 2.5', 'concentration': {'value': 43.0, 'unit': 'μg/m³'}},
                        {'code': 'co', 'name': 'CO', 'concentration': {'value': 0.9, 'unit': 'mg/m³'}},
                    ],
                }
            if path.startswith('/weatheralert/v1/current/'):
                return {'alerts': [{'headline': '大风蓝色预警', 'eventType': {'name': '大风'}, 'color': {'code': 'blue'}, 'description': '注意防风。'}]}
            raise ConnectionError('unexpected ' + path)

        with mock.patch.object(ts_home, 'weather_credentials', return_value=('api.qweather.com', 'testkey123456')), \
             mock.patch.object(ts_home, 'load_config', return_value={'city': '北京'}), \
             mock.patch.object(w, '_get', side_effect=fake_get):
            det_bj, dseq = await w.get_detail_with_seq(None)
            self.assertTrue(det_bj['ok'])
            self.assertEqual(dseq, 1)
            self.assertEqual(det_bj['city'], '北京')
            self.assertEqual(det_bj['cities'], ['北京', '上海', '广州', '深圳'])
            self.assertEqual(det_bj['now']['temp'], '3')

            # Fetch 广州 and 深圳; verify Home base cache remains 北京!
            det_gz, gz_seq = await w.get_detail_with_seq(None, city='广州')
            self.assertTrue(det_gz['ok'])
            self.assertEqual(gz_seq, 1)
            self.assertEqual(det_gz['city'], '广州')
            self.assertEqual(det_gz['adm1'], '广东省')
            self.assertEqual(det_gz['now']['temp'], '21')

            det_sz, sz_seq = await w.get_detail_with_seq(None, city='深圳')
            self.assertEqual(sz_seq, 1)
            self.assertEqual(det_sz['city'], '深圳')
            self.assertEqual(det_sz['adm2'], '深圳')
            self.assertEqual(det_sz['now']['temp'], '23')

            n_before = len(calls)
            base, bseq = await w.get_with_seq(None)
            self.assertEqual(len(calls), n_before)
            self.assertEqual(base['city'], '北京')
            self.assertEqual(bseq, 1)

            # Force refresh respects per-city 20s cooldown independently
            _, gz_seq2 = await w.get_detail_with_seq(None, force=True, city='广州')
            self.assertEqual(gz_seq2, 2)
            n_after_force = len(calls)
            _, gz_seq3 = await w.get_detail_with_seq(None, force=True, city='广州')
            self.assertEqual(gz_seq3, 2)
            self.assertEqual(len(calls), n_after_force)

    async def test_02_http_guard_allowlist_and_etag(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = ts_home.Inbox(path=pathlib.Path(td) / 'messages.json')
            app = ts_home.make_app(inbox=inbox)
            live_cities = ts_home.weather_cities()
            target_city = live_cities[-1]
            async with TestClient(TestServer(app)) as cli:
                r_bad = await cli.get('/api/home/weather/detail', headers={'Host': 'evil.example.com'})
                self.assertEqual(r_bad.status, 403)
                r_xs = await cli.get('/api/home/weather/detail', headers={'Host': '127.0.0.1:8095', 'Sec-Fetch-Site': 'cross-site'})
                self.assertEqual(r_xs.status, 403)
                # Unknown city rejected by strict allowlist
                r_un = await cli.get('/api/home/weather/detail?city=纽约', headers={'Host': '127.0.0.1:8095'})
                self.assertEqual(r_un.status, 400)

                r1 = await cli.get(f'/api/home/weather/detail?city={quote(target_city)}', headers={'Host': '127.0.0.1:8095'})
                self.assertEqual(r1.status, 200)
                etag = r1.headers.get('ETag')
                self.assertTrue(etag)
                j = await r1.json()
                self.assertTrue(j.get('ok'))
                self.assertEqual(j.get('city'), target_city)

                r2 = await cli.get(f'/api/home/weather/detail?city={quote(target_city)}', headers={'Host': '127.0.0.1:8095', 'If-None-Match': etag})
                self.assertEqual(r2.status, 304)

    async def test_03_live_daemon_and_html_invariants(self):
        html = HOME_HTML.read_text(encoding='utf-8')
        for token in ('id="wpage"', 'id="wpHome"', 'id="wpCities"', 'id="wpRef"', 'id="wpStats"', 'id="wpPols"',
                      'id="wpIdxs"', 'id="wpHourly"', 'id="wpDays7"', 'WP_EXTRA_CITIES', 'selectWpCity(',
                      'body.wp-open #wIcon *', '/api/home/weather/detail'):
            self.assertIn(token, html)
        for city in ('北京', '上海', '广州', '深圳'):
            self.assertIn(city, html)
        # Verify 7-day forecast places max temp on the left and min temp on the right
        idx_max = html.index('<span class="wp-drange"><span class="wp-dmax num">')
        idx_min = html.index('<span class="wp-dmin num">', idx_max)
        self.assertLess(idx_max, idx_min)

        live_cities = ts_home.weather_cities()
        async with aiohttp.ClientSession() as sess:
            for city in live_cities:
                u = f'http://127.0.0.1:8095/api/home/weather/detail?city={quote(city)}'
                async with sess.get(u, headers={'Host': '127.0.0.1:8095'}) as r:
                    self.assertEqual(r.status, 200)
                    etag = r.headers.get('ETag')
                    data = await r.json()
                    self.assertTrue(data.get('ok'))
                    self.assertEqual(data.get('city'), city)
                    self.assertEqual(data.get('cities'), live_cities)
                    self.assertEqual(len(data.get('hourly') or []), 24)
                    self.assertEqual(len(data.get('daily') or []), 7)
                async with sess.get(u, headers={'Host': '127.0.0.1:8095', 'If-None-Match': etag}) as r304:
                    self.assertEqual(r304.status, 304)


if __name__ == '__main__':
    unittest.main(verbosity=2)
