#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R84: Round 5 Deep Non-Xiaozhi Audit & Regression Tests."""
import ast
import asyncio
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
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

E = os.environ.get
ROOT = pathlib.Path(E('R84_ROOT') or E('STAGE_ROOT') or '/')


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


class TestAdminR84(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adm_path = _p('/usr/local/bin/thinksmart-admin.py')
        cls.adm_src = cls.adm_path.read_text('utf-8')
        cls.adm = _load_py_clean('r84_admin', cls.adm_path)

    def test_auth_thread_safety_stress(self):
        with tempfile.TemporaryDirectory() as td:
            auth = self.adm.Auth(pathlib.Path(td))
            auth.set_password_record({'salt': '00', 'hash': '11'})
            toks = [auth.create(True, f'ua-{i}')[0] for i in range(10)]
            errors = []

            def worker(tid):
                try:
                    for i in range(25):
                        t = toks[(tid + i) % len(toks)]
                        auth.record()
                        auth.check(t)
                        auth.count()
                        if i % 7 == 0:
                            nt, _ = auth.create(False, f'w-{tid}')
                            auth.check(nt)
                            auth.revoke(nt)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=10)
            self.assertFalse(errors)
            self.assertGreater(auth.count(), 0)
            self.assertLessEqual(auth.count(), 30)

    def test_clash_stream_cap_and_disconnect_watcher(self):
        from aiohttp.test_utils import make_mocked_request
        adm_obj = self.adm.Admin.__new__(self.adm.Admin)
        adm_obj.ws_count = 8
        adm_obj.secret = ''
        adm_obj.open_clash_reqs = set()
        req_cap = make_mocked_request('GET', '/admin/api/clash/logs')
        async def run_cap():
            with self.assertRaises(self.adm.ApiError) as ctx:
                await adm_obj.clash(req_cap)
            self.assertEqual(ctx.exception.status, 429)
        asyncio.run(run_cap())
        self.assertIn("watch_disc", self.adm_src)
        self.assertIn("tr.is_closing()", self.adm_src)


