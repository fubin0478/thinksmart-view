#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R85: Round 6 Deep Non-Xiaozhi Audit & Regression Tests."""
import ast
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import stat
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
ROOT = pathlib.Path(E('R85_ROOT') or E('STAGE_ROOT') or '/')


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


class TestQqMusicAirPlayR85(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq_path = _p('/usr/local/bin/thinksmart-qqmusic.py')
        cls.qq_src = cls.qq_path.read_text('utf-8')
        cls.qq = _load_py_clean('r85_qq', cls.qq_path, strip_names=('engine',))
        cls.ap_path = _p('/usr/local/bin/thinksmart-airplay.py')
        cls.ap_src = cls.ap_path.read_text('utf-8')

    def test_refresh_musickey_uses_graph_p_skey(self):
        class _Resp:
            def __init__(self, body, headers=None):
                self._b = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
                self.status = 200
                self.headers = dict(headers or {})
            def read(self, *_a):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                pass

        cfg = {
            'uin': '123456',
            'qm_keyst': 'old_mk',
            'graph_p_skey': 'GRAPH_PSKEY_ONLY',
            'cookies_str': 'uin=o123456; p_skey=YQQ_DIFF_PSKEY; qm_keyst=old_mk',
        }
        saved = []
        auth_cookies = []

        def fake_open(req, timeout=5):
            url = req.full_url
            if 'authorize' in url:
                auth_cookies.append(req.get_header('Cookie') or '')
                return _Resp(b'', headers={'Location': 'https://y.qq.com/portal/wx_redirect.html?code=CODE1'})
            return _Resp({'req': {'data': {'musickey': 'NEW_VIP_KEY_1234'}}})

        with mock.patch.object(self.qq, '_LAST_REFRESH_FAIL', (None, 0.0)), \
             mock.patch.object(self.qq, '_has_only_global_ips', return_value=True), \
             mock.patch.object(self.qq, 'load_config', side_effect=lambda: dict(cfg)), \
             mock.patch.object(self.qq, 'save_config', side_effect=lambda d: saved.append(dict(d))), \
             mock.patch.object(self.qq, '_safe_urlopen', side_effect=fake_open), \
             mock.patch.object(self.qq.urllib.request, 'build_opener') as m_bo:
            m_bo.return_value.open.side_effect = fake_open
            ok = self.qq._refresh_musickey_from_cookies()
        self.assertTrue(ok)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['qm_keyst'], 'NEW_VIP_KEY_1234')
        self.assertTrue(auth_cookies and 'p_skey=GRAPH_PSKEY_ONLY' in auth_cookies[0])
        self.assertIn('p_skey=YQQ_DIFF_PSKEY', saved[0]['cookies_str'])

    def test_wake_preserves_day_vol_and_ambient_sleep_clears_on_seek_pause_jump(self):
        e = object.__new__(self.qq.QQMusicEngine)
        e._lock = threading.Lock()
        e._vol_lock = threading.Lock()
        e._aec_lock = threading.Lock()
        e.aec_clients = set()
        e._proc = e._proc2 = e._pacat = None
        e._gen = 1
        e._starting_gen = -1
        e.volume = 25
        e._wake = {'day_vol': 68, 'vol': 25, 'user_vol': False}
        e._preview = None
        e._sleep_state = {'restore': None}
        e.sleep_session = None
        e.sleep_mode = None
        e.ambient_preset = 'rain'
        e.ambient_layer = None
        e._ambient_buf = e._ambient_layer_buf = None
        e.playing = True
        e.paused = False
        e.ducked = False
        e.study_duck_until = 0.0
        e._duck_leases = {}
        e.queue = [{'mid': 'm1', 'title': 'A', 'duration': 200}, {'mid': 'm2', 'title': 'B', 'duration': 200}]
        e.index = 0
        e.elapsed_base = 0.0
        e.play_started_mono = 0.0
        e.lyrics = []
        e.lyrics_seq = 1
        e.queue_seq = 1
        e.play_mode = 'loop'
        e._just_started_mono = None
        e._radio_prog_saved_mono = 0.0
        e._sleep_idle_since = None
        e.bt_streaming = False
        e.save_session = lambda *a, **k: None
        e._send_aec_flush = lambda: None
        e._start_current_track = lambda *a, **k: None
        e._stop_proc_locked = lambda: False
        e._snapshot_radio_progress_locked = lambda: False
        e.set_volume = lambda v: setattr(e, 'volume', int(v)) or int(v)
        e.set_ambient = lambda *a, **k: {'ok': True}
        e.set_sleep = lambda *a, **k: {'ok': True, 'message': '30分钟后停'}

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(self.qq, 'SLEEP_STATE_PATH', pathlib.Path(td) / 'sleep.json'), \
                 mock.patch.object(self.qq, '_sunrise_mark_stopped') as m_sun:
                res = e.sleep_session_start(sound='rain', minutes=30, volume=22)
                self.assertTrue(res['ok'])
                self.assertEqual(e._sleep_state['restore']['day_volume'], 68)
                self.assertIsNone(e._wake)
                self.assertTrue(m_sun.called)

                ended = []
                orig_end = self.qq.QQMusicEngine._sleep_session_end
                e._sleep_session_end = lambda why: (ended.append(why), orig_end(e, why))

                e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'rain'}}
                e.control('pause')
                self.assertIsNone(e.sleep_session)
                self.assertEqual(ended[-1], 'stop')

                e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'rain'}}
                e.control('jump', index=1)
                self.assertIsNone(e.sleep_session)
                self.assertEqual(ended[-1], 'replaced')

                e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'rain'}}
                e.control('next')
                self.assertIsNone(e.sleep_session)
                self.assertEqual(ended[-1], 'replaced')

                e.sleep_session = {'sound': {'kind': 'ambient', 'preset': 'rain'}}
                e.seek(15.0)
                self.assertIsNone(e.sleep_session)
                self.assertEqual(ended[-1], 'replaced')

    def test_open_fav_radio_shuffle_cache_and_airplay_video_end(self):
        e = object.__new__(self.qq.QQMusicEngine)
        e._lock = threading.Lock()
        e.playing = False
        e.paused = True
        e.ducked = False
        e.open_ui_seq = 0
        res = e.control('open_fav')
        self.assertTrue(res['ok'])
        self.assertEqual(res['tab'], 'fav')

        with mock.patch.object(self.qq, '_RADIO_CH_CACHE', {'ch1': (time.monotonic(), [{'mid': f'm{i}'} for i in range(10)])}), \
             mock.patch.object(self.qq, 'qq_album_tracks', side_effect=AssertionError('must hit cache')):
            got = self.qq.resolve_radio_channel_tracks({'id': 'ch1', 'title': 't', 'badge': 'b', 'cover': 'c'}, shuffle=True)
        self.assertEqual(len(got), 10)

        self.assertIn('"End streaming to GStreamer video pipeline" in line', self.ap_src)
        self.assertIn('if MIRROR_FLAG_PATH.exists() or MIRROR_FLAG_PATH.is_symlink():\n        set_screen_hold(False)', self.ap_src)


