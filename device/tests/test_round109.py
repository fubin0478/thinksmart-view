#!/opt/thinksmart-addons/venv/bin/python
"""Round 109 tests: home page service, music sleep timer, pomodoro focus + voice channel,
phone message board relay, voice tools. Run with the add-ons venv as root or ubuntu:
    /opt/thinksmart-addons/venv/bin/python -B /opt/thinksmart-addons/test_round109.py
R109_ROOT=<dir> tests copies in <dir> instead of the installed files (used before deploying).
Never touches live state: every module runs against temporary state directories, the music
engine is built without __init__ and its session saving is disabled.
"""
import array
import ast
import asyncio
import atexit
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

STAGE = os.environ.get('R109_ROOT')
PATHS = {
    'home': 'thinksmart-home.py' if STAGE else '/usr/local/bin/thinksmart-home.py',
    'qq': 'thinksmart-qqmusic.py' if STAGE else '/usr/local/bin/thinksmart-qqmusic.py',
    'study': 'thinksmart-study.py' if STAGE else '/usr/local/bin/thinksmart-study.py',
    'intercom': 'thinksmart-intercom.py' if STAGE else '/usr/local/bin/thinksmart-intercom.py',
    'monitor': 'thinksmart-monitor.py' if STAGE else '/usr/local/bin/thinksmart-monitor.py',
    'bridge': 'sitecustomize.py' if STAGE else '/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py',
    'airplay': 'thinksmart-airplay.py' if STAGE else '/usr/local/bin/thinksmart-airplay.py',
}
VENDOR = os.path.join(STAGE, 'vendor') if STAGE else '/opt/thinksmart-addons/vendor'
sys.path.insert(0, VENDOR)
sys.path.insert(1, '/usr/local/bin')


def path_of(key):
    p = PATHS[key]
    return pathlib.Path(STAGE, p) if STAGE else pathlib.Path(p)


def load(name, key, drop_assign=()):
    p = path_of(key)
    t = ast.parse(p.read_text(encoding='utf-8'))
    if drop_assign:
        t.body = [n for n in t.body if not (isinstance(n, ast.Assign) and any(
            isinstance(x, ast.Name) and x.id in drop_assign for x in n.targets))]
    # never run a __main__ block
    t.body = [n for n in t.body if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                                        and getattr(n.test.left, 'id', '') == '__name__')]
    m = types.ModuleType(name)
    m.__file__ = str(p)
    exec(compile(t, str(p), 'exec'), m.__dict__)
    return m


TMP = tempfile.TemporaryDirectory()
atexit.register(TMP.cleanup)
os.environ['STATE_DIRECTORY'] = os.path.join(TMP.name, 'state')
HOME = load('home_r109', 'home')
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

H8095 = {'Host': '127.0.0.1:8095'}


def run(coro):
    return asyncio.run(coro)