class TestTutorR84(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tut_path = _p('/usr/local/bin/thinksmart-tutor.py')
        cls.tut_src = cls.tut_path.read_text('utf-8')
        cls.tut = _load_py_clean('r84_tutor', cls.tut_path)

    def test_stage1_retest_demotes_on_peek_or_attempts_without_solved(self):
        with tempfile.TemporaryDirectory() as td:
            eng = self.tut.TutorEngine.__new__(self.tut.TutorEngine)
            eng._mistakes_loaded = True
            eng._ensure_mistakes_loaded = lambda: None
            eng.mistakes = [{
                'id': 'm_r84',
                'title': '测试错题',
                'question': '1+1=?',
                'stage': 1,
                'graduated': False,
                'retest_pass_count': 1,
                'weak_step': 0,
                'next_review_at': 0,
                'updated_at': 0,
            }]
            saved = []
            eng._save_mistakes = lambda: saved.append(True)
            sess = {
                'id': 's1',
                'question': '1+1=?',
                'teach_mode': 'standard',
                'mistake_id': 'm_r84',
                'steps': [{'step': 1, 'board': 'a'}],
            }
            res = eng._enroll_or_refresh_mistake(sess, {'peeked': True, 'solved': False, 'weak_step': 2})
            self.assertIsNone(res)
            self.assertEqual(eng.mistakes[0]['stage'], 0)
            self.assertFalse(eng.mistakes[0]['graduated'])
            self.assertEqual(eng.mistakes[0]['weak_step'], 2)
            self.assertTrue(saved)

    def test_phone_studio_save_prefs_preserves_external_mobile_url(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'prefs.json'
            p.write_text(json.dumps({'mobile_entry_url': 'https://old.example/tutor/'}), encoding='utf-8')
            with mock.patch.object(self.tut, 'PREFS_PATH', p), \
                 mock.patch.object(self.tut, 'STATE_DIR', pathlib.Path(td)):
                pm = object.__new__(self.tut.PhoneStudioManager)
                pm.seq = 1
                pm.pages = []
                pm.active_id = ''
                pm._entry_url = 'https://old.example/tutor/'
                pm._prefs_sig = pm._stat_prefs_sig()
                pm._fb_rotate = 0
                pm._fb_uploaded_at = 0
                pm._fb_note = ''
                pm._fb_last_crop = None
                # External admin update changes mobile_entry_url
                time.sleep(0.01)
                p.write_text(json.dumps({'mobile_entry_url': 'https://new.example/tutor/'}), encoding='utf-8')
                pm._save_prefs()
                after1 = json.loads(p.read_text('utf-8'))
                self.assertEqual(after1.get('mobile_entry_url'), 'https://new.example/tutor/')
                # External admin clears mobile_entry_url
                time.sleep(0.01)
                p.write_text(json.dumps({}), encoding='utf-8')
                pm._save_prefs()
                after2 = json.loads(p.read_text('utf-8'))
                self.assertNotIn('mobile_entry_url', after2)


class TestQqMusicR84(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq_path = _p('/usr/local/bin/thinksmart-qqmusic.py')
        cls.qq_src = cls.qq_path.read_text('utf-8')
        cls.qq = _load_py_clean('r84_qq', cls.qq_path, strip_names=('engine',))

    def test_openblas_cache_filter_and_sleep_session_clear(self):
        self.assertIn("os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')", self.qq_src)
        self.assertIn("os.environ.setdefault('OMP_NUM_THREADS', '1')", self.qq_src)
        self.assertIn("'.sqlite-wal'", self.qq_src)
        self.assertIn("'.sqlite-shm'", self.qq_src)
        self.assertIn("def make_app(ctrl_pool=None)", self.qq_src)

        e = object.__new__(self.qq.QQMusicEngine)
        e._lock = threading.Lock()
        e._aec_lock = threading.Lock()
        e.aec_clients = set()
        e._proc = e._proc2 = e._pacat = None
        e._gen = 1
        e._starting_gen = -1
        e.ambient_preset = 'rain'
        e.ambient_layer = None
        e._ambient_buf = e._ambient_layer_buf = None
        e.sleep_mode = 'time'
        e.sleep_deadline = time.monotonic() + 1800
        e.sleep_total = 30
        e.sleep_tracks_left = 0
        e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'rain'}}
        e.ducked = False
        e.study_duck_until = 0.0
        e._duck_leases = {}
        e.playing = False
        e.paused = True
        e.queue = [{'mid': 'm1', 'title': 'Song 1', 'artist': 'A'}]
        e.index = 0
        e.elapsed_base = 12.0
        e.play_started_mono = 0.0
        e.lyrics = []
        e.lyrics_seq = 1
        e.queue_seq = 1
        e.play_mode = 'loop'
        e._just_started_mono = None
        e._preview = None
        e._radio_prog_saved_mono = 0.0
        e._sleep_state = {}
        e._sleep_idle_since = None
        e.bt_streaming = False
        e.save_session = lambda *a, **k: None
        ended = []
        orig_end = self.qq.QQMusicEngine._sleep_session_end
        def _wrap_end(reason):
            ended.append(reason)
            return orig_end(e, reason)
        e._sleep_session_end = _wrap_end
        e._send_aec_flush = lambda: None
        e._start_current_track = lambda *a, **k: None

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(self.qq, 'SLEEP_STATE_PATH', pathlib.Path(td) / 'sleep.json'):
                e.set_ambient('off')
                self.assertIsNone(e.sleep_session)
                self.assertIsNone(e.sleep_mode)
                self.assertEqual(ended[-1], 'stop')

                # Resume from ambient sleep session ends sleep session as 'replaced'
                e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'rain'}}
                e.sleep_mode = 'time'
                e.ambient_preset = 'rain'
                e.control('resume')
                self.assertIsNone(e.sleep_session)
                self.assertIsNone(e.sleep_mode)
                self.assertEqual(ended[-1], 'replaced')

                # play_tracks ends active sleep_session when not _sleep_starting, but preserves standalone sleep_mode
                e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'waves'}}
                e.sleep_mode = 'time'
                e.play_tracks([{'mid': 'm2', 'title': 'Song 2'}], 0)
                self.assertIsNone(e.sleep_session)
                self.assertEqual(ended[-1], 'replaced')

                e.sleep_mode = 'time'
                e.sleep_session = None
                e.set_ambient('off')
                self.assertEqual(e.sleep_mode, 'time')

    def test_album_tracks_anon_fallback_and_url_probe_2xx_only(self):
        calls = []

        class _Resp:
            def __init__(self, payload, status=200):
                self._b = json.dumps(payload).encode('utf-8')
                self.status = status
                self.headers = {}
            def read(self, *_a):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                pass

        def fake_open(req, timeout=6):
            calls.append(dict(req.header_items()))
            if len(calls) == 1:
                return _Resp({'req_0': {'code': 0, 'data': {'songList': []}}})
            return _Resp({'req_0': {'code': 0, 'data': {'songList': [{'songInfo': {'mid': 'alb1', 'name': 'Story 1'}}]}}})

        with mock.patch.object(self.qq, 'load_config', return_value={'cookies_str': 'uin=100; qm_keyst=bad', 'qm_keyst': 'bad'}), \
             mock.patch.object(self.qq, '_safe_urlopen', side_effect=fake_open):
            tracks = self.qq.qq_album_tracks('001abc')
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]['mid'], 'alb1')

        # Non-2xx probe must NOT enter _SAFE_FFMPEG_URL_CACHE
        with tempfile.TemporaryDirectory():
            with mock.patch.object(self.qq, '_SAFE_FFMPEG_URL_CACHE', {}), \
                 mock.patch.object(self.qq, '_has_only_global_ips', return_value=True):
                orig_uo = self.qq.urllib.request.urlopen
                try:
                    self.qq._ORIG_URLOPEN = object()
                    class _FakeErr(self.qq.urllib.error.HTTPError):
                        def __init__(self):
                            super().__init__('https://93.184.216.34/test.mp3', 403, 'Forbidden', {}, None)
                    class _FakeOpener:
                        def open(self, *_a, **_k):
                            raise _FakeErr()
                    with mock.patch.object(self.qq.urllib.request, 'build_opener', return_value=_FakeOpener()):
                        self.qq._resolve_safe_ffmpeg_url('https://93.184.216.34/test.mp3')
                        self.assertEqual(len(self.qq._SAFE_FFMPEG_URL_CACHE), 0)
                finally:
                    self.qq._ORIG_URLOPEN = orig_uo


