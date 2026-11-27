#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R86: Round 7 Deep Non-Xiaozhi Audit & Regression Tests."""
import ast
import asyncio
import base64
import contextlib
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
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

from aiohttp.test_utils import TestClient, TestServer

E = os.environ.get
ROOT = pathlib.Path(E('R86_ROOT') or E('STAGE_ROOT') or '/')


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


class TestTutorAndAdminR86(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tut_path = _p('/usr/local/bin/thinksmart-tutor.py')
        cls.tut = _load_py_clean('r86_tutor', cls.tut_path)
        cls.adm_path = _p('/usr/local/bin/thinksmart-admin.py')
        cls.adm = _load_py_clean('r86_admin', cls.adm_path)

    def test_tutor_redact_ocr_asr_and_upload_validation(self):
        red = self.tut._redact_secrets(
            'POST https://u:pw@tts.example.com/v1/tts?token=SECRET_XYZ99 '
            'Authorization: Bearer my.jwt.token_abc sk-1234567890abcdef extraSecret99',
            'extraSecret99',
        )
        self.assertNotIn('u:pw', red)
        self.assertNotIn('SECRET_XYZ99', red)
        self.assertNotIn('my.jwt.token_abc', red)
        self.assertNotIn('1234567890abcdef', red)
        self.assertNotIn('extraSecret99', red)
        self.assertIn('https://tts.example.com/***', red)
        self.assertIn('Bearer ***', red)
        self.assertIn('sk-***', red)

        eng = object.__new__(self.tut.TutorEngine)
        eng._post_chat_once = mock.AsyncMock(return_value='抱歉，图片太模糊，无法识别题干内容。')
        res = asyncio.run(eng._ocr_image_opening(b'\xff\xd8\xff\xd9'))
        self.assertEqual(res, ('拍题微课', '', ''))

        class _BoomHttp:
            def post(self, *_a, **_k):
                raise asyncio.TimeoutError()

        eng.http = _BoomHttp()
        eng._asr_endpoint = lambda: ('https://asr.internal.example/v1?tk=SECRET123', 'sk-topsecret', 'whisper')
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(eng.transcribe_voice_wav(b'RIFF1234'))
        self.assertIn('超时', str(ctx.exception))
        self.assertNotIn('asr.internal.example', str(ctx.exception))
        self.assertNotIn('SECRET123', str(ctx.exception))

        # api_phone_upload should fail closed with 400 when normalize_to_jpeg rejects image
        stub_ps = types.SimpleNamespace(
            mark_mobile_seen=lambda *_a: None,
            normalize_to_jpeg=mock.AsyncMock(side_effect=ValueError('unsupported image')),
            active_id='p1',
        )
        stub_eng = types.SimpleNamespace(
            phone_studio=stub_ps,
            active_session_id='',
            sessions={},
            bg_tasks=set(),
            http=None,
            tts_http=None,
            _cancel_previous_session_work=lambda: None,
            camera=types.SimpleNamespace(stop_stream=mock.AsyncMock()),
            recorder=types.SimpleNamespace(cancel=mock.AsyncMock()),
        )
        app = self.tut.make_app(stub_eng)

        async def _hit_upload():
            async with TestClient(TestServer(app)) as cli:
                payload = {'image_b64': base64.b64encode(b'not-an-image' * 20).decode('ascii'), 'question': 'help'}
                r = await cli.post('/api/tutor/phone_upload', json=payload, headers={'X-TS-Internal': '1'})
                return r.status, await r.json()

        st, body = asyncio.run(_hit_upload())
        self.assertEqual(st, 400)
        self.assertFalse(body.get('ok'))

        tut_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertEqual(tut_html.count("if (typeof ddClear === 'function' && (ddUrl || ddEl)) ddClear();"), 4)
        mob_html = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('printReqSeq', mob_html)
        self.assertIn('_revokePrintBlob', mob_html)
        man = json.loads(_p('/usr/local/share/thinksmart-tutor/manifest.json').read_text('utf-8'))
        self.assertEqual(man.get('theme_color'), '#e3dbce')

    def test_admin_redact_same_origin_import_dedup_and_edgetts(self):
        red = self.adm._redact_err(
            'http://gateway.local:9000/secret/tts?key=ABC12345 Bearer tokXYZ.9 sk-9988776655 secretPass9',
            'secretPass9',
        )
        self.assertNotIn('ABC12345', red)
        self.assertNotIn('tokXYZ.9', red)
        self.assertNotIn('9988776655', red)
        self.assertNotIn('secretPass9', red)
        self.assertIn('http://gateway.local:9000/...', red)

        # same_origin_ok preserves http://127.0.0.1 test requests while blocking scheme & explicit port mismatches
        req_ok = types.SimpleNamespace(headers={'Origin': 'http://127.0.0.1', 'Host': '127.0.0.1:45678'})
        req_bad_port = types.SimpleNamespace(headers={'Origin': 'http://127.0.0.1:9999', 'Host': '127.0.0.1:8097'})
        req_bad_scheme = types.SimpleNamespace(headers={'Origin': 'https://127.0.0.1:8097', 'Host': '127.0.0.1:8097'})
        self.assertTrue(self.adm.same_origin_ok(req_ok))
        self.assertFalse(self.adm.same_origin_ok(req_bad_port))
        self.assertFalse(self.adm.same_origin_ok(req_bad_scheme))

        # ImportJob duplicate (name, sz) must never leak inflight_remaining even if destination appears mid-flight
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            u1, u2 = tdp / 'd1' / 'song.mp3', tdp / 'd2' / 'song.mp3'
            u1.parent.mkdir()
            u2.parent.mkdir()
            u1.write_bytes(b'x' * 512)
            u2.write_bytes(b'x' * 512)
            loc = tdp / 'local'
            loc.mkdir()
            with mock.patch.object(self.adm, 'MUSIC_LOCAL', loc):
                job = self.adm.ImportJob(
                    [(u1, 'song.mp3', 512), (u2, 'song.mp3', 512)],
                    threading.Lock(),
                    inflight_box={'local': 0},
                    need_bytes=512,
                    reserved_keys=[('song.mp3', 512)],
                )
                job.run()
                self.assertEqual(len(job.imported), 1)
                self.assertEqual(len(job.skipped), 1)
                self.assertEqual(job.inflight_remaining, 0)

                # Pre-existing at run time (TOCTOU skip) also releases reserved bytes
                job2 = self.adm.ImportJob(
                    [(u1, 'song.mp3', 512)],
                    threading.Lock(),
                    inflight_box={'local': 0},
                    need_bytes=512,
                    reserved_keys=[('song.mp3', 512)],
                )
                job2.run()
                self.assertEqual(len(job2.skipped), 1)
                self.assertEqual(job2.inflight_remaining, 0)

        # EdgeTTS uses --text=<txt> (handles leading dash) and does NOT fall through on failure when active TTS != EdgeTTS
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            edge_bin = tdp / '.venv' / 'bin' / 'edge-tts'
            edge_bin.parent.mkdir(parents=True)
            edge_bin.write_text('#!/bin/sh\n', encoding='utf-8')
            srv = object.__new__(self.adm.Admin)
            srv.sess = mock.MagicMock()
            seen_cmds = []

            class _FakeProc:
                returncode = 0

            async def _fake_exec(*args, **_kw):
                seen_cmds.append(list(args))
                for i, a in enumerate(args):
                    if a == '--write-media':
                        pathlib.Path(args[i + 1]).write_bytes(b'ID3' + b'\x00' * 600)
                return _FakeProc()

            req_tts = types.SimpleNamespace(headers={'Host': '127.0.0.1:8097'})
            with mock.patch.object(self.adm, 'HUB_DIR', tdp), \
                 mock.patch.object(self.adm, 'STATE_DIR', tdp / 'state'), \
                 mock.patch.object(self.adm, 'load_hub_configs', return_value=({'selected_module': {'TTS': 'IndexStreamTTS'}, 'TTS': {'EdgeTTS': {}}}, {})), \
                 mock.patch.object(self.adm, 'json_body', mock.AsyncMock(return_value={'kind': 'tts_preview', 'selected': 'EdgeTTS', 'text': '-5℃天冷了'})), \
                 mock.patch.object(self.adm, '_communicate_bounded', mock.AsyncMock(return_value=(b'', b''))), \
                 mock.patch.object(self.adm.asyncio, 'create_subprocess_exec', side_effect=_fake_exec):
                resp = asyncio.run(srv.test_settings(req_tts))
            self.assertEqual(resp.status, 200)
            self.assertTrue(any('--text=-5℃天冷了' in cmd for cmd in seen_cmds))

            # Missing binary + active TTS != EdgeTTS -> must raise 502 instead of falling through to /kiosk/tts
            with mock.patch.object(self.adm, 'HUB_DIR', tdp / 'missing'), \
                 mock.patch.object(self.adm, 'load_hub_configs', return_value=({'selected_module': {'TTS': 'IndexStreamTTS'}, 'TTS': {'EdgeTTS': {}}}, {})), \
                 mock.patch.object(self.adm, 'json_body', mock.AsyncMock(return_value={'kind': 'tts_preview', 'selected': 'EdgeTTS', 'text': 'hello'})):
                with self.assertRaises(self.adm.ApiError) as err_ctx:
                    asyncio.run(srv.test_settings(req_tts))
            self.assertEqual(err_ctx.exception.status, 502)

        adm_html = _p('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertIn('function syncStudyTtsPvBtn(mode)', adm_html)
        self.assertIn("syncStudyTtsPvBtn('');", adm_html)
        sk_py = _p('/usr/local/sbin/thinksmart-set-search-keys').read_text('utf-8')
        self.assertIn('user=pw.pw_uid', sk_py)
        self.assertNotIn("'sudo'", sk_py)
        pw_sh = _p('/usr/local/sbin/thinksmart-set-admin-password').read_text('utf-8')
        self.assertIn('LC_ALL=C.UTF-8', pw_sh)
        self.assertIn('setpriv --reuid=ubuntu', pw_sh)


class TestHomeMonitorBlankerR86(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = _load_py_clean('r86_home', _p('/usr/local/bin/thinksmart-home.py'))
        cls.mon = _load_py_clean('r86_mon', _p('/usr/local/bin/thinksmart-monitor.py'))
        cls.ib = _load_script('r86_ib', _p('/usr/local/bin/idle-blanker.py'))

    def test_home_options_preflight_and_lunar_voice_tool(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            ib = self.home.Inbox(tdp)
            cds = self.home.CountdownStore(tdp / 'countdowns.json')
            app = self.home.make_app(inbox=ib, countdowns=cds)

            async def _check_http():
                async with TestClient(TestServer(app)) as cli:
                    for path in ('/api/home/orient', '/api/home/messages/play'):
                        r = await cli.options(
                            path,
                            headers={'Host': '127.0.0.1:8095', 'Origin': 'http://127.0.0.1:8092'},
                        )
                        self.assertEqual(r.status, 204, path)
                        self.assertIn('OPTIONS', r.headers.get('Access-Control-Allow-Methods', ''))
                        self.assertEqual(r.headers.get('Access-Control-Allow-Origin'), 'http://127.0.0.1:8092')
                    r_cd = await cli.post(
                        '/api/home/countdowns/tool',
                        json={'action': 'add', 'title': '妈妈农历生日', 'cal': 'lunar', 'repeat': 'once', 'lunar': '八月十五', 'date': '08-15'},
                        headers={'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'},
                    )
                    self.assertEqual(r_cd.status, 200)
                    cd_json = await r_cd.json()
                    self.assertTrue(cd_json.get('ok'), cd_json)

            asyncio.run(_check_http())

        hm_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('const want = parentVisible && tpOpen()', hm_html)
        self.assertIn('ntDimBusy', hm_html)
        self.assertIn('(!isYearlyRule || useStartYear)', hm_html)
        ho_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn("orEtag = '';", ho_js)
        self.assertNotIn("xzAuto", ho_js)   # 2026-11-25：小麦没有单独的页面，横竖屏都是胶囊
        or_idx = ho_js.index('function orSetClasses(o)')
        self.assertIn('syncVoicePill();', ho_js[or_idx:or_idx + 220])

    def test_monitor_hot_reload_and_proxy_valueerror_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            ad = pathlib.Path(td)
            (ad / 'servers.json').write_text('[{"id": 1, "name": "s1"}]', encoding='utf-8')
            (ad / 'traffic.json').write_text('{}', encoding='utf-8')
            cfg = ad / 'config.json'
            cfg.write_text(json.dumps({'nezha_host': 'https://a.example.com'}), encoding='utf-8')
            m = self.mon.Monitor(assets=ad, state_dir=ad)
            m._runtime_override()
            m._alive_state[1] = False
            m.speed_hist[1] = [12.3]
            m.received_at = time.monotonic()
            m.received_wall = time.time()
            m._override_checked = 0.0
            cfg.write_text(json.dumps({'nezha_host': 'https://b.example.com'}), encoding='utf-8')
            m.compute_badge()
            self.assertEqual(m._override_cfg.get('nezha_host'), 'https://b.example.com')
            self.assertEqual(m._alive_state, {})
            self.assertIsNone(m.received_at)

            # Proxy raises ValueError -> must retry direct (cand=None)
            seen_proxies = []

            class _FakeResp:
                status = 200
                content = types.SimpleNamespace(read=mock.AsyncMock(side_effect=[b'{"ok":true}', b'']))
                json = mock.AsyncMock(return_value={'ok': True})
                def raise_for_status(self):
                    pass
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *_a):
                    pass

            class _FakeSess:
                closed = False
                def get(self, _url, proxy=None, **_kw):
                    seen_proxies.append(proxy)
                    if proxy is not None:
                        raise ValueError('bad proxy url')
                    return _FakeResp()

            m.traffic_session = _FakeSess()
            with mock.patch.object(m, '_proxy_order', return_value=('http://127.0.0.1:7890', None)):
                data = asyncio.run(m._http_get_json('https://b.example.com/api/v1/service'))
            self.assertEqual(data, {'ok': True})
            self.assertEqual(seen_proxies, ['http://127.0.0.1:7890', None])

    def test_idle_blanker_pre0630_sunrise_fifo_and_daemon_resumes(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            fifo = tdp / 'pipe'
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                self.ib._read_small_text(fifo)

            tgt = tdp / 'real.txt'
            tgt.write_text('2000\n')
            lnk = tdp / 'lnk.txt'
            lnk.symlink_to(tgt)
            with self.assertRaises(OSError):
                self.ib._read_small_text(lnk)

        # Pre-06:30 sunrise source check + 06:30 invariant preserved
        ib_src = _p('/usr/local/bin/idle-blanker.py').read_text('utf-8')
        self.assertIn('(NIGHT_LEVEL if in_night() else normal_brightness())', ib_src)
        self.assertIn('WebKitNetworkProcess', ib_src)
        self.assertIn('thinksmart-kiosk.py', ib_src)

        pre_dawn = time.mktime((2026, 11, 21, 5, 30, 0, 0, 0, -1))
        at_0630 = time.mktime((2026, 11, 21, 6, 30, 0, 0, 0, -1))
        with mock.patch.object(self.ib, 'NIGHT_TEST', True):
            self.assertTrue(self.ib.in_night(pre_dawn))
            self.assertFalse(self.ib.in_night(at_0630))

        for path_str, marker in (
            ('/usr/local/bin/vcnl4200-proximity-daemon.py', 'bp.is_symlink()'),
            ('/usr/local/bin/vcnl4200-proximity-daemon.py', 'WebKitNetworkProcess'),
            ('/usr/local/bin/screen-wake-daemon.py', 'WebKitNetworkProcess'),
            ('/usr/local/bin/gpu-watchdog.py', "pkill', '-CONT'"),
        ):
            self.assertIn(marker, _p(path_str).read_text('utf-8'))


class TestQqMusicAirPlayR86(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq_path = _p('/usr/local/bin/thinksmart-qqmusic.py')
        cls.qq = _load_py_clean('r86_qq', cls.qq_path, strip_names=('engine',))
        cls.ap_src = _p('/usr/local/bin/thinksmart-airplay.py').read_text('utf-8')

    def test_qq_bind_uin_clears_graph_p_skey_and_safe_urlopen_closes_httperror(self):
        cfg = {'uin': '111111', 'graph_p_skey': 'OLD_GRAPH_PSKEY', 'musickey': 'old_mk'}
        saved = []
        with mock.patch.object(self.qq, 'load_config', side_effect=lambda: dict(cfg)), \
             mock.patch.object(self.qq, 'save_config', side_effect=lambda d: saved.append(dict(d))):
            self.qq.qq_bind_uin('uin=o222222; p_skey=NEW_YQQ; qm_keyst=NEW_MK')
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['uin'], '222222')
        self.assertNotIn('graph_p_skey', saved[0])

        closed = []

        class _MockHttpErr(urllib.error.HTTPError):
            def close(self):
                closed.append(True)
                super().close()

        err = _MockHttpErr('https://y.qq.com/test', 503, 'Unavailable', {}, io.BytesIO(b'err'))
        with mock.patch.object(self.qq.urllib.request, 'urlopen', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                self.qq._safe_urlopen('https://y.qq.com/test')
        self.assertEqual(closed, [True])

    def test_wake_step_resets_sg_last_and_airplay_ntp_timeout(self):
        e = object.__new__(self.qq.QQMusicEngine)
        e._lock = threading.Lock()
        e.volume = 18
        e._sg_last = (0.2, 1.0)
        e.playing = True
        e.paused = False
        e.ambient_preset = 'rain'
        e.ambient_seq = 1
        e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'rain'}}
        e.alarm_ringing = False
        e.bt_streaming = False
        e._proc = e._proc2 = e._pacat = None
        e._stop_proc_locked = lambda: False
        e.save_session = lambda *a, **k: None
        e.set_volume = lambda v: setattr(e, 'volume', int(v)) or int(v)
        wk = {
            'seq': 1, 'preset': 'rain', 'target': 20, 'day_vol': 55, 'vol': 18, 'user_vol': False,
            'max_vol': 25, 'start': time.monotonic() - 200, 'ramp': 60, 'hold': 60,
            'end': time.monotonic() - 1.0, 'gen': 1, 'timer': None, 'stopped': False,
        }
        e._wake = wk
        e._sleep_state = {'restore': None}
        with tempfile.TemporaryDirectory() as td:
            none_flag = pathlib.Path(td) / 'missing.flag'
            with mock.patch.object(self.qq, 'CALL_FLAG_PATH', none_flag), \
                 mock.patch.object(self.qq, 'MIRROR_FLAG_PATH', none_flag), \
                 mock.patch.object(self.qq, 'SLEEP_STATE_PATH', pathlib.Path(td) / 'sleep.json'), \
                 mock.patch.object(self.qq, '_sunrise_mark_stopped'), \
                 mock.patch.object(self.qq, '_write_sleep_state'):
                e._wake_step(wk)
        self.assertEqual(e._sg_last[0], 1.0)

        self.assertIn('"ntp timeout" in line', self.ap_src)
        self.assertIn('def pause_conflicting_media(force: bool = False)', self.ap_src)
        self.assertIn('pause_conflicting_media(force=True)', self.ap_src)

        qq_html = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        for marker in ('function packPlayTracks', 'favRetryAfter', 'radioDspBusy', 'clearPendingSeek();'):
            self.assertIn(marker, qq_html)


class TestIntercomStudyUsbSystemdR86(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ic_path = _p('/usr/local/bin/thinksmart-intercom.py')
        cls.ic = _load_py_clean('r86_ic', cls.ic_path)
        cls.st = _load_py_clean('r86_st', _p('/usr/local/bin/thinksmart-study.py'))
        cls.wd_src = _p('/usr/local/sbin/thinksmart-usb-watchdog').read_text('utf-8')

    def test_intercom_lock_order_and_shutter_demand(self):
        srv = object.__new__(self.ic.IntercomServer)
        srv.clients = set()
        srv.video_paused = set()
        srv.in_call = False
        srv.cam_proc = None
        srv.cam_task = None
        srv.call_lock = asyncio.Lock()
        srv.mic_lock = asyncio.Lock()
        srv.camera_lock = asyncio.Lock()
        order = []

        async def _fake_stop_call(fast=False):
            assert not srv.camera_lock.locked(), 'camera_lock held before stop_call (AB-BA deadlock!)'
            order.append('stop_call')

        async def _fake_stop_mic():
            assert not srv.camera_lock.locked(), 'camera_lock held before stop_mic_if_idle!'
            order.append('stop_mic')

        srv.stop_call = _fake_stop_call
        srv.stop_mic_if_idle = _fake_stop_mic
        asyncio.run(srv.stop_camera_if_idle(fast=True))
        self.assertEqual(order, ['stop_call', 'stop_mic'])

        srv.in_call = True
        self.assertTrue(srv._has_video_demand())
        self.assertFalse(srv._has_video_demand(shutter_closed=True))

        ic_src = self.ic_path.read_text('utf-8')
        self.assertIn('HttpProcessingError', ic_src)
        ui_src = _p('/usr/local/bin/thinksmart-intercom-ui.py').read_text('utf-8')
        self.assertIn('target_box=(1280, 800)', ui_src)

    def test_study_sleep_allow_retry_tunnel_usb_and_systemd(self):
        c = self.st.Companion.__new__(self.st.Companion)
        c.stopping = False
        c.voice_lock = asyncio.Lock()
        c._night_view_active = lambda: False
        c.media_busy = lambda: False
        c.client_idle = mock.AsyncMock(return_value=True)
        with tempfile.TemporaryDirectory() as td:
            pcm_p = pathlib.Path(td) / 'phrase.pcm'
            pcm_p.write_bytes(b'\x00' * 4800)
            c.phrase_file = lambda _txt, _v=None: pcm_p
            c.music = mock.AsyncMock(return_value={'sleep_session': True, 'playing': False, 'paused': True})
            with mock.patch.object(self.st, '_write_sleep_state', None, create=True):
                # Mock _play_pcm so no audio device is opened
                c._play_pcm = mock.AsyncMock(return_value=True)
                ok_blocked = asyncio.run(c.play('休息了', from_duck=False, allow_sleep=False))
                self.assertFalse(ok_blocked)

            called_calls = []

            async def _fake_play(text, from_duck=False, allow_sleep=False):
                called_calls.append((text, from_duck, allow_sleep))
                return len(called_calls) >= 2

            c.play = _fake_play
            c.announcements = set()
            c._deferred_resume = None
            c._deferred_resume_at = 0.0

            async def _run_retry():
                t = c._announce_retry('提醒', tries=2, gap=0.0, from_duck=True)
                return await t

            self.assertTrue(asyncio.run(_run_retry()))
            self.assertEqual(called_calls, [('提醒', True, True), ('提醒', False, True)])

        for tun in ('/usr/local/bin/thinksmart-intercom-tunnel.sh', '/usr/local/bin/thinksmart-intercom-tunnel-jp.sh'):
            txt = _p(tun).read_text('utf-8')
            self.assertLess(txt.index('port_held=1'), txt.index('up_elapsed" -ge 45'))
            self.assertIn('proxy_up_elapsed:-0}" -ge 45', txt)

        self.assertIn('next_check = time.monotonic() + 30.0', self.wd_src)
        usb_sh = _p('/usr/local/bin/usb-automount.sh').read_text('utf-8')
        self.assertIn('flock -w 5 -x 9 || flock -w 10 -x 9 || exit 0', usb_sh)
        self.assertIn('ntfs3', usb_sh)

        for rel, expected in (
            ('/etc/systemd/system/screen-wake.service.d/override.conf', ('MemoryMax=64M', 'TasksMax=16')),
            ('/etc/systemd/system/thinksmart-gpu-watchdog.service.d/override.conf', ('MemoryMax=64M', 'TasksMax=16')),
            ('/etc/systemd/system/thinksmart-intercom-tunnel.service.d/hardening.conf', ('MemoryMax=64M',)),
            ('/etc/systemd/system/thinksmart-intercom-tunnel-jp.service.d/hardening.conf', ('MemoryMax=64M',)),
            ('/etc/systemd/system/thinksmart-intercom.service.d/hardening.conf', ('MemoryMax=240M', 'TasksMax=96')),
            ('/etc/systemd/system/thinksmart-qqmusic.service.d/limits.conf', ('MemoryMax=256M', 'TasksMax=64')),
            ('/etc/systemd/system/thinksmart-usb-mount@.service', ('TimeoutStopSec=20',)),
        ):
            cfg_txt = _p(rel).read_text('utf-8')
            for exp in expected:
                self.assertIn(exp, cfg_txt, f'{rel} missing {exp}')


if __name__ == '__main__':
    unittest.main()