class TestTutorAndAdminR85(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tut_path = _p('/usr/local/bin/thinksmart-tutor.py')
        cls.tut = _load_py_clean('r85_tutor', cls.tut_path)
        cls.adm_path = _p('/usr/local/bin/thinksmart-admin.py')
        cls.adm = _load_py_clean('r85_admin', cls.adm_path)

    def test_tutor_check_seq_hydration_frame_meta_and_frontend_guards(self):
        with tempfile.TemporaryDirectory() as td:
            sd = pathlib.Path(td)
            (sd / 'phone_stage_p1.jpg').write_bytes(b'\xff\xd8' + b'x' * 256)
            prefs = sd / 'prefs.json'
            prefs.write_text(json.dumps({
                'phone_stage': {
                    'seq': 3,
                    'active_id': 'p1',
                    'pages': [{'id': 'p1', 'seq': 3, 'check_seq': 7, 'check_summary': '全对'}],
                }
            }), encoding='utf-8')
            with mock.patch.object(self.tut, 'STATE_DIR', sd), \
                 mock.patch.object(self.tut, 'PREFS_PATH', prefs):
                pm = self.tut.PhoneStudioManager()
                self.assertEqual(pm.pages[0]['check_seq'], 7)

            with mock.patch.object(self.tut, 'STATE_DIR', sd), \
                 mock.patch.object(self.tut, 'PREFS_PATH', sd / 'cam_prefs.json'):
                cam = self.tut.UVCCameraManager()
                cam._last_frame = b'raw-preview'
                cam._last_frame_ts = time.monotonic()
                cam._last_frame_meta = (0, 1.0, False, False)
                with mock.patch.object(cam, 'discover', return_value={'connected': True, 'dev': '/dev/video99', 'name': 'fake'}), \
                     mock.patch.object(cam, 'probe_resolutions', return_value={'hi_res': '1600x1200', 'preview_res': '640x480', 'input_fmt': 'yuyv'}):
                    hit = asyncio.run(cam.capture_jpeg(high_res=False, crop_box=None, pencil_boost=False))
                    self.assertEqual(hit, b'raw-preview')
                    with mock.patch.object(self.tut.asyncio, 'create_subprocess_exec', side_effect=RuntimeError('miss_on_pencil')):
                        with self.assertRaisesRegex(RuntimeError, 'miss_on_pencil'):
                            asyncio.run(cam.capture_jpeg(high_res=False, crop_box=None, pencil_boost=True))

        tut_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('let lastCoreIdx = -1;', tut_html)
        self.assertIn('function isDdSrc()', tut_html)
        self.assertIn('if (mySeq !== ddSeq) return;', tut_html)

    def test_admin_symlink_guards_and_import_need_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            real_target = tdp / 'secret.json'
            real_target.write_text('{}', encoding='utf-8')
            link = tdp / 'auth.json'
            link.symlink_to(real_target)
            with self.assertRaises(OSError):
                self.adm.write_private_json(link, {'x': 1})

            usb_base = tdp / 'usb'
            usb_base.mkdir()
            (tdp / 'escape').mkdir()
            (usb_base / self.adm.INCOMING_DIR).symlink_to(tdp / 'escape')
            with self.assertRaises(self.adm.ApiError):
                self.adm._incoming(usb_base, '.upload-1.part')

            job = self.adm.ImportJob([
                (tdp / 'a.mp3', 'a.mp3', 1000),
                (tdp / 'b.mp3', 'b.mp3', 2000),
            ], threading.Lock(), need_bytes=2000)
            self.assertEqual(job.total_bytes, 3000)
            self.assertEqual(job.inflight_remaining, 2000)

        adm_html = _p('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertIn('cfgUi.studyPvPending', adm_html)
        pw_sh = _p('/usr/local/sbin/thinksmart-set-admin-password').read_text('utf-8')
        self.assertIn("PW=${PW%$'\\r'}", pw_sh)
        sk_py = _p('/usr/local/sbin/thinksmart-set-search-keys').read_text('utf-8')
        self.assertIn('bdfd, bleaf = _open_parent_dir_nofollow(backup)', sk_py)


class TestHomeMonitorDaemonsR85(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = _load_py_clean('r85_home', _p('/usr/local/bin/thinksmart-home.py'))
        cls.mon = _load_py_clean('r85_mon', _p('/usr/local/bin/thinksmart-monitor.py'))
        cls.ib = _load_script('r85_ib', _p('/usr/local/bin/idle-blanker.py'))
        cls.px = _load_py_clean('r85_px', _p('/usr/local/bin/vcnl4200-proximity-daemon.py'))
        cls.sw = _load_py_clean('r85_sw', _p('/usr/local/bin/screen-wake-daemon.py'))

    def test_idle_blanker_rescan_screen_wake_fifo_and_vcnl_por(self):
        with tempfile.TemporaryDirectory() as td:
            fifo = pathlib.Path(td) / 'pipe'
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                self.sw._read_small_text(fifo)

        ps = self.px.ProximitySensor()
        ps.bus = None
        ps.bus_failed = True
        ps._bus_retry_at = time.monotonic() + 30.0
        ps.als_enabled = True
        ps.sensor_enabled = True
        ps._last_conf_check = time.monotonic()
        ps._als_zero_streak = 0
        with mock.patch.object(ps, 'i2c_get', side_effect=[0] * 15 + [0x0001]):
            for _ in range(14):
                ps.last_als = None
                ps.poll_als(0.0)
            self.assertTrue(ps.als_enabled)
            ps.last_als = None
            ps.poll_als(0.0)
            self.assertFalse(ps.als_enabled)
            self.assertFalse(ps.sensor_enabled)

        self.assertTrue(hasattr(self.ib, 'rescan_input_devices'))
        with mock.patch.object(self.ib.glob, 'glob', return_value=['/dev/null']):
            added = self.ib.rescan_input_devices([])
            try:
                self.assertEqual(len(added), 1)
                self.assertEqual(self.ib.rescan_input_devices(added), [])
            finally:
                for fd in added:
                    os.close(fd)

    def test_monitor_latency_fail_backoff_and_frontend_guards(self):
        with tempfile.TemporaryDirectory() as td:
            ad = pathlib.Path(td)
            (ad / 'servers.json').write_text('[{"id": 1, "name": "s1"}]', encoding='utf-8')
            (ad / 'traffic.json').write_text('{}', encoding='utf-8')
            m = self.mon.Monitor(assets=ad, state_dir=ad)
            m.servers = [{'id': 1, 'alive': False}]
            calls = []

            async def fake_get(*_a):
                calls.append(1)
                raise OSError('down')

            with mock.patch.object(m, '_http_get_json', side_effect=fake_get), \
                 mock.patch.object(m, '_can_fetch_server_service', return_value=True):
                asyncio.run(m.fetch_server_latency(1))
                asyncio.run(m.fetch_server_latency(1))
            self.assertEqual(len(calls), 1)

        hm_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('const want = parentVisible && tpOpen()', hm_html)
        ho_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('function scheduleSubEvict(entry)', ho_js)
        mon_html = _p('/usr/local/share/thinksmart-monitor/index.html').read_text('utf-8')
        self.assertIn('if(graceShown&&monoNow()>=wakeGraceUntil&&lastData)render(lastData);', mon_html)


class TestIntercomStudyUsbSystemdR85(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ic_path = _p('/usr/local/bin/thinksmart-intercom.py')
        cls.ic = _load_py_clean('r85_ic', cls.ic_path)
        cls.st = _load_py_clean('r85_st', _p('/usr/local/bin/thinksmart-study.py'))
        cls.wd = _load_py_clean('r85_wd', _p('/usr/local/sbin/thinksmart-usb-watchdog'))

    def test_intercom_video_demand_study_hold_and_watchdog_pending(self):
        srv = object.__new__(self.ic.IntercomServer)
        srv.in_call = False
        ws1 = object()
        srv.clients = {ws1}
        srv.video_paused = {ws1}
        self.assertFalse(srv._has_video_demand())
        srv.in_call = True
        self.assertTrue(srv._has_video_demand())

        ic_txt = self.ic_path.read_text('utf-8')
        self.assertLess(ic_txt.index('var embedActive = true;'), ic_txt.index('syncClockTimer();'))
        self.assertIn('while proc and proc.stdout:', ic_txt)
        self.assertIn('r.close()', ic_txt)

        c = self.st.Companion.__new__(self.st.Companion)
        c.focus = {'phase': 'focus', 'with_study': True, 'ends_at': time.time() - 5.0}
        self.assertFalse(c._focus_holding_study())
        c.focus = {'phase': 'focus', 'with_study': True, 'unsynced': True, 'ends_mono': time.monotonic() + 30.0}
        self.assertTrue(c._focus_holding_study())

        with tempfile.TemporaryDirectory() as td:
            role = pathlib.Path(td) / 'role'
            role.write_text('host\n')
            usb1 = pathlib.Path(td) / 'usb1'
            usb1.mkdir()
            wd = self.wd.UsbWatchdog(role_path=str(role), usb1_path=str(usb1), cooldown_sec=12.0, settle_sec=0.0, role_gap_sec=0.0)
            wd.last_recover_mono = time.monotonic()
            self.assertFalse(wd.recover('HC died'))
            self.assertTrue(wd.needs_roothub_recovery())

        tas = _p('/home/ubuntu/.config/systemd/user/tas5782m-preinit.service').read_text('utf-8')
        self.assertIn('Wants=pipewire-pulse.service', tas)
        self.assertIn('--device="$SINK"', tas)
        self.assertIn('TimeoutStartSec=15', _p('/etc/systemd/system/thinksmart-usb-host.service').read_text('utf-8'))
        self.assertIn('MemoryMax=64M', _p('/etc/systemd/system/thinksmart-intercom-tunnel.service').read_text('utf-8'))
        self.assertIn('-B /usr/local/bin/idle-blanker.py', _p('/etc/systemd/user/idle-blanker.service').read_text('utf-8'))
        self.assertIn('MemoryHigh=160M', _p('/etc/systemd/system/thinksmart-admin.service').read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