class TestHomeMonitorDaemonsR84(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home_path = _p('/usr/local/bin/thinksmart-home.py')
        cls.home = _load_py_clean('r84_home', cls.home_path)
        cls.mon = _load_py_clean('r84_monitor', _p('/usr/local/bin/thinksmart-monitor.py'))
        cls.ib = _load_script('r84_ib', _p('/usr/local/bin/idle-blanker.py'))
        cls.ap = _load_py_clean('r84_airplay', _p('/usr/local/bin/thinksmart-airplay.py'))

    def test_home_unduck_weather_and_orient_etag(self):
        home_src = self.home_path.read_text('utf-8')
        self.assertNotIn('_clear_duck_flag_if_idle', home_src)
        self.assertGreaterEqual(home_src.count('await _unduck_home_audio_if_idle()'), 7)

        w = self.home.Weather()
        with mock.patch.object(self.home, 'weather_credentials', return_value=('h.example', 'key12345678')), \
             mock.patch.object(self.home, 'weather_cities', return_value=['北京', '北京']):
            w._sync_creds()
            w.last_force = time.monotonic()
            w._slot('北京')['last_force'] = time.monotonic()
            with mock.patch.object(self.home, 'weather_cities', return_value=['北京', '北京', '上海']):
                w._sync_creds()
            self.assertEqual(w.last_force, 0.0)
            self.assertEqual(w._slot('北京')['last_force'], 0.0)

        from aiohttp.test_utils import TestClient, TestServer
        async def run_orient():
            with tempfile.TemporaryDirectory() as td:
                iio = pathlib.Path(td) / 'iio:device0'
                iio.mkdir()
                (iio / 'in_accel_x_raw').write_text('0\n')
                (iio / 'in_accel_y_raw').write_text('512\n')
                (iio / 'in_accel_z_raw').write_text('0\n')
                with mock.patch.dict(os.environ, {'HOME_IIO_DIR': str(td)}), \
                     mock.patch.object(self.home, '_ACCEL_CACHE', {'dir': None, 'checked': 0.0, 'sample': None, 'sample_dir': None, 'sample_at': 0.0}):
                    app = self.home.make_app(self.home.Inbox(pathlib.Path(td) / 'm.json'), self.home.CountdownStore(pathlib.Path(td) / 'c.json'))
                    async with TestClient(TestServer(app)) as cli:
                        r1 = await cli.get('/api/home/orient', headers={'Host': '127.0.0.1:8095'})
                        self.assertEqual(r1.status, 200)
                        etag = r1.headers.get('ETag')
                        self.assertTrue(etag)
                        r2 = await cli.get('/api/home/orient', headers={'Host': '127.0.0.1:8095', 'If-None-Match': etag})
                        self.assertEqual(r2.status, 304)
        asyncio.run(run_orient())

    def test_monitor_override_hot_reload_resets_caches(self):
        with tempfile.TemporaryDirectory() as td:
            sd = pathlib.Path(td) / 'state'
            ad = pathlib.Path(td) / 'assets'
            sd.mkdir()
            ad.mkdir()
            (ad / 'servers.json').write_text('[]', encoding='utf-8')
            (ad / 'traffic.json').write_text('{}', encoding='utf-8')
            cfg = sd / 'config.json'
            cfg.write_text(json.dumps({'nezha_host': 'https://a.example', 'nezha_proxy': 'http://127.0.0.1:7890'}), encoding='utf-8')
            m = self.mon.Monitor(assets=ad, state_dir=sd)
            m._runtime_override()
            m.servers = [{'id': 1}]
            m.carriers = [{'name': 'old'}]
            m._srv_latency_cache[1] = {'ts': 1.0}
            m._prefer_direct_until = time.monotonic() + 180.0
            gen0 = m._upstream_gen
            time.sleep(0.01)
            cfg.write_text(json.dumps({'nezha_host': 'https://b.example', 'nezha_proxy': 'http://127.0.0.1:7891'}), encoding='utf-8')
            m._override_checked = 0.0
            m._runtime_override()
            self.assertGreater(m._upstream_gen, gen0)
            self.assertEqual(m._prefer_direct_until, 0.0)
            self.assertEqual(m.servers, [])
            self.assertEqual(m.carriers, [])
            self.assertEqual(m._srv_latency_cache, {})

    def test_idle_blanker_and_airplay_fixes(self):
        with tempfile.TemporaryDirectory() as td:
            bp = pathlib.Path(td) / 'brightness'
            bp.write_text('100\n')
            with mock.patch.object(self.ib, 'BACKLIGHT_PATH', str(bp)), \
                 mock.patch.object(pathlib.Path, 'write_text', side_effect=PermissionError), \
                 mock.patch.object(self.ib.subprocess, 'run', side_effect=self.ib.subprocess.TimeoutExpired('sudo', 2)) as m_run:
                self.assertFalse(self.ib.set_brightness(120))
                self.assertIn('-n', m_run.call_args.args[0])

        ib_src = _p('/usr/local/bin/idle-blanker.py').read_text('utf-8')
        self.assertIn('except (SystemExit, KeyboardInterrupt):', ib_src)

        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [self.ap.subprocess.TimeoutExpired('uxplay', 0.5), self.ap.subprocess.TimeoutExpired('uxplay', 1.0)]
        fake_proc.kill.side_effect = ProcessLookupError
        self.ap.stop_proc(fake_proc)  # must not raise
        ap_src = _p('/usr/local/bin/thinksmart-airplay.py').read_text('utf-8')
        self.assertIn('"End streaming to GStreamer audio pipeline" in line', ap_src)


class TestIntercomStudyAndFrontendR84(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ic_path = _p('/usr/local/bin/thinksmart-intercom.py')
        cls.ic_src = cls.ic_path.read_text('utf-8')
        cls.ic = _load_py_clean('r84_intercom', cls.ic_path)
        cls.st_path = _p('/usr/local/bin/thinksmart-study.py')
        cls.st = _load_py_clean('r84_study', cls.st_path)

    def test_intercom_no_numpy_pcm_scaling_and_fast_ws_send(self):
        self.assertNotIn('\nimport numpy', self.ic_src)
        import struct
        samples = (-32768, -5000, -1, 0, 1, 5000, 32767)
        raw = struct.pack('<' + 'h' * len(samples), *samples)
        out8 = struct.unpack('<' + 'h' * len(samples), self.ic._scale_pcm_s16le(raw, 8.0))
        self.assertEqual(out8, tuple(max(-32768, min(32767, x * 8)) for x in samples))
        out25 = struct.unpack('<' + 'h' * len(samples), self.ic._scale_pcm_s16le(raw, 2.5))
        self.assertEqual(out25[0], -32768)
        self.assertEqual(out25[-1], 32767)
        self.assertEqual(out25[1], -12500)

        srv = object.__new__(self.ic.IntercomServer)
        srv.clients = set()
        srv.listeners = set()
        srv.video_paused = set()
        srv.frame_div = {}
        srv.in_call = False
        srv.call_owner = None
        srv.call_lock = asyncio.Lock()
        srv.camera_lock = asyncio.Lock()
        srv.mic_lock = asyncio.Lock()
        srv.cam_proc = srv.mic_proc = srv.cam_task = srv.mic_task = None
        srv.sensor_rot = 0
        srv.flip_h = True
        srv.read_gpio_switches = lambda: (False, False)

        class _SlowWs:
            closed = False
            def __init__(self, buf_sz=0, sleep_s=0.0):
                self._writer = types.SimpleNamespace(transport=types.SimpleNamespace(get_write_buffer_size=lambda: buf_sz))
                self.sleep_s = sleep_s
                self.sent = []
            async def send_json(self, d):
                if self.sleep_s:
                    await asyncio.sleep(self.sleep_s)
                self.sent.append(d)

        async def run_ws():
            full_ws = _SlowWs(buf_sz=200000)
            hang_ws = _SlowWs(buf_sz=0, sleep_s=2.0)
            ok_ws = _SlowWs(buf_sz=0, sleep_s=0.0)
            srv.clients = {full_ws, ok_ws}
            t0 = time.monotonic()
            await srv.broadcast_status()
            await srv._send_json_fast(hang_ws, {'type': 'ping'}, max_buf=None, timeout=0.08)
            dt = time.monotonic() - t0
            self.assertLess(dt, 0.5)
            self.assertEqual(full_ws.sent, [])
            self.assertEqual(len(ok_ws.sent), 1)
        asyncio.run(run_ws())

    def test_study_long_tts_quiet_night_and_unsynced_focus_done(self):
        c = self.st.Companion.__new__(self.st.Companion)
        c.focus = {
            'phase': 'focus', 'started_at': 100.0, 'ends_at': 1600.0, 'minutes': 25,
            'break_minutes': 5, 'with_study': False, 'study_auto': False,
            'started_mono': time.monotonic() - 10.0, 'ends_mono': time.monotonic() + 1490.0,
            'unsynced': True,
        }
        logged = []
        c.save_focus = lambda *_a: None
        c.append_log = logged.append
        c._cancel_focus_announcements = lambda: None
        c.focus_wake = asyncio.Event()
        c.wake = asyncio.Event()
        with mock.patch.object(self.st.time, 'time', return_value=2000000000.0):
            c.focus_stop(reason='user')
        self.assertEqual(logged[-1]['minutes_done'], 0)

        st_src = self.st_path.read_text('utf-8')
        self.assertIn('4 * 1024 * 1024', st_src)
        self.assertIn('pcm_secs + 8.0', st_src)
        self.assertIn("before.get('sleep_session')", st_src)

    def test_frontend_and_tunnel_and_systemd_invariants(self):
        ui_src = _p('/usr/local/bin/thinksmart-intercom-ui.py').read_text('utf-8')
        self.assertIn('def _decode_worker_loop(', ui_src)
        self.assertIn('"size-prepared"', ui_src)
        enq_block = ui_src.split('def enqueue_frame(')[1].split('def flush_frames(')[0]
        self.assertNotIn('last_pb1', enq_block)

        adm_html = _p('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertIn('const seq = pv.seq;', adm_html)
        self.assertIn('grokFetchedBots', adm_html)

        tut_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('if (hadDd) playingInlineQa = false;', tut_html)
        self.assertIn('audioEl.src === targetSrc', tut_html)

        mob_html = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('body.print-sheet-ready', mob_html)
        self.assertIn('renderPreviewStage(false)', mob_html)
        self.assertIn('let wasZeroWidth', mob_html)

        qq_html = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        self.assertIn("'white_noise', 'pink_noise', 'brown_noise'", qq_html)
        self.assertIn('let stateMutSeq = 0;', qq_html)

        ko_js = _p('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js').read_text('utf-8')
        self.assertIn("tab === 'fav' ? 'openFav'", ko_js)
        self.assertIn('let tickDueAt = 0;', ko_js)

        hm_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('if (abortScrRec) abortScrRec();', hm_html)

        ho_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('subEvictTimers', ho_js)
        self.assertIn("'If-None-Match': orEtag", ho_js)

        disp_html = _p('/usr/local/share/thinksmart-monitor/display.html').read_text('utf-8')
        self.assertIn("'.orient-row','.theme-row'", disp_html)

        par_html = _p('/usr/local/share/thinksmart-study/parent.html').read_text('utf-8')
        self.assertIn('embedActive = !!active', par_html)

        t = _p('/usr/local/bin/thinksmart-intercom-tunnel.sh').read_text('utf-8')
        self.assertIn('/proc/$SSH_PID/comm', t)
        self.assertIn('SLP_PID=$!', t)
        self.assertIn('pkill -TERM -P "$SSH_PID"', t)

        self.assertIn('MemoryMax=48M', _p('/etc/systemd/system/thinksmart-usb-watchdog.service').read_text('utf-8'))
        self.assertIn('MemoryMax=240M', _p('/home/ubuntu/.config/systemd/user/thinksmart-airplay.service').read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
