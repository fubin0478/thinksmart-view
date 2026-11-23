#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R88: Round 9 Deep Non-Xiaozhi Audit & Regression Tests."""
import ast
import asyncio
import concurrent.futures
import contextlib
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from http.cookiejar import Cookie, CookieJar
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

E = os.environ.get
ROOT = pathlib.Path(E('R88_ROOT') or E('STAGE_ROOT') or '/')


def _p(rel: str) -> pathlib.Path:
    return ROOT / rel.lstrip('/')


def _load_py_clean(name: str, path: pathlib.Path, strip_names=()):
    for d in (str(path.parent), '/usr/local/bin'):
        if d not in sys.path:
            sys.path.insert(0, d)
    src = path.read_text('utf-8')
    tree = ast.parse(src, filename=str(path))
    strip_set = set(strip_names)
    new_body = []
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and getattr(node.test.left, 'id', '') == '__name__':
            continue
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in strip_set for t in node.targets):
            continue
        new_body.append(node)
    tree.body = new_body
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    exec(compile(tree, str(path), 'exec'), mod.__dict__)
    return mod


def _load_script(name: str, path: pathlib.Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


def _load_intercom_ui_mod(name: str, path: pathlib.Path):
    saved = {k: sys.modules.get(k) for k in ('gi', 'gi.repository')}
    try:
        gi_mod = types.ModuleType('gi')
        gi_mod.require_version = lambda *_a, **_k: None
        repo_mod = types.ModuleType('gi.repository')
        for cls_name in ('Gtk', 'Gdk', 'GLib', 'GdkPixbuf'):
            dummy = mock.MagicMock()
            if cls_name == 'Gtk':
                dummy.Window = type('DummyWindow', (), {'__init__': lambda *_a, **_k: None})
            setattr(repo_mod, cls_name, dummy)
        gi_mod.repository = repo_mod
        sys.modules['gi'] = gi_mod
        sys.modules['gi.repository'] = repo_mod
        return _load_py_clean(name, path)
    finally:
        for k, prev in saved.items():
            if prev is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev


class TestTutorAndAdminR88(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tut_path = _p('/usr/local/bin/thinksmart-tutor.py')
        cls.tut = _load_py_clean('r88_tutor', cls.tut_path)
        cls.adm_path = _p('/usr/local/bin/thinksmart-admin.py')
        cls.adm = _load_py_clean('r88_admin', cls.adm_path)

    def test_phone_studio_exposes_and_resets_check_error(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            prefs_p = td_p / 'prefs.json'
            with mock.patch.object(self.tut, 'STATE_DIR', td_p), \
                 mock.patch.object(self.tut, 'PREFS_PATH', prefs_p):
                psm = self.tut.PhoneStudioManager()
                jpg = b'\xff\xd8\xff\xe0' + (b'\x00' * 256)
                pid = psm.save_page(jpg, client_key='cp_test1')
                pg = next(p for p in psm.pages if p['id'] == pid)
                pg['check_error'] = 'VLM timeout'
                pub = psm.public_state()
                self.assertEqual(pub['check_error'], 'VLM timeout')
                self.assertEqual(pub['pages'][0]['check_error'], 'VLM timeout')

                # Non-rotation overwrite clears stale check_error
                psm.save_page(jpg, client_key='cp_test1', page_id=pid, rot_steps=0)
                pub2 = psm.public_state()
                self.assertEqual(pub2['check_error'], '')
                self.assertEqual(pub2['pages'][0]['check_error'], '')

    def test_tutor_llm_chain_fallback_and_url_suffixes(self):
        eng = object.__new__(self.tut.TutorEngine)
        eng._load_tutor_models = lambda: {}

        # Case 1: OpenAILLM has placeholder key -> prefer configured DeepSeekLLM, preserve /v4
        eng._load_gateway_cfg = lambda: {
            'LLM': {
                'OpenAILLM': {'url': 'https://oa.example.com/openai', 'api_key': '你的api_key', 'model_name': 'gpt-6.1-terra'},
                'DeepSeekLLM': {'base_url': 'https://ds.example.com/v4', 'api_key': 'real-ds-secret', 'model_name': 'deepseek-v4.1-flash'},
            }
        }
        v1, key, chain = eng._resolve_llm_chain('fast')
        self.assertEqual(v1, 'https://ds.example.com/v4')
        self.assertEqual(key, 'real-ds-secret')
        self.assertTrue(chain)
        targets = eng._iter_llm_targets('fast')
        self.assertTrue(any(t[0] == 'https://ds.example.com/v4' and t[1] == 'real-ds-secret' for t in targets))

        # Case 2: OpenAILLM /v3 suffix preserved and mock mode preserved
        eng._load_gateway_cfg = lambda: {
            'LLM': {
                'OpenAILLM': {'url': 'https://mock.local/v3', 'api_key': 'mock-k', 'model_name': 'mock-gpt'},
            }
        }
        mv1, mk, mchain = eng._resolve_llm_chain('fast')
        self.assertEqual(mv1, 'https://mock.local/v3')
        self.assertEqual(mk, 'mock-k')
        self.assertEqual(mchain, [('mock-gpt', {})])

    def test_tutor_cam_stage_page_no_double_pencil_boost(self):
        mock_eng = mock.MagicMock()
        mock_eng.camera.capture_jpeg = mock.AsyncMock(return_value=b'\xff\xd8\xff\xe0' + b'\x00' * 256)
        mock_eng.camera._frozen_raw_jpg = None
        mock_eng.phone_studio.save_page.return_value = 'p1'
        mock_eng.phone_studio.public_state.return_value = {'has_page': True}
        mock_eng.check_staged_worksheet_page = mock.AsyncMock(return_value={'ok': True, 'pending': False})
        mock_eng.poll_camera_hotplug.return_value = {'connected': False}
        mock_eng.recorder.cancel = mock.AsyncMock()
        mock_eng.camera.stop_stream = mock.AsyncMock()
        mock_eng.bg_tasks = []
        mock_eng.http = None
        mock_eng.tts_http = None

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                share_d = pathlib.Path(td)
                (share_d / 'index.html').write_text('<html></html>', encoding='utf-8')
                with mock.patch.object(self.tut, 'SHARE_DIR', share_d), \
                     mock.patch.dict(os.environ, {'TUTOR_WARM_GOLDEN': '0'}):
                    app = self.tut.make_app(engine=mock_eng)
                    srv = TestServer(app)
                    cli = TestClient(srv)
                    await cli.start_server()
                    try:
                        h = {'Host': f'127.0.0.1:{srv.port}'}
                        r = await cli.post('/api/tutor/camera/stage_page', json={'pencil_boost': True, 'check_page': True}, headers=h)
                        self.assertEqual(r.status, 200)
                    finally:
                        await cli.close()

        asyncio.run(_run())
        mock_eng.camera.capture_jpeg.assert_awaited_once_with(high_res=True, crop_box=None, pencil_boost=False)
        mock_eng.check_staged_worksheet_page.assert_awaited_once_with(page_id='p1', pencil_boost=True)

    def test_admin_upload_fd_lock_and_save_apis_vllm_inheritance(self):
        adm_src = self.adm_path.read_text('utf-8')
        self.assertIn('fd_lock = threading.Lock()', adm_src)
        self.assertIn('_open_part', adm_src)
        self.assertIn('_write_part', adm_src)
        self.assertIn('_finish_part', adm_src)
        self.assertIn("['Intent', 'VLLM'] if vl_patch is not None else ['Intent']", adm_src)

        # Verify save_settings_apis inherits non-'local' key from active VLLM entry and triggers VLLM reload
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            merged_cfg = {
                'selected_module': {'VLLM': 'ChatGLMVLLM'},
                'VLLM': {
                    'ChatGLMVLLM': {'api_key': 'real-glm-key', 'url': 'https://open.bigmodel.cn/api/paas/v4', 'model_name': 'glm-4v'},
                },
                'Intent': {'function_call': {}},
                'plugins': {},
            }
            mutated = []
            reloaded = []

            class _DummyAdmin:
                cfg_lock = threading.RLock()
                sess = mock.MagicMock()

                async def _notify_hub_reload(self, secs):
                    reloaded.append(list(secs))
                    return 'ok'

                async def _full_settings(self):
                    return {'ok': True}

            da = _DummyAdmin()
            get_resp = mock.MagicMock()
            get_resp.read = mock.AsyncMock(return_value=b'{}')
            cm = mock.MagicMock()
            cm.__aenter__ = mock.AsyncMock(return_value=get_resp)
            cm.__aexit__ = mock.AsyncMock(return_value=False)
            da.sess.get.return_value = cm

            body = {
                'city': '北京',
                'vllm': {'base_url': 'https://api.openai.com/v1', 'model': 'gpt-4o'},
            }
            req = make_mocked_request('POST', '/api/admin/settings/apis', headers={'Host': '127.0.0.1:8097'})
            with mock.patch.object(self.adm, 'json_body', new=mock.AsyncMock(return_value=body)), \
                 mock.patch.object(self.adm, 'load_hub_configs', return_value=(merged_cfg, {})), \
                 mock.patch.object(self.adm, 'mutate_hub_yaml', side_effect=lambda ops, _l: mutated.extend(ops)), \
                 mock.patch.object(self.adm, 'HOME_STATE', td_p):
                resp = asyncio.run(self.adm.Admin.save_settings_apis(da, req))
            self.assertEqual(resp.status, 200)
            self.assertEqual(reloaded, [['Intent', 'VLLM']])
            key_op = [o for o in mutated if o.get('path') == ['VLLM', 'OpenAIVLLM', 'api_key']]
            self.assertEqual(len(key_op), 1)
            self.assertEqual(key_op[0]['value'], 'real-glm-key')


class TestQQMusicAirPlayAndBlankerR88(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq_path = _p('/usr/local/bin/thinksmart-qqmusic.py')
        cls.qq = _load_py_clean('r88_qqmusic', cls.qq_path)
        cls.ap_path = _p('/usr/local/bin/thinksmart-airplay.py')
        cls.ap = _load_py_clean('r88_airplay', cls.ap_path)
        cls.ib_path = _p('/usr/local/bin/idle-blanker.py')

    def test_oauth_preserves_graph_p_skey(self):
        # First HTTP response returns check_sig with Set-Cookie for graph.qq.com then .qq.com empty/stale p_skey,
        # and cj holds graph.qq.com vs .ptlogin2.qq.com
        cj = CookieJar()
        for dom, val in (('graph.qq.com', 'GRAPH_SKEY'), ('.ptlogin2.qq.com', 'PT_SKEY')):
            c = Cookie(
                version=0, name='p_skey', value=val, port=None, port_specified=False,
                domain=dom, domain_specified=True, domain_initial_dot=dom.startswith('.'),
                path='/', path_specified=True, secure=True, expires=None, discard=True,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            )
            cj.set_cookie(c)
        c_uin = Cookie(
            version=0, name='p_uin', value='o123456', port=None, port_specified=False,
            domain='.qq.com', domain_specified=True, domain_initial_dot=True,
            path='/', path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        cj.set_cookie(c_uin)

        class _Headers:
            def __init__(self, vals):
                self._vals = list(vals)

            def get_all(self, name, default=None):
                return list(self._vals) if name.lower() == 'set-cookie' else (default or [])

        seen_post = {}

        class _FakeResp:
            def __init__(self, headers, body=b'', url='https://y.qq.com/?code=FAKECODE'):
                self.headers = headers
                self._body = body
                self._url = url

            def geturl(self):
                return self._url

            def read(self, *_a):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        class _FakeOpener:
            def open(self, req, timeout=6):
                if isinstance(req, str):
                    return _FakeResp(_Headers([
                        'p_skey=GRAPH_SKEY; Domain=graph.qq.com; Path=/',
                        'p_skey=STALE_COM_SKEY; Domain=.qq.com; Path=/',
                    ]))
                body = req.data.decode('utf-8') if req.data else ''
                seen_post['url'] = req.full_url
                seen_post['body'] = body
                return _FakeResp(_Headers([]), b'HTTP/1.1 200 OK')

        with mock.patch.object(self.qq.urllib.request, 'build_opener', return_value=_FakeOpener()), \
             mock.patch.object(self.qq.http.cookiejar, 'CookieJar', return_value=cj), \
             mock.patch.object(self.qq, '_has_only_global_ips', return_value=True), \
             mock.patch.object(self.qq, '_exchange_code_for_musickey', return_value={'ok': True}):
            res_auth = self.qq._complete_qq_oauth('https://ssl.ptlogin2.qq.com/check_sig?s=1')
        self.assertTrue(res_auth.get('ok'))
        self.assertIn('g_tk=', seen_post.get('body', ''))
        expected_gtk = str(self.qq._gtk('GRAPH_SKEY'))
        self.assertIn(f'g_tk={expected_gtk}', seen_post['body'])

    def test_sleep_races_alarm_preview_wake_and_radio_mode(self):
        # 1) sleep_tick skips when _sleep_starting is True
        eng = object.__new__(self.qq.QQMusicEngine)
        eng._lock = threading.RLock()
        eng._sleep_starting = True
        eng.sleep_mode = 'time'
        eng.sleep_deadline = time.monotonic() - 10.0
        self.assertFalse(eng.sleep_tick())

        # 2) sleep_session_start keeps day_volume when r['at'] is None (legacy R12 invariant),
        #    and discards stale day_volume when r['at'] is not None & user changed volume
        with tempfile.TemporaryDirectory() as td:
            eng2 = object.__new__(self.qq.QQMusicEngine)
            eng2._lock = threading.RLock()
            eng2.volume = 60
            eng2.playing = False
            eng2.paused = False
            eng2.ambient_preset = None
            eng2.sleep_mode = None
            eng2.sleep_session = None
            eng2.sleep_fade = 60.0
            eng2._preview = None
            eng2._wake = None
            eng2._sleep_state = {'restore': {'day_volume': 75, 'sleep_volume': 25, 'at': time.time() - 60}}
            eng2._snap_sleep_state_locked = lambda: dict(eng2._sleep_state)
            eng2._clear_sleep_locked = lambda: None
            eng2._sleep_session_public_locked = lambda: {'volume': 25}
            eng2.set_volume = mock.MagicMock()
            eng2.set_ambient = mock.MagicMock(return_value={'ok': True})
            eng2.set_sleep = mock.MagicMock(return_value={'ok': True, 'message': 'ok'})
            with mock.patch.object(self.qq, '_write_sleep_state'):
                res = eng2.sleep_session_start(sound='rain', minutes=30, volume=25)
            self.assertTrue(res['ok'])
            self.assertEqual(eng2._sleep_state['restore']['day_volume'], 60)

        # 3) on_alarm_ringing stops active _preview & saves deferred restore even if volume changed externally
        eng3 = object.__new__(self.qq.QQMusicEngine)
        eng3._lock = threading.RLock()
        eng3.sleep_session = None
        eng3._preview = {'prev_volume': 58, 'volume': 22, 'label': 'rain'}
        eng3._wake = None
        eng3.volume = 40
        eng3.playing = False
        eng3.paused = False
        eng3.ambient_preset = 'rain'
        eng3.sleep_mode = 'time'
        eng3._sleep_state = {}
        eng3._snap_sleep_state_locked = lambda: dict(eng3._sleep_state)
        eng3._clear_sleep_locked = lambda: setattr(eng3, 'sleep_mode', None)
        eng3.set_ambient = mock.MagicMock(return_value={'ok': True})
        eng3.control = mock.MagicMock()
        wrote = []
        with mock.patch.object(self.qq, '_write_sleep_state', side_effect=wrote.append):
            self.assertTrue(eng3.on_alarm_ringing())
        eng3.set_ambient.assert_called_once_with('off')
        self.assertIsNone(eng3._preview)
        self.assertEqual(wrote[-1]['restore']['day_volume'], 58)
        self.assertEqual(wrote[-1]['restore']['sleep_volume'], 22)

        # 4) set_radio_audio_mode stops active live radio proc before spawning new stream
        eng4 = object.__new__(self.qq.QQMusicEngine)
        eng4._lock = threading.RLock()
        eng4.playing = True
        eng4.paused = False
        eng4.ambient_preset = None
        eng4.index = 0
        eng4.queue = [{'radio_id': 'r1', 'is_live': True}]
        eng4.elapsed_base = 0.0
        eng4._gen = 3

        def _stop_live():
            eng4._gen += 1
            return True

        eng4._stop_proc_locked = mock.MagicMock(side_effect=_stop_live)
        eng4._send_aec_flush = mock.MagicMock()
        started = []
        done_ev = threading.Event()
        eng4._start_current_track = lambda seek, gen: (started.append((seek, gen)), done_ev.set())
        with mock.patch.object(self.qq, 'load_config', return_value={'radio_audio_mode': 'hifi'}), \
             mock.patch.object(self.qq, 'save_config'):
            res_rm = eng4.set_radio_audio_mode('voice')
        self.assertTrue(res_rm['ok'])
        self.assertTrue(done_ev.wait(2.0))
        eng4._stop_proc_locked.assert_called_once()
        eng4._send_aec_flush.assert_called_once()
        self.assertEqual(started, [(0.0, 4)])

    def test_airplay_and_idle_blanker_guards(self):
        ap_src = self.ap_path.read_text('utf-8')
        self.assertIn('if require_mirror and not MIRROR_FLAG_PATH.exists():', ap_src)
        self.assertIn("for p in parts[1:4]", ap_src)

        # Verify set_webkit_paused detects thinksmart-kiosk.py when python is invoked with -B
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            p_dir = td_p / '99999'
            p_dir.mkdir()
            (p_dir / 'cmdline').write_bytes(b'/usr/bin/python3\x00-B\x00/usr/local/bin/thinksmart-kiosk.py\x00')
            (p_dir / 'stat').write_text('99999 (python3) S 1 1 1 0', encoding='utf-8')
            fake_entry = mock.MagicMock()
            fake_entry.name = '99999'
            fake_entry.path = str(p_dir)
            fake_entry.stat.return_value = mock.MagicMock(st_uid=1000)
            sc = mock.MagicMock()
            sc.__enter__.return_value = sc
            sc.__exit__.return_value = False
            sc.__iter__.return_value = iter([fake_entry])
            with mock.patch.object(self.ap, 'INTERCOM_FLAG_PATH', td_p / 'no-ic'), \
                 mock.patch.object(self.ap, 'BACKLIGHT_PATH', td_p / 'no-bl'), \
                 mock.patch.object(self.ap.os, 'scandir', return_value=sc), \
                 mock.patch.object(self.ap.os, 'kill') as m_kill:
                self.ap.set_webkit_paused(True)
            m_kill.assert_called_once()

        ib_src = self.ib_path.read_text('utf-8')
        self.assertIn('and sunrise is None and not overlay_active', ib_src)
        self.assertIn('(cur_bl is not None and cur_bl <= BLANK_BRIGHTNESS)', ib_src)


class TestHomeAndMonitorR88(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hm_path = _p('/usr/local/bin/thinksmart-home.py')
        cls.hm = _load_py_clean('r88_home', cls.hm_path)
        cls.mon_path = _p('/usr/local/bin/thinksmart-monitor.py')
        cls.mon = _load_py_clean('r88_monitor', cls.mon_path)

    def test_home_sunrise_guard_etag_open_fresh_and_solar_parse(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            sr_p = td_p / 'sunrise.json'
            sr_p.write_text(json.dumps({'start': time.time() - 10, 'ramp': 600, 'hold': 600}), encoding='utf-8')
            inbox = self.hm.Inbox(td_p / 'inbox.json')
            cd = self.hm.CountdownStore(td_p / 'cd.json')

            async def _fake_get_json(_s, url, *_a, **_k):
                if 'music' in str(url):
                    return {'playing': False, 'paused': False, 'volume': 20}
                return {'enabled': False, 'focus': {'phase': None}}

            async def _run():
                with mock.patch.object(self.hm, 'SUNRISE_RUNTIME_PATH', sr_p), \
                     mock.patch.object(self.hm, '_get_json', side_effect=_fake_get_json):
                    app = self.hm.make_app(inbox=inbox, countdowns=cd)
                    srv = TestServer(app)
                    cli = TestClient(srv)
                    await cli.start_server()
                    try:
                        h = {'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'}
                        # screen_off blocked while sunrise_wake is active
                        r_off = await cli.post('/api/home/sleepmode', json={'action': 'screen_off'}, headers=h)
                        self.assertEqual(r_off.status, 400)

                        # get_state exposes sunrise_wake and invalidates ETag when open_fresh expires
                        cd.trigger_open_ui()
                        r1 = await cli.get('/api/home/state', headers=h)
                        self.assertEqual(r1.status, 200)
                        j1 = await r1.json()
                        self.assertTrue(j1['music']['sunrise_wake'])
                        self.assertTrue(j1['countdowns']['open_fresh'])
                        et1 = r1.headers.get('ETag')

                        cd._open_ui_mono = time.monotonic() - 30.0
                        r2 = await cli.get('/api/home/state', headers={**h, 'If-None-Match': et1})
                        self.assertEqual(r2.status, 200)
                        j2 = await r2.json()
                        self.assertFalse(j2['countdowns']['open_fresh'])

                        rb = await cli.post('/api/home/countdowns/tool', json={'action': 'add', 'title': '测试', 'date': '4月31日'}, headers=h)
                        self.assertEqual(rb.status, 200)
                        self.assertFalse((await rb.json())['ok'])

                        today_cn = dt.datetime.now(self.hm.LOCAL_TZ).date()
                        fut_cand = today_cn + dt.timedelta(days=15)
                        if fut_cand.year == today_cn.year:
                            md_str = f'{fut_cand.month}月{fut_cand.day}日'
                            rc = await cli.post('/api/home/countdowns/tool', json={'action': 'add', 'kind': 'countup', 'title': '恋爱纪念', 'date': md_str}, headers=h)
                            self.assertEqual(rc.status, 200)
                            jc = await rc.json()
                            self.assertTrue(jc['ok'])
                            self.assertTrue(jc['item']['target_date'].startswith(f'{today_cn.year - 1}-'))
                    finally:
                        await cli.close()

            asyncio.run(_run())

        # _expire_stale_buf source check
        hm_src = self.hm_path.read_text('utf-8')
        self.assertIn('(self.buf or self.aborted_reason):', hm_src)

    def test_monitor_reload_servers_preserves_live_public_note(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            cfg_p = td_p / 'servers.json'
            cfg_p.write_text(json.dumps([
                {'id': 1, 'name': 'Node1', 'expires': '2026-01-01', 'price': 'old', 'cycle': 'Year', 'traffic_limit': '100GB', 'traffic_reset_day': 16},
            ]), encoding='utf-8')
            m = object.__new__(self.mon.Monitor)
            m._servers_cfg_checked = -999.0
            m._servers_cfg_path = cfg_p
            m._servers_cfg_sig = None
            m.seed = []
            m.seed_by_id = {}
            m._seed_fallback = []
            m.ids = [1]
            m.servers = [{'id': 1, 'name': 'Node1', 'alive': True, 'expires': '2027-12-31', 'price': '99CNY', 'cycle': 'Month', 'traffic_limit': '2TB'}]
            m._seq = 0
            cfg_p.write_text(json.dumps([
                {'id': 1, 'name': 'Node1', 'expires': '2026-01-01', 'price': 'old', 'cycle': 'Year', 'traffic_limit': '100GB', 'traffic_reset_day': 20},
            ]), encoding='utf-8')
            self.assertTrue(m._reload_servers_config(force=True))
            s0 = m.servers[0]
            self.assertEqual(s0['alive'], True)
            self.assertEqual(s0['expires'], '2027-12-31')
            self.assertEqual(s0['price'], '99CNY')
            self.assertEqual(s0['cycle'], 'Month')
            self.assertEqual(s0['traffic_limit'], '2TB')
            self.assertEqual(s0['traffic_reset_day'], 20)


class TestIntercomStudyUsbAndFrontendsR88(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ic_path = _p('/usr/local/bin/thinksmart-intercom.py')
        cls.ic = _load_py_clean('r88_intercom', cls.ic_path)
        cls.ui_path = _p('/usr/local/bin/thinksmart-intercom-ui.py')
        cls.ui = _load_intercom_ui_mod('r88_intercom_ui', cls.ui_path)
        cls.st_path = _p('/usr/local/bin/thinksmart-study.py')
        cls.st = _load_py_clean('r88_study', cls.st_path)
        cls.wd_path = _p('/usr/local/sbin/thinksmart-usb-watchdog')
        cls.wd = _load_script('r88_usb_watchdog', cls.wd_path)

    def test_intercom_ws_backpressure_and_connect_invariant(self):
        ic_src = self.ic_path.read_text('utf-8')
        # CRITICAL R87/test_admin_20261120 invariant: exact 2 occurrences
        self.assertEqual(ic_src.count("&& !/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) connectWS();"), 2)
        self.assertIn('self._cam_backoff_until = 0.0', ic_src)
        self.assertIn('self.cam_restarts = self.cam_restarts[-5:]', ic_src)
        self.assertIn('asyncio.wait_for(ws.send_bytes(payload), timeout=0.4)', ic_src)
        self.assertIn('asyncio.wait_for(ws.send_bytes(tx_mv), timeout=0.25)', ic_src)

        ws = mock.MagicMock()
        ws._writer.protocol._paused = True
        ws._writer.protocol._drain_waiter = None
        ws._writer.transport.get_write_buffer_size.return_value = 0
        self.assertTrue(self.ic.IntercomServer._ws_backpressured(ws, 65536))
        ws._writer.protocol._paused = False
        ws._writer.protocol._drain_waiter = object()
        self.assertTrue(self.ic.IntercomServer._ws_backpressured(ws, 65536))
        ws._writer.protocol._drain_waiter = None
        self.assertFalse(self.ic.IntercomServer._ws_backpressured(ws, 65536))

    def test_intercom_ui_remote_shown_flush_after_cam_off(self):
        win = self.ui.IntercomWindow.__new__(self.ui.IntercomWindow)
        win.decode_cond = threading.Condition()
        win.frame_lock = threading.Lock()
        win.remote_off_pending = False
        win.flush_scheduled = False
        win._remote_shown = True
        win.ready_f1 = None
        win.ready_f2 = None
        win.latest_f1 = None
        win.f1_seq = 5
        win.f2_seq = 0
        win.last_remote_ts = None
        win.last_local_ts = None
        win.sensor_rot = 0
        win._apply_chrome_orientation = lambda *_a: None
        win.last_pb1 = object()
        win.remote_img = mock.MagicMock()
        win.avatar_box = mock.MagicMock()
        ret = win.flush_frames()
        self.assertFalse(ret)
        self.assertFalse(win._remote_shown)
        self.assertIsNone(win.last_pb1)
        win.remote_img.clear.assert_called_once()
        win.avatar_box.show.assert_called_once()

    def test_study_prepare_voice_short_circuit_and_play_recheck(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            c = self.st.Companion.__new__(self.st.Companion)
            c.stopping = False
            calls = []

            async def _fail_synth(_cfg, txt):
                calls.append(txt)
                return False

            c.synthesize = _fail_synth
            c.phrase_file = lambda txt, _v=None: td_p / f'{hash(txt)}.pcm'
            with mock.patch.object(self.st, 'VOICE_DIR', td_p / 'voices'):
                ok = asyncio.run(c.prepare_voice({'voice': 'xiaohe'}))
            self.assertFalse(ok)
            self.assertEqual(len(calls), 1)

            # play() rechecks media_busy AFTER synthesize() before calling music('pause')
            c2 = self.st.Companion.__new__(self.st.Companion)
            c2.stopping = False
            c2.voice_lock = asyncio.Lock()
            c2._night_view_active = lambda: False
            busy_flag = [False]
            c2.media_busy = lambda: busy_flag[0]
            c2.client_idle = mock.AsyncMock(return_value=True)
            pcm = td_p / 'new_phrase.pcm'
            c2.phrase_file = lambda _t, _v=None: pcm

            async def _ok_synth(_cfg, _t):
                busy_flag[0] = True
                pcm.write_bytes(b'\x00' * 4800)
                return True

            c2.synthesize = _ok_synth
            c2.music = mock.AsyncMock()
            with mock.patch.object(self.st, 'load_config', return_value={'voice': 'xiaohe'}):
                self.assertFalse(asyncio.run(c2.play('休息了')))
            c2.music.assert_not_called()

    def test_usb_watchdog_non_host_and_pending_wait(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            role_p = td_p / 'role'
            role_p.write_text('host\n', encoding='utf-8')
            wd = self.wd.UsbWatchdog(
                role_path=str(role_p), usb1_path=str(td_p / 'usb1'), usb_mode_bin='/bin/true',
                mnt_dir=str(td_p / 'mnt'), lock_path=str(td_p / 'lock'),
                cooldown_sec=0.0, settle_sec=0.0, role_gap_sec=0.0,
            )

            def _role_becomes_none(cmd, *_a, **_k):
                if 'device' in cmd:
                    role_p.write_text('none\n', encoding='utf-8')
                return mock.MagicMock(returncode=0)

            with mock.patch.object(self.wd.subprocess, 'run', side_effect=_role_becomes_none):
                wd.recover('test_transient_none_role')
            self.assertTrue(wd._host_restore_pending)
        self.assertIn('wd._dead_kmsg_pending or wd._host_restore_pending:', self.wd_path.read_text('utf-8'))

    def test_frontend_html_js_fixes_r88(self):
        tut_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('!ddSwitching && audioEl.paused && ddUrl && !audioEl.ended && isDdSrc()', tut_idx)

        tut_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('function _cancelPrintReq()', tut_mob)
        self.assertIn("if (action === 'check_page' && !baseCanvas && serverHasStaged)", tut_mob)
        self.assertIn('crop_box: baseCanvas ? crop : null', tut_mob)

        qq_idx = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        self.assertIn('if (pollBusyAt !== myBusyAt) return;', qq_idx)
        self.assertIn('trackSwitchBusy', qq_idx)
        self.assertIn('document.body.classList.toggle(\'live-radio\', !!t.is_live);', qq_idx)
        self.assertIn('if (!r || !r.isConnected || r.disabled) return;', qq_idx)

        hm_idx = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('st.music.sunrise_wake', hm_idx)
        self.assertIn('if (!(ackItem && ackItem.due) && !d.due) clearScrPlayUi();', hm_idx)

        mon_idx = _p('/usr/local/share/thinksmart-monitor/index.html').read_text('utf-8')
        self.assertIn('else if(d.received_at===null){lastGoodTraffic=null;', mon_idx)
        self.assertIn('Boolean((t.complete||t.calibrated)&&t.remaining_bytes!=null)', mon_idx)

        disp_html = _p('/usr/local/share/thinksmart-monitor/display.html').read_text('utf-8')
        self.assertIn('if(dragging){dragging=false;dragPid=null;tellParentTouch(false)}', disp_html)

        ho = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('html.ts-portrait #settings,html.ts-portrait #wifi{', ho)
        self.assertIn('due: !!targetItem?.due', ho)
        self.assertIn('nativeRaf(() => nativeRaf(() => {', ho)
        self.assertIn(':not(#powerOff):not(#ring):not(#tsRotCover):not(#tsVoiceCard):not(#tsOrHint):not(.tskb-dock)', ho)

        parent_html = _p('/usr/local/share/thinksmart-study/parent.html').read_text('utf-8')
        self.assertIn('function issuesOf(e)', parent_html)
        self.assertIn('Array.isArray(e.issues) ? e.issues : issuesOf(e)', parent_html)


if __name__ == '__main__':
    unittest.main()