class HomeServiceTests(unittest.TestCase):
    def test_weather_credentials_parse(self):
        p = pathlib.Path(TMP.name, 'cfg.yaml')
        p.write_text('plugins:\n  web_search:\n    api_key: nope123456\n  get_weather:\n    # 和风\n'
                     '    api_host: abc123.re.qweatherapi.com\n    api_key: "0123456789abcdef"\n  other:\n    api_key: zzz\n',
                     encoding='utf-8')
        self.assertEqual(HOME.weather_credentials(p), ('abc123.re.qweatherapi.com', '0123456789abcdef'))
        p.write_text('plugins:\n  get_weather:\n    api_host: evil.example.com\n    api_key: 0123456789abcdef\n', encoding='utf-8')
        self.assertEqual(HOME.weather_credentials(p), (None, '0123456789abcdef'))

    def test_calendar_lunar_holidays(self):
        g = HOME.month_grid(2026, 10)
        self.assertEqual(len(g['days']), 42)
        oct1 = next(d for d in g['days'] if d['date'] == '2026-10-01')
        self.assertEqual(oct1['hol'], 'off')
        self.assertEqual(oct1['label'], '国庆')
        g2 = HOME.month_grid(2027, 2)
        cny = next(d for d in g2['days'] if d['date'] == '2027-02-06')
        self.assertEqual(cny['label'], '春节')
        nov7 = next(d for d in HOME.month_grid(2026, 11)['days'] if d['date'] == '2026-11-07')
        self.assertEqual((nov7['label'], nov7['kind']), ('立冬', 'jq'))
        t = HOME.today_info()
        self.assertTrue(t['lunar'].startswith('农历'))
        self.assertTrue(t['year'].endswith('年'))

    def test_inbox_store(self):
        path = pathlib.Path(TMP.name, 'inbox1', 'messages.json')
        box = HOME.Inbox(path)
        m = box.add('  晚饭在锅里，\n  记得热一下  ')
        self.assertEqual(m['text'], '晚饭在锅里，\n记得热一下')
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            box.add('x' * 201)
        with self.assertRaises(ValueError):
            box.add('   ')
        self.assertEqual(len(box.unread()), 1)
        item, first = box.ack(m['id'])
        self.assertTrue(first and item['read_at'])
        self.assertEqual(box.ack(m['id'])[1], False)
        self.assertEqual(HOME.Inbox(path).recent(5)[0]['read_at'], item['read_at'])
        for _ in range(HOME.MSG_RATE[0] - 1):
            box.add('hi')
        with self.assertRaises(OverflowError):
            box.add('too many')

    def test_http_guard_and_message_flow(self):
        async def go():
            box = HOME.Inbox(pathlib.Path(TMP.name, 'inbox2', 'messages.json'))
            said = []

            async def fake_post(session, url, body, timeout=5.0, headers=None):
                said.append((url, body, headers))
                return 200, {'ok': True}
            with mock.patch.object(HOME, '_post_json', fake_post), mock.patch.object(HOME, 'send_preview', None):
                client = TestClient(TestServer(HOME.make_app(box)))
                await client.start_server()
                try:
                    r = await client.get('/api/home/inbox', headers={'Host': 'evil.example:8095'})
                    self.assertEqual(r.status, 403)
                    r = await client.get('/api/home/inbox', headers=dict(H8095, Origin='http://evil.example'))
                    self.assertEqual(r.status, 403)
                    r = await client.post('/api/home/messages', json={'text': 'x'}, headers=H8095)
                    self.assertEqual(r.status, 403)
                    r = await client.post('/api/home/messages', json={'text': 'x'},
                                          headers=dict(H8095, **{'X-TS-Internal': '1', 'Origin': 'http://127.0.0.1:8090'}))
                    self.assertEqual(r.status, 403)
                    r = await client.post('/api/home/messages', data='text=x',
                                          headers=dict(H8095, **{'X-TS-Internal': '1', 'Content-Type': 'application/x-www-form-urlencoded'}))
                    self.assertEqual(r.status, 415)
                    r = await client.post('/api/home/messages', json={'text': '作业写完先吃饭'}, headers=dict(H8095, **{'X-TS-Internal': '1'}))
                    self.assertEqual(r.status, 200)
                    mid = (await r.json())['message']['id']
                    r = await client.options('/api/home/inbox/ack', headers=dict(H8095, Origin='http://127.0.0.1:8090'))
                    self.assertEqual(r.status, 204)
                    self.assertEqual(r.headers.get('Access-Control-Allow-Origin'), 'http://127.0.0.1:8090')
                    r = await client.get('/api/home/inbox', headers=dict(H8095, Origin='http://127.0.0.1:8090'))
                    j = await r.json()
                    self.assertEqual((j['count'], j['unread'][0]['id']), (1, mid))
                    r = await client.post('/api/home/inbox/ack', json={'id': 'nothex'}, headers=H8095)
                    self.assertEqual(r.status, 400)
                    r = await client.post('/api/home/inbox/ack', json={'id': mid}, headers=dict(H8095, Origin='http://127.0.0.1:8090'))
                    self.assertEqual(r.status, 200)
                    self.assertEqual(len(said), 1)
                    self.assertTrue(said[0][0].endswith('/api/voice/say'))
                    self.assertEqual(said[0][1]['text'], '留言：作业写完先吃饭')
                    self.assertEqual(said[0][2], {'X-TS-Internal': '1'})
                    await client.post('/api/home/inbox/ack', json={'id': mid}, headers=H8095)
                    self.assertEqual(len(said), 1)  # read out only once
                    r = await client.get('/api/home/messages?limit=5', headers=H8095)
                    self.assertTrue((await r.json())['messages'][0]['read_at'])
                    r = await client.post('/api/home/sleep', json={'minutes': 30, 'tracks': 1}, headers=H8095)
                    self.assertEqual(r.status, 400)
                    r = await client.post('/api/home/music', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(r.status, 400)
                    r = await client.get('/api/home/calendar?ym=2026-13', headers=H8095)
                    self.assertEqual(r.status, 400)
                    r = await client.get('/api/home/calendar?ym=2026-11', headers=H8095)
                    self.assertEqual(r.status, 200)
                    self.assertEqual(len((await r.json())['month']['days']), 42)
                    r = await client.get('/', headers=H8095)
                    self.assertEqual(r.status, 200)
                    etag = r.headers.get('ETag', '')
                    self.assertTrue(etag.startswith('"') and etag.endswith('"'))
                    csp_hdr = r.headers.get('Content-Security-Policy', '')
                    self.assertIn('https://*.qpic.cn', csp_hdr)
                    self.assertIn('https://*.gtimg.cn', csp_hdr)
                    r304 = await client.get('/', headers=dict(H8095, **{'If-None-Match': etag}))
                    self.assertEqual((r304.status, r304.headers.get('Content-Length')), (304, '0'))
                    self.assertEqual(await r304.read(), b'')
                    self.assertIn('https://*.qpic.cn', r304.headers.get('Content-Security-Policy', ''))
                    ra = await client.get('/apps.json', headers=H8095)
                    self.assertEqual(ra.status, 200)
                    ra304 = await client.get('/apps.json', headers=dict(H8095, **{'If-None-Match': ra.headers['ETag']}))
                    self.assertEqual((ra304.status, ra304.headers.get('Content-Length')), (304, '0'))
                    # Supplement 13+14: JSON API ETag / 304 + Content-Length: 0 + CORS headers on /api/home/inbox and /api/home/state
                    ri = await client.get('/api/home/inbox', headers=dict(H8095, Origin='http://127.0.0.1:8090'))
                    self.assertEqual((ri.status, ri.headers.get('Access-Control-Expose-Headers')), (200, 'ETag'))
                    self.assertIn('If-None-Match', ri.headers.get('Access-Control-Allow-Headers', ''))
                    ri304 = await client.get('/api/home/inbox', headers=dict(H8095, Origin='http://127.0.0.1:8090', **{'If-None-Match': ri.headers['ETag']}))
                    self.assertEqual((ri304.status, ri304.headers.get('Content-Length'), await ri304.read(), ri304.headers.get('Access-Control-Allow-Origin')), (304, '0', b'', 'http://127.0.0.1:8090'))
                    rs = await client.get('/api/home/state', headers=H8095)
                    self.assertEqual((rs.status, rs.headers.get('Cache-Control')), (200, 'no-store'))
                    rs304 = await client.get('/api/home/state', headers=dict(H8095, **{'If-None-Match': rs.headers['ETag']}))
                    self.assertEqual((rs304.status, rs304.headers.get('Content-Length'), await rs304.read(), rs304.headers.get('Cache-Control')), (304, '0', b'', 'no-store'))
                finally:
                    await client.close()
        run(go())

    def test_home_page_assets(self):
        share = pathlib.Path(STAGE) if STAGE else HOME.SHARE
        html = (share / ('home_index.html' if STAGE else 'index.html')).read_text(encoding='utf-8')
        self.assertIn("post({ ready: true })", html)
        apps = json.loads((share / 'apps.json').read_text(encoding='utf-8'))
        ids = [a['id'] for a in apps['apps']]
        for want in ('xiaozhi', 'music', 'study', 'focus', 'messages', 'server', 'brightness', 'settings'):
            self.assertIn(want, ids)


class MusicSleepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.q = load('qq_r109', 'qq', drop_assign=('engine',))

    def engine(self):
        q = self.q
        e = object.__new__(q.QQMusicEngine)
        import threading
        e._lock = threading.Lock()
        e._aec_lock = threading.Lock()
        e.aec_clients = set()
        e.queue = [{'mid': 'a', 'title': 'A', 'duration': 200}, {'mid': 'b', 'title': 'B', 'duration': 180}]
        e.index, e.playing, e.paused, e.elapsed_base = 0, True, False, 0.0
        e.play_started_mono, e._gen, e._fail_streak, e.play_mode = 0.0, 5, 0, 'loop'
        e.lyrics, e.lyrics_seq, e._just_started_mono, e.ducked = [], 1, None, False
        e.sleep_mode, e.sleep_deadline, e.sleep_total, e.sleep_tracks_left = None, 0.0, 0, 0
        e.save_session = lambda: None
        e.started = []
        e._start_current_track = lambda seek, gen: e.started.append((seek, gen))
        return e

    def test_apply_gain(self):
        buf = bytearray(array.array('f', [1.0] * 1920).tobytes())
        self.q.QQMusicEngine._apply_gain(buf, 1.0, 0.0)
        a = array.array('f'); a.frombytes(bytes(buf))
        self.assertAlmostEqual(a[0], 1.0, places=5)
        self.assertEqual(a[0], a[1])
        self.assertLess(a[-1], 0.01)
        self.assertTrue(all(a[2 * i] >= a[2 * i + 2] for i in range(959)))
        buf = bytearray(array.array('f', [0.8] * 1920).tobytes())
        self.q.QQMusicEngine._apply_gain(buf, 0.5, 0.5)
        a = array.array('f'); a.frombytes(bytes(buf))
        self.assertAlmostEqual(a[100], 0.4, places=5)

    def test_time_mode_and_fade(self):
        e = self.engine()
        self.assertFalse(e.set_sleep(minutes=0)['ok'])
        self.assertFalse(e.set_sleep(minutes=True)['ok'])
        r = e.set_sleep(minutes=30)
        self.assertTrue(r['ok'])
        self.assertEqual(r['sleep']['mode'], 'time')
        self.assertGreater(r['sleep']['remaining'], 1790)
        self.assertEqual(e._sleep_gain_locked(), 1.0)
        e.sleep_deadline = time.monotonic() + 10
        self.assertAlmostEqual(e._sleep_gain_locked(), 0.25, delta=0.03)
        self.assertTrue(e.sleep_status()['fading'])
        e.sleep_deadline = time.monotonic() - 0.1
        calls = []
        e.control = lambda action: calls.append(action) or {'ok': True}
        self.assertTrue(e.sleep_tick())
        self.assertEqual(calls, ['pause'])
        self.assertIsNone(e.sleep_mode)
        self.assertEqual(e.set_sleep(cancel=True)['sleep'], None)

    def test_tracks_mode_pauses_on_next_track(self):
        e = self.engine()
        e.set_sleep(tracks=2)
        e._advance_on_eof(5)
        self.assertEqual((e.index, e.paused, e.sleep_tracks_left), (1, False, 1))
        self.assertEqual(len(e.started), 1)
        e.elapsed_base = 170.0
        self.assertLess(e._sleep_gain_locked(), 0.5)  # last 20 s of the last track fade out
        gen = e._gen
        e._advance_on_eof(gen)
        self.assertEqual((e.index, e.paused, e.playing, e.sleep_mode), (0, True, True, None))
        self.assertEqual(len(e.started), 1)  # did not start another track
        self.assertEqual(e.elapsed_base, 0.0)

    def test_index_etag_304(self):
        async def go():
            html_file = pathlib.Path(TMP.name, 'qq_ui.html')
            html_file.write_text('<!doctype html><title>qq</title>', encoding='utf-8')
            with mock.patch.object(self.q, 'UI_HTML_PATH', html_file), mock.patch.object(self.q, 'engine', self.engine(), create=True):
                app = self.q.make_app()
                app.on_startup.clear()
                app.on_cleanup.clear()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    h = {'Host': '127.0.0.1:8091'}
                    r = await client.get('/', headers=h)
                    self.assertEqual(r.status, 200)
                    etag = r.headers.get('ETag', '')
                    self.assertTrue(etag.startswith('"') and etag.endswith('"'))
                    r304 = await client.get('/', headers=dict(h, **{'If-None-Match': etag}))
                    self.assertEqual(r304.status, 304)
                    self.assertEqual(await r304.read(), b'')
                finally:
                    await client.close()
        run(go())


class StudyFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = load('study_r109', 'study')
        d = pathlib.Path(TMP.name, 'study')
        cls.s.STATE = d
        cls.s.CONFIG_PATH = d / 'config.json'
        cls.s.SECRET_PATH = d / 'secret'
        cls.s.VOICE_DIR = d / 'voice'
        cls.s.LOG_DIR = d / 'log'
        cls.s.FOCUS_PATH = d / 'focus.json'
        cls.s._CONFIG_CACHE = (None, None)

    def companion(self):
        c = self.s.Companion()
        c.spoken, c.switched = [], []
        c._announce = lambda text: c.spoken.append(text)
        c._announce_retry = lambda text, tries=12, gap=10.0: c.spoken.append(('retry', text))

        def switch(on, reason='', announce=True):
            c.switched.append((on, reason, announce))
            cfg = self.s.load_config()
            cfg['enabled'], cfg['enabled_at'] = bool(on), int(time.time()) if on else 0
            self.s.save_config(cfg)
            return cfg
        c.switch = switch
        return c

    def test_focus_cycle_and_summary(self):
        async def go():
            c = self.companion()
            f = c.focus_start(25, 5, True)
            self.assertEqual((f['phase'], f['minutes']), ('focus', 25))
            self.assertGreater(f['remaining'], 1490)
            self.assertEqual(c.switched, [(True, 'focus', False)])
            self.assertEqual(c.spoken, ['好的，开始专注25分钟，加油！'])
            self.assertEqual(os.stat(self.s.FOCUS_PATH).st_mode & 0o777, 0o600)
            self.assertEqual(self.s.Companion.load_focus()['study_auto'], True)
            # the focus period ends -> done event, companion off, break starts, reminder queued
            c.focus['ends_at'] = time.time() - 1
            task = asyncio.create_task(c.focus_loop())
            await asyncio.sleep(0.2)
            self.assertEqual(c.focus['phase'], 'break')
            self.assertIn((False, 'focus', False), c.switched)
            self.assertIn(('retry', '专注完成啦！站起来看看窗外，休息5分钟吧。'), c.spoken)
            c.focus['ends_at'] = time.time() - 1
            c.focus_wake.set()
            await asyncio.sleep(0.2)
            self.assertIsNone(c.focus)
            self.assertIn(('retry', self.s.FOCUS_BREAK_OVER), c.spoken)
            c.focus_start(45, 0, False)
            c.focus['started_at'] -= 600
            c.focus_stop()
            c.stopping = True
            c.focus_wake.set()
            await asyncio.wait_for(task, 2)
            s = self.s.summarize(self.s.read_day(self.s.local_now().date().isoformat()))
            self.assertEqual(s['focus_sessions'], 1)
            self.assertEqual(s['focus_minutes'], 25 + 10)
            self.assertEqual(c.today()['focus'], {'sessions': 1, 'minutes': 35})
        run(go())
        parent_p = pathlib.Path(STAGE, 'parent.html') if STAGE else self.s.PARENT_PAGE
        parent_html = parent_p.read_text(encoding='utf-8')
        self.assertIn("e.event === 'focus_start'", parent_html)
        self.assertIn("e.event === 'focus_done'", parent_html)
        self.assertIn("e.event === 'focus_stop'", parent_html)
        self.assertIn("focus: '番茄专注'", parent_html)

    def test_voice_say_route(self):
        async def go():
            c = self.companion()
            c.supervise = lambda: asyncio.sleep(3600)
            client = TestClient(TestServer(self.s.app_for(c)))
            await client.start_server()
            try:
                r = await client.post('/api/voice/say', json={'text': '你好'})
                self.assertEqual(r.status, 403)
                r = await client.post('/api/voice/say', json={'text': '你好'},
                                      headers={'X-TS-Internal': '1', 'Origin': 'http://127.0.0.1:8090'})
                self.assertEqual(r.status, 403)
                r = await client.post('/api/voice/say', json={'text': 'x' * 241}, headers={'X-TS-Internal': '1'})
                self.assertEqual(r.status, 400)
                r = await client.post('/api/voice/say', json={'text': ' 留言：  晚饭在锅里 '}, headers={'X-TS-Internal': '1'})
                self.assertEqual(r.status, 200)
                self.assertIn(('retry', '留言： 晚饭在锅里'), c.spoken)
                r = await client.post('/api/study/focus', json={'action': 'start', 'minutes': 0})
                self.assertEqual(r.status, 400)
                r = await client.post('/api/study/focus', json={'action': 'start', 'minutes': 30, 'with_study': False})
                self.assertEqual((await r.json())['minutes'], 30)
                r = await client.get('/api/study')
                self.assertEqual((await r.json())['focus']['phase'], 'focus')
                r = await client.post('/api/study/focus', json={'action': 'stop'})
                self.assertIsNone((await r.json())['phase'])
                rs = await client.get('/api/study')
                self.assertEqual((rs.status, rs.headers.get('Cache-Control')), (200, 'no-store'))
                rs304 = await client.get('/api/study', headers={'If-None-Match': rs.headers['ETag']})
                self.assertEqual((rs304.status, rs304.headers.get('Content-Length'), await rs304.read()), (304, '0', b''))
                rp = await client.get('/api/parent/state')
                self.assertEqual((rp.status, rp.headers.get('Cache-Control')), (200, 'no-store'))
                rp304 = await client.get('/api/parent/state', headers={'If-None-Match': rp.headers['ETag']})
                self.assertEqual((rp304.status, rp304.headers.get('Content-Length'), await rp304.read(), rp304.headers.get('Cache-Control')), (304, '0', b'', 'no-store'))
            finally:
                c.stopping = True
                await client.close()
        run(go())


class IntercomRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.i = load('intercom_r109', 'intercom')

    def test_origin_rules(self):
        ok = self.i._msg_origin_ok
        self.assertTrue(ok({}))
        self.assertTrue(ok({'origin': 'https://cam.example.org', 'host': 'cam.example.org'}))
        self.assertFalse(ok({'origin': 'https://evil.example', 'host': 'cam.example.org'}))
        self.assertTrue(ok({'origin': 'http://127.0.0.1:18091', 'host': '127.0.0.1:18091'}))
        self.assertFalse(ok({'origin': 'http://evil.example', 'host': '127.0.0.1:18091'}))

    def test_relay(self):
        async def go():
            seen = []

            async def fake_home(request):
                seen.append((request.method, request.headers.get('X-TS-Internal'), request.headers.get('Origin'),
                             await request.text() if request.method == 'POST' else request.query_string))
                if request.method == 'POST':
                    return web.json_response({'ok': True, 'message': {'id': 'abcdefabcdef'}})
                return web.json_response({'messages': [{'id': 'abcdefabcdef', 'text': 'hi', 'at': 1, 'read_at': None, 'source': 'phone'}]})
            up = web.Application()
            up.router.add_route('*', '/api/home/messages', fake_home)
            upstream = TestServer(up)
            await upstream.start_server()
            import aiohttp
            app = web.Application()
            sess_key = getattr(self.i, 'STUDY_SESSION_KEY', 'study_session')
            app[sess_key] = aiohttp.ClientSession()
            app.router.add_get('/api/messages', self.i.handle_messages)
            app.router.add_post('/api/message', self.i.handle_messages)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                with mock.patch.object(self.i, 'HOME_API', f'http://127.0.0.1:{upstream.port}'):
                    r = await client.post('/api/message', json={'text': 'hi'})
                    self.assertEqual(r.status, 403)
                    r = await client.post('/api/message', json={'text': 'hi'}, headers={'X-Intercom-Msg': '1', 'Origin': 'http://evil.example'})
                    self.assertEqual(r.status, 403)
                    r = await client.post('/api/message', json={'text': '晚上见'}, headers={'X-Intercom-Msg': '1'})
                    pj = await r.json()
                    self.assertEqual((r.status, pj['ok'], pj['message']['id']), (200, True, 'abcdefabcdef'))
                    r = await client.get('/api/messages', headers={'X-Intercom-Msg': '1'})
                    j = await r.json()
                    self.assertEqual(list(j['messages'][0]), ['id', 'text', 'at', 'read_at'])
                    self.assertEqual(seen[0][:3], ('POST', '1', None))
                    self.assertIn('晚上见', json.loads(seen[0][3])['text'])
            finally:
                await client.close()
                await app[sess_key].close()
                await upstream.close()
        run(go())

    def test_intercom_and_monitor_static_etag_304(self):
        mon = load('monitor_r109', 'monitor')
        from unittest.mock import AsyncMock

        async def go():
            srv = object.__new__(self.i.IntercomServer)
            app_i = web.Application()
            app_i.on_response_prepare.append(self.i.keepalive_304)
            app_i.router.add_get('/', srv.handle_index)
            ci = TestClient(TestServer(app_i))
            await ci.start_server()
            try:
                r = await ci.get('/')
                self.assertEqual(r.status, 200)
                etag = r.headers.get('ETag', '')
                self.assertTrue(etag.startswith('"') and etag.endswith('"'))
                r304 = await ci.get('/', headers={'If-None-Match': etag})
                self.assertEqual((r304.status, r304.headers.get('Content-Length'), await r304.read()), (304, '0', b''))
            finally:
                await ci.close()

            assets = pathlib.Path(TMP.name, 'mon_assets')
            assets.mkdir(exist_ok=True)
            (assets / 'servers.json').write_text('[{"id":1,"name":"n1","country":"jp"}]', encoding='utf-8')
            (assets / 'index.html').write_text('<!doctype html><title>m</title>', encoding='utf-8')
            (assets / 'display.html').write_text('<!doctype html><title>d</title>', encoding='utf-8')
            (assets / 'home_overlay.js').write_text('/* overlay */', encoding='utf-8')
            m = mon.Monitor(assets)
            m.consume = AsyncMock()
            cm = TestClient(TestServer(m.app()))
            await cm.start_server()
            try:
                for pth in ('/', '/assets/home_overlay.js', '/assets/servers.json'):
                    r = await cm.get(pth)
                    self.assertEqual(r.status, 200, pth)
                    et = r.headers.get('ETag', '')
                    self.assertTrue(et.startswith('"') and et.endswith('"'), pth)
                    r2 = await cm.get(pth, headers={'If-None-Match': et})
                    self.assertEqual((r2.status, r2.headers.get('Content-Length'), await r2.read()), (304, '0', b''), pth)
                m.connected = True
                m.received_at = time.monotonic()
                m.received_wall = 1762680000.0
                m.servers = [{'id': 1, 'name': 'n1', 'country': 'jp', 'alive': True, 'cpu': 12.3}]
                rst = await cm.get('/api/state')
                self.assertEqual((rst.status, rst.headers.get('Cache-Control')), (200, 'no-store'))
                m.last_view = 0.0
                rst304 = await cm.get('/api/state', headers={'If-None-Match': rst.headers['ETag']})
                self.assertEqual((rst304.status, rst304.headers.get('Content-Length'), await rst304.read(), m.viewed_recently()), (304, '0', b'', True))
            finally:
                await cm.close()
        run(go())


class BridgeToolTests(unittest.TestCase):
    def test_tool_bodies(self):
        b = load('bridge_r109', 'bridge')
        names = [t['name'] for t in b.MUSIC_TOOLS]
        self.assertIn('self.music.sleep_timer', names)
        self.assertIn('self.study.focus', names)
        self.assertEqual(b._sleep_body({'minutes': 30}), {'minutes': 30, 'fade': 300})  # 2026-11-12: voice timers fade 5 min
        self.assertEqual(b._sleep_body({'tracks': '1'}), {'tracks': 1})
        self.assertEqual(b._sleep_body({'cancel': True, 'minutes': 5}), {'cancel': True})
        self.assertEqual(b._sleep_body({'minutes': 999}), {'minutes': 30, 'fade': 300})
        self.assertEqual(b._sleep_body({'until': '23:30'}), {'until': '23:30', 'fade': 300})
        self.assertEqual(b._sleep_body({'all_night': True, 'minutes': 5}), {'night': True})
        self.assertEqual(b._focus_body({'action': 'start'}), {'action': 'start', 'minutes': 25})
        self.assertEqual(b._focus_body({'action': 'start', 'minutes': 45}), {'action': 'start', 'minutes': 45})
        self.assertEqual(b._focus_body({'action': 'stop', 'minutes': 45}), {'action': 'stop'})


class Supplement15SecurityTests(unittest.TestCase):
    def test_home_cross_site_and_error_cors_headers(self):
        async def go():
            box = HOME.Inbox(pathlib.Path(TMP.name, 'inbox_s15', 'messages.json'))
            client = TestClient(TestServer(HOME.make_app(box)))
            await client.start_server()
            try:
                rx = await client.get('/api/home/inbox', headers=dict(H8095, **{'Sec-Fetch-Site': 'cross-site'}))
                self.assertEqual(rx.status, 403)
                r404 = await client.post('/api/home/inbox/ack', json={'id': '0123456789ab'},
                                         headers=dict(H8095, Origin='http://127.0.0.1:8090'))
                self.assertEqual(r404.status, 404)
                self.assertEqual(r404.headers.get('Access-Control-Allow-Origin'), 'http://127.0.0.1:8090')
                self.assertEqual(r404.headers.get('X-Content-Type-Options'), 'nosniff')
                ra = await client.get('/apps.json', headers=dict(H8095, Origin='http://127.0.0.1:8090'))
                ra304 = await client.get('/apps.json', headers=dict(H8095, Origin='http://127.0.0.1:8090', **{'If-None-Match': ra.headers['ETag']}))
                self.assertEqual((ra304.status, ra304.headers.get('Content-Length'), ra304.headers.get('Access-Control-Allow-Origin'), ra304.headers.get('X-Content-Type-Options')),
                                 (304, '0', 'http://127.0.0.1:8090', 'nosniff'))
            finally:
                await client.close()
        run(go())

    def test_qqmusic_stream_ssrf_304_and_json_guard(self):
        q = MusicSleepTests.q
        self.assertFalse(q._is_safe_stream_url('http://100.64.0.1/stream.mp3'))
        self.assertFalse(q._is_safe_stream_url('http://[::ffff:100.64.0.1]/stream.mp3'))
        self.assertFalse(q._is_safe_stream_url('http://127.0.0.1:8090/api/wake'))
        self.assertTrue(q._is_safe_stream_url('https://dl.stream.qqmusic.qq.com/C400001.flac'))
        self.assertTrue(q._is_safe_stream_url('https://183.3.226.35/stream.mp3'))

        async def go():
            ui_file = pathlib.Path(TMP.name, 'qq_ui.html')
            ui_file.write_text('<!doctype html><title>qq</title>', encoding='utf-8')
            calls = []
            fake_eng = types.SimpleNamespace(
                status=lambda **kw: {'ok': True},
                control=lambda a, i=None: calls.append((a, i)) or {'ok': True},
                sleep_mode=None,
            )
            with mock.patch.object(q, 'UI_HTML_PATH', ui_file), \
                 mock.patch.object(q, 'engine', fake_eng, create=True), \
                 mock.patch.object(q, 'watch_xiaozhi_events', mock.AsyncMock()):
                app = q.make_app()
                app.on_startup.clear()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    r = await client.get('/', headers={'Host': '127.0.0.1:8091'})
                    self.assertEqual(r.status, 200)
                    self.assertIn("frame-ancestors 'self'", r.headers.get('Content-Security-Policy', ''))
                    r304 = await client.get('/', headers={'Host': '127.0.0.1:8091', 'If-None-Match': r.headers['ETag']})
                    self.assertEqual((r304.status, r304.headers.get('Content-Length'), await r304.read()), (304, '0', b''))
                    self.assertIn("frame-ancestors 'self'", r304.headers.get('Content-Security-Policy', ''))
                    bad1 = await client.post('/api/music/control', data=b'not-json',
                                             headers={'Host': '127.0.0.1:8091', 'Content-Type': 'application/json'})
                    self.assertEqual(bad1.status, 400)
                    bad2 = await client.post('/api/music/control', data=b'[]',
                                             headers={'Host': '127.0.0.1:8091', 'Content-Type': 'application/json'})
                    self.assertEqual(bad2.status, 400)
                    self.assertEqual(calls, [])
                    xsite = await client.post('/api/music/control', json={'action': 'pause'},
                                              headers={'Host': '127.0.0.1:8091', 'Sec-Fetch-Site': 'cross-site'})
                    self.assertEqual(xsite.status, 403)
                finally:
                    await client.close()
        run(go())

    def test_intercom_cswsh_and_dns_rebinding_guard(self):
        inter = load('intercom_s15', 'intercom')
        self.assertFalse(inter._msg_origin_ok({
            'host': '127.0.0.1:18091',
            'origin': 'https://evil.example:18443',
            'sec-fetch-site': 'cross-site',
        }))
        self.assertFalse(inter._msg_origin_ok({
            'host': 'evil.example:18091',
            'origin': 'http://evil.example:18091',
        }))
        self.assertTrue(inter._msg_origin_ok({
            'host': '127.0.0.1:18091',
            'origin': 'https://gateway.example:18443',
            'sec-fetch-site': 'same-origin',
        }))


class Supplement16SecurityTests(unittest.TestCase):
    def test_intercom_same_origin_get_dns_rebind_and_xfh_spoof(self):
        inter = load('intercom_s16', 'intercom')
        # Same-origin GET (Origin omitted) from a DNS-rebound public host on :18091 MUST be rejected
        self.assertFalse(inter._msg_origin_ok({
            'host': 'evil.example:18091',
            'sec-fetch-site': 'same-origin',
        }))
        # X-Forwarded-Host spoofing when raw Host is non-loopback MUST be rejected
        self.assertFalse(inter._msg_origin_ok({
            'host': 'evil.example:18091',
            'x-forwarded-host': '127.0.0.1:18091',
            'origin': 'http://evil.example:18091',
        }))
        self.assertFalse(inter._msg_origin_ok({
            'host': 'evil.example:18091',
            'x-forwarded-host': '127.0.0.1:18091',
            'sec-fetch-site': 'same-origin',
        }))
        # Legitimate LAN and reverse-SSH gateway cases remain allowed
        self.assertTrue(inter._msg_origin_ok({
            'host': '192.168.1.88:18091',
            'sec-fetch-site': 'same-origin',
        }))
        self.assertTrue(inter._msg_origin_ok({
            'host': '127.0.0.1:18091',
            'x-forwarded-host': 'gateway.example:18443',
            'x-forwarded-proto': 'https',
            'sec-fetch-site': 'same-origin',
        }))

        async def go():
            app = web.Application()
            app.router.add_route('*', '/study/{tail:.*}', inter.handle_study)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r1 = await client.get('/study/api/parent/day', headers={
                    'Host': 'evil.example:18091',
                    'Sec-Fetch-Site': 'same-origin',
                })
                self.assertEqual(r1.status, 403)
                r2 = await client.get('/study/api/parent/day', headers={
                    'Host': 'evil.example:18091',
                    'X-Forwarded-Host': '127.0.0.1:18091',
                    'Sec-Fetch-Site': 'same-origin',
                })
                self.assertEqual(r2.status, 403)
            finally:
                await client.close()
        run(go())

    def test_qqmusic_redirect_ssrf_cookie_strip_and_usb_symlink_guard(self):
        q = MusicSleepTests.q
        import io
        import urllib.error
        import urllib.request
        self.assertFalse(q._is_safe_stream_url('http://127.0.0.1.nip.io/stream.mp3'))
        self.assertFalse(q._is_safe_stream_url('https://10.0.0.1.sslip.io/a.flac'))

        rh = q._SafeRedirectHandler()
        base_req = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            headers={'Cookie': 'uin=12345; qm_keyst=SECRET_VIP', 'User-Agent': 'test'},
        )
        # 1) Redirect to loopback blocked
        with self.assertRaises(urllib.error.URLError):
            rh.redirect_request(base_req, io.BytesIO(b''), 302, 'Found', {}, 'http://127.0.0.1:8090/api/wake')
        # 2) HTTPS -> HTTP downgrade blocked
        with self.assertRaises(urllib.error.URLError):
            rh.redirect_request(base_req, io.BytesIO(b''), 302, 'Found', {}, 'http://dl.stream.qqmusic.qq.com/a.mp3')
        # 3) Cross-domain redirect strips Cookie; same-root qq.com redirect preserves Cookie
        with mock.patch.object(q, '_has_only_global_ips', return_value=True):
            cross = rh.redirect_request(base_req, io.BytesIO(b'ok'), 302, 'Found', {}, 'https://cdn.example.org/a.flac')
            self.assertNotIn('Cookie', cross.headers)
            self.assertNotIn('Cookie', cross.unredirected_hdrs)
            same = rh.redirect_request(base_req, io.BytesIO(b'ok'), 302, 'Found', {}, 'https://c.y.qq.com/lyric')
            self.assertEqual(same.headers.get('Cookie'), 'uin=12345; qm_keyst=SECRET_VIP')

        # 4) search_local refuses symlinks escaping LOCAL_DIRS
        usb_root = pathlib.Path(TMP.name, 'usb_s16')
        outside_root = pathlib.Path(TMP.name, 'outside_s16')
        usb_root.mkdir(exist_ok=True)
        outside_root.mkdir(exist_ok=True)
        inside_mp3 = usb_root / 'allowed_song.mp3'
        outside_mp3 = outside_root / 'secret_outside.mp3'
        inside_mp3.write_bytes(b'ID3')
        outside_mp3.write_bytes(b'ID3')
        esc_link = usb_root / 'escape_link.mp3'
        if not esc_link.exists():
            esc_link.symlink_to(outside_mp3)
        esc_dir = usb_root / 'escape_dir'
        if not esc_dir.exists():
            esc_dir.symlink_to(outside_root)
        with mock.patch.object(q, 'LOCAL_DIRS', [usb_root]):
            found = q.search_local('')
        titles = [x['title'] for x in found]
        self.assertEqual(titles, ['allowed_song'])

    def test_loopback_localhost_cross_site_compat_and_full_csp(self):
        q = MusicSleepTests.q

        async def go():
            box = HOME.Inbox(pathlib.Path(TMP.name, 'inbox_s16', 'messages.json'))
            app_h = HOME.make_app(box)
            ch = TestClient(TestServer(app_h))
            await ch.start_server()
            try:
                # http://localhost:8090 -> http://127.0.0.1:8095 sends Sec-Fetch-Site: cross-site with allowed Origin
                ok_cors = await ch.get('/api/home/inbox', headers={
                    'Host': '127.0.0.1:8095',
                    'Origin': 'http://localhost:8090',
                    'Sec-Fetch-Site': 'cross-site',
                })
                self.assertEqual(ok_cors.status, 200)
                ok_iframe = await ch.get('/', headers={
                    'Host': '127.0.0.1:8095',
                    'Sec-Fetch-Site': 'cross-site',
                    'Sec-Fetch-Dest': 'iframe',
                })
                self.assertEqual(ok_iframe.status, 200)
                evil_cors = await ch.get('/api/home/inbox', headers={
                    'Host': '127.0.0.1:8095',
                    'Origin': 'https://evil.example',
                    'Sec-Fetch-Site': 'cross-site',
                })
                self.assertEqual(evil_cors.status, 403)
            finally:
                await ch.close()

            ui_file = pathlib.Path(TMP.name, 'qq_ui_s16.html')
            ui_file.write_text('<!doctype html><title>qq</title>', encoding='utf-8')
            captured = []
            fake_eng = types.SimpleNamespace(
                status=lambda **kw: {'ok': True},
                control=lambda a, i=None: {'ok': True},
                play_tracks=lambda trs, idx, open_ui: captured.extend(trs) or {'ok': True},
                _safe_dur=lambda d: 180,
                sleep_mode=None,
            )
            with mock.patch.object(q, 'UI_HTML_PATH', ui_file), \
                 mock.patch.object(q, 'engine', fake_eng, create=True), \
                 mock.patch.object(q, 'watch_xiaozhi_events', mock.AsyncMock()):
                app_q = q.make_app()
                app_q.on_startup.clear()
                cq = TestClient(TestServer(app_q))
                await cq.start_server()
                try:
                    ri = await cq.get('/', headers={
                        'Host': '127.0.0.1:8091',
                        'Sec-Fetch-Site': 'cross-site',
                        'Sec-Fetch-Dest': 'iframe',
                    })
                    self.assertEqual(ri.status, 200)
                    csp = ri.headers.get('Content-Security-Policy', '')
                    self.assertIn("default-src 'self'", csp)
                    self.assertIn("frame-ancestors 'self'", csp)
                    rc = await cq.post('/api/music/control', json={'action': 'pause'}, headers={
                        'Host': '127.0.0.1:8091',
                        'Origin': 'http://localhost:8090',
                        'Sec-Fetch-Site': 'cross-site',
                    })
                    self.assertEqual(rc.status, 200)
                    rp = await cq.post('/api/music/play_tracks', json={
                        'tracks': [{
                            'mid': '001abc',
                            'title': 'Song',
                            'cover': 'https://evil.tracker.example/pixel.png',
                            'thumb': 'https://y.gtimg.cn/music/photo_new/T002R150x150M000abc.jpg',
                        }],
                        'index': 0,
                    }, headers={'Host': '127.0.0.1:8091'})
                    self.assertEqual(rp.status, 200)
                    self.assertEqual(captured[0]['cover'], '')
                    self.assertTrue(captured[0]['thumb'].startswith('https://y.gtimg.cn/'))
                finally:
                    await cq.close()
        run(go())

    def test_study_append_log_cap_and_bridge_flush(self):
        s = StudyFocusTests.s
        log_dir = pathlib.Path(TMP.name, 'study_logs_s16')
        log_dir.mkdir(exist_ok=True)
        with mock.patch.object(s, 'LOG_DIR', log_dir):
            e1 = s.Companion.append_log({'event': 'on', 'by': 'test', 'at': 1762680000.0})
            date_iso = s.local_now(e1['at']).date().isoformat()
            lp = log_dir / f'{date_iso}.jsonl'
            # Simulate file near 8 MiB cap so next append cannot push it over 8 MiB
            with open(lp, 'ab') as f:
                f.write(b' ' * (8 * 1024 * 1024 - lp.stat().st_size - 4))
            size_before = lp.stat().st_size
            s.Companion.append_log({'event': 'off', 'by': 'test', 'at': 1762680010.0})
            self.assertEqual(lp.stat().st_size, size_before)
            self.assertEqual(len(s.read_day(date_iso)), 1)

        b = load('bridge_s16', 'bridge')
        seq0 = b._flush_seq
        b._pcm_q.put_nowait('frame1')
        b._clear_pcm_q()
        self.assertTrue(b._pcm_q.empty())
        self.assertGreaterEqual(b._flush_seq - seq0, 2)

    def test_qqmusic_smartbox_suggest_and_pinyin_ui(self):
        import io
        q = MusicSleepTests.q
        q._SMARTBOX_CACHE.clear()
        fake_sb = {
            'data': {
                'singer': {'itemlist': [{'name': '周杰伦'}]},
                'song': {'itemlist': [{'name': '七里香 (Live)', 'singer': '周杰伦'}]},
            }
        }

        class _FakeResp(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.close()

        with mock.patch.object(q, '_safe_urlopen', return_value=_FakeResp(json.dumps(fake_sb).encode('utf-8'))):
            sb = q.qq_smartbox('qilixiang\x00')
        self.assertTrue(sb['ok'])
        self.assertIn('周杰伦', sb['words'])
        self.assertIn('七里香', sb['words'])
        self.assertEqual(sb['items'][0]['type'], 'singer')
        self.assertEqual(sb['items'][1]['title'], '七里香 (Live)')
        self.assertIsNotNone(q._get_cached_smartbox('qilixiang'))

        # Expired musickey (code=2001) on qq_search automatically retries cookie-less and returns songs
        calls = []
        def _fake_search_open(req_obj, timeout=5):
            calls.append(bool(req_obj.headers.get('Cookie') or req_obj.unredirected_hdrs.get('Cookie')))
            if len(calls) == 1:
                return _FakeResp(json.dumps({'req': {'code': 2001, 'data': {'body': {'song': {'list': []}}}}}).encode())
            return _FakeResp(json.dumps({
                'req': {'code': 0, 'data': {'body': {'song': {'list': [
                    {'mid': '004Z8Ihr0JIu5s', 'name': '七里香', 'singer': [{'name': '周杰伦'}], 'interval': 299}
                ]}}}}
            }).encode())

        with mock.patch.object(q, 'load_config', return_value={'uin': '12345', 'qm_keyst': 'EXPIRED', 'cookies_str': 'qm_keyst=EXPIRED'}), \
             mock.patch.object(q, '_refresh_musickey_from_cookies', return_value=False) as m_ref, \
             mock.patch.object(q, '_safe_urlopen', side_effect=_fake_search_open):
            found_songs = q.qq_search('七里香', limit=5, page=1)
        self.assertEqual(calls, [True, False])
        m_ref.assert_called_once()
        self.assertEqual(len(found_songs), 1)
        self.assertEqual(found_songs[0]['title'], '七里香')

        async def go():
            ui_file = pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')
            html_txt = ui_file.read_text(encoding='utf-8')
            self.assertIn('PY_IME_DATA', html_txt)
            self.assertIn('getImeCandidates', html_txt)
            self.assertIn('toggleKbdLang', html_txt)

            fake_eng = types.SimpleNamespace(
                status=lambda **kw: {'ok': True},
                control=lambda a, i=None: {'ok': True},
                _safe_dur=lambda d: 180,
                sleep_mode=None,
            )
            with mock.patch.object(q, 'UI_HTML_PATH', ui_file), \
                 mock.patch.object(q, 'engine', fake_eng, create=True), \
                 mock.patch.object(q, 'watch_xiaozhi_events', mock.AsyncMock()):
                app_q = q.make_app()
                app_q.on_startup.clear()
                cq = TestClient(TestServer(app_q))
                await cq.start_server()
                try:
                    r_hit = await cq.get('/api/music/suggest?q=qilixiang', headers={'Host': '127.0.0.1:8091'})
                    self.assertEqual(r_hit.status, 200)
                    d_hit = await r_hit.json()
                    self.assertIn('七里香', d_hit['words'])
                    r_empty = await cq.get('/api/music/suggest?q=', headers={'Host': '127.0.0.1:8091'})
                    self.assertEqual(r_empty.status, 200)
                    d_empty = await r_empty.json()
                    self.assertEqual(d_empty['words'], [])
                finally:
                    await cq.close()
        run(go())

    def test_supplement18_security_and_ime_hardening(self):
        import io
        import urllib.request
        q = MusicSleepTests.q

        # 1) Offline boot safety: thinksmart-home.service must not block kiosk on network-online.target
        svc_path = (
            pathlib.Path(STAGE, 'thinksmart-home.service')
            if STAGE else pathlib.Path('/etc/systemd/system/thinksmart-home.service')
        )
        svc_txt = svc_path.read_text(encoding='utf-8')
        self.assertIn('After=network.target', svc_txt)
        self.assertNotIn('network-online.target', svc_txt)

        # 2) Inbox write-before-mutate + clean 503 on disk OSError
        box_path = pathlib.Path(TMP.name, 'inbox_s18', 'messages.json')
        box = HOME.Inbox(box_path)
        item = box.add('测试留言', '妈妈')
        self.assertEqual(len(box.unread()), 1)
        with mock.patch.object(HOME, '_write_private_json', side_effect=OSError('ENOSPC')):
            with self.assertRaises(OSError):
                box.add('会失败的留言', '爸爸')
            self.assertEqual(len(box.items), 1)
            with self.assertRaises(OSError):
                box.ack(item['id'])
            self.assertEqual(len(box.unread()), 1)

        # 3) _refresh_musickey_from_cookies post-logout guard + 60s failure cooldown
        q._LAST_REFRESH_FAIL = (None, 0.0)

        class _FakeResp(io.BytesIO):
            headers = {'Location': 'https://y.qq.com/portal/wx_redirect.html?code=FAKECODE123'}
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.close()

        class _FakeOpener:
            def open(self, req, timeout=5):
                return _FakeResp(b'')

        logged_in_cfg = {'uin': '88888', 'qm_keyst': 'OLD', 'cookies_str': 'uin=88888; p_skey=PSKEY1; qm_keyst=OLD'}
        logged_out_cfg = {'uin': '', 'qm_keyst': '', 'cookies_str': ''}
        cfg_reads = [logged_in_cfg, logged_out_cfg]
        mk_resp = json.dumps({'req': {'data': {'musickey': 'NEW_VIP_KEY_123456'}}}).encode('utf-8')
        with mock.patch.object(q, 'load_config', side_effect=lambda: dict(cfg_reads.pop(0))), \
             mock.patch.object(urllib.request, 'build_opener', return_value=_FakeOpener()), \
             mock.patch.object(q, '_safe_urlopen', return_value=_FakeResp(mk_resp)), \
             mock.patch.object(q, 'save_config') as m_save:
            ok_ref = q._refresh_musickey_from_cookies()
        self.assertFalse(ok_ref)
        m_save.assert_not_called()

        # Cooldown suppresses repeated failed OAuth calls for 60s
        q._LAST_REFRESH_FAIL = (None, 0.0)
        opener_calls = []

        class _FailOpener:
            def open(self, req, timeout=5):
                opener_calls.append(1)
                raise OSError('network down')

        with mock.patch.object(q, 'load_config', return_value=dict(logged_in_cfg)), \
             mock.patch.object(urllib.request, 'build_opener', return_value=_FailOpener()):
            self.assertFalse(q._refresh_musickey_from_cookies())
            self.assertFalse(q._refresh_musickey_from_cookies())
        self.assertEqual(len(opener_calls), 1)
        q._LAST_REFRESH_FAIL = (None, 0.0)

        # 4) :18443 Origin fallback requires Sec-Fetch-Site: same-origin, + _safe_cover_url blocks /events & /\evil
        inter = load('intercom_s18', 'intercom')

        async def go():
            app_h = HOME.make_app(box)
            ch = TestClient(TestServer(app_h))
            await ch.start_server()
            try:
                with mock.patch.object(HOME, '_write_private_json', side_effect=OSError('ENOSPC')):
                    r503 = await ch.post('/api/home/inbox/ack', json={'id': item['id']}, headers={
                        'Host': '127.0.0.1:8095',
                        'Origin': 'http://127.0.0.1:8095',
                    })
                    self.assertEqual(r503.status, 503)
            finally:
                await ch.close()

            app_i = web.Application()
            app_i.router.add_route('*', '/study/{tail:.*}', inter.handle_study)
            ci = TestClient(TestServer(app_i))
            await ci.start_server()
            try:
                # Reverse-SSH :18443 Origin fallback without Sec-Fetch-Site: same-origin must be 403
                r_spoof = await ci.get('/study/api/parent/day', headers={
                    'Host': '127.0.0.1:18091',
                    'Origin': 'https://gateway.example:18443',
                })
                self.assertEqual(r_spoof.status, 403)
            finally:
                await ci.close()

            ui_file = pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')
            html_txt = ui_file.read_text(encoding='utf-8')
            self.assertIn('resetSearchState', html_txt)
            self.assertIn('INIT_PRIORITY', html_txt)
            self.assertNotIn(r'\/(?!\/)', html_txt)

            captured = []
            fake_eng = types.SimpleNamespace(
                status=lambda **kw: {'ok': True},
                control=lambda a, i=None: {'ok': True},
                play_tracks=lambda trs, idx, open_ui: captured.extend(trs) or {'ok': True},
                _safe_dur=lambda d: 180,
                sleep_mode=None,
            )
            with mock.patch.object(q, 'UI_HTML_PATH', ui_file), \
                 mock.patch.object(q, 'engine', fake_eng, create=True), \
                 mock.patch.object(q, 'watch_xiaozhi_events', mock.AsyncMock()):
                app_q = q.make_app()
                app_q.on_startup.clear()
                cq = TestClient(TestServer(app_q))
                await cq.start_server()
                try:
                    rp = await cq.post('/api/music/play_tracks', json={
                        'tracks': [{
                            'mid': '002xyz',
                            'title': 'SafeCoverTest',
                            'cover': '/events',
                            'thumb': '/\\evil.example/a.jpg',
                        }],
                        'index': 0,
                    }, headers={'Host': '127.0.0.1:8091'})
                    self.assertEqual(rp.status, 200)
                    self.assertEqual(captured[0]['cover'], '')
                    self.assertEqual(captured[0]['thumb'], '')
                finally:
                    await cq.close()
        run(go())

    def test_supplement19_hardening(self):
        q = MusicSleepTests.q
        s = StudyFocusTests.s
        intercom_mod = load('intercom_s19', 'intercom')
        mon_mod = load('monitor_s19', 'monitor')

        # 1. QR error sanitization: never leak raw Python exception text to LAN clients
        with mock.patch.object(q, '_safe_urlopen', side_effect=RuntimeError('internal-socket-detail-99')):
            r_qr1 = q.qq_qr_create('qq')
            r_qr2 = q.qq_qr_check('valid_sig_123', 'qq')
        self.assertFalse(r_qr1['ok'])
        self.assertNotIn('internal-socket-detail-99', r_qr1['error'])
        self.assertFalse(r_qr2['ok'])
        self.assertNotIn('internal-socket-detail-99', r_qr2['error'])

        # 2. qq_user_playlists & qq_playlist_tracks recover from expired qm_keyst (code=2001) without poisoning _DIR201_CACHE
        class _FakeHttpResp:
            def __init__(self, obj):
                self._raw = json.dumps(obj).encode('utf-8')
            def read(self, n=-1):
                if not self._raw:
                    return b''
                if n is None or n < 0:
                    b, self._raw = self._raw, b''
                    return b
                b, self._raw = self._raw[:n], self._raw[n:]
                return b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        calls_201 = []
        def _fake_urlopen(req, timeout=5):
            url = req.full_url
            if 'fcg_user_created_diss' in url:
                return _FakeHttpResp({'data': {'disslist': []}})
            body = json.loads((req.data or b'{}').decode('utf-8'))
            has_auth = 'Cookie' in dict(req.header_items()) or 'g_tk' in (body.get('comm') or {})
            calls_201.append(has_auth)
            if has_auth:
                return _FakeHttpResp({'code': 0, 'req': {'code': 2001, 'data': {}}})
            return _FakeHttpResp({'code': 0, 'req': {'code': 0, 'data': {
                'dirinfo': {'id': 9988, 'songnum': 1},
                'songlist': [{'mid': '001abc', 'title': '晴天', 'singer': [{'name': '周杰伦'}], 'interval': 269}],
            }}})

        q._DIR201_CACHE = None
        with mock.patch.object(q, 'load_config', return_value={'uin': '123456', 'qm_keyst': 'expired_k', 'cookies_str': 'uin=123456; qm_keyst=expired_k'}), \
             mock.patch.object(q, '_refresh_musickey_from_cookies', return_value=False), \
             mock.patch.object(q, '_safe_urlopen', side_effect=_fake_urlopen):
            pls = q.qq_user_playlists()
            self.assertEqual(len(pls), 1)
            self.assertEqual(pls[0]['count'], 1)
            q._DIR201_CACHE = None
            trs = q.qq_playlist_tracks('dir_201', limit=20)
            self.assertEqual(len(trs), 1)
            self.assertEqual(trs[0]['mid'], '001abc')
        self.assertGreaterEqual(len(calls_201), 4)

        # 3. Study Companion.__init__ tightens 0644 secret to 0600 + parent CSP + Sec-Fetch-Site: same-site block
        sec_dir = pathlib.Path(TMP.name, 'study_s19')
        sec_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chmod(sec_dir, 0o755)
        sec_file = sec_dir / 'secret'
        sec_file.write_text('aa' * 32 + '\n', encoding='utf-8')
        os.chmod(sec_file, 0o644)
        parent_p = pathlib.Path(STAGE, 'parent.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-study/parent.html')

        async def go():
            with mock.patch.object(s, 'SECRET_PATH', sec_file), \
                 mock.patch.object(s, 'PARENT_PAGE', parent_p):
                comp = s.Companion()
                self.assertIsNotNone(comp.secret)
                if os.name == 'posix':
                    self.assertEqual(sec_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(sec_dir.stat().st_mode & 0o777, 0o700)
                comp.supervise = lambda: asyncio.sleep(3600)
                cs = TestClient(TestServer(s.app_for(comp)))
                await cs.start_server()
                try:
                    rp = await cs.get('/', headers={'Host': '127.0.0.1:8094'})
                    self.assertEqual(rp.status, 200)
                    self.assertIn("default-src 'self'", rp.headers.get('Content-Security-Policy', ''))
                    r_ss = await cs.get('/api/parent/state', headers={'Host': '127.0.0.1:8094', 'Sec-Fetch-Site': 'same-site'})
                    self.assertEqual(r_ss.status, 403)
                    r_so = await cs.get('/api/parent/state', headers={'Host': '127.0.0.1:8094', 'Sec-Fetch-Site': 'same-origin'})
                    self.assertEqual(r_so.status, 200)
                finally:
                    await cs.close()

            # 4. Monitor & Intercom CSP + nosniff headers
            mon_assets = pathlib.Path(TMP.name, 'mon_assets_s19')
            mon_assets.mkdir(parents=True, exist_ok=True)
            (mon_assets / 'servers.json').write_text('[]', encoding='utf-8')
            (mon_assets / 'index.html').write_text('<!doctype html><title>m</title>', encoding='utf-8')
            (mon_assets / 'display.html').write_text('<!doctype html><title>d</title>', encoding='utf-8')
            m_inst = mon_mod.Monitor(mon_assets)
            m_inst.consume = mock.AsyncMock()
            cm = TestClient(TestServer(m_inst.app()))
            await cm.start_server()
            try:
                rm = await cm.get('/', headers={'Host': '127.0.0.1:8092'})
                self.assertEqual(rm.status, 200)
                self.assertIn("default-src 'self'", rm.headers.get('Content-Security-Policy', ''))
                rh = await cm.get('/api/health', headers={'Host': '127.0.0.1:8092'})
                self.assertEqual(rh.headers.get('X-Content-Type-Options'), 'nosniff')
            finally:
                await cm.close()

            self.assertIn('Content-Security-Policy', intercom_mod.STUDY_FWD_RESP)
            srv_i = object.__new__(intercom_mod.IntercomServer)
            app_i = web.Application()
            app_i.router.add_get('/', srv_i.handle_index)
            ci = TestClient(TestServer(app_i))
            await ci.start_server()
            try:
                ri = await ci.get('/')
                self.assertEqual(ri.status, 200)
                self.assertIn("default-src 'self'", ri.headers.get('Content-Security-Policy', ''))
            finally:
                await ci.close()

            # 5. Frontend S19 invariants
            qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
            home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
            ov_js = (pathlib.Path(STAGE, 'home_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')).read_text(encoding='utf-8')
            self.assertIn('PY_SPACED', qq_ui)
            self.assertIn("cmd: 'reloading'", qq_ui)
            self.assertIn("d.cmd === 'reloading'", ov_js)
            self.assertIn('inboxAckEpoch', ov_js)
            self.assertIn('msgFetchQueued', home_ui)
            self.assertIn('slBase', home_ui)
        run(go())

    def test_supplement20_hardening(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

        q = MusicSleepTests.q
        intercom_mod = load('intercom_s20', 'intercom')
        mon_mod = load('monitor_s20', 'monitor')

        async def go():
            # 1. _DIR201_CACHE cleared on logout & bind_uin
            fake_cfg = {'uin': '123456', 'qm_keyst': '', 'cookies_str': ''}
            with mock.patch.object(q, 'load_config', side_effect=lambda: dict(fake_cfg)), \
                 mock.patch.object(q, 'save_config', side_effect=lambda c: fake_cfg.update(c)):
                q._DIR201_CACHE = ('123456', 'SQ 无损 FLAC', time.monotonic(), [{'mid': 'm1'}])
                res_bind = q.qq_bind_uin('888888')
                self.assertTrue(res_bind['ok'])
                self.assertIsNone(q._DIR201_CACHE)
                q._DIR201_CACHE = ('888888', 'SQ 无损 FLAC', time.monotonic(), [{'mid': 'm2'}])
                res_out = q.qq_logout()
                self.assertTrue(res_out['ok'])
                self.assertIsNone(q._DIR201_CACHE)

            # 2. resolve_hires_fallback rejects malformed bvid
            import io

            class _FakeSearchResp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    self.close()

            bad_search = json.dumps({'data': {'result': [{'bvid': 'BV123/../../bad', 'title': 't'}]}}).encode('utf-8')
            fake_opener = mock.MagicMock()
            fake_opener.open.side_effect = [
                _FakeSearchResp(b'ok'),
                _FakeSearchResp(bad_search),
            ]
            with mock.patch('urllib.request.build_opener', return_value=fake_opener):
                self.assertIsNone(q.resolve_hires_fallback('Song', 'Artist'))
            self.assertEqual(fake_opener.open.call_count, 2)

            # 2b. qq_search falls back cleanly to shc.y.qq.com when musicu.fcg returns code=2001
            shc_payload = json.dumps({
                'code': 0,
                'data': {'song': {'list': [{
                    'songmid': '004Z8Ihr0JIu5s',
                    'songname': '七里香',
                    'singer': [{'name': '周杰伦'}],
                    'albumname': '七里香',
                    'albummid': '003DFRzD192KKD',
                    'interval': 299,
                    'pay': {'payplay': 1},
                }]}},
            }).encode('utf-8')
            musicu_2001 = json.dumps({'code': 0, 'req': {'code': 2001}}).encode('utf-8')
            with mock.patch.object(q, 'load_config', return_value={'uin': '', 'qm_keyst': '', 'cookies_str': ''}), \
                 mock.patch.object(q, '_safe_urlopen', side_effect=[_FakeSearchResp(musicu_2001), _FakeSearchResp(shc_payload)]):
                fb_tracks = q.qq_search('七里香', 10, 1)
            self.assertEqual(len(fb_tracks), 1)
            self.assertEqual(fb_tracks[0]['mid'], '004Z8Ihr0JIu5s')
            self.assertEqual(fb_tracks[0]['title'], '七里香')
            self.assertTrue(fb_tracks[0]['vip'])

            # 3. POST /api/music/control requires non-empty string action + LAN same-site Sec-Fetch-Site blocked
            ctrl_calls = []
            fake_eng = types.SimpleNamespace(
                status=lambda **kw: {'ok': True},
                control=lambda a, i=None: ctrl_calls.append(a) or {'ok': True},
            )
            with mock.patch.object(q, 'engine', fake_eng, create=True), \
                 mock.patch.object(q, 'watch_xiaozhi_events', mock.AsyncMock()):
                app_q = q.make_app()
                app_q.on_startup.clear()
                cq = TestClient(TestServer(app_q))
                await cq.start_server()
                try:
                    rc_empty = await cq.post('/api/music/control', json={}, headers={'Host': '127.0.0.1:8091'})
                    self.assertEqual(rc_empty.status, 400)
                    rc_type = await cq.post('/api/music/control', json={'action': 123}, headers={'Host': '127.0.0.1:8091'})
                    self.assertEqual(rc_type.status, 400)
                    self.assertEqual(ctrl_calls, [])
                finally:
                    await cq.close()

                mw = app_q.middlewares[0]
                lan_req = make_mocked_request(
                    'GET', '/api/music/status',
                    headers={'Host': '192.168.1.88:8091', 'Sec-Fetch-Site': 'same-site'},
                ).clone(remote='192.168.1.100')
                with self.assertRaises(web.HTTPForbidden):
                    await mw(lan_req, lambda _r: asyncio.sleep(0))

            # 4. Intercom nosniff + _MSG_RELAY_EPOCH guards concurrent GET vs POST race
            app_i = web.Application()
            app_i.on_response_prepare.append(intercom_mod.keepalive_304)

            async def _ping(_r):
                return web.json_response({'ok': True})

            app_i.router.add_get('/api/ping', _ping)
            ci = TestClient(TestServer(app_i))
            await ci.start_server()
            try:
                rp = await ci.get('/api/ping')
                self.assertEqual(rp.headers.get('X-Content-Type-Options'), 'nosniff')
            finally:
                await ci.close()
            self.assertTrue(hasattr(intercom_mod, '_MSG_RELAY_EPOCH'))

            # 5. Monitor CSP frame-src allows 127.0.0.1:8092 & localhost:8092 + S20 frontend invariants
            mon_assets = pathlib.Path(TMP.name, 'mon_assets_s20')
            mon_assets.mkdir(exist_ok=True)
            (mon_assets / 'index.html').write_text('<!doctype html><title>m</title>')
            (mon_assets / 'servers.json').write_text('[]')
            m_inst = mon_mod.Monitor(mon_assets)
            m_inst.consume = mock.AsyncMock()
            cm = TestClient(TestServer(m_inst.app()))
            await cm.start_server()
            try:
                rm = await cm.get('/', headers={'Host': 'localhost:8092'})
                self.assertEqual(rm.status, 200)
                self.assertIn('frame-src \'self\' http://127.0.0.1:8092 http://localhost:8092', rm.headers.get('Content-Security-Policy', ''))
            finally:
                await cm.close()

            qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
            home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
            self.assertIn('"shei":"谁"', qq_ui)
            self.assertIn('"zhei":"这"', qq_ui)
            self.assertIn('isSingleExactSyl', qq_ui)
            self.assertIn('isComposing', qq_ui)
            self.assertIn('resetCalDrag', home_ui)
        run(go())

    def test_supplement21_home_seek_and_clean_xiaozhi(self):
        from aiohttp.test_utils import TestClient, TestServer

        q = MusicSleepTests.q

        async def go():
            # 1. Home POST /api/home/music {action: 'seek', sec: ...} validation & proxy
            seek_calls = []

            async def fake_post_json(_sess, url, payload, *_a):
                if url.endswith('/seek'):
                    seek_calls.append(payload)
                    return 200, {'ok': True, 'elapsed': payload['sec']}
                return 200, {'ok': True}

            box = HOME.Inbox(pathlib.Path(TMP.name, 'inbox_s21', 'messages.json'))
            with mock.patch.object(HOME, '_post_json', side_effect=fake_post_json):
                ch = TestClient(TestServer(HOME.make_app(box)))
                await ch.start_server()
                try:
                    for bad in ({'action': 'seek'}, {'action': 'seek', 'sec': True}, {'action': 'seek', 'sec': -1}, {'action': 'seek', 'sec': '50'}):
                        rb = await ch.post('/api/home/music', json=bad, headers={'Host': '127.0.0.1:8095'})
                        self.assertEqual(rb.status, 400)
                    rg = await ch.post('/api/home/music', json={'action': 'seek', 'sec': 92.4}, headers={'Host': '127.0.0.1:8095'})
                    self.assertEqual(rg.status, 200)
                    jd = await rg.json()
                    self.assertEqual((jd.get('ok'), jd.get('elapsed')), (True, 92.4))
                    self.assertEqual(seek_calls, [{'sec': 92.4}])
                finally:
                    await ch.close()

            # 2. QQMusicEngine.seek works when paused AND when stopped (without auto-starting audio)
            e = MusicSleepTests().engine()
            e._proc = None
            e._pacat = None
            e.playing = False
            e.paused = False
            res_stopped = e.seek(66.5)
            self.assertEqual(res_stopped, {'ok': True, 'elapsed': 66.5})
            self.assertTrue(e.playing and e.paused)
            self.assertEqual(e.started, [])

            # 3. Frontend S21 invariants: XiaoZhi dock removed, voice open_ui_seq kept, Home #mBar/#mThumb slider present
            ko_js = (pathlib.Path(STAGE, 'kiosk_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js')).read_text(encoding='utf-8')
            home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
            self.assertNotIn('tsQqMusicDock', ko_js)
            self.assertNotIn('tsQqMiniCtrls', ko_js)
            self.assertIn('open_ui_seq', ko_js)
            self.assertIn('__tsOpenSub', ko_js)
            self.assertIn('id="mBar"', home_ui)
            self.assertIn('id="mThumb"', home_ui)
            self.assertIn("action: 'seek'", home_ui)
        run(go())

    def test_supplement22_progress_bar_polish_and_fast_seek(self):
        q = MusicSleepTests.q

        # 1. _SAFE_FFMPEG_URL_CACHE eliminates redundant Range probes on same-track seeks while re-checking DNS global IP
        probe_calls = []

        class _FakeResp:
            status = 206
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeOpener:
            def open(self, req, timeout=3.5):
                probe_calls.append((req.full_url, req.get_header('Range')))
                return _FakeResp()

        q._SAFE_FFMPEG_URL_CACHE.clear()
        stream_url = 'https://dl.stream.qqmusic.qq.com/C400001TestSeek.m4a?guid=1&vkey=xyz'
        with mock.patch.object(q.urllib.request, 'urlopen', q._ORIG_URLOPEN), \
             mock.patch.object(q.urllib.request, 'build_opener', return_value=_FakeOpener()), \
             mock.patch.object(q, '_has_only_global_ips', return_value=True):
            r1 = q._resolve_safe_ffmpeg_url(stream_url, {'Referer': 'https://y.qq.com/'})
            r2 = q._resolve_safe_ffmpeg_url(stream_url, {'Referer': 'https://y.qq.com/'})
            self.assertEqual(r1, stream_url)
            self.assertEqual(r2, stream_url)
            self.assertEqual(len(probe_calls), 1)

        # Even with warm cache, DNS rebinding to non-global IP is rejected immediately
        with mock.patch.object(q.urllib.request, 'urlopen', q._ORIG_URLOPEN), \
             mock.patch.object(q, '_has_only_global_ips', return_value=False):
            self.assertEqual(q._resolve_safe_ffmpeg_url(stream_url, {'Referer': 'https://y.qq.com/'}), '')
        q._SAFE_FFMPEG_URL_CACHE.clear()

        # 2. Frontend S22 ergonomics & zero-layout compositing invariants
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        self.assertIn('id="mSeekTip"', home_ui)
        self.assertIn('mGrabNearThumb', home_ui)
        self.assertIn('mSeekOffCancel', home_ui)
        self.assertIn('松手取消', home_ui)
        self.assertIn('height: 44px', home_ui)
        self.assertNotIn('thumb.style.left', home_ui)

    def test_supplement23_music_ambient_voice_msg_and_timer(self):
        import math
        import struct
        from aiohttp.test_utils import TestClient, TestServer

        q = MusicSleepTests.q
        b = load('bridge_r109_s23', 'bridge')

        # 1. MCP tools include natural ambient soundscapes & desktop countdown timer
        names = [t['name'] for t in b.MUSIC_TOOLS]
        self.assertIn('self.music.ambient', names)
        self.assertIn('self.home.timer', names)
        amb_tool = next(t for t in b.MUSIC_TOOLS if t['name'] == 'self.music.ambient')
        self.assertEqual(
            amb_tool['inputSchema']['properties']['preset']['enum'],
            [
                'rain', 'waves', 'forest', 'stream', 'night', 'sleep_piano',
                'fireplace', 'library', 'singing_bowl',
                'white_noise', 'pink_noise', 'brown_noise',  # 2026-11-12 (Claude): generated noises
                'deep_study', 'cozy_read', 'deep_sleep', 'off',
            ],
        )

        # 2. All 9 natural study/sleep presets registered + fallback stereo PCM loop works offline
        self.assertEqual(
            tuple(q.AMBIENT_PRESETS.keys()),
            ('rain', 'waves', 'forest', 'stream', 'night', 'sleep_piano', 'fireplace', 'library', 'singing_bowl',
             'white_noise', 'pink_noise', 'brown_noise'),  # 2026-11-12 (Claude): + three generated noises
        )
        for p in q.AMBIENT_PRESETS:
            pcm = q._fallback_ambient_pcm(p)
            self.assertEqual(len(pcm), q.OUT_RATE * 4 * 2 * 4)

        # 3. Song bookmarking (favorites.json / local_fav) removed in favor of direct dir_201 我喜欢 navigation
        self.assertFalse(hasattr(q, 'FAV_PATH'))
        self.assertFalse(hasattr(q, 'update_favorite_track'))

        # 4. Two-way voice messages (WAV -> MP3, ETag 304, screen reply direction) & Desktop Timer API
        async def go():
            box = HOME.Inbox(pathlib.Path(TMP.name, 'inbox_s23', 'messages.json'))
            app_h = HOME.make_app(box)
            ch = TestClient(TestServer(app_h))
            await ch.start_server()
            try:
                # Build 1.2s 16kHz 440Hz sine WAV (11-12: voice under 1 s is not sent)
                ns = int(16000 * 1.2)
                samples = array.array('h', [int(14000 * math.sin(2.0 * math.pi * 440.0 * i / 16000.0)) for i in range(ns)])
                pcm_b = samples.tobytes()
                wav_hdr = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + len(pcm_b), b'WAVE', b'fmt ', 16, 1, 1, 16000, 32000, 2, 16, b'data', len(pcm_b))
                rv = await ch.post(
                    '/api/home/messages/voice',
                    data=wav_hdr + pcm_b,
                    headers=dict(H8095, **{'Content-Type': 'audio/wav', 'X-TS-Internal': '1'}),
                )
                self.assertEqual(rv.status, 200)
                jv = await rv.json()
                vid = jv['message']['id']
                self.assertEqual(jv['message']['kind'], 'voice')
                self.assertEqual(len(box.unread()), 1)

                ra = await ch.get(f'/api/home/messages/audio/{vid}', headers=H8095)
                self.assertEqual((ra.status, ra.headers.get('Content-Type')), (200, 'audio/mpeg'))
                mp3_out = await ra.read()
                self.assertGreater(len(mp3_out), 200)
                ra304 = await ch.get(f'/api/home/messages/audio/{vid}', headers=dict(H8095, **{'If-None-Match': ra.headers['ETag']}))
                self.assertEqual(ra304.status, 304)

                # Screen text reply does NOT count as screen-unread, and phone ack marks it read
                rr = await ch.post('/api/home/messages/reply', json={'text': '👌 收到啦'}, headers=H8095)
                self.assertEqual(rr.status, 200)
                rid = (await rr.json())['message']['id']
                self.assertEqual(len(box.unread()), 1)  # still only the 1 phone voice msg
                rak = await ch.post('/api/home/messages/ack_phone', json={'id': rid}, headers=dict(H8095, **{'X-TS-Internal': '1'}))
                self.assertEqual(rak.status, 200)
                self.assertTrue(any(m['id'] == rid and m['read_at'] for m in box.recent(5)))

                # Desktop countdown timer lifecycle: start -> pause -> resume -> add -> stop
                rt1 = await ch.post('/api/home/timer', json={'action': 'start', 'seconds': 900, 'label': '做题限时'}, headers=H8095)
                self.assertEqual(rt1.status, 200)
                self.assertEqual((await rt1.json())['timer']['phase'], 'running')
                rt2 = await ch.post('/api/home/timer', json={'action': 'pause'}, headers=H8095)
                self.assertEqual((await rt2.json())['timer']['phase'], 'paused')
                rt3 = await ch.post('/api/home/timer', json={'action': 'add', 'seconds': 60}, headers=H8095)
                self.assertGreater((await rt3.json())['timer']['remaining'], 950)
                rt4 = await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                self.assertIsNone((await rt4.json())['timer'])

                # Desktop count-up stopwatch + lap lifecycle: start(stopwatch) -> lap -> pause -> resume -> stop
                sw1 = await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '做卷测速'}, headers=H8095)
                self.assertEqual(sw1.status, 200)
                jsw1 = (await sw1.json())['timer']
                self.assertEqual((jsw1['mode'], jsw1['phase'], jsw1['label']), ('stopwatch', 'running', '做卷测速'))
                await asyncio.sleep(0.12)
                sw2 = await ch.post('/api/home/timer', json={'action': 'lap'}, headers=H8095)
                self.assertEqual(sw2.status, 200)
                jsw2 = (await sw2.json())['timer']
                self.assertEqual(len(jsw2['laps']), 1)
                self.assertEqual(jsw2['laps'][0]['idx'], 1)
                self.assertGreaterEqual(jsw2['laps'][0]['split'], 0.1)
                sw3 = await ch.post('/api/home/timer', json={'action': 'pause'}, headers=H8095)
                self.assertEqual((await sw3.json())['timer']['phase'], 'paused')
                sw4 = await ch.post('/api/home/timer', json={'action': 'resume'}, headers=H8095)
                self.assertEqual((await sw4.json())['timer']['phase'], 'running')
                sw5 = await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                self.assertIsNone((await sw5.json())['timer'])
            finally:
                await ch.close()
        run(go())

    def test_supplement24_unified_focus_timer_large_lyrics_and_like_playlist(self):
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        ho_js = (pathlib.Path(STAGE, 'home_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')).read_text(encoding='utf-8')

        # 1. Message Board aligned with 424px Calendar & Music card lengthened; zero-overlap 番茄倒计时 + 正向计时
        self.assertIn('.mid { display: grid; grid-template-columns: 1fr 424px; gap: 14px;', home_ui)
        self.assertNotIn('clkTimerBtn', home_ui)
        self.assertIn('id="tabFocusMode"', home_ui)
        self.assertIn('id="tabTimerMode"', home_ui)
        self.assertIn('>番茄倒计时</button>', home_ui)  # 2026-11-12 home redesign (Claude): 计时睡眠 page tabs, line icons instead of emoji
        self.assertIn('>正向计时</button>', home_ui)
        self.assertIn("mode: 'stopwatch'", home_ui)

        # 2. Song bookmarking removed; replaced with one-tap 我喜欢 (openFav -> dir_201)
        self.assertNotIn('id="mFav"', home_ui)
        self.assertNotIn('id="btnFavBar"', qq_ui)
        self.assertNotIn('id="mLikeBtn"', home_ui)  # 2026-11-12 home redesign (Claude): 我喜欢 stays on the music page
        self.assertIn('id="mModeIco"', home_ui)
        self.assertIn('d.subCmd', ho_js)
        self.assertIn('openFavPlaylist', qq_ui)

        # 3. Clean top header (no QQ音乐 / 已暂停 clutter, Title + inline Artist) & dual-line lyrics
        self.assertNotIn('id="mSrcLbl"', home_ui)
        self.assertNotIn('id="mState"', home_ui)
        self.assertIn('class="m-title-row"', home_ui)
        self.assertIn('class="m-artist-in" id="mArtist"', home_ui)
        self.assertIn('id="mLyric2"', home_ui)
        self.assertIn('.m-lyric { font-size: 26px;', home_ui)  # 2026-11-12 home redesign (Claude)
        self.assertIn('font-size: 25px', qq_ui)
        self.assertIn('font-size: 32px', qq_ui)

    def test_supplement25_audit_optimizations_and_ambient_ownership(self):
        from aiohttp.test_utils import TestClient, TestServer
        h = HOME
        sc_src = path_of('bridge').read_text(encoding='utf-8')
        self.assertIn("mode == 'stopwatch'", sc_src)
        self.assertIn("'lap'", sc_src)

        async def go():
            amb_calls = []
            ctrl_calls = []
            study_phase = ['focus']
            music_playing = [False]
            music_srv = web.Application()
            async def m_status(_r):
                return web.json_response({'playing': music_playing[0], 'paused': not music_playing[0], 'volume': 50, 'track': None, 'ambient': None})
            async def m_amb(r):
                amb_calls.append(await r.json())
                return web.json_response({'ok': True})
            async def m_ctrl(r):
                ctrl_calls.append(await r.json())
                return web.json_response({'ok': True})
            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/ambient', m_amb)
            music_srv.router.add_post('/control', m_ctrl)

            study_srv = web.Application()
            async def s_get(_r):
                return web.json_response({'enabled': False, 'focus': {'phase': study_phase[0], 'remaining': 60, 'total': 60}, 'today': {}})
            async def s_foc(r):
                b = await r.json()
                if b.get('action') == 'stop':
                    study_phase[0] = None
                    return web.json_response({'ok': True, 'phase': None})
                study_phase[0] = 'focus'
                return web.json_response({'ok': True, 'phase': 'focus', 'remaining': 120, 'total': 120})
            study_srv.router.add_get('/api/study', s_get)
            study_srv.router.add_post('/api/study/focus', s_foc)

            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()
            t_dir = pathlib.Path(TMP.name, 'home_s25')
            t_dir.mkdir(parents=True, exist_ok=True)
            box = h.Inbox(t_dir / 'messages.json')
            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'):
                ch = TestClient(TestServer(h.make_app(box)))
                await ch.start_server()
                try:
                    # 1. Stopwatch duplicate-lap debounce (<0.08s while paused does not add duplicate lap)
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '测速', 'ambient': 'rain'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'rain')
                    await asyncio.sleep(0.12)
                    await ch.post('/api/home/timer', json={'action': 'pause'}, headers=H8095)
                    r_l1 = await (await ch.post('/api/home/timer', json={'action': 'lap'}, headers=H8095)).json()
                    r_l2 = await (await ch.post('/api/home/timer', json={'action': 'lap'}, headers=H8095)).json()
                    self.assertEqual(len(r_l1['timer']['laps']), 1)
                    self.assertEqual(len(r_l2['timer']['laps']), 1)
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'off')

                    # 2. Focus ambient ownership & mutual exclusion with timer_api
                    amb_calls.clear()
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '自习', 'ambient': 'rain'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'rain')
                    await ch.post('/api/home/focus', json={'action': 'start', 'minutes': 25, 'break_minutes': 0, 'with_study': False, 'ambient': 'waves'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'waves')
                    t_after_foc = await (await ch.get('/api/home/timer', headers=H8095)).json()
                    self.assertIsNone(t_after_foc['timer'])
                    # Starting timer_api while focus is active automatically stops study.focus
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '测速', 'ambient': 'off'}, headers=H8095)
                    self.assertIsNone(study_phase[0])
                    self.assertEqual(amb_calls[-1].get('preset'), 'off')
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    # Focus start + auto-stop when focus phase ends
                    amb_calls.clear()
                    await ch.post('/api/home/focus', json={'action': 'start', 'minutes': 25, 'break_minutes': 0, 'with_study': False, 'ambient': 'waves'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'waves')
                    study_phase[0] = None
                    await ch.get('/api/home/state', headers=H8095)
                    await asyncio.sleep(0.05)
                    self.assertEqual(amb_calls[-1].get('preset'), 'off')

                    # 3. Screen voice reply start & voice message play duck speaker music/ambient and unduck on cancel/stop
                    ctrl_calls.clear()
                    with mock.patch.object(h.ScreenVoiceRecorder, 'start', new=mock.AsyncMock(return_value={'ok': True, 'max_seconds': 15})), \
                         mock.patch.object(h.ScreenVoiceRecorder, 'cancel', new=mock.AsyncMock(return_value={'ok': True})):
                        await ch.post('/api/home/voice_reply', json={'action': 'start'}, headers=H8095)
                        self.assertTrue(any(c.get('action') == 'duck' for c in ctrl_calls))
                        await ch.post('/api/home/voice_reply', json={'action': 'cancel'}, headers=H8095)
                        self.assertTrue(any(c.get('action') == 'unduck' for c in ctrl_calls))

                    ctrl_calls.clear()
                    vm = box.add_voice(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 128, 2.5, 'phone')
                    with mock.patch.object(h.SpeakerClipPlayer, 'play_mp3_file', return_value=True) as m_play:
                        r_vp = await ch.post('/api/home/messages/play', json={'id': vm['id']}, headers=H8095)
                        self.assertEqual(r_vp.status, 200)
                        self.assertTrue(m_play.called)
                    self.assertTrue(any(c.get('action') == 'duck' for c in ctrl_calls))
                    await ch.post('/api/home/messages/play', json={'stop': True}, headers=H8095)
                    self.assertTrue(any(c.get('action') == 'unduck' for c in ctrl_calls))

                    # 4. S26 & S27 frontend, intercom & ambient sleep timer invariants
                    import threading
                    q_mod = MusicSleepTests.q
                    eng_s27 = q_mod.QQMusicEngine.__new__(q_mod.QQMusicEngine)
                    eng_s27._lock = threading.Lock()
                    eng_s27.ambient_preset = 'rain'
                    eng_s27.sleep_mode = None
                    eng_s27.sleep_deadline = 0.0
                    eng_s27.sleep_total = 0
                    eng_s27.sleep_tracks_left = 0
                    sl_res = eng_s27.set_sleep(tracks=1)
                    self.assertTrue(sl_res['ok'])
                    self.assertEqual(eng_s27.sleep_mode, 'time')
                    self.assertEqual(eng_s27.sleep_total, 15)

                    home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
                    qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
                    ic_src = path_of('intercom').read_text(encoding='utf-8')
                    self.assertIn("action === 'prev' || action === 'next'", home_ui)
                    self.assertIn('clearScrPlayUi', home_ui)
                    self.assertIn("$('trStop').onclick", home_ui)  # 2026-11-12 home redesign (Claude)
                    self.assertIn('fmtSwHero', home_ui)
                    self.assertIn('sTmrLap', home_ui)
                    self.assertIn('ambSleep', home_ui)
                    self.assertIn("const activeOut = playing || !!amb;", qq_ui)
                    self.assertIn("if (st.ambient)", qq_ui)
                    self.assertNotIn("AMB_CYCLE_IDS", qq_ui)
                    self.assertIn("if (state && state.ambient)", qq_ui)
                    self.assertIn('vRecStarting', ic_src)
                    b_mod = load('bridge_s29', 'bridge')
                    self.assertEqual(b_mod._focus_body({'action': 'start', 'minutes': 30, 'ambient': 'rain'}),
                                     {'action': 'start', 'minutes': 30, 'ambient': 'rain'})
                    # S30: starting music/ambient via post_music stops clip_player and clears ambient_owner
                    with mock.patch.object(h.SpeakerClipPlayer, 'stop') as m_clip_stop:
                        await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'forest'}, headers=H8095)
                        self.assertTrue(m_clip_stop.called)
                    # S32: mid-session ambient ownership sync + full-screen dial ambient switcher + side message quick button + QQ Music stage ambient switcher
                    amb_calls.clear()
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '沉浸自习', 'ambient': 'off'}, headers=H8095)
                    await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'night'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'night')
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'off')
                    self.assertIn('id="trAmbBtn"', home_ui)  # 2026-11-12 home redesign (Claude)
                    self.assertIn('id="taPlay"', home_ui)
                    self.assertIn('id="sMsgQuick"', home_ui)
                    self.assertIn('stopMusicAmbient', qq_ui)
                    self.assertIn('data-m="10"', qq_ui)
                    # S33: live timer label switching + paused timer ambient ownership + phone intercom text-reply ack + cold-boot overlay resilience
                    amb_calls.clear()
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '沉浸自习', 'ambient': 'off'}, headers=H8095)
                    r_lbl = await (await ch.post('/api/home/timer', json={'action': 'label', 'label': '做卷测速'}, headers=H8095)).json()
                    self.assertTrue(r_lbl['ok'])
                    self.assertEqual(r_lbl['timer']['label'], '做卷测速')
                    await ch.post('/api/home/timer', json={'action': 'pause'}, headers=H8095)
                    await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'stream'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'stream')
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'off')
                    ov_js = (pathlib.Path(STAGE, 'home_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')).read_text(encoding='utf-8')
                    self.assertIn('bootCycles', ov_js)
                    self.assertIn('ackPhoneMsg', ic_src)
                    self.assertIn("action: 'label'", home_ui)
                    # S34: control('resume') while .m4a ambient process is active in self._proc stops ambient and resumes saved track
                    eng_s34 = q_mod.QQMusicEngine.__new__(q_mod.QQMusicEngine)
                    eng_s34._lock = threading.Lock()
                    eng_s34.queue = [{'mid': 'M34', 'title': '长大成人', 'artist': '范茹', 'album': '长大成人', 'duration': 218}]
                    eng_s34.index = 0
                    eng_s34.playing = False
                    eng_s34.paused = True
                    eng_s34.ducked = False
                    eng_s34.ambient_preset = 'rain'
                    eng_s34.elapsed_base = 35.2
                    eng_s34._proc = mock.MagicMock()
                    eng_s34._gen = 7
                    eng_s34._starting_gen = 7
                    started_tracks = []
                    eng_s34._stop_proc_locked = mock.MagicMock(side_effect=lambda: setattr(eng_s34, '_proc', None) or True)
                    eng_s34._start_current_track = lambda seek_s, g: started_tracks.append((seek_s, g))
                    eng_s34._send_aec_flush = lambda: None
                    eng_s34._pause_bluetooth_if_streaming = lambda: None
                    eng_s34.save_session = lambda: None
                    res_s34 = eng_s34.control('resume')
                    self.assertTrue(res_s34['ok'])
                    self.assertIsNone(eng_s34.ambient_preset)
                    self.assertTrue(eng_s34._stop_proc_locked.called)
                    await asyncio.sleep(0.05)
                    self.assertEqual(started_tracks, [(35.2, 7)])
                    self.assertIn('resumeSavedTrack', qq_ui)
                    self.assertIn('ambToQq', home_ui)
                    self.assertIn("openFocusModal('timer')", home_ui)
                    # S35: duck/unduck in QQMusicEngine.control, break-phase ambient preservation, and Companion.play ambient duck/no-resume guard
                    res_duck = eng_s34.control('duck')
                    self.assertTrue(res_duck['ok'])
                    self.assertGreater(eng_s34.study_duck_until, time.monotonic())
                    res_unduck = eng_s34.control('unduck')
                    self.assertTrue(res_unduck['ok'])
                    self.assertEqual(eng_s34.study_duck_until, 0.0)
                    # Break-phase ambient started manually is NOT killed by GET /api/home/state
                    study_phase[0] = 'break'
                    await ch.get('/api/home/state', headers=H8095)
                    amb_calls.clear()
                    await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'forest'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'forest')
                    await ch.get('/api/home/state', headers=H8095)
                    await asyncio.sleep(0.05)
                    self.assertEqual(amb_calls[-1].get('preset'), 'forest')
                    study_phase[0] = None
                    self.assertIn('ambResume', home_ui)
                    self.assertIn('_call_music_ambient', path_of('bridge').read_text(encoding='utf-8'))
                    # S36: cross-page song playback clears stale ambient_owner, timer stop/add stops clip_player, and QQ Music bottom bar #btnAmb
                    amb_calls.clear()
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '自习', 'ambient': 'rain'}, headers=H8095)
                    self.assertEqual(amb_calls[-1].get('preset'), 'rain')
                    music_playing[0] = True
                    await ch.get('/api/home/state', headers=H8095)
                    music_playing[0] = False
                    amb_calls.clear()
                    with mock.patch.object(h.SpeakerClipPlayer, 'stop') as m_clip_s36:
                        await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                        self.assertTrue(m_clip_s36.called)
                    self.assertEqual(amb_calls, [])
                    self.assertNotIn('id="btnAmb"', qq_ui)
                    self.assertNotIn('toggleMusicAmbientBar', qq_ui)
                    self.assertIn('再计时 5 分钟', home_ui)
                    self.assertNotIn('id="mVolBtn"', home_ui)
                    self.assertIn('id="taGrid"', home_ui)  # 2026-11-12 home redesign (Claude)
                    self.assertIn('声景 · 高级混音', home_ui)
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go())

    def test_supplement38_dual_layer_ambient_and_ui_ergonomics(self):
        import array
        import threading
        from aiohttp.test_utils import TestClient, TestServer

        q_mod = MusicSleepTests.q
        h = HOME
        self.assertEqual(tuple(q_mod.AMBIENT_RECIPES.keys()), ('deep_study', 'cozy_read', 'deep_sleep'))

        # 1. Dual-layer PCM block mixing headroom & soft-clip bounds
        frames = 960
        b1 = bytearray(array.array('f', [0.8] * (frames * 2)).tobytes())
        b2 = bytearray(array.array('f', [0.8] * (frames * 2)).tobytes())
        q_mod.QQMusicEngine._mix_layer_block(b1, b2, 0.25, 0.85)
        mixed = array.array('f')
        mixed.frombytes(bytes(b1))
        self.assertEqual(len(mixed), frames * 2)
        self.assertTrue(all(-0.9501 <= v <= 0.9501 for v in mixed))
        self.assertGreater(mixed[-1], mixed[0])

        # 2. QQMusicEngine.set_ambient: recipes, Chinese aliases, layer dedup, and zero-restart gain update
        eng = q_mod.QQMusicEngine.__new__(q_mod.QQMusicEngine)
        eng._lock = threading.Lock()
        eng.playing = False
        eng.paused = True
        eng.ducked = False
        eng.bt_streaming = False
        eng.volume = 22
        eng._gen = 10
        eng._proc = None
        eng._proc2 = None
        eng._pacat = None
        worker_starts = []
        eng._stop_proc_locked = mock.MagicMock(side_effect=lambda: setattr(eng, '_gen', eng._gen + 1) or False)
        eng._send_aec_flush = lambda: None
        eng._pause_bluetooth_if_streaming = lambda: None
        eng._ambient_worker = lambda *args: worker_starts.append(args)

        r_rec = eng.set_ambient('围炉夜读')
        self.assertTrue(r_rec['ok'])
        self.assertEqual(r_rec['ambient']['preset'], 'fireplace')
        self.assertEqual(r_rec['ambient']['layer'], 'sleep_piano')
        self.assertAlmostEqual(r_rec['ambient']['layer_gain'], 0.45, places=2)
        self.assertEqual(len(worker_starts), 1)

        # Updating layer_gain while (preset, layer) is already running mutates in-place without restarting worker
        r_gain = eng.set_ambient('fireplace', layer='sleep_piano', layer_gain=70)
        self.assertTrue(r_gain['ok'])
        self.assertAlmostEqual(r_gain['ambient']['layer_gain'], 0.70, places=2)
        self.assertEqual(len(worker_starts), 1)

        # Deduplicating identical primary & secondary layer
        r_same = eng.set_ambient('rain', layer='rain')
        self.assertTrue(r_same['ok'])
        self.assertEqual(r_same['ambient']['preset'], 'rain')
        self.assertEqual(r_same['ambient']['layer'], '')
        self.assertEqual(len(worker_starts), 2)

        r_off = eng.set_ambient('off')
        self.assertTrue(r_off['ok'])
        self.assertIsNone(r_off['ambient'])
        self.assertIsNone(eng.ambient_preset)
        self.assertIsNone(eng.ambient_layer)

        # 3. thinksmart-home.py forwards dual-layer ambient & exposes full public mix fields
        pub_st = h._music_public({
            'playing': False,
            'paused': True,
            'volume': 20,
            'play_mode': 'shuffle',
            'track': None,
            'ambient': {
                'preset': 'rain',
                'name': '窗外细雨 + 书房自习',
                'primary_name': '窗外细雨',
                'icon': '🌧️',
                'desc': '窗外细雨叠加书房自习（45%）· 双层立体声混音',
                'layer': 'library',
                'layer_name': '书房自习',
                'layer_icon': '📖',
                'layer_gain': 0.45,
            },
        })
        self.assertEqual(pub_st['ambient']['layer'], 'library')
        self.assertEqual(pub_st['ambient']['layer_icon'], '📖')
        self.assertAlmostEqual(pub_st['ambient']['layer_gain'], 0.45, places=2)

        async def go_s38():
            amb_reqs = []
            music_srv = web.Application()
            async def m_amb(r):
                amb_reqs.append(await r.json())
                return web.json_response({'ok': True, 'message': '正在播放📚 深度自习'})
            music_srv.router.add_post('/ambient', m_amb)
            cm = TestClient(TestServer(music_srv))
            await cm.start_server()
            t_dir = pathlib.Path(TMP.name, 'home_s38')
            t_dir.mkdir(parents=True, exist_ok=True)
            box = h.Inbox(t_dir / 'messages.json')
            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'):
                ch = TestClient(TestServer(h.make_app(box)))
                await ch.start_server()
                try:
                    resp = await ch.post(
                        '/api/home/music',
                        json={'action': 'ambient', 'preset': 'rain', 'layer': 'library', 'layer_gain': 0.35},
                        headers=H8095,
                    )
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(amb_reqs[-1], {'preset': 'rain', 'layer': 'library', 'layer_gain': 0.35})
                    resp_rec = await ch.post(
                        '/api/home/music',
                        json={'action': 'ambient', 'preset': 'deep_sleep'},
                        headers=H8095,
                    )
                    self.assertEqual(resp_rec.status, 200)
                    self.assertEqual(amb_reqs[-1], {'preset': 'deep_sleep'})
                finally:
                    await ch.close()
                    await cm.close()
        run(go_s38())

        # 4. Frontend invariants: enlarged #mMode/#mSleep, 9 presets + 3 recipes, in-place modal updates (no auto close)
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        self.assertIn('.m-qbtn, .m-sleep { height: 52px;', home_ui)  # 2026-11-12 home redesign (Claude)
        self.assertIn('font-size: 16.5px', home_ui)
        self.assertIn('AMB_RECIPES', home_ui)
        self.assertIn('applyAmbientMix', home_ui)
        self.assertIn('syncAmbientModalUi', home_ui)
        self.assertIn("id: 'fireplace'", home_ui)
        self.assertIn("id: 'library'", home_ui)
        self.assertIn("id: 'singing_bowl'", home_ui)
        # S40: full-screen QQ Music removes natural-sound button/picker; Home 专注与助眠 enlarges 叠加第二层 & modal typography
        self.assertNotIn('AMB_PRESETS_UI', qq_ui)
        self.assertNotIn('amb-stage-grid', qq_ui)
        self.assertIn('.amb-layer-lbl { font-size: 19px;', home_ui)
        self.assertIn('font-size: 18px; font-weight: 800;', home_ui)
        self.assertIn('.amb-card b { font-size: 20.5px;', home_ui)

    def test_supplement39_reboot_home_landing_and_open_ui_freshness(self):
        q = MusicSleepTests.q
        sess_file = pathlib.Path(TMP.name, 'session_s39.json')
        sess_file.write_text(json.dumps({
            'playing': True,
            'index': 0,
            'elapsed_base': 12.5,
            'open_ui_seq': 1794327522748,
            'lyrics': [],
            'queue': [{'mid': '001S39', 'title': 'BootSong', 'artist': 'Tester', 'duration': 200}],
        }), encoding='utf-8')
        with mock.patch.object(q, 'SESSION_PATH', sess_file), \
             mock.patch.object(q.QQMusicEngine, '_read_initial_volume', lambda self: None), \
             mock.patch.object(q.time, 'time', return_value=1700000000.0):
            e1 = q.QQMusicEngine()
            e2 = q.QQMusicEngine()
            # Unique per-process boot_id even when wall-clock time.time() is identical before NTP sync
            self.assertNotEqual(e1.boot_id, e2.boot_id)
            # Stale persisted open_ui_seq in session.json is ignored on boot
            self.assertEqual(e1.open_ui_seq, 0)
            st0 = e1.status(lite=True)
            self.assertEqual(st0['open_ui_seq'], 0)
            self.assertFalse(st0['open_ui_fresh'])
            # Saving session strips open_ui_seq from session.json
            e1.save_session()
            saved = json.loads(sess_file.read_text(encoding='utf-8'))
            self.assertNotIn('open_ui_seq', saved)
            # Default play_tracks(open_ui=False) plays music without hijacking screen away from Home
            with mock.patch.object(e1, '_start_current_track', lambda *a, **kw: None), \
                 mock.patch.object(e1, '_pause_bluetooth_if_streaming', lambda: None):
                e1.play_tracks([{'mid': '002S39', 'title': 'QuietPlay', 'duration': 180}], 0)
                self.assertEqual(e1.open_ui_seq, 0)
                self.assertFalse(e1.status(lite=True)['open_ui_fresh'])
                # Explicit open_ui increments open_ui_seq and marks open_ui_fresh=True for 10s
                r_open = e1.control('open_ui')
                self.assertTrue(r_open['ok'])
                self.assertEqual(e1.open_ui_seq, 1)
                self.assertTrue(e1.status(lite=True)['open_ui_fresh'])
                self.assertTrue(e1.status(lite=False)['open_ui_fresh'])
                e1._open_ui_mono = q.time.monotonic() - 15.0
                self.assertFalse(e1.status(lite=True)['open_ui_fresh'])

        ko_js = (pathlib.Path(STAGE, 'kiosk_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js')).read_text(encoding='utf-8')
        ho_js = (pathlib.Path(STAGE, 'home_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')).read_text(encoding='utf-8')
        self.assertIn('open_ui_fresh', ko_js)
        self.assertNotIn('window.location.href', ko_js)
        self.assertNotIn('tsView', ho_js)   # 2026-11-25：只剩主页一个底面，不用记「在哪个页面」

    def test_supplement41_audit_fixes_dual_layer_lifecycle_and_focus_sync(self):
        import io
        import threading
        from aiohttp.test_utils import TestClient, TestServer

        q = MusicSleepTests.q
        h = HOME

        # 1. _ambient_worker reaps both proc & proc2 if generation advances during startup
        eng = q.QQMusicEngine.__new__(q.QQMusicEngine)
        eng._lock = threading.Lock()
        eng._gen = 50
        eng.ambient_preset = 'fireplace'
        eng.ambient_layer = 'sleep_piano'
        eng.ambient_layer_gain = 0.45
        eng._shutting_down = False
        eng._proc = None
        eng._proc2 = None
        eng._pacat = None
        eng._ambient_worker_gen = 50

        spawned = []

        class FakeProc:
            def __init__(self, bump_gen=False):
                self.stdin = None
                self.stdout = io.BytesIO(b'')
                self.stderr = None
                self.killed = False
                self.waited = False
                if bump_gen:
                    eng._gen = 51

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                self.waited = True

        def fake_popen(cmd, **kwargs):
            p = FakeProc(bump_gen=(len(spawned) == 1))
            spawned.append(p)
            return p

        with mock.patch.object(q.subprocess, 'Popen', side_effect=fake_popen):
            eng._ambient_worker(50, 'fireplace', pathlib.Path('/tmp/f1.m4a'), None, 'sleep_piano', pathlib.Path('/tmp/f2.m4a'), None)
        self.assertEqual(len(spawned), 2)
        self.assertTrue(all(p.killed and p.waited and p.stdout.closed for p in spawned))
        self.assertIsNone(eng._proc)
        self.assertIsNone(eng._proc2)

        # Verify main() shutdown reaping includes _proc2
        q_src = (pathlib.Path(STAGE, 'thinksmart-qqmusic.py') if STAGE else pathlib.Path('/usr/local/bin/thinksmart-qqmusic.py')).read_text(encoding='utf-8')
        self.assertIn("getattr(engine, '_proc2', None)", q_src)

        # 2. sitecustomize.py MCP schemas expose all 3 Master Recipes on study.focus & home.timer
        sc_mod = load('s41_sitecustomize', 'bridge')
        by_name = {t['name']: t for t in sc_mod.MUSIC_TOOLS}
        for tname in ('self.study.focus', 'self.home.timer'):
            enum_vals = by_name[tname]['inputSchema']['properties']['ambient']['enum']
            for rec_id in ('deep_study', 'cozy_read', 'deep_sleep'):
                self.assertIn(rec_id, enum_vals)

        # 3. thinksmart-home.py forwards layer/layer_gain on focus & timer start, and clears owner when ambient stops (playing=False)
        async def go_s41():
            amb_reqs = []
            cur_amb = [{'preset': 'rain', 'name': '窗外细雨 + 书房自习', 'icon': '🌧️', 'desc': '双层混音', 'layer': 'library', 'layer_gain': 0.45}]
            music_srv = web.Application()
            study_srv = web.Application()

            async def m_status(r):
                return web.json_response({
                    'playing': False,
                    'paused': True,
                    'volume': 20,
                    'play_mode': 'shuffle',
                    'track': {'mid': 'm1', 'title': 'Song', 'artist': 'Art', 'duration': 180},
                    'ambient': cur_amb[0],
                })

            async def m_amb(r):
                b = await r.json()
                amb_reqs.append(b)
                if b.get('preset') == 'off':
                    cur_amb[0] = None
                return web.json_response({'ok': True, 'ambient': cur_amb[0]})

            async def s_state(r):
                return web.json_response({'enabled': True, 'focus': {'phase': 'focus', 'minutes': 25, 'remaining': 1400}})

            async def s_focus(r):
                return web.json_response({'ok': True, 'phase': 'focus', 'minutes': 25, 'total': 1500})

            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/ambient', m_amb)
            study_srv.router.add_get('/api/study', s_state)
            study_srv.router.add_post('/api/study/focus', s_focus)
            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()
            t_dir = pathlib.Path(TMP.name, 'home_s41')
            t_dir.mkdir(parents=True, exist_ok=True)
            box = h.Inbox(t_dir / 'messages.json')
            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'):
                ch = TestClient(TestServer(h.make_app(box)))
                await ch.start_server()
                try:
                    rf = await ch.post(
                        '/api/home/focus',
                        json={'action': 'start', 'minutes': 25, 'ambient': 'rain', 'layer': 'library', 'layer_gain': 0.45},
                        headers=H8095,
                    )
                    self.assertEqual(rf.status, 200)
                    self.assertEqual(amb_reqs[-1], {'preset': 'rain', 'layer': 'library', 'layer_gain': 0.45})

                    rt = await ch.post(
                        '/api/home/timer',
                        json={'action': 'start', 'mode': 'stopwatch', 'label': '沉浸自习', 'ambient': 'fireplace', 'layer': 'sleep_piano', 'layer_gain': 55},
                        headers=H8095,
                    )
                    self.assertEqual(rt.status, 200)
                    self.assertEqual(amb_reqs[-1], {'preset': 'fireplace', 'layer': 'sleep_piano', 'layer_gain': 0.55})

                    # External ambient stop (playing=False, ambient=None after ambient was observed active) clears ownership
                    await ch.get('/api/home/state', headers=H8095)
                    await asyncio.sleep(0.85)
                    cur_amb[0] = None
                    await ch.get('/api/home/state', headers=H8095)
                    amb_reqs.clear()
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_reqs, [])
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go_s41())

        # 4. Frontend S41 invariants
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        self.assertIn('ambHoldUntil', home_ui)
        self.assertIn("'暂停歌曲' : '续播歌曲'", home_ui)  # 2026-11-12 home redesign (Claude)
        self.assertIn('正在播歌：《', home_ui)
        self.assertIn('.ta-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));', home_ui)
        self.assertIn("amb:${amb.preset || ''}:${amb.layer || ''}", qq_ui)
        self.assertIn('st.ambient.desc', qq_ui)

    def test_supplement42_deep_audit_edge_cases(self):
        import threading
        from aiohttp.test_utils import TestClient, TestServer

        q = MusicSleepTests.q
        h = HOME

        # 1. set_ambient persists paused song progress via save_session() & increments ambient_seq;
        #    control('duck') flushes AEC buffer once when entering study ducking while audio/ambient is active.
        eng = q.QQMusicEngine.__new__(q.QQMusicEngine)
        eng._lock = threading.Lock()
        eng.playing = True
        eng.paused = False
        eng.elapsed_base = 47.6
        eng.queue = [{'mid': 's42_m1', 'title': 'SavedSong', 'artist': 'Art', 'duration': 210}]
        eng.index = 0
        eng.lyrics = []
        eng.ducked = False
        eng.bt_streaming = False
        eng.volume = 17
        eng._gen = 20
        eng._proc = None
        eng._proc2 = None
        eng._pacat = None
        eng.ambient_preset = None
        eng.ambient_layer = None
        eng.ambient_layer_gain = 0.45
        eng.ambient_seq = 0
        eng.study_duck_until = 0.0
        saved_calls = []
        flush_calls = []
        eng._stop_proc_locked = mock.MagicMock(side_effect=lambda: setattr(eng, '_gen', eng._gen + 1) or True)
        eng._send_aec_flush = lambda: flush_calls.append(1)
        eng._pause_bluetooth_if_streaming = lambda: None
        eng.save_session = lambda: saved_calls.append((eng.playing, eng.paused, round(eng.elapsed_base, 1)))
        eng._ambient_worker = lambda *args: None

        r1 = eng.set_ambient('rain', layer='library', layer_gain=0.45)
        self.assertTrue(r1['ok'])
        self.assertEqual(saved_calls, [(True, True, 47.6)])
        self.assertEqual(r1['ambient']['seq'], 1)
        self.assertEqual(len(flush_calls), 1)

        # control('duck') flushes AEC buffer immediately on entry, but not redundantly while already study-ducked
        r_duck1 = eng.control('duck')
        self.assertEqual(r_duck1['state'], 'ducked')
        self.assertEqual(len(flush_calls), 2)
        r_duck2 = eng.control('duck')
        self.assertEqual(r_duck2['state'], 'ducked')
        self.assertEqual(len(flush_calls), 2)
        eng.control('unduck')
        self.assertEqual(eng.study_duck_until, 0.0)

        r_off = eng.set_ambient('off')
        self.assertTrue(r_off['ok'])
        self.assertEqual(eng.ambient_seq, 2)

        # 2. thinksmart-home.py owned_amb_seq guard:
        #    - Does NOT stop a newer ambient session started directly on :8091 while Home state wasn't polled;
        #    - DOES stop an ambient session switched through Home while timer/focus remained active.
        async def go_s42():
            amb_reqs = []
            seq_counter = [0]
            cur_amb = [None]
            music_srv = web.Application()
            study_srv = web.Application()

            async def m_status(r):
                return web.json_response({
                    'playing': False,
                    'paused': True,
                    'volume': 17,
                    'play_mode': 'loop',
                    'track': None,
                    'ambient': cur_amb[0],
                })

            async def m_amb(r):
                b = await r.json()
                amb_reqs.append(b)
                if b.get('preset') == 'off':
                    cur_amb[0] = None
                else:
                    seq_counter[0] += 1
                    cur_amb[0] = {
                        'preset': b['preset'],
                        'seq': seq_counter[0],
                        'name': b['preset'],
                        'icon': '🌧️',
                        'desc': 'desc',
                        'layer': b.get('layer') or '',
                        'layer_gain': b.get('layer_gain', 0.45),
                    }
                return web.json_response({'ok': True, 'ambient': cur_amb[0]})

            async def s_state(r):
                return web.json_response({'enabled': False, 'focus': {'phase': None}})

            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/ambient', m_amb)
            study_srv.router.add_get('/api/study', s_state)
            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()
            t_dir = pathlib.Path(TMP.name, 'home_s42')
            t_dir.mkdir(parents=True, exist_ok=True)
            box = h.Inbox(t_dir / 'messages.json')
            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'):
                ch = TestClient(TestServer(h.make_app(box)))
                await ch.start_server()
                try:
                    # Case A: Timer starts rain (seq=1), then :8091 directly switches to fireplace (seq=99) without /api/home/state poll
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '沉浸自习', 'ambient': 'rain'}, headers=H8095)
                    self.assertEqual(cur_amb[0]['seq'], 1)
                    cur_amb[0] = {'preset': 'fireplace', 'seq': 99, 'name': '壁炉柴火', 'icon': '🔥', 'desc': 'd', 'layer': '', 'layer_gain': 0.45}
                    amb_reqs.clear()
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_reqs, [])
                    self.assertEqual(cur_amb[0]['preset'], 'fireplace')

                    # Case B: Timer starts rain (seq=2), user switches via Home to waves (seq=3), stopping timer DOES stop waves
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '沉浸自习', 'ambient': 'rain'}, headers=H8095)
                    await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'waves'}, headers=H8095)
                    amb_reqs.clear()
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_reqs, [{'preset': 'off'}])
                    self.assertIsNone(cur_amb[0])
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go_s42())

        # 3. Frontend S42 invariants across index.html, kiosk_overlay.js, and qqmusic_index.html
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        ko_js = (pathlib.Path(STAGE, 'kiosk_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        self.assertIn('if (!ambUiState.preset && !nextLay)', home_ui)
        self.assertIn('await applyAmbientMix(nextAmb.id, keepLay, gainPct, msg);', home_ui)
        self.assertIn('if (typeof syncAmbientModalUi === \'function\') syncAmbientModalUi(false);\n  syncSecTimer();', home_ui)
        self.assertIn('restarted && seq > 0 && fresh', ko_js)
        self.assertIn('let lastSongPlaying = null;', qq_ui)
        self.assertIn('const el = hasTrk ? (st.elapsed || 0) : 0;', qq_ui)
        # S43: Stopwatch blue circle stays fixed (frac: 1, stroke-dashoffset: '0', static ::before glow; no per-minute sweep)
        self.assertNotIn('el % 60', home_ui)
        self.assertIn('return { t, isSw: true, el, rem: 0, frac: 1 };', home_ui)
        self.assertIn("setAttr($('sideRingArc'), 'stroke-dashoffset', '0');", home_ui)  # 2026-11-12 home redesign (Claude)
        self.assertIn('frac = 1;   // stopwatch ring stays full', home_ui)
        self.assertNotIn('.ring::before', home_ui)

    def test_supplement44_deep_audit_fixes(self):
        import io
        import threading
        from aiohttp.test_utils import TestClient, TestServer

        q = MusicSleepTests.q
        h = HOME
        s = StudyFocusTests.s

        # 1. QQMusicEngine: study_duck_until holds _decode_worker & elapsed_base during timer chime,
        #    and seek()/control()/set_ambient()/play_tracks() reset study_duck_until & bump ambient_seq.
        eng = q.QQMusicEngine.__new__(q.QQMusicEngine)
        eng._lock = threading.Lock()
        eng._aec_lock = threading.Lock()
        eng.aec_clients = set()
        eng.playing = True
        eng.paused = False
        eng.ducked = False
        eng.elapsed_base = 10.0
        eng.queue = [{'mid': 's44_m1', 'title': 'DuckSong', 'artist': 'Art', 'duration': 200}]
        eng.index = 0
        eng.queue_seq = 1
        eng.lyrics = []
        eng.lyrics_seq = 1
        eng._fail_streak = 0
        eng.play_started_mono = 0.0
        eng.just_started = False
        eng.open_ui_seq = 0
        eng._open_ui_mono = 0.0
        eng.sleep_mode = None
        eng.sleep_deadline = 0.0
        eng._gen = 30
        eng._proc = None
        eng._proc2 = None
        eng._pacat = None
        eng.ambient_preset = 'rain'
        eng.ambient_layer = None
        eng.ambient_seq = 5
        eng.study_duck_until = time.monotonic() + 10.0
        eng._stop_proc_locked = lambda: False
        eng._send_aec_flush = lambda: None
        eng._pause_bluetooth_if_streaming = lambda: None
        eng.save_session = lambda: None
        eng._start_current_track = lambda *a: None

        r_seek = eng.seek(42.0)
        self.assertTrue(r_seek['ok'])
        self.assertEqual(eng.study_duck_until, 0.0)
        self.assertEqual(eng.ambient_seq, 6)
        self.assertIsNone(eng.ambient_preset)

        # Verify _decode_worker holds decoding while study_duck_until is active, then advances once unducked
        class _FakeSock:
            def __init__(self):
                self.frames = 0
            def sendall(self, mv):
                self.frames += 1
            def close(self):
                pass

        class _FakeProc:
            def __init__(self, payload):
                self.stdout = io.BytesIO(payload)
                self.stdin = None
                self.stderr = None
            def kill(self):
                pass
            def wait(self, timeout=None):
                return 0

        fake_sock = _FakeSock()
        eng.aec_clients.add(fake_sock)
        eng._starting_gen = -1
        eng._advance_on_eof = lambda *a, **kw: None
        eng.playing = True
        eng.paused = False
        eng.ducked = False
        eng.elapsed_base = 10.0
        eng.study_duck_until = time.monotonic() + 5.0
        fproc = _FakeProc(b'\x00' * (7680 * 3))
        with mock.patch.object(q, '_is_allowed_local_audio', return_value=True), \
             mock.patch.object(q.subprocess, 'Popen', return_value=fproc):
            t_dec = threading.Thread(
                target=eng._decode_worker,
                args=(30, {'url': '/media/test.mp3', 'is_local': True}, 0.0),
                daemon=True,
            )
            t_dec.start()
            time.sleep(0.09)
            self.assertEqual(fake_sock.frames, 0)
            self.assertAlmostEqual(eng.elapsed_base, 10.0, places=2)
            eng.control('unduck')
            t_dec.join(timeout=1.5)
        self.assertEqual(fake_sock.frames, 3)
        self.assertAlmostEqual(eng.elapsed_base, 10.06, places=2)

        # 2. Companion.play / voice_say with from_duck=True immediately sends unduck if TTS cannot play
        async def go_s44_study():
            comp = s.Companion()
            unduck_hits = []
            async def fake_music(action=None):
                if action == 'unduck':
                    unduck_hits.append(1)
                    return {'ok': True}
                return {'playing': False, 'paused': True, 'ambient': None}
            comp.music = fake_music
            comp.client_idle = lambda: asyncio.sleep(0, result=False)
            comp.supervise = lambda: asyncio.sleep(3600)
            app_s = s.app_for(comp)
            cs = TestClient(TestServer(app_s))
            await cs.start_server()
            try:
                rv = await cs.post('/api/voice/say', json={'text': '倒计时时间到啦', 'from_duck': True}, headers={'X-TS-Internal': '1'})
                self.assertEqual(rv.status, 200)
                await asyncio.sleep(0.08)
                self.assertEqual(len(unduck_hits), 1)
                for task in list(comp.announcements):
                    task.cancel()
                await asyncio.gather(*comp.announcements, return_exceptions=True)
            finally:
                await cs.close()
        run(go_s44_study())

        # 3. thinksmart-home.py:
        #    (a) _on_timer_alarm skips duck when idle, and sends immediate unduck + skips voice_say if dismissed during chime;
        #    (b) explicit ambient='off' stops unowned 专注与助眠 ambient, while omitted ambient='' preserves it;
        #    (c) ScreenVoiceRecorder aborts immediately if CALL_FLAG appears mid-recording.
        async def go_s44_home():
            ctrl_reqs = []
            amb_reqs = []
            say_reqs = []
            music_playing = [False]
            cur_amb = [None]
            music_srv = web.Application()
            study_srv = web.Application()

            async def m_status(_r):
                return web.json_response({
                    'playing': music_playing[0],
                    'paused': not music_playing[0],
                    'volume': 17,
                    'track': {'mid': 'm1', 'title': 'Song'} if music_playing[0] else None,
                    'ambient': cur_amb[0],
                })

            async def m_ctrl(r):
                b = await r.json()
                ctrl_reqs.append(b.get('action'))
                return web.json_response({'ok': True})

            async def m_amb(r):
                b = await r.json()
                amb_reqs.append(b)
                if b.get('preset') == 'off':
                    cur_amb[0] = None
                else:
                    cur_amb[0] = {'preset': b.get('preset'), 'seq': 9}
                return web.json_response({'ok': True, 'ambient': cur_amb[0]})

            async def s_get(_r):
                return web.json_response({'enabled': False, 'focus': {'phase': None}, 'today': {}})

            async def s_foc(_r):
                return web.json_response({'ok': True, 'phase': 'focus', 'remaining': 300, 'total': 300})

            async def s_say(r):
                say_reqs.append(await r.json())
                return web.json_response({'ok': True, 'queued': True})

            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/control', m_ctrl)
            music_srv.router.add_post('/ambient', m_amb)
            study_srv.router.add_get('/api/study', s_get)
            study_srv.router.add_post('/api/study/focus', s_foc)
            study_srv.router.add_post('/api/voice/say', s_say)

            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()
            t_dir = pathlib.Path(TMP.name, 'home_s44')
            t_dir.mkdir(parents=True, exist_ok=True)
            fake_call_flag = t_dir / 'intercom.call'
            fake_mirror_flag = t_dir / 'airplay.active'
            box = h.Inbox(t_dir / 'messages.json')
            orig_arm = h.DeskTimer._arm
            def _fast_arm(dt_self, sec_d):
                orig_arm(dt_self, 0.05 if dt_self.mode == 'countdown' and sec_d <= 6.0 else sec_d)

            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'), \
                 mock.patch.object(h, 'CALL_FLAG', fake_call_flag), \
                 mock.patch.object(h, 'MIRROR_FLAG', fake_mirror_flag), \
                 mock.patch.object(h.DeskTimer, '_arm', _fast_arm), \
                 mock.patch.object(h.SpeakerClipPlayer, 'play_timer_chime', lambda self: None):
                ch = TestClient(TestServer(h.make_app(box)))
                await ch.start_server()
                try:
                    # (a1) Idle music -> alarm does NOT send duck, and sends voice_say without from_duck
                    music_playing[0] = False
                    await ch.post('/api/home/timer', json={'action': 'start', 'seconds': 5, 'label': '静默'}, headers=H8095)
                    await asyncio.sleep(1.30)
                    self.assertNotIn('duck', ctrl_reqs)
                    self.assertEqual(len(say_reqs), 1)
                    self.assertNotIn('from_duck', say_reqs[0])
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    say_reqs.clear()
                    ctrl_reqs.clear()

                    # (a2) Active music -> alarm sends duck; dismissing during chime wakes immediately, sends unduck, and skips voice_say!
                    music_playing[0] = True
                    await ch.post('/api/home/timer', json={'action': 'start', 'seconds': 5, 'label': '热身'}, headers=H8095)
                    for _ in range(20):
                        await asyncio.sleep(0.03)
                        if 'duck' in ctrl_reqs:
                            break
                    self.assertIn('duck', ctrl_reqs)
                    t_cancel0 = time.monotonic()
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    await asyncio.sleep(0.08)
                    self.assertIn('unduck', ctrl_reqs)
                    self.assertLess(time.monotonic() - t_cancel0, 0.5)
                    self.assertEqual(say_reqs, [])

                    # (b) Unowned 专注与助眠 ambient: omitted ambient='' keeps it, explicit ambient='off' stops it
                    music_playing[0] = False
                    cur_amb[0] = {'preset': 'rain', 'name': '窗外细雨', 'seq': 99}
                    amb_reqs.clear()
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '口算'}, headers=H8095)
                    self.assertEqual(amb_reqs, [])
                    self.assertIsNotNone(cur_amb[0])
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '静音测速', 'ambient': 'off'}, headers=H8095)
                    self.assertEqual(amb_reqs, [{'preset': 'off'}])
                    self.assertIsNone(cur_amb[0])
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)

                    # Same for Focus start with explicit ambient='off'
                    cur_amb[0] = {'preset': 'fireplace', 'name': '壁炉柴火', 'seq': 100}
                    amb_reqs.clear()
                    await ch.post('/api/home/focus', json={'action': 'start', 'minutes': 25, 'break_minutes': 0, 'with_study': False, 'ambient': 'off'}, headers=H8095)
                    self.assertEqual(amb_reqs, [{'preset': 'off'}])
                    self.assertIsNone(cur_amb[0])

                    # (c) ScreenVoiceRecorder aborts mid-recording when CALL_FLAG appears
                    rec = h.ScreenVoiceRecorder()
                    class _FakeAsyncOut:
                        async def read(self, n):
                            await asyncio.sleep(0.04)
                            return b'\x01\x00' * 1024
                    class _FakeAsyncProc:
                        def __init__(self):
                            self.stdout = _FakeAsyncOut()
                            self.returncode = None
                        def terminate(self):
                            self.returncode = -15
                        def kill(self):
                            self.returncode = -9
                        async def wait(self):
                            return self.returncode or 0
                    with mock.patch('asyncio.create_subprocess_exec', new=mock.AsyncMock(return_value=_FakeAsyncProc())):
                        await rec.start()
                        await asyncio.sleep(0.06)
                        fake_call_flag.write_text('1', encoding='utf-8')
                        await asyncio.sleep(0.08)
                        with self.assertRaises(ValueError) as ctx:
                            await rec.finish_to_mp3()
                        self.assertIn('自动取消录音', str(ctx.exception))
                        fake_call_flag.unlink(missing_ok=True)
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go_s44_home())

        # 4. Frontend S44 invariants across index.html and qqmusic_index.html
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        self.assertNotIn('focusOwnedAmbient', home_ui)
        self.assertNotIn('timerOwnedAmbient', home_ui)
        self.assertIn("if (scrPlayId) {\n        clearScrPlayUi();\n        api('/api/home/messages/play', { stop: true }).catch(() => {});\n      }", home_ui)
        self.assertIn('if (sbox) sbox.scrollTop = 0;', qq_ui)

    def test_supplement45_deep_audit_duck_sleep_and_ntp_guards(self):
        import threading
        from aiohttp.test_utils import TestClient, TestServer

        q = MusicSleepTests.q
        h = HOME
        s = StudyFocusTests.s

        # 1. QQMusicEngine:
        #    (a) control('duck', seconds=24) honours custom duration & max() window;
        #    (b) control('pause') skips save_session() when already paused with no ambient;
        #    (c) set_ambient converts tracks sleep timer -> time mode & _sleep_gain_locked never muffles ambient on paused song's <20s tail.
        eng = q.QQMusicEngine.__new__(q.QQMusicEngine)
        eng._lock = threading.Lock()
        eng.playing = True
        eng.paused = False
        eng.ducked = False
        eng.alarm_ringing = False
        eng.elapsed_base = 195.0
        eng.play_started_mono = 0.0
        eng.queue = [{'mid': 's45_m1', 'title': 'TailSong', 'artist': 'Art', 'duration': 200}]
        eng.index = 0
        eng.lyrics = []
        eng.lyrics_seq = 1
        eng._fail_streak = 0
        eng.just_started = False
        eng.volume = 17
        eng._gen = 40
        eng._proc = None
        eng._proc2 = None
        eng._pacat = None
        eng.ambient_preset = None
        eng.ambient_layer = None
        eng.ambient_layer_gain = 0.45
        eng.ambient_seq = 0
        eng.study_duck_until = 0.0
        eng.sleep_mode = 'tracks'
        eng.sleep_tracks_left = 1
        eng.sleep_total = 1
        eng.sleep_deadline = 0.0
        saves = []
        eng._stop_proc_locked = lambda: True
        eng._send_aec_flush = lambda: None
        eng._pause_bluetooth_if_streaming = lambda: None
        eng.save_session = lambda: saves.append((eng.playing, eng.paused))
        eng._ambient_worker = lambda *a: None

        r_d24 = eng.control('duck', seconds=24)
        self.assertEqual((r_d24['ok'], r_d24['active']), (True, True))
        until_24 = eng.study_duck_until
        self.assertGreater(until_24 - time.monotonic(), 21.0)
        eng.control('duck', seconds=5)
        self.assertGreaterEqual(eng.study_duck_until, until_24)
        eng.control('unduck')
        self.assertEqual(eng.study_duck_until, 0.0)

        # Entering ambient with tracks=1 sleep timer (song at 195s / 200s = 5s left!) converts to 15-min time sleep & gain=1.0
        r_amb = eng.set_ambient('rain', layer='library', layer_gain=0.45)
        self.assertTrue(r_amb['ok'])
        self.assertEqual(eng.sleep_mode, 'time')
        self.assertEqual(eng.sleep_total, 15)
        self.assertEqual(eng.sleep_tracks_left, 0)
        with eng._lock:
            self.assertAlmostEqual(eng._sleep_gain_locked(), 1.0, places=4)
        self.assertEqual(len(saves), 1)  # paused active song saved once

        # Pausing stops ambient -> saves once; pausing AGAIN when already paused & no ambient -> zero redundant saves!
        saves.clear()
        eng.control('pause')
        self.assertEqual(len(saves), 1)
        eng.control('pause')
        self.assertEqual(len(saves), 1)

        # 2. thinksmart-home.py: voice clip playback & voice reply duck/unduck preserve Stopwatch ambient_owner,
        #    and external ambient stop before first state poll still clears ambient_owner via _note_owned_amb.
        async def go_s45_home():
            ctrl_reqs = []
            amb_reqs = []
            cur_amb = [None]
            music_srv = web.Application()
            study_srv = web.Application()

            async def m_status(_r):
                return web.json_response({
                    'playing': False,
                    'paused': True,
                    'volume': 17,
                    'track': None,
                    'ambient': cur_amb[0],
                })

            async def m_ctrl(r):
                b = await r.json()
                ctrl_reqs.append(b)
                return web.json_response({'ok': True, 'active': cur_amb[0] is not None})

            async def m_amb(r):
                b = await r.json()
                amb_reqs.append(b)
                if b.get('preset') == 'off':
                    cur_amb[0] = None
                else:
                    cur_amb[0] = {'preset': b.get('preset'), 'seq': len(amb_reqs), 'name': '细雨'}
                return web.json_response({'ok': True, 'ambient': cur_amb[0]})

            async def s_get(_r):
                return web.json_response({'enabled': False, 'focus': {'phase': None}, 'today': {}})

            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/control', m_ctrl)
            music_srv.router.add_post('/ambient', m_amb)
            study_srv.router.add_get('/api/study', s_get)
            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()
            t_dir = pathlib.Path(TMP.name, 'home_s45')
            t_dir.mkdir(parents=True, exist_ok=True)
            box = h.Inbox(t_dir / 'messages.json')
            vm = box.add_voice(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 128, 3.0, 'phone')

            def fake_play_done(self_cp, mp3_path, on_done=None):
                if on_done is not None:
                    threading.Timer(0.04, on_done).start()
                return True

            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'), \
                 mock.patch.object(h.SpeakerClipPlayer, 'play_mp3_file', fake_play_done):
                ch = TestClient(TestServer(h.make_app(box)))
                await ch.start_server()
                try:
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '伴读', 'ambient': 'rain'}, headers=H8095)
                    self.assertIsNotNone(cur_amb[0])
                    ctrl_reqs.clear()
                    # Playing unread voice clip via inbox/ack ducks (24s) and auto-unducks on completion WITHOUT killing owned ambient!
                    r_ack = await ch.post('/api/home/inbox/ack', json={'id': vm['id']}, headers=H8095)
                    self.assertEqual(r_ack.status, 200)
                    await asyncio.sleep(0.12)
                    acts = [x.get('action') for x in ctrl_reqs]
                    self.assertEqual(acts, ['duck', 'unduck'])
                    self.assertEqual(ctrl_reqs[0].get('seconds'), 24.0)
                    self.assertIsNotNone(cur_amb[0])

                    # Stopping stopwatch still stops its owned ambient because voice playback did NOT clear ambient_owner!
                    amb_reqs.clear()
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_reqs, [{'preset': 'off'}])

                    # Immediate external stop on :8091 BEFORE any prior get_state still clears ambient_owner on first get_state
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '伴读2', 'ambient': 'waves'}, headers=H8095)
                    cur_amb[0] = None
                    await ch.get('/api/home/state', headers=H8095)
                    amb_reqs.clear()
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_reqs, [])
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go_s45_home())

        # 3. thinksmart-study.py pre-NTP cold-boot guards:
        #    (a) focus_public() caps remaining at phase total during backward pre-NTP skew;
        #    (b) focus_loop() waits while _boot_clock_unsynced() on disk-restored focus.json, then expires silently once NTP jumps >FOCUS_STALE;
        #    (c) last_today() reloads today's log from disk when wall-clock date advances after NTP sync.
        async def go_s45_study():
            s_dir = pathlib.Path(TMP.name, 'study_s45')
            s_dir.mkdir(parents=True, exist_ok=True)
            f_path = s_dir / 'focus.json'
            l_dir = s_dir / 'log'
            l_dir.mkdir(parents=True, exist_ok=True)
            real_now = 1794360000.0
            pre_ntp = real_now - 36000.0  # 10 hours behind real time
            # Saved focus ended 9h59m59s ago in real time, but looks 1s past ends_at in pre-NTP fake-hwclock!
            f_path.write_text(json.dumps({
                'phase': 'focus',
                'started_at': pre_ntp - 1501.0,
                'ends_at': pre_ntp - 1.0,
                'minutes': 25,
                'break_minutes': 5,
                'with_study': False,
                'study_auto': False,
            }), encoding='utf-8')
            today_iso = s.local_now(real_now).date().isoformat()
            (l_dir / f'{today_iso}.jsonl').write_text(
                json.dumps({'at': real_now - 60.0, 't': '09:59:00', 'present': True, 'focused': True, 'activity': 'studying', 'posture': 'good'}) + '\n',
                encoding='utf-8',
            )
            fake_clock = [pre_ntp]
            unsynced = [True]
            spoken = []
            with mock.patch.object(s, 'FOCUS_PATH', f_path), \
                 mock.patch.object(s, 'LOG_DIR', l_dir), \
                 mock.patch.object(s.time, 'time', side_effect=lambda: fake_clock[0]), \
                 mock.patch.object(s, '_boot_clock_unsynced', side_effect=lambda: unsynced[0]):
                comp = s.Companion()
                self.assertTrue(comp.focus.get('from_disk'))
                self.assertIsNone(comp.last_today())  # pre-NTP date has no log

                # Cap check when ends_at is far in future relative to pre-NTP clock
                comp.focus['ends_at'] = pre_ntp + 7200.0
                self.assertEqual(comp.focus_public()['remaining'], 25 * 60)
                comp.focus['ends_at'] = pre_ntp - 1.0

                comp._announce_retry = lambda txt, **kw: spoken.append(txt)
                ftask = asyncio.create_task(comp.focus_loop())
                try:
                    await asyncio.sleep(0.08)
                    # Still deferred while _boot_clock_unsynced() is True!
                    self.assertIsNotNone(comp.focus)
                    self.assertEqual(spoken, [])

                    # Simulate systemd-timesyncd jumping clock to real_now!
                    fake_clock[0] = real_now
                    unsynced[0] = False
                    comp.focus_wake.set()
                    await asyncio.sleep(0.08)
                    self.assertIsNone(comp.focus)
                    self.assertEqual(spoken, [])  # stale >FOCUS_STALE -> silent cleanup!
                    self.assertFalse(f_path.exists())
                    loaded_last = comp.last_today()
                    self.assertIsNotNone(loaded_last)
                    self.assertEqual(loaded_last.get('activity'), 'studying')
                finally:
                    comp.stopping = True
                    ftask.cancel()
                    await asyncio.gather(ftask, return_exceptions=True)
        run(go_s45_study())

        # 4. Frontend S45 invariants across index.html & qqmusic_index.html
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        self.assertIn('if (Math.floor(Date.now() / 60000) !== lastClockMin) tickClock();', home_ui)
        self.assertIn("if (ep !== modalEpoch || !$('modal').classList.contains('show') || !vBtn.isConnected)", home_ui)
        self.assertIn("if (action === 'pause' && (state.track || state.ambient))", qq_ui)

    def test_supplement46_monitor_cold_clash_chime_gen_and_focus_voice_guards(self):
        import datetime as dt
        import threading
        from aiohttp.test_utils import TestClient, TestServer

        h = HOME
        s = StudyFocusTests.s
        mon_mod = load('monitor_s46', 'monitor')
        intercom_src = path_of('intercom').read_text(encoding='utf-8')

        # 1. Monitor: 3.5s retry TTL on failure vs 30s cache TTL on success + shielded _ensure_traffic_wait across /api/state
        async def go_s46_mon():
            mon_dir = pathlib.Path(TMP.name, 'mon_s46')
            mon_dir.mkdir(parents=True, exist_ok=True)
            (mon_dir / 'index.html').write_text('<!doctype html><title>m</title>', encoding='utf-8')
            (mon_dir / 'servers.json').write_text('[{"id": 3, "name": "US", "country": "us"}]', encoding='utf-8')
            now_dt = dt.datetime.now(dt.timezone.utc)
            p_from = (now_dt - dt.timedelta(days=5)).isoformat()
            p_to = (now_dt + dt.timedelta(days=25)).isoformat()
            (mon_dir / 'traffic.json').write_text(json.dumps({
                'rule_id': '4',
                'server_id': 3,
                'limit_bytes': 1000000000000,
                'complete_from': p_from,
            }), encoding='utf-8')

            calls = []
            mode = {'fail': True, 'delay': 0.0}
            upstream = web.Application()

            async def svc(_r):
                calls.append(1)
                if mode['delay'] > 0:
                    await asyncio.sleep(mode['delay'])
                if mode['fail']:
                    return web.json_response({'success': False})
                return web.json_response({
                    'success': True,
                    'data': {'cycle_transfer_stats': {'4': {
                        'from': p_from,
                        'to': p_to,
                        'max': 1000000000000,
                        'transfer': {'3': 500000000000},
                    }}},
                })

            upstream.router.add_get('/svc', svc)
            us = TestServer(upstream)
            await us.start_server()
            try:
                with mock.patch.object(mon_mod, 'TRAFFIC_UPSTREAM', str(us.make_url('/svc'))), \
                     mock.patch.dict(os.environ, {'NEZHA_PROXY': ''}):
                    m = mon_mod.Monitor(mon_dir)
                    m.consume = mock.AsyncMock()
                    m.connected = True
                    m.received_at = time.monotonic()

                    # Failure retries after 3.5s (not locked out for 30s!), while success caches for 30s
                    await m.refresh_traffic()
                    self.assertFalse(m.traffic['available'])
                    self.assertEqual(len(calls), 1)
                    await m.refresh_traffic()
                    self.assertEqual(len(calls), 1)  # immediate retry <3.5s still debounced
                    mode['fail'] = False
                    m.traffic_checked -= 4.0
                    await m.refresh_traffic()
                    self.assertTrue(m.traffic['available'])
                    self.assertEqual(len(calls), 2)
                    m.traffic_checked -= 10.0
                    await m.refresh_traffic()
                    self.assertEqual(len(calls), 2)  # 30s cache holds on success!

                    # Slow cold-proxy fetch (>t_wait) stays shielded in background and completes for follow-up /api/state
                    m.traffic = None
                    m.traffic_checked = None
                    mode['delay'] = 0.62
                    with mock.patch.object(mon_mod, 'FIRST_DATA_WAIT', 0.05):
                        cm = TestClient(TestServer(m.app()))
                        await cm.start_server()
                        try:
                            # Here t_wait = max(0.5, FIRST_DATA_WAIT + 1.0) -> patch _ensure_traffic_wait wait window via short call
                            orig_wait = m._ensure_traffic_wait
                            m._ensure_traffic_wait = lambda _tw: orig_wait(0.12)
                            r1 = await (await cm.get('/api/state')).json()
                            self.assertIsNone(r1['traffic'])  # still in flight, NOT cancelled!
                            self.assertEqual(len(calls), 3)
                            await asyncio.sleep(0.58)
                            r2 = await (await cm.get('/api/state')).json()
                            self.assertTrue(r2['traffic']['available'])
                            self.assertEqual(len(calls), 3)  # single uncancelled request reused!
                        finally:
                            await cm.close()
            finally:
                await us.close()
        run(go_s46_mon())

        # 2. SpeakerClipPlayer.play_timer_chime tracks gen & is_playing and aborts cleanly if stop() races synthesis/Popen
        cp = h.SpeakerClipPlayer()
        raw1 = cp._get_chime_raw()
        raw2 = cp._get_chime_raw()
        self.assertIs(raw1, raw2)
        self.assertEqual(len(raw1), int(48000 * 1.45) * 2 * 4)

        spawned = []
        gate = threading.Event()
        killed = threading.Event()

        class _FakeStdin:
            def write(self, _b):
                pass
            def close(self):
                pass

        class _FakePacat:
            def __init__(self):
                self.stdin = _FakeStdin()
                self._rc = None
            def poll(self):
                return self._rc
            def wait(self, timeout=None):
                if not killed.wait(timeout or 0.05):
                    raise subprocess.TimeoutExpired('pacat', timeout)
                return self._rc
            def kill(self):
                self._rc = -9
                killed.set()

        def slow_popen(*_a, **_kw):
            gate.wait(0.5)
            p = _FakePacat()
            spawned.append(p)
            return p

        done_ev = threading.Event()
        with mock.patch.object(h, 'CALL_FLAG', pathlib.Path(TMP.name, 'no_call_s46')), \
             mock.patch.object(h, 'MIRROR_FLAG', pathlib.Path(TMP.name, 'no_mirror_s46')), \
             mock.patch.object(h.subprocess, 'Popen', side_effect=slow_popen):
            self.assertTrue(cp.play_timer_chime(on_done=done_ev.set))
            self.assertTrue(cp.is_playing)
            # Immediately call stop() BEFORE Popen completes -> stale _chime must NOT register procs or stay playing!
            cp.stop()
            self.assertFalse(cp.is_playing)
            gate.set()
            self.assertTrue(done_ev.wait(1.0))
            self.assertEqual(cp.procs, ())
            self.assertFalse(cp.is_playing)

        # 3. Companion: focus_stop / focus_start cancel focus_announcements while preserving /api/voice/say external_announcements
        async def go_s46_study():
            s_dir = pathlib.Path(TMP.name, 'study_s46')
            s_dir.mkdir(parents=True, exist_ok=True)
            cfg_p = s_dir / 'config.json'
            f_p = s_dir / 'focus.json'
            l_dir = s_dir / 'log'
            l_dir.mkdir(parents=True, exist_ok=True)
            cfg_p.write_text(json.dumps(dict(s.DEFAULTS, enabled=False)), encoding='utf-8')
            with mock.patch.object(s, 'CONFIG_PATH', cfg_p), \
                 mock.patch.object(s, 'FOCUS_PATH', f_p), \
                 mock.patch.object(s, 'LOG_DIR', l_dir), \
                 mock.patch.object(s, '_CONFIG_CACHE', (None, None)):
                comp = s.Companion()
                comp.supervise = lambda: asyncio.sleep(3600)
                focus_play_gate = asyncio.Event()
                ext_play_gate = asyncio.Event()
                played = []

                async def fake_play(text, **_kw):
                    if '休息' in text or '开始专注' in text:
                        await focus_play_gate.wait()
                    else:
                        await ext_play_gate.wait()
                    played.append(text)
                    return True

                comp.play = fake_play
                app_s = s.app_for(comp)
                cs = TestClient(TestServer(app_s))
                await cs.start_server()
                try:
                    # Queue external /api/voice/say (e.g. incoming phone message TTS)
                    rv = await cs.post('/api/voice/say', json={'text': '收到新留言：早点休息'}, headers={'Host': '127.0.0.1:8094', 'X-TS-Internal': '1'})
                    self.assertEqual(rv.status, 200)
                    self.assertEqual(len(comp.external_announcements), 1)

                    # Start focus (with_study=True toggles switch(True), must NOT cancel external_announcements!)
                    comp.focus_start(25, 5, with_study=True)
                    self.assertEqual(len(comp.focus_announcements), 1)
                    self.assertEqual(len(comp.external_announcements), 1)

                    # Stop focus -> cancels pending focus_start announcement & toggles switch(False), STILL preserving external voice_say!
                    comp.focus_stop('home')
                    await asyncio.sleep(0.03)
                    self.assertEqual(len(comp.focus_announcements), 0)
                    self.assertEqual(len(comp.external_announcements), 1)

                    ext_play_gate.set()
                    focus_play_gate.set()
                    await asyncio.sleep(0.05)
                    self.assertEqual(played, ['收到新留言：早点休息'])
                finally:
                    await comp.shutdown()
                    await cs.close()
        run(go_s46_study())

        # 4. Frontend S46 invariants across monitor_index.html, thinksmart-intercom.py & index.html
        mon_ui = (pathlib.Path(STAGE, 'monitor_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/index.html')).read_text(encoding='utf-8')
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        self.assertIn('fastRetry', mon_ui)
        self.assertIn('fastRetryT', mon_ui)
        self.assertIn('stopPhoneVoicePlay', intercom_src)
        self.assertIn('voicePlaySeq', intercom_src)
        self.assertIn('var scrPlaySeq = 0;', home_ui)

    def test_supplement47_focus_ambient_races_voice_duck_hold_and_intercom_guards(self):
        import threading
        from aiohttp.test_utils import TestClient, TestServer

        h = HOME
        s = StudyFocusTests.s
        q = MusicSleepTests.q
        b = load('bridge_s47', 'bridge')
        mon_mod = load('monitor_s47', 'monitor')
        intercom_mod = load('intercom_s47', 'intercom')
        intercom_src = path_of('intercom').read_text(encoding='utf-8')

        # 1. Home:
        #    (a) post_focus starts :8093 focus BEFORE :8091 ambient (no music disruption if :8093 fails)
        #        and survives transient :8093 outage without killing Focus ambient;
        #    (b) failed :8091/ambient does NOT bind ambient_owner;
        #    (c) home_duck_hold prevents premature unduck during voice clip -> voice reply transition,
        #        and CALL_FLAG-blocked post_message_play does NOT mark unread phone voice message as read;
        #    (d) Inbox startup orphan .mp3 prune + expected_source guard + pre-ffmpeg rate limit check;
        #    (e) DeskTimer.public() done_until expiry bumps seq & add_seconds caps total at 10800.
        dt_t = h.DeskTimer(lambda: None, lambda *_a: asyncio.sleep(0))
        dt_t.phase = 'done'
        dt_t.total = 60
        dt_t.done_until = time.monotonic() - 1.0
        seq_before = dt_t.seq
        self.assertIsNone(dt_t.public())
        self.assertIsNone(dt_t.phase)
        self.assertEqual(dt_t.seq, seq_before + 1)

        dt_t.phase = 'paused'
        dt_t.total = 10780
        dt_t.paused_rem = 10780.0
        dt_t.add_seconds(600)
        self.assertEqual(dt_t.total, 10800)

        # Inbox startup orphan prune + expected_source
        ib_dir = pathlib.Path(TMP.name, 'inbox_s47')
        (ib_dir / 'voice').mkdir(parents=True, exist_ok=True)
        orphan_mp3 = ib_dir / 'voice' / 'deadbeef0001.mp3'
        orphan_mp3.write_bytes(b'ID3' + b'\x00' * 128)
        box = h.Inbox(ib_dir / 'messages.json')
        self.assertFalse(orphan_mp3.exists())
        m_ph = box.add_voice(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 128, 4.0, 'phone')
        m_sc = box.add('我马上回来', 'screen')
        self.assertEqual(box.ack(m_sc['id'], expected_source='phone'), (None, False))
        self.assertEqual(box.ack(m_ph['id'], expected_source='screen'), (None, False))

        async def go_s47_home():
            music_srv = web.Application()
            study_srv = web.Application()
            amb_reqs, ctrl_reqs = [], []
            cur_amb = [None]
            amb_fail = [False]
            study_down = [False]
            study_reject_focus = [False]
            foc_state = [{'phase': None}]

            async def m_status(_r):
                return web.json_response({'ok': True, 'playing': False, 'paused': False, 'track': None, 'ambient': cur_amb[0], 'sleep': None})

            async def m_amb(r):
                bd = await r.json()
                amb_reqs.append(bd)
                if amb_fail[0]:
                    return web.json_response({'ok': False}, status=500)
                if bd.get('preset') == 'off':
                    cur_amb[0] = None
                else:
                    cur_amb[0] = {'preset': bd.get('preset'), 'seq': len(amb_reqs), 'name': '细雨'}
                return web.json_response({'ok': True, 'ambient': cur_amb[0]})

            async def m_ctrl(r):
                bd = await r.json()
                ctrl_reqs.append(bd)
                return web.json_response({'ok': True, 'active': True})

            async def s_get(_r):
                if study_down[0]:
                    return web.Response(status=503)
                return web.json_response({'enabled': False, 'focus': foc_state[0], 'today': {}})

            async def s_foc(r):
                bd = await r.json()
                if study_reject_focus[0]:
                    return web.json_response({'ok': False, 'error': 'busy'}, status=400)
                if bd.get('action') == 'start':
                    foc_state[0] = {'phase': 'focus', 'minutes': int(bd.get('minutes') or 25), 'break_minutes': 5, 'remaining': 1500}
                    return web.json_response({'ok': True, 'total': 1500, **foc_state[0]})
                foc_state[0] = {'phase': None}
                return web.json_response({'ok': True})

            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/ambient', m_amb)
            music_srv.router.add_post('/control', m_ctrl)
            study_srv.router.add_get('/api/study', s_get)
            study_srv.router.add_post('/api/study/focus', s_foc)
            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()

            clip_done_Holder = [None]
            active_clip = [False]

            def fake_play(self_cp, _p, on_done=None):
                if h.CALL_FLAG.exists():
                    return False
                active_clip[0] = True
                clip_done_Holder[0] = on_done
                return True

            def fake_stop(self_cp):
                was = active_clip[0]
                active_clip[0] = False
                cb = clip_done_Holder[0]
                clip_done_Holder[0] = None
                if was and cb is not None:
                    cb()

            async def fake_rec_start(self_vr, on_done=None):
                self_vr._recording = True
                self_vr._test_done = on_done
                return {'ok': True, 'max_seconds': 15}

            async def fake_rec_cancel(self_vr):
                self_vr._recording = False
                return {'ok': True}

            fake_call = pathlib.Path(TMP.name, 'call_s47.flag')
            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'), \
                 mock.patch.object(h, 'CALL_FLAG', fake_call), \
                 mock.patch.object(h, 'MIRROR_FLAG', pathlib.Path(TMP.name, 'no_mirror_s47')), \
                 mock.patch.object(h.SpeakerClipPlayer, 'play_mp3_file', fake_play), \
                 mock.patch.object(h.SpeakerClipPlayer, 'stop', fake_stop), \
                 mock.patch.object(h.SpeakerClipPlayer, 'is_playing', property(lambda _s: active_clip[0])), \
                 mock.patch.object(h.ScreenVoiceRecorder, 'start', fake_rec_start), \
                 mock.patch.object(h.ScreenVoiceRecorder, 'cancel', fake_rec_cancel):
                ch = TestClient(TestServer(h.make_app(box)))
                await ch.start_server()
                try:
                    # (a1) Rejected focus start does NOT touch :8091/ambient
                    study_reject_focus[0] = True
                    rf_bad = await ch.post('/api/home/focus', json={'action': 'start', 'minutes': 25, 'break_minutes': 5, 'ambient': 'rain'}, headers=H8095)
                    self.assertEqual(rf_bad.status, 400)
                    self.assertEqual(amb_reqs, [])

                    # (a2) Successful focus start + transient :8093 503 during get_state keeps Focus ambient alive!
                    study_reject_focus[0] = False
                    rf_ok = await ch.post('/api/home/focus', json={'action': 'start', 'minutes': 25, 'break_minutes': 5, 'ambient': 'rain'}, headers=H8095)
                    self.assertEqual(rf_ok.status, 200)
                    self.assertEqual(len(amb_reqs), 1)
                    study_down[0] = True
                    await ch.get('/api/home/state', headers=H8095)
                    await asyncio.sleep(0.05)
                    self.assertIsNotNone(cur_amb[0])  # NOT stopped during transient :8093 outage!
                    study_down[0] = False
                    await ch.post('/api/home/focus', json={'action': 'stop'}, headers=H8095)
                    self.assertIsNone(cur_amb[0])

                    # (b) Failed :8091/ambient on timer start does NOT bind ambient_owner['timer']
                    amb_reqs.clear()
                    amb_fail[0] = True
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'ambient': 'waves'}, headers=H8095)
                    amb_fail[0] = False
                    amb_reqs.clear()
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertEqual(amb_reqs, [])

                    # (c1) CALL_FLAG blocks post_message_play AND preserves unread status!
                    fake_call.write_text('1\n', encoding='utf-8')
                    rp_blk = await ch.post('/api/home/messages/play', json={'id': m_ph['id']}, headers=H8095)
                    self.assertFalse((await rp_blk.json())['ok'])
                    self.assertEqual(len(box.unread()), 1)
                    fake_call.unlink(missing_ok=True)

                    # (c2) Playing voice clip -> starting voice_reply holds duck without premature unduck!
                    ctrl_reqs.clear()
                    rp_ok = await ch.post('/api/home/messages/play', json={'id': m_ph['id']}, headers=H8095)
                    self.assertTrue((await rp_ok.json())['ok'])
                    self.assertEqual(len(box.unread()), 0)
                    rv_st = await ch.post('/api/home/voice_reply', json={'action': 'start'}, headers=H8095)
                    self.assertEqual(rv_st.status, 200)
                    await asyncio.sleep(0.05)
                    self.assertEqual([x.get('action') for x in ctrl_reqs], ['duck', 'duck'])
                    await ch.post('/api/home/voice_reply', json={'action': 'cancel'}, headers=H8095)
                    self.assertEqual([x.get('action') for x in ctrl_reqs], ['duck', 'duck', 'unduck'])

                    # (d) Source-mismatch ack routes return 404, and rate-limited voice post returns 429 before ffmpeg
                    r_wrong1 = await ch.post('/api/home/inbox/ack', json={'id': m_sc['id']}, headers=H8095)
                    self.assertEqual(r_wrong1.status, 404)
                    r_wrong2 = await ch.post('/api/home/messages/ack_phone', json={'id': m_ph['id']}, headers=dict(H8095, **{'X-TS-Internal': '1'}))
                    self.assertEqual(r_wrong2.status, 404)
                    with box.lock:
                        now_m = time.monotonic()
                        box.recent_adds.clear()
                        for _ in range(h.MSG_RATE[0]):
                            box.recent_adds.append(now_m)
                    with mock.patch.object(h, '_transcode_to_voice_mp3', side_effect=AssertionError('ffmpeg should not run when rate-limited')):
                        r_429 = await ch.post('/api/home/messages/voice', data=b'RIFFxxxxWAVE', headers={'Host': '127.0.0.1:8095', 'Content-Type': 'audio/wav', 'X-TS-Internal': '1'})
                        self.assertEqual(r_429.status, 429)
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go_s47_home())

        # 2. Study: switch() preserves focus_announcements + cancel_tag unducks even when cancelled before attempt() runs!
        async def go_s47_study():
            s_dir = pathlib.Path(TMP.name, 'study_s47')
            s_dir.mkdir(parents=True, exist_ok=True)
            cfg_p = s_dir / 'config.json'
            cfg_p.write_text(json.dumps(dict(s.DEFAULTS, enabled=False)), encoding='utf-8')
            music_acts = []
            with mock.patch.object(s, 'CONFIG_PATH', cfg_p), \
                 mock.patch.object(s, 'FOCUS_PATH', s_dir / 'focus.json'), \
                 mock.patch.object(s, 'LOG_DIR', s_dir / 'log'), \
                 mock.patch.object(s, 'VOICE_DIR', s_dir / 'voice'), \
                 mock.patch.object(s, '_CONFIG_CACHE', (None, None)):
                (s_dir / 'log').mkdir(parents=True, exist_ok=True)
                (s_dir / 'voice').mkdir(parents=True, exist_ok=True)
                comp = s.Companion()
                comp.supervise = lambda: asyncio.sleep(3600)
                comp.media_busy = lambda: False

                async def fake_idle():
                    return True

                async def fake_music(act=None):
                    if act is None:
                        return {'playing': False, 'ambient': {'preset': 'rain'}}
                    music_acts.append(act)
                    return True

                play_gate = asyncio.Event()

                async def slow_synth(_cfg, _txt):
                    await play_gate.wait()
                    return False

                comp.client_idle = fake_idle
                comp.music = fake_music
                comp.synthesize = slow_synth
                app_s = s.app_for(comp)
                cs = TestClient(TestServer(app_s))
                await cs.start_server()
                try:
                    # Queue focus announcement, then switch(True, announce=False) -> focus_announcements preserved!
                    comp._track_focus_ann(comp._announce_retry('开始专注25分钟'))
                    self.assertEqual(len(comp.focus_announcements), 1)
                    comp.switch(True, 'toggle', announce=False)
                    self.assertEqual(len(comp.focus_announcements), 1)

                    # Queue tagged timer_alarm with from_duck=True (blocked on voice_lock / synthesize), then cancel_tag -> unducks!
                    hdr = {'Host': '127.0.0.1:8094', 'X-TS-Internal': '1'}
                    r_say = await cs.post('/api/voice/say', json={'text': '13分钟倒计时时间到啦', 'tag': 'timer_alarm', 'from_duck': True}, headers=hdr)
                    self.assertEqual(r_say.status, 200)
                    r_can = await cs.post('/api/voice/say', json={'cancel_tag': 'timer_alarm'}, headers=hdr)
                    self.assertEqual((await r_can.json()).get('cancelled'), 1)
                    await asyncio.sleep(0.05)
                    self.assertIn('unduck', music_acts)
                finally:
                    play_gate.set()
                    await comp.shutdown()
                    await cs.close()
        run(go_s47_study())

        # 3. sitecustomize NaN/Inf guards + QQMusic 65s duck & in-place layer_gain just_started
        import io
        seen_reqs = []

        def fake_opener_open(req, timeout=6):
            seen_reqs.append(json.loads(req.data.decode('utf-8')))
            return io.BytesIO(b'{"ok":true}')

        with mock.patch.object(b._NO_PROXY_OPENER, 'open', side_effect=fake_opener_open):
            b._call_home_timer({'action': 'start', 'minutes': float('nan')})
            b._call_home_timer({'action': 'start', 'minutes': float('inf')})
            b._call_music_ambient({'preset': 'rain', 'layer_gain': float('nan')})
        self.assertEqual(seen_reqs[0]['seconds'], 900)
        self.assertEqual(seen_reqs[1]['seconds'], 900)
        self.assertNotIn('layer_gain', seen_reqs[2])

        ms_helper = MusicSleepTests()
        eng = ms_helper.engine()
        eng._send_aec_flush = lambda: None
        eng.control('duck', seconds=55.0)
        self.assertGreater(eng.study_duck_until - time.monotonic(), 50.0)
        eng.ambient_preset = 'rain'
        eng.ambient_layer = 'fireplace'
        eng.ambient_layer_gain = 0.45
        eng.volume = 25
        eng.ducked = True
        eng.just_started = False
        eng._ambient_worker_gen = eng._gen
        res_lg = eng.set_ambient('rain', layer='fireplace', layer_gain=0.75)
        self.assertTrue(res_lg['ok'])
        self.assertTrue(eng.just_started)

        # 4. Monitor idle->viewed wake_retry + Intercom crash-volume flag & S47 frontend invariants
        async def go_s47_mon_ic():
            mon_dir = pathlib.Path(TMP.name, 'mon_s47')
            mon_dir.mkdir(parents=True, exist_ok=True)
            (mon_dir / 'servers.json').write_text('[{"id": 3, "name": "US", "country": "us"}]', encoding='utf-8')
            m = mon_mod.Monitor(mon_dir)
            self.assertFalse(m.wake_retry.is_set())
            m.touch()  # idle -> viewed sets wake_retry!
            self.assertTrue(m.wake_retry.is_set())
            m.wake_retry.clear()
            m.touch()  # already viewed -> does NOT spam wake_retry!
            self.assertFalse(m.wake_retry.is_set())

            cf_path = pathlib.Path(TMP.name, 'ic_call_s47.flag')
            with mock.patch.object(intercom_mod, 'INTERCOM_CALL_FLAG', cf_path):
                ic_srv = object.__new__(intercom_mod.IntercomServer)
                ic_srv._write_call_flag(json.dumps({'vol': '32%'}) + '\n')
                parsed = json.loads(intercom_mod._read_regular_file(cf_path, max_bytes=256).strip())
                self.assertEqual(parsed, {'vol': '32%'})
        run(go_s47_mon_ic())

        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        ov_ui = (pathlib.Path(STAGE, 'home_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')).read_text(encoding='utf-8')
        ko_ui = (pathlib.Path(STAGE, 'kiosk_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js')).read_text(encoding='utf-8')
        self.assertIn('function startScrPlayUi(', home_ui)
        self.assertIn('volDragging = false;', qq_ui)
        self.assertIn('id="btnVoiceSearch"', qq_ui)
        self.assertIn('!sheetOpen()', ov_ui)
        self.assertIn('wantOpenOnWake', ko_ui)
        self.assertIn('hiddenJpeg', intercom_src)
        self.assertIn('msgInFlight', intercom_src)
        self.assertIn('saved_ambient', intercom_src)

    def test_supplement48_deep_audit_scoped_duck_voice_and_overlay_guards(self):
        """Supplement 48 deep audit fixes across QQMusic, Bridge, Home, Study, Monitor, Intercom & WebKit overlays."""
        import io
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        h = HOME
        s = StudyFocusTests.s
        q = MusicSleepTests.q
        b = load('bridge_s48', 'bridge')
        mon_mod = load('monitor_s48', 'monitor')
        intercom_src = path_of('intercom').read_text(encoding='utf-8')
        H8095 = {'Host': '127.0.0.1:8095'}

        # 1. QQMusicEngine: scoped _duck_leases ('home' vs 'study'), set_ambient validation & song-title disambiguation
        ms_helper = MusicSleepTests()
        eng = ms_helper.engine()
        eng._send_aec_flush = lambda: None
        eng.volume = 17
        res_bad = eng.set_ambient('not_a_valid_preset', volume=88)
        self.assertFalse(res_bad['ok'])
        self.assertEqual(eng.volume, 17)

        eng.control('duck', seconds=65.0, source='home')
        eng.control('duck', seconds=18.0, source='study')
        eng.control('unduck', source='study')
        self.assertGreater(eng.study_duck_until - time.monotonic(), 50.0)
        # set_ambient('off') when no ambient is active must NOT clear active duck leases!
        eng.ambient_preset = None
        eng.set_ambient('off')
        self.assertGreater(eng.study_duck_until - time.monotonic(), 50.0)
        eng.control('unduck', source='home')
        self.assertEqual(eng.study_duck_until, 0.0)

        searched, amb_calls = [], []
        eng.play_tracks = lambda tracks, index=0, open_ui=False: {'ok': True, 'count': len(tracks)}
        eng.set_ambient = lambda preset, **kw: (amb_calls.append((preset, kw.get('layer'))) or {'ok': True})
        with mock.patch.object(q, 'qq_search', side_effect=lambda kw, limit=20: (searched.append(kw) or [{'mid': 's1', 'title': kw, 'artist': '歌手', 'duration': 200}])):
            eng.play_query('播放黄品源的海浪')
            eng.play_query('放一首海浪')
            eng.play_query('播放篝火旁')
            self.assertEqual(len(searched), 3)
            self.assertEqual(amb_calls, [])
            eng.play_query('播放雨声')
            eng.play_query('开启围炉夜读')
            self.assertEqual(amb_calls, [('rain', None), ('cozy_read', None)])

        # 2. Bridge _call_music_ambient normalizes stop words -> 'off' (never 'rain') and Chinese recipes -> canonical recipe id
        seen_amb = []

        def fake_open(req, timeout=6):
            seen_amb.append(json.loads(req.data.decode('utf-8')))
            return io.BytesIO(b'{"ok":true}')

        with mock.patch.object(b._NO_PROXY_OPENER, 'open', side_effect=fake_open):
            b._call_music_ambient({'preset': '关闭'})
            b._call_music_ambient({'preset': 'stop'})
            b._call_music_ambient({'preset': False})
            b._call_music_ambient({'preset': 0})
            b._call_music_ambient({'preset': '围炉夜读'})
        self.assertEqual([x.get('preset') for x in seen_amb[:4]], ['off', 'off', 'off', 'off'])
        self.assertEqual(seen_amb[4].get('preset'), 'cozy_read')

        # 3. Home: _transcode_to_voice_mp3 TimeoutExpired -> OSError; recording blocks play/ack (409) without marking read;
        #    rate-limit blocks voice_reply('start') (429); stale timer alarm seq does NOT kill newly started timer's ambient.
        with mock.patch.object(h.subprocess, 'run', side_effect=h.subprocess.TimeoutExpired(cmd=['ffmpeg'], timeout=8.0)):
            with self.assertRaises(OSError):
                h._transcode_to_voice_mp3(b'\x10\x08' * 20000, True)   # 1.25 s (11-12: under 1 s is refused first)

        ib_dir = pathlib.Path(TMP.name, 'inbox_s48')
        box = h.Inbox(ib_dir / 'messages.json')
        m_voice = box.add_voice(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 128, 4.0, 'phone')

        async def go_s48_home():
            music_srv = web.Application()
            study_srv = web.Application()
            amb_reqs, ctrl_reqs = [], []
            cur_amb = [None]

            async def m_status(_r):
                return web.json_response({'ok': True, 'playing': False, 'paused': False, 'track': None, 'ambient': cur_amb[0], 'sleep': None})

            async def m_amb(r):
                bd = await r.json()
                amb_reqs.append(bd)
                cur_amb[0] = None if bd.get('preset') == 'off' else {'preset': bd.get('preset'), 'seq': len(amb_reqs), 'name': '海浪'}
                return web.json_response({'ok': True, 'ambient': cur_amb[0]})

            async def m_ctrl(r):
                bd = await r.json()
                ctrl_reqs.append(bd)
                return web.json_response({'ok': True, 'active': True})

            async def s_get(_r):
                return web.json_response({'enabled': False, 'focus': {'phase': None}, 'today': {}})

            async def s_say(_r):
                return web.json_response({'ok': True})

            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/ambient', m_amb)
            music_srv.router.add_post('/control', m_ctrl)
            study_srv.router.add_get('/api/study', s_get)
            study_srv.router.add_post('/api/voice/say', s_say)
            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()

            async def fake_rec_start(self_vr, on_done=None):
                self_vr._recording = True
                return {'ok': True, 'max_seconds': 15}

            async def fake_rec_cancel(self_vr):
                self_vr._recording = False
                return {'ok': True}

            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'), \
                 mock.patch.object(h, 'CALL_FLAG', pathlib.Path(TMP.name, 'no_call_s48')), \
                 mock.patch.object(h, 'MIRROR_FLAG', pathlib.Path(TMP.name, 'no_mirror_s48')), \
                 mock.patch.object(h.SpeakerClipPlayer, 'play_timer_chime', lambda *_a, **_k: None), \
                 mock.patch.object(h.ScreenVoiceRecorder, 'start', fake_rec_start), \
                 mock.patch.object(h.ScreenVoiceRecorder, 'cancel', fake_rec_cancel):
                app_h = h.make_app(box)
                ch = TestClient(TestServer(app_h))
                await ch.start_server()
                try:
                    rv = await ch.post('/api/home/voice_reply', json={'action': 'start'}, headers=H8095)
                    self.assertEqual(rv.status, 200)
                    # Scoped 'home' source sent to :8091/control
                    self.assertEqual(ctrl_reqs[-1].get('source'), 'home')
                    # While recording, voice play & voice ack return 409 AND keep voice message unread!
                    rp_409 = await ch.post('/api/home/messages/play', json={'id': m_voice['id']}, headers=H8095)
                    self.assertEqual(rp_409.status, 409)
                    ra_409 = await ch.post('/api/home/inbox/ack', json={'id': m_voice['id']}, headers=H8095)
                    self.assertEqual(ra_409.status, 409)
                    self.assertEqual(len(box.unread()), 1)
                    await ch.post('/api/home/voice_reply', json={'action': 'cancel'}, headers=H8095)
                    self.assertEqual(ctrl_reqs[-1].get('source'), 'home')

                    # Stale alarm seq must NOT kill newly started timer's ambient
                    rt = await ch.post('/api/home/timer', json={'action': 'start', 'seconds': 600, 'ambient': 'waves'}, headers=H8095)
                    self.assertEqual(rt.status, 200)
                    self.assertIsNotNone(cur_amb[0])
                    # Simulate a queued old alarm task from previous seq (seq - 1) waking up now:
                    stale_seq = app_h[h.DESK_TIMER].seq - 1
                    await app_h[h.DESK_TIMER]._on_alarm('旧计时', 60, stale_seq)
                    self.assertIsNotNone(cur_amb[0])
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertIsNone(cur_amb[0])

                    # Pre-recording rate limit returns 429 without starting recorder or ducking
                    ctrl_reqs.clear()
                    with box.lock:
                        now_m = time.monotonic()
                        box.recent_adds.clear()
                        for _ in range(h.MSG_RATE[0]):
                            box.recent_adds.append(now_m)
                    rv_429 = await ch.post('/api/home/voice_reply', json={'action': 'start'}, headers=H8095)
                    self.assertEqual(rv_429.status, 429)
                    self.assertEqual(ctrl_reqs, [])
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go_s48_home())

        # 4. Study _deferred_resume across interrupted play() + Monitor disconnected touch(>12s) wake_retry + S48 invariants
        async def go_s48_study_and_mon():
            s_dir = pathlib.Path(TMP.name, 'study_s48')
            s_dir.mkdir(parents=True, exist_ok=True)
            cfg_p = s_dir / 'config.json'
            cfg_p.write_text(json.dumps(dict(s.DEFAULTS, enabled=False)), encoding='utf-8')
            music_acts = []
            song_playing = [True]
            idle_seq = [True, False, False, True, True, True]

            with mock.patch.object(s, 'CONFIG_PATH', cfg_p), \
                 mock.patch.object(s, 'FOCUS_PATH', s_dir / 'focus.json'), \
                 mock.patch.object(s, 'LOG_DIR', s_dir / 'log'), \
                 mock.patch.object(s, 'VOICE_DIR', s_dir / 'voice'), \
                 mock.patch.object(s, '_CONFIG_CACHE', (None, None)):
                (s_dir / 'voice').mkdir(parents=True, exist_ok=True)
                comp = s.Companion()
                comp.media_busy = lambda: False

                async def fake_idle():
                    return idle_seq.pop(0) if idle_seq else True

                async def fake_music(act=None):
                    if act is None:
                        return {'playing': song_playing[0], 'paused': not song_playing[0], 'ambient': None, 'boot_id': 'b1', 'index': 0, 'alarm_ringing': False, 'track': {'mid': 'm1', 'title': '海浪', 'artist': '黄品源'}}
                    music_acts.append(act)
                    if act == 'pause':
                        song_playing[0] = False
                    elif act == 'resume':
                        song_playing[0] = True
                    return True

                async def fake_synth(_cfg, txt):
                    comp.phrase_file(txt).write_bytes(b'\x00\x01' * 3000)
                    return True

                class DummyProc:
                    returncode = 0

                    async def wait(self):
                        return 0

                comp.client_idle = fake_idle
                comp.music = fake_music
                comp.synthesize = fake_synth
                with mock.patch.object(s.asyncio, 'create_subprocess_exec', return_value=DummyProc()):
                    # First play() pauses song, then aborts when post-synth client_idle() returns False:
                    ok1 = await comp.play('休息五分钟')
                    self.assertFalse(ok1)
                    self.assertIsNotNone(comp._deferred_resume)
                    self.assertEqual(music_acts, ['pause'])
                    # Retry play() sees song_playing == False, speaks, and resumes the deferred song!
                    ok2 = await comp.play('休息五分钟')
                    self.assertTrue(ok2)
                    self.assertIsNone(comp._deferred_resume)
                    self.assertEqual(music_acts, ['pause', 'resume'])

            mon_dir = pathlib.Path(TMP.name, 'mon_s48')
            mon_dir.mkdir(parents=True, exist_ok=True)
            (mon_dir / 'servers.json').write_text('[{"id": 3, "name": "US", "country": "us"}]', encoding='utf-8')
            m = mon_mod.Monitor(mon_dir)
            m.connected = False
            m.touch()
            m.wake_retry.clear()
            m.last_view = time.monotonic() - 15.0
            m.touch()  # still within VIEW_HOLD (35s), disconnected >12s -> wakes upstream retry!
            self.assertTrue(m.wake_retry.is_set())
        run(go_s48_study_and_mon())

        svc_txt = (pathlib.Path(STAGE, 'thinksmart-home.service') if STAGE else pathlib.Path('/etc/systemd/system/thinksmart-home.service')).read_text(encoding='utf-8')
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        ov_ui = (pathlib.Path(STAGE, 'home_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')).read_text(encoding='utf-8')
        mon_ui = (pathlib.Path(STAGE, 'monitor_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/index.html')).read_text(encoding='utf-8')
        self.assertIn('TasksMax=64', svc_txt)
        self.assertIn('volHoldUntil', home_ui)
        self.assertIn('inboxAckFail', home_ui)
        self.assertIn("'__optimistic__'", qq_ui)
        self.assertIn('stopOverlayClip', ov_ui)
        self.assertIn('lastNeedFast', mon_ui)
        self.assertIn('callConfirmed', intercom_src)
        self.assertIn('saved_ambient_owned', intercom_src)

    def test_supplement49_deep_audit_persistence_airplay_wx_and_monotonic_ui(self):
        """Supplement 49 deep audit fixes across all 7 backend services + all WebKit/phone UIs."""
        import io
        import struct
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        h = HOME
        s = StudyFocusTests.s
        q = MusicSleepTests.q
        b = load('bridge_s49', 'bridge')
        mon_mod = load('monitor_s49', 'monitor')
        air_mod = load('airplay_s49', 'airplay')
        intercom_src = path_of('intercom').read_text(encoding='utf-8')
        bridge_src = path_of('bridge').read_text(encoding='utf-8')
        H8095 = {'Host': '127.0.0.1:8095'}

        # 1. Monitor normalize_traffic: UTC 'Z' vs '+08:00' timezone-aware calibration match
        limit_b = 1073741824000
        baseline_b = 769411229286
        observed_b = 790000000000
        now_utc = mon_mod.dt.datetime(2026, 11, 11, 6, 0, tzinfo=mon_mod.dt.timezone.utc)
        mon_cfg = {
            'rule_id': 1,
            'server_id': 3,
            'limit_bytes': limit_b,
            'complete_from': '2026-11-30T16:00:00Z',
            'calibration': {
                'from': '2026-11-01T00:00:00+08:00',
                'to': '2026-12-01T00:00:00+08:00',
                'used_bytes': baseline_b,
                'observed_bytes': observed_b,
            },
        }
        cal_res = mon_mod.normalize_traffic({
            'success': True,
            'data': {
                'cycle_transfer_stats': {
                    '1': {
                        'max': limit_b,
                        'from': '2026-10-31T16:00:00Z',
                        'to': '2026-11-30T16:00:00Z',
                        'transfer': {'3': 800000000000},
                    }
                }
            },
        }, mon_cfg, now=now_utc)
        self.assertTrue(cal_res['calibrated'])
        self.assertEqual(cal_res['used_bytes'], baseline_b + (800000000000 - observed_b))
        uncal_res = mon_mod.normalize_traffic({
            'success': True,
            'data': {
                'cycle_transfer_stats': {
                    '1': {
                        'max': limit_b,
                        'from': '2026-11-30T16:00:00Z',
                        'to': '2026-12-31T16:00:00Z',
                        'transfer': {'3': 800000000000},
                    }
                }
            },
        }, mon_cfg, now=mon_mod.dt.datetime(2026, 12, 5, 6, 0, tzinfo=mon_mod.dt.timezone.utc))
        self.assertFalse(uncal_res['calibrated'])
        self.assertEqual(uncal_res['used_bytes'], 800000000000)

        # 2. Bridge: ambient Chinese stop/alias normalization, focus_body params, timer cancel/add/unknown validation
        self.assertEqual(b._norm_ambient_arg('关闭白噪音'), 'off')
        self.assertEqual(b._norm_ambient_arg('停止雨声'), 'off')
        self.assertEqual(b._norm_ambient_arg('下雨声'), 'rain')
        self.assertEqual(b._norm_ambient_arg('海浪声'), 'waves')
        self.assertEqual(b._norm_ambient_arg('森林自然音'), 'forest')
        self.assertEqual(b._norm_ambient_arg('禅意颂钵'), 'singing_bowl')
        self.assertEqual(b._focus_body({'action': 'start'}), {'action': 'start', 'minutes': 25})
        self.assertEqual(
            b._focus_body({'action': 'start', 'minutes': 40, 'break_minutes': 0, 'with_study': False, 'ambient': '海浪声'}),
            {'action': 'start', 'minutes': 40, 'break_minutes': 0, 'with_study': False, 'ambient': 'waves'},
        )
        self.assertEqual(b._focus_body({'action': '结束专注'}), {'action': 'stop'})

        seen_tmr = []

        def fake_tmr_open(req, timeout=6):
            seen_tmr.append(json.loads(req.data.decode('utf-8')))
            return io.BytesIO(b'{"ok":true,"message":"ok"}')

        with mock.patch.object(b._NO_PROXY_OPENER, 'open', side_effect=fake_tmr_open):
            b._call_home_timer({'action': 'cancel'})
            b._call_home_timer({'action': '重置'})
            b._call_home_timer({'action': 'add', 'seconds': 180})
            with self.assertRaises(ValueError):
                b._call_home_timer({'action': 'unknown_action'})
        self.assertEqual(seen_tmr[0], {'action': 'stop'})
        self.assertEqual(seen_tmr[1], {'action': 'stop'})
        self.assertEqual(seen_tmr[2], {'action': 'add', 'seconds': 180})
        self.assertIn('socket.MSG_PEEK', bridge_src)
        self.assertIn('_clear_pcm_q()', bridge_src)

        # 3. AirPlay wrapper: pause_conflicting_media stops Home voice clip & set_audio_active manages uxplay-audio-active
        import threading as _th
        air_urls = []
        air_done = _th.Event()
        air_tmp_mirror = pathlib.Path(TMP.name, 'air_s49_run', 'uxplay-mirroring-active')
        air_tmp_mirror.parent.mkdir(parents=True, exist_ok=True)

        def fake_air_open(req, timeout=1.2):
            air_urls.append((req.full_url, json.loads((req.data or b'{}').decode('utf-8'))))
            if 'alarm-tone' in req.full_url:
                air_done.set()
            cm_dummy = mock.MagicMock()
            cm_dummy.__enter__.return_value = io.BytesIO(b'{"ok":true}')
            return cm_dummy

        air_mod._last_pause_ts = 0.0
        with mock.patch.object(air_mod._NO_PROXY_OPENER, 'open', side_effect=fake_air_open), \
             mock.patch.object(air_mod, 'pause_bluetooth_media', lambda: None), \
             mock.patch.object(air_mod, 'MIRROR_FLAG_PATH', air_tmp_mirror):
            air_mod.pause_conflicting_media()
            self.assertTrue(air_done.wait(timeout=2.0))
            self.assertEqual(air_mod._audio_flag_path().name, 'uxplay-audio-active')
            air_mod.set_audio_active(True)
            self.assertTrue(air_mod._audio_flag_path().exists())
            air_mod.set_audio_active(False)
            self.assertFalse(air_mod._audio_flag_path().exists())
        self.assertTrue(any(':8095/api/home/messages/play' in u and d.get('stop') is True for u, d in air_urls))

        # 4. QQMusic: _clear_duck_except_home_locked preserves 'home' lease; WeChat _qq_comm; play_query stop/mode_only
        ms_helper = MusicSleepTests()
        eng = ms_helper.engine()
        eng._proc = eng._proc2 = eng._pacat = None
        eng.ambient_seq = 0
        eng._duck_leases = {}
        eng.study_duck_until = 0.0
        eng._send_aec_flush = lambda: None
        eng.control('duck', seconds=60.0, source='home')
        eng.control('duck', seconds=15.0, source='study')
        eng.ambient_preset = 'rain'
        eng.set_ambient('off')
        self.assertGreater(eng.study_duck_until - time.monotonic(), 45.0)
        self.assertEqual(list(eng._duck_leases.keys()), ['home'])
        eng.control('pause')
        self.assertGreater(eng.study_duck_until - time.monotonic(), 45.0)
        eng.control('unduck', source='home')
        self.assertEqual(eng.study_duck_until, 0.0)

        wx_cfg = {
            'uin': '1152921504812345678',
            'qm_keyst': 'W_X_abc123',
            'cookies_str': '',
        }
        with mock.patch.object(q, 'load_config', return_value=wx_cfg):
            self.assertTrue(q._is_wx_login(wx_cfg))
            comm = q._qq_comm()
            hdrs = q._qq_headers()
            self.assertEqual(comm.get('tmeLoginType'), 1)
            self.assertEqual(comm.get('uin'), '1152921504812345678')
            self.assertIn('wxuin=1152921504812345678', hdrs.get('Cookie', ''))
            self.assertIn('tmeLoginType=1', hdrs.get('Cookie', ''))

        with mock.patch.object(q, 'qq_search', side_effect=AssertionError('should not search')):
            stop_r = eng.play_query('关闭白噪音')
            self.assertTrue(stop_r.get('ok'))
            self.assertTrue(stop_r.get('mode_only'))

        # 5. Study: background _deferred_resume_task auto-resumes without needing a second reminder + manual switch study_auto=False + uxplay-audio-active media_busy
        async def go_s49_study():
            s_dir = pathlib.Path(TMP.name, 'study_s49')
            s_dir.mkdir(parents=True, exist_ok=True)
            cfg_p = s_dir / 'config.json'
            cfg_p.write_text(json.dumps(dict(s.DEFAULTS, enabled=True, enabled_at=int(time.time()))), encoding='utf-8')
            run_dir = s_dir / 'run'
            run_dir.mkdir(parents=True, exist_ok=True)
            resumed = asyncio.Event()
            idle_flag = [False]

            with mock.patch.object(s, 'CONFIG_PATH', cfg_p), \
                 mock.patch.object(s, 'FOCUS_PATH', s_dir / 'focus.json'), \
                 mock.patch.object(s, 'LOG_DIR', s_dir / 'log'), \
                 mock.patch.object(s, 'VOICE_DIR', s_dir / 'voice'), \
                 mock.patch.object(s, 'RUN', run_dir), \
                 mock.patch.object(s, '_CONFIG_CACHE', (None, None)):
                comp = s.Companion()
                (run_dir / 'uxplay-audio-active').write_text('1\n', encoding='utf-8')
                self.assertTrue(comp.media_busy())
                (run_dir / 'uxplay-audio-active').unlink()
                self.assertFalse(comp.media_busy())

                comp.focus = {'phase': 'focus', 'ends_at': time.time() + 600, 'minutes': 25, 'break_minutes': 5, 'with_study': True, 'study_auto': True}
                comp.switch(False, 'home', announce=False)
                self.assertFalse(comp.focus['study_auto'])

                async def fake_idle():
                    return idle_flag[0]

                async def fake_music(act=None):
                    if act is None:
                        return {'playing': False, 'paused': True, 'ambient': None, 'boot_id': 'b49', 'index': 0, 'alarm_ringing': False, 'track': {'mid': 'm49', 'title': '晴天'}}
                    if act == 'resume':
                        resumed.set()
                    return True

                comp.client_idle = fake_idle
                comp.music = fake_music
                orig_sleep = s.asyncio.sleep

                async def fast_sleep(_sec):
                    idle_flag[0] = True
                    await orig_sleep(0.02)

                with mock.patch.object(s.asyncio, 'sleep', side_effect=fast_sleep):
                    comp._set_deferred_resume({'boot_id': 'b49', 'index': 0, 'track': {'mid': 'm49', 'title': '晴天'}})
                    await asyncio.wait_for(resumed.wait(), timeout=1.5)
                self.assertIsNone(comp._deferred_resume)
                comp._clear_deferred_resume()
        run(go_s49_study())

        # 6. Home: DeskTimer persistence across restart, done+negative add_seconds, manual ambient preservation + rebind_owner,
        #    AUDIO_MIRROR_FLAG 409 guard, cold-cache timer->Pomodoro stop, >256B WAV header parsing, evicted audio 404 on If-None-Match
        junk_payload = b'J' * 32000
        pcm_payload = b'\x10\x04' * 16000
        riff_body = (
            b'WAVE'
            + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 16000, 32000, 2, 16)
            + b'JUNK' + struct.pack('<I', len(junk_payload)) + junk_payload
            + b'data' + struct.pack('<I', len(pcm_payload)) + pcm_payload
        )
        wav_300 = b'RIFF' + struct.pack('<I', len(riff_body)) + riff_body

        def fake_ff_run(cmd, input=None, **_kw):
            return types.SimpleNamespace(returncode=0, stdout=b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\xaa' * 400)

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_ff_run):
            mp3_out, dur_s = h._transcode_to_voice_mp3(wav_300, False)
        self.assertAlmostEqual(dur_s, 1.0, places=1)
        self.assertTrue(mp3_out.startswith(b'ID3'))

        async def go_s49_home():
            ib_dir = pathlib.Path(TMP.name, 'inbox_s49')
            box = h.Inbox(ib_dir / 'messages.json')
            tmr_file = ib_dir / 'timer.json'

            async def _noop_alarm(*_a):
                return None

            tmr1 = h.DeskTimer(lambda: None, _noop_alarm, timer_path=tmr_file)
            tmr1.start(seconds=180, label='煮面')
            self.assertTrue(tmr_file.exists())
            tmr1.shutdown()
            self.assertTrue(tmr_file.exists())  # shutdown preserves timer.json!

            tmr2 = h.DeskTimer(lambda: None, _noop_alarm, timer_path=tmr_file)
            tmr2.restore_on_start()
            st_restored = tmr2.public()
            self.assertIsNotNone(st_restored)
            self.assertEqual(st_restored['label'], '煮面')
            self.assertGreater(st_restored['remaining'], 150)
            tmr2.phase = 'done'
            tmr2.paused_rem = 0.0
            tmr2.done_until = time.monotonic() + 90.0
            st_neg = tmr2.add_seconds(-60)
            self.assertTrue(st_neg['ok'])
            self.assertIsNone(st_neg['timer'])
            self.assertIsNone(tmr2.phase)
            self.assertFalse(tmr_file.exists())

            # Evicted voice message must return 404 even when client sends matching If-None-Match
            v_msg = box.add_voice(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\xbb' * 400, 3.0, 'phone')
            music_srv = web.Application()
            study_srv = web.Application()
            cur_amb = [None]
            study_foc_active = [True]
            study_focus_stops = []

            async def m_status(_r):
                return web.json_response({'ok': True, 'playing': False, 'paused': False, 'track': None, 'ambient': cur_amb[0], 'sleep': None})

            async def m_amb(r):
                bd = await r.json()
                cur_amb[0] = None if bd.get('preset') == 'off' else {'preset': bd.get('preset'), 'seq': 1, 'name': '细雨'}
                return web.json_response({'ok': True, 'ambient': cur_amb[0]})

            async def m_ctrl(_r):
                return web.json_response({'ok': True, 'active': False})

            async def s_get(_r):
                foc = {'phase': 'focus', 'minutes': 25, 'remaining': 900} if study_foc_active[0] else {'phase': None}
                return web.json_response({'enabled': True, 'focus': foc, 'today': {}})

            async def s_focus(r):
                bd = await r.json()
                if bd.get('action') == 'stop':
                    study_foc_active[0] = False
                    study_focus_stops.append(bd)
                return web.json_response({'ok': True, 'phase': None})

            async def s_say(_r):
                return web.json_response({'ok': True})

            music_srv.router.add_get('/status', m_status)
            music_srv.router.add_post('/ambient', m_amb)
            music_srv.router.add_post('/control', m_ctrl)
            study_srv.router.add_get('/api/study', s_get)
            study_srv.router.add_post('/api/study/focus', s_focus)
            study_srv.router.add_post('/api/voice/say', s_say)
            cm = TestClient(TestServer(music_srv))
            cs = TestClient(TestServer(study_srv))
            await cm.start_server()
            await cs.start_server()
            aud_flag = pathlib.Path(TMP.name, 'uxplay_audio_s49')

            with mock.patch.object(h, 'MUSIC', f'http://127.0.0.1:{cm.port}'), \
                 mock.patch.object(h, 'STUDY', f'http://127.0.0.1:{cs.port}'), \
                 mock.patch.object(h, 'CALL_FLAG', pathlib.Path(TMP.name, 'no_call_s49')), \
                 mock.patch.object(h, 'MIRROR_FLAG', pathlib.Path(TMP.name, 'no_mirror_s49')), \
                 mock.patch.object(h, 'AUDIO_MIRROR_FLAG', aud_flag):
                app_h = h.make_app(box)
                ch = TestClient(TestServer(app_h))
                await ch.start_server()
                try:
                    # 1) Cold state_cache timer start still stops active Study Pomodoro!
                    rt = await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch', 'label': '自习'}, headers=H8095)
                    self.assertEqual(rt.status, 200)
                    self.assertEqual(len(study_focus_stops), 1)
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)

                    # 2) Manual ambient started first -> silent stopwatch + manual preset switch must NOT steal ownership,
                    #    while rebind_owner=True explicitly re-attaches ownership (e.g. Intercom post-call restore)!
                    await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'rain'}, headers=H8095)
                    await ch.post('/api/home/timer', json={'action': 'start', 'mode': 'stopwatch'}, headers=H8095)
                    await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'waves'}, headers=H8095)
                    st_j = await (await ch.get('/api/home/state', headers=H8095)).json()
                    self.assertFalse(st_j.get('ambient_owned'))
                    await ch.post('/api/home/music', json={'action': 'ambient', 'preset': 'rain', 'rebind_owner': True}, headers=H8095)
                    st_j2 = await (await ch.get('/api/home/state', headers=H8095)).json()
                    self.assertTrue(st_j2.get('ambient_owned'))
                    await ch.post('/api/home/timer', json={'action': 'stop'}, headers=H8095)
                    self.assertIsNone(cur_amb[0])

                    # 3) AirPlay audio-only flag blocks voice play & voice_reply with 400
                    aud_flag.write_text('1\n', encoding='utf-8')
                    rp_busy = await ch.post('/api/home/messages/play', json={'id': v_msg['id']}, headers=H8095)
                    self.assertEqual(rp_busy.status, 400)
                    rv_busy = await ch.post('/api/home/voice_reply', json={'action': 'start'}, headers=H8095)
                    self.assertEqual(rv_busy.status, 400)
                    aud_flag.unlink()

                    # 4) Audio ETag: 200 -> 304 while in inbox -> 404 once evicted from inbox.items!
                    ra1 = await ch.get(f"/api/home/messages/audio/{v_msg['id']}", headers=H8095)
                    self.assertEqual(ra1.status, 200)
                    etag = ra1.headers.get('ETag')
                    ra2 = await ch.get(f"/api/home/messages/audio/{v_msg['id']}", headers={**H8095, 'If-None-Match': etag})
                    self.assertEqual(ra2.status, 304)
                    with box.lock:
                        box.items = [x for x in box.items if x.get('id') != v_msg['id']]
                    ra3 = await ch.get(f"/api/home/messages/audio/{v_msg['id']}", headers={**H8095, 'If-None-Match': etag})
                    self.assertEqual(ra3.status, 404)
                finally:
                    await ch.close()
                    await cm.close()
                    await cs.close()
        run(go_s49_home())

        # 7. Frontend & Intercom invariants
        home_ui = (pathlib.Path(STAGE, 'index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-home/index.html')).read_text(encoding='utf-8')
        qq_ui = (pathlib.Path(STAGE, 'qqmusic_index.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-qqmusic/index.html')).read_text(encoding='utf-8')
        ov_ui = (pathlib.Path(STAGE, 'home_overlay.js') if STAGE else pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')).read_text(encoding='utf-8')
        parent_ui = (pathlib.Path(STAGE, 'parent.html') if STAGE else pathlib.Path('/usr/local/share/thinksmart-study/parent.html')).read_text(encoding='utf-8')
        self.assertIn('function setRingTime(', home_ui)
        self.assertIn('function syncBgTimerWake(', home_ui)
        self.assertIn('function timerLapOrAdd(', home_ui)  # 2026-11-12 home redesign (Claude)
        self.assertIn('function stopActiveScrClip(', home_ui)
        self.assertIn('closeBrightness(false)', ov_ui)
        self.assertIn("d.cmd === 'timerDone'", ov_ui)
        self.assertIn('const AMB_PRESETS =', qq_ui)
        self.assertIn("cancelBtn.classList.toggle('wide'", qq_ui)
        self.assertIn('const scrubRow =', parent_ui)
        self.assertIn('rebind_owner', intercom_src)
        self.assertIn('uxplay-audio-active', intercom_src)
        self.assertIn('@media (max-width: 500px)', intercom_src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
