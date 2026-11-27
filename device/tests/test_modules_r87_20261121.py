#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R87: Round 8 Deep Non-Xiaozhi Audit & Regression Tests."""
import ast
import asyncio
import concurrent.futures
import contextlib
import importlib.machinery
import importlib.util
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
from collections import OrderedDict
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

E = os.environ.get
ROOT = pathlib.Path(E('R87_ROOT') or E('STAGE_ROOT') or '/')


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


class TestSystemdAndDaemonsR87(unittest.TestCase):
    def test_systemd_hardening_and_watchdog_timeout(self):
        tutor_svc = _p('/etc/systemd/system/thinksmart-tutor.service').read_text('utf-8')
        for directive in (
            'ProtectKernelLogs=yes',
            'ProtectClock=yes',
            'ProtectHostname=yes',
            'RestrictRealtime=yes',
            'RestrictSUIDSGID=yes',
            'LockPersonality=yes',
        ):
            self.assertIn(directive, tutor_svc)

        wd_svc = _p('/etc/systemd/system/thinksmart-usb-watchdog.service').read_text('utf-8')
        self.assertIn('TimeoutStopSec=25', wd_svc)
        self.assertNotIn('TimeoutStopSec=5', wd_svc)

    def test_screen_wake_symlink_guard_and_night_low_intact(self):
        sw_path = _p('/usr/local/bin/screen-wake-daemon.py')
        sw_src = sw_path.read_text('utf-8')
        self.assertIn('def should_wake_night_low', sw_src)
        mod = _load_py_clean('r87_screen_wake', sw_path)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            real_target = td_p / 'victim.txt'
            real_target.write_text('keep', encoding='utf-8')
            sym_bl = td_p / 'brightness_link'
            sym_bl.symlink_to(real_target)
            with mock.patch.object(mod, 'BACKLIGHT_SYSFS', sym_bl), \
                 mock.patch.object(mod, 'log', lambda *_a, **_k: None), \
                 mock.patch.object(mod.subprocess, 'run') as m_run:
                ok = mod.write_brightness(120)
                self.assertFalse(ok)
                self.assertEqual(real_target.read_text('utf-8'), 'keep')
                m_run.assert_not_called()

    def test_usb_watchdog_recovering_marker_lifecycle(self):
        wd_path = _p('/usr/local/sbin/thinksmart-usb-watchdog')
        mod = _load_script('r87_usb_watchdog', wd_path)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            role_p = td_p / 'role'
            usb1_p = td_p / 'usb1'
            lock_p = td_p / 'usb.lock'
            role_p.write_text('host\n', encoding='utf-8')

            # Persisted .recovering marker across restart keeps recovery armed
            rec_marker = pathlib.Path(f'{lock_p}.recovering')
            rec_marker.write_text('1', encoding='utf-8')
            wd = mod.UsbWatchdog(
                role_path=str(role_p), usb1_path=str(usb1_p), usb_mode_bin='/bin/true',
                mnt_dir=str(td_p / 'mnt'), lock_path=str(lock_p), cooldown_sec=0.0, settle_sec=0.0, role_gap_sec=0.0,
            )
            self.assertTrue(wd._host_restore_pending)
            self.assertTrue(wd.needs_roothub_recovery())

            seen_marker_before_device = []
            rec_marker.unlink(missing_ok=True)
            wd2 = mod.UsbWatchdog(
                role_path=str(role_p), usb1_path=str(usb1_p), usb_mode_bin='/bin/true',
                mnt_dir=str(td_p / 'mnt'), lock_path=str(lock_p), cooldown_sec=0.0, settle_sec=0.0, role_gap_sec=0.0,
            )
            self.assertFalse(wd2._host_restore_pending)

            real_calls = []

            def _recover_run(cmd, *_a, **_k):
                real_calls.append(list(cmd))
                if 'device' in cmd:
                    seen_marker_before_device.append(rec_marker.exists())
                elif 'host' in cmd:
                    usb1_p.write_text('ok', encoding='utf-8')
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            with mock.patch.object(mod.subprocess, 'run', side_effect=_recover_run):
                ok = wd2.recover('test_usb1_missing')
                self.assertTrue(ok)
                self.assertEqual(seen_marker_before_device, [True])
                self.assertFalse(rec_marker.exists())
                self.assertFalse(wd2._host_restore_pending)


class TestIntercomUIAndAirPlayR87(unittest.TestCase):
    def test_intercom_ui_timeout_notify_and_ftype4_decode_flag(self):
        ui_mod = _load_intercom_ui_mod('r87_intercom_ui', _p('/usr/local/bin/thinksmart-intercom-ui.py'))
        win = ui_mod.IntercomWindow.__new__(ui_mod.IntercomWindow)
        win.start_ts = time.monotonic() - 10.0
        win.last_elapsed = -1
        win.timer_lbl = mock.MagicMock()
        win.decode_cond = threading.Condition()
        win._closing = False
        win.f1_fresh = False
        win.f2_fresh = False
        win.f1_seq = 0
        win.f2_seq = 0
        win.last_remote_ts = time.monotonic() - 5.0
        win.last_local_ts = time.monotonic() - 4.0
        win.last_f1 = b'old1'
        win.latest_f1 = b'old1'
        win.ready_f1 = b'old1'
        win.last_f2 = b'old2'
        win.latest_f2 = b'old2'
        win.ready_f2 = b'old2'
        win._worker_pb1 = object()
        win._worker_pb2 = object()
        win.remote_off_pending = False
        win.flush_scheduled = False
        win.remote_img = mock.MagicMock()
        win.avatar_box = mock.MagicMock()
        win.local_img = mock.MagicMock()
        win.pip_frame = mock.MagicMock()

        with mock.patch.object(win.decode_cond, 'notify_all', wraps=win.decode_cond.notify_all) as m_notify:
            ret = win.on_tick()
            self.assertTrue(ret)
            self.assertIsNone(win.last_remote_ts)
            self.assertIsNone(win.last_local_ts)
            self.assertIsNone(win.ready_f1)
            self.assertIsNone(win.ready_f2)
            self.assertGreaterEqual(m_notify.call_count, 1)

        with mock.patch.object(ui_mod.GLib, 'idle_add') as m_idle:
            win.flush_scheduled = False
            with mock.patch.object(win.decode_cond, 'notify_all', wraps=win.decode_cond.notify_all) as m_notify2:
                win.enqueue_frame(4, b'')
                self.assertTrue(win.remote_off_pending)
                self.assertGreaterEqual(m_notify2.call_count, 1)
                self.assertTrue(m_idle.called)

        # Verify worker loop clears stale _worker_pb1/_worker_pb2 when closing on next wait
        ui_src = _p('/usr/local/bin/thinksmart-intercom-ui.py').read_text('utf-8')
        self.assertIn('clear_pb1 = (f1 is None and self.last_remote_ts is None and not self.f1_fresh)', ui_src)
        self.assertIn('clear_pb2 = (f2 is None and self.last_local_ts is None and not self.f2_fresh)', ui_src)

    def test_airplay_backlight_symlink_and_watchdog_on_stop(self):
        ap_path = _p('/usr/local/bin/thinksmart-airplay.py')
        ap_src = ap_path.read_text('utf-8')
        self.assertIn('mark_expected_restart', ap_src)
        self.assertIn('was_audio = audio_active', ap_src)
        ap = _load_py_clean('r87_airplay', ap_path)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            hint_p = td_p / 'hint.json'
            victim = td_p / 'victim'
            victim.write_text('keep', encoding='utf-8')
            sym_bl = td_p / 'bl_link'
            sym_bl.symlink_to(victim)
            with mock.patch.object(ap, 'HINT_PATH', hint_p), \
                 mock.patch.object(ap, 'BACKLIGHT_PATH', sym_bl), \
                 mock.patch.object(ap, 'RUNTIME_DIR', td_p), \
                 mock.patch.object(ap, 'INTERCOM_FLAG_PATH', td_p / 'ic'), \
                 mock.patch.object(ap, 'MIRROR_FLAG_PATH', td_p / 'mir'), \
                 mock.patch.object(ap, 'log', lambda *_a, **_k: None), \
                 mock.patch.object(ap.subprocess, 'run') as m_run:
                ap.set_screen_hold(True)
                self.assertEqual(victim.read_text('utf-8'), 'keep')
                m_run.assert_not_called()

        # Verify start_wayland_watchdog invokes on_stop before stop_proc on intercom yield
        stop_ev = threading.Event()
        called_on_stop = threading.Event()
        stopped_procs = []
        proc = mock.MagicMock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            (td_p / 'ic').write_text('1', encoding='utf-8')
            (td_p / 'mir').write_text('1', encoding='utf-8')
            with mock.patch.object(ap, 'INTERCOM_FLAG_PATH', td_p / 'ic'), \
                 mock.patch.object(ap, 'MIRROR_FLAG_PATH', td_p / 'mir'), \
                 mock.patch.object(ap, 'RUNTIME_DIR', td_p), \
                 mock.patch.object(ap, 'stop_proc', side_effect=lambda p: (stopped_procs.append(p), stop_ev.set())), \
                 mock.patch.object(ap, 'log', lambda *_a, **_k: None):
                ap.start_wayland_watchdog(
                    lambda: (proc, 123),
                    interval=0.05,
                    stop_event=stop_ev,
                    on_stop=called_on_stop.set,
                )
                self.assertTrue(called_on_stop.wait(2.0))
                self.assertEqual(stopped_procs, [proc])


class TestQQMusicR87(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq_path = _p('/usr/local/bin/thinksmart-qqmusic.py')
        cls.qq = _load_py_clean('r87_qqmusic', cls.qq_path)

    def test_sleep_tick_and_alarm_preserve_sg_last_and_abort_preview_defers(self):
        eng = object.__new__(self.qq.QQMusicEngine)
        eng._lock = threading.RLock()
        eng.sleep_mode = 'time'
        eng.sleep_deadline = time.monotonic() - 1.0
        eng.playing = True
        eng.paused = False
        eng.ambient_preset = None
        eng.sleep_session = None
        eng._sg_last = (1.0, time.monotonic())
        eng._clear_sleep_locked = lambda: setattr(eng, 'sleep_mode', None)
        seen_sg_before_pause = []
        eng.control = lambda act: seen_sg_before_pause.append((act, eng._sg_last[0]))
        eng._preview_end = mock.MagicMock()
        eng._sleep_session_end = mock.MagicMock()
        self.assertTrue(eng.sleep_tick())
        self.assertEqual(seen_sg_before_pause, [('pause', 0.0)])
        self.assertIsInstance(eng._sg_last, tuple)
        self.assertEqual(eng._sg_last[0], 1.0)

        # sleep_session_stop invokes _wake_stop_soon when _wake exists
        eng2 = object.__new__(self.qq.QQMusicEngine)
        eng2._lock = threading.RLock()
        eng2._wake = {'id': 1}
        eng2.sleep_session = None
        eng2._wake_stop_soon = mock.MagicMock()
        res_stop = eng2.sleep_session_stop()
        self.assertTrue(res_stop.get('ok'))
        eng2._wake_stop_soon.assert_called_once_with('stop')

        # set_ambient('off') respects _in_preview_op=True without calling _abort_preview or clearing sleep_session
        eng3 = object.__new__(self.qq.QQMusicEngine)
        eng3._lock = threading.RLock()
        eng3._in_preview_op = True
        eng3.ambient_preset = 'rain'
        eng3.sleep_session = {'id': 's1'}
        eng3._abort_preview = mock.MagicMock()
        eng3._clear_duck_except_home_locked = mock.MagicMock()
        eng3._stop_proc_locked = mock.MagicMock(return_value=False)
        eng3.status = mock.MagicMock(return_value={'ok': True})
        eng3.set_ambient('off')
        eng3._abort_preview.assert_not_called()
        self.assertIsNotNone(eng3.sleep_session)

        # _abort_preview writes deferred restore instead of changing volume while ext_busy (MIRROR_FLAG_PATH) exists
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            mir = td_p / 'uxplay-mirroring-active'
            mir.write_text('1', encoding='utf-8')
            eng4 = object.__new__(self.qq.QQMusicEngine)
            eng4._lock = threading.RLock()
            eng4._preview = {'volume': 20, 'prev_volume': 55}
            eng4.volume = 20
            eng4.sleep_mode = None
            eng4.sleep_session = None
            eng4.alarm_ringing = False
            eng4.bt_streaming = False
            eng4._sleep_state = {'id': 's1'}
            eng4._snap_sleep_state_locked = lambda: dict(eng4._sleep_state)
            eng4.set_volume = mock.MagicMock()
            wrote = []
            with mock.patch.object(self.qq, 'CALL_FLAG_PATH', td_p / 'no-call'), \
                 mock.patch.object(self.qq, 'MIRROR_FLAG_PATH', mir), \
                 mock.patch.object(self.qq, '_write_sleep_state', side_effect=wrote.append):
                eng4._abort_preview()
            eng4.set_volume.assert_not_called()
            self.assertEqual(len(wrote), 1)
            self.assertEqual(wrote[0]['restore']['day_volume'], 55)

    def test_hires_http_error_and_set_quality_and_status_sig_cache(self):
        # HTTPError(403) closes response and resets _BILI_CJ
        self.qq._BILI_CJ = object()
        he = urllib.error.HTTPError('https://api.bilibili.com/test', 403, 'Forbidden', {}, None)
        he.close = mock.MagicMock()
        fake_opener = mock.MagicMock()
        fake_opener.open.side_effect = he
        with mock.patch.object(self.qq, '_has_only_global_ips', return_value=True), \
             mock.patch.object(self.qq.urllib.request, 'build_opener', return_value=fake_opener):
            self.assertIsNone(self.qq.resolve_hires_fallback('test song'))
        he.close.assert_called_once()
        self.assertIsNone(self.qq._BILI_CJ)

        # make_app status non-blocking sig_cache & set_quality non-mutating copy + paused decoder flush
        qq_src = self.qq_path.read_text('utf-8')
        self.assertIn("async with sess.post(", qq_src)
        self.assertIn("await _fr.read()", qq_src)

        mock_eng = mock.MagicMock()
        mock_eng._lock = threading.RLock()
        mock_eng.playing = True
        mock_eng.paused = True
        mock_eng.ambient_preset = None
        mock_eng.index = 0
        old_t0 = {'name': 'Song A', 'url': 'https://cdn.example/a.flac', '_url_ts': 123, 'quality': 'SQ'}
        old_t1 = {'name': 'Song B', 'url': 'https://cdn.example/b.flac', '_url_ts': 456, 'quality': 'SQ'}
        mock_eng.queue = [old_t0, old_t1]
        mock_eng.queue_seq = 1
        mock_eng._stop_proc_locked.return_value = True
        mock_eng.status.return_value = {'ok': True}

        async def _run():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool, \
                 mock.patch.object(self.qq, 'engine', mock_eng), \
                 mock.patch.object(self.qq, 'local_lib_sig', return_value='l1'), \
                 mock.patch.object(self.qq, 'usb_mount_sig', return_value='u1'), \
                 mock.patch.object(self.qq, 'load_config', return_value={'quality_level': 'lossless'}), \
                 mock.patch.object(self.qq, 'save_config'):
                app = self.qq.make_app(ctrl_pool=pool)
                srv = TestServer(app)
                cli = TestClient(srv)
                await cli.start_server()
                try:
                    h = {'Host': f'127.0.0.1:{srv.port}'}
                    r_st = await cli.get('/api/music/status', headers=h)
                    self.assertEqual(r_st.status, 200)
                    r_sq = await cli.post('/api/music/quality', json={'quality_level': 'standard'}, headers=h)
                    self.assertEqual(r_sq.status, 200)
                finally:
                    await cli.close()

        asyncio.run(_run())
        self.assertEqual(old_t0['url'], 'https://cdn.example/a.flac')
        self.assertEqual(old_t1['url'], 'https://cdn.example/b.flac')
        self.assertIsNot(mock_eng.queue[1], old_t1)
        self.assertIsNone(mock_eng.queue[1]['url'])
        mock_eng._stop_proc_locked.assert_called_once()
        mock_eng._send_aec_flush.assert_called_once()


class TestHomeAndMonitorR87(unittest.TestCase):
    def test_home_post_ack_preplay_and_rollback(self):
        hm_path = _p('/usr/local/bin/thinksmart-home.py')
        hm_src = hm_path.read_text('utf-8')
        # Verify play_op_gen[0] += 1 guards in sleepmode_api, post_focus, timer_api
        self.assertGreaterEqual(hm_src.count('play_op_gen[0] += 1'), 6)
        hm = _load_py_clean('r87_home', hm_path)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            inbox = hm.Inbox(td_p / 'inbox.json')
            vdir = td_p / 'voice'
            vdir.mkdir(exist_ok=True)
            (vdir / 'a1b2c3d4e5f6.mp3').write_bytes(b'ID3fake')
            inbox.voice_dir = vdir
            item_fail = {
                'id': 'a1b2c3d4e5f6', 'text': 'voice note', 'at': time.time(),
                'read_at': None, 'source': 'phone', 'kind': 'voice', 'duration': 5.0,
            }
            inbox.items.append(dict(item_fail))
            cd = hm.CountdownStore(td_p / 'cd.json')

            async def _run():
                with mock.patch.object(hm, '_media_busy', return_value=False), \
                     mock.patch.object(hm, '_post_json', new=mock.AsyncMock(return_value=(200, {'ok': True}))) as m_post:
                    app = hm.make_app(inbox=inbox, countdowns=cd)
                    srv = TestServer(app)
                    cli = TestClient(srv)
                    await cli.start_server()
                    try:
                        h = {'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'}
                        with mock.patch.object(hm.SpeakerClipPlayer, 'play_mp3_file', return_value=False):
                            r1 = await cli.post('/api/home/inbox/ack', json={'id': 'a1b2c3d4e5f6'}, headers=h)
                            self.assertEqual(r1.status, 409)
                            self.assertIsNone(inbox.items[0]['read_at'])
                            self.assertTrue(any(c.args[2].get('action') == 'unduck' for c in m_post.await_args_list))

                        # Now playback succeeds, but inbox.ack() raises OSError -> 503 + stop()
                        with mock.patch.object(hm.SpeakerClipPlayer, 'play_mp3_file', return_value=True), \
                             mock.patch.object(hm.SpeakerClipPlayer, 'stop') as m_stop, \
                             mock.patch.object(inbox, 'ack', side_effect=OSError('disk full')):
                            r2 = await cli.post('/api/home/inbox/ack', json={'id': 'a1b2c3d4e5f6'}, headers=h)
                            self.assertEqual(r2.status, 503)
                            self.assertGreaterEqual(m_stop.call_count, 2)
                    finally:
                        await cli.close()

            asyncio.run(_run())

    def test_monitor_host_gen_and_origin_sec_fetch_hardening(self):
        mon_path = _p('/usr/local/bin/thinksmart-monitor.py')
        mon = _load_py_clean('r87_monitor', mon_path)
        mon_src = mon_path.read_text('utf-8')
        self.assertIn("getattr(self, '_host_gen', 0) != my_host_gen", mon_src)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ov_p = td_p / 'config.json'
            ov_p.write_text(json.dumps({'nezha_host': 'https://a.example.com'}), encoding='utf-8')
            m = object.__new__(mon.Monitor)
            m.assets = td_p
            m.compute_badge = lambda: {'ok': True}
            m._host_gen = 0
            m._upstream_gen = 0
            m._override_checked = -999.0
            m._override_sig = None
            m._override_cfg = {}
            m._eff_override_tuple = None
            m._override_cfg_path = ov_p
            m.servers = [{'id': 1}]
            m.carriers = [{'name': 'c'}]
            m._alive_state = {}
            m._srv_latency_cache = {}
            m.speed_hist = []
            m._seq = 0
            m._traffic_failures = 3
            m._carriers_updated_mono = 10.0

            m._runtime_override()
            self.assertEqual(m._host_gen, 0)
            ov_p.write_text(json.dumps({'nezha_host': 'https://b.example.com'}), encoding='utf-8')
            m._override_checked = -999.0
            m._override_sig = None
            m._runtime_override()
            self.assertEqual(m._host_gen, 1)
            self.assertEqual(m.servers, [])
            self.assertEqual(m.carriers, [])

            # Test host_guard blocks foreign-port Origin even when Sec-Fetch-Site=same-site,
            # and voice_summary blocks Origin or Sec-Fetch-Mode
            m.connected = True
            m.touch = lambda lease=None: None
            m._wake_and_snapshot = mock.AsyncMock(return_value={'connected': True, 'servers': [], 'isp_map': {}})
            m.snapshot = lambda: {'connected': True, 'servers': [], 'isp_map': {}}
            m._can_fetch_server_service = lambda: False

            async def _run():
                app = mon.Monitor.app(m)
                app.cleanup_ctx.clear()
                srv = TestServer(app)
                cli = TestClient(srv)
                await cli.start_server()
                try:
                    base_h = {'Host': f'127.0.0.1:{srv.port}'}
                    # Blocked: Sec-Fetch-Site=same-site with foreign Origin
                    r_bad = await cli.get('/api/badge', headers={
                        **base_h, 'Origin': 'https://evil.lan:9999', 'Sec-Fetch-Site': 'same-site',
                    })
                    self.assertEqual(r_bad.status, 403)
                    # Allowed: matching Origin
                    r_ok = await cli.get('/api/badge', headers={
                        **base_h, 'Origin': f'http://127.0.0.1:{srv.port}', 'Sec-Fetch-Site': 'same-origin',
                    })
                    self.assertEqual(r_ok.status, 200)
                    # Blocked: voice_summary with Origin or Sec-Fetch-Mode
                    r_vs1 = await cli.get('/api/voice_summary', headers={**base_h, 'Origin': f'http://127.0.0.1:{srv.port}'})
                    self.assertEqual(r_vs1.status, 403)
                    r_vs2 = await cli.get('/api/voice_summary', headers={**base_h, 'Sec-Fetch-Mode': 'cors'})
                    self.assertEqual(r_vs2.status, 403)
                    # Allowed: internal CLI without Origin/Sec-Fetch-Mode
                    r_vs_ok = await cli.get('/api/voice_summary', headers=base_h)
                    self.assertEqual(r_vs_ok.status, 200)
                finally:
                    await cli.close()

            asyncio.run(_run())


class TestTutorAndStudyR87(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tut_path = _p('/usr/local/bin/thinksmart-tutor.py')
        cls.tut = _load_py_clean('r87_tutor', cls.tut_path)
        cls.st_path = _p('/usr/local/bin/thinksmart-study.py')
        cls.st = _load_py_clean('r87_study', cls.st_path)

    def test_tutor_lru_coalescing_read_bytes_and_api_hardening(self):
        # _trim_sessions skips active/streaming sessions and evicts oldest idle
        eng = object.__new__(self.tut.TutorEngine)
        eng.active_session_id = 's0'
        eng.sessions = OrderedDict([
            ('s0', {'status': 'ready'}),
            ('s1', {'status': 'streaming'}),
            ('s2', {'status': 'ready'}),
            ('s3', {'status': 'ready'}),
        ])
        eng._trim_sessions(max_len=3)
        self.assertNotIn('s2', eng.sessions)
        self.assertIn('s0', eng.sessions)
        self.assertIn('s1', eng.sessions)
        self.assertIn('s3', eng.sessions)

        # _read_regular_bytes refuses symlink and oversized file
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            real_f = td_p / 'real.jpg'
            real_f.write_bytes(b'abc')
            sym_f = td_p / 'link.jpg'
            sym_f.symlink_to(real_f)
            with self.assertRaises(OSError):
                self.tut._read_regular_bytes(sym_f, 1024)
            big_f = td_p / 'big.bin'
            big_f.write_bytes(b'x' * 32)
            with self.assertRaises(OSError):
                self.tut._read_regular_bytes(big_f, 16)
            sig, data = self.tut._read_regular_bytes(real_f, 1024)
            self.assertEqual(data, b'abc')

        # Static checks for page_check_inflight, set_check_marks check_error='', mob_check check_error capture
        tut_src = self.tut_path.read_text('utf-8')
        self.assertIn('_page_check_inflight', tut_src)
        self.assertIn("pg['check_error'] = ''", tut_src)
        self.assertIn("'check_error': pg.get('check_error') or ''", tut_src)

        # HTTP _json_dict + api_state(ui_ver) via make_app
        mock_eng = mock.MagicMock()
        mock_eng.poll_camera_hotplug.return_value = {'connected': False}
        mock_eng.phone_studio.public_state.return_value = {'seq': 1, 'pages': []}
        mock_eng.recorder.is_recording = False
        mock_eng.history = []
        mock_eng.public_mistakes.return_value = []
        mock_eng.public_mistake_summary.return_value = {}
        mock_eng.get_fresh_parent_nudge.return_value = None
        mock_eng.active_session_id = None
        mock_eng.open_ui_seq = 0
        mock_eng.open_ui_target = ''
        mock_eng.open_ui_pencil = False
        mock_eng.is_open_ui_fresh.return_value = False
        mock_eng.board_direction = 'landscape'
        mock_eng.camera.stop_stream = mock.AsyncMock()
        mock_eng.recorder.cancel = mock.AsyncMock()
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
                        r_st = await cli.get('/api/tutor/state', headers=h)
                        self.assertEqual(r_st.status, 200)
                        js = await r_st.json()
                        self.assertTrue(bool(js.get('ui_ver')))
                        # List JSON rejected with 400 by _json_dict
                        r_bad = await cli.post('/api/tutor/start', json=[1, 2], headers=h)
                        self.assertEqual(r_bad.status, 400)
                    finally:
                        await cli.close()

        asyncio.run(_run())

    def test_study_monotonic_focus_sync_clock_edge_and_alarm_guard(self):
        c = self.st.Companion.__new__(self.st.Companion)
        now_m = time.monotonic()
        # Unsynced wall clock jumped backward (-1000s), monotonic ends_mono still +300s -> holding remains True
        c.focus = {
            'phase': 'focus', 'minutes': 25, 'break_minutes': 5, 'with_study': True,
            'started_at': time.time() - 5000, 'ends_at': time.time() - 3500,
            'started_mono': now_m - 60.0, 'ends_mono': now_m + 300.0,
        }
        self.assertTrue(c._focus_holding_study())
        with mock.patch.object(self.st, '_boot_clock_unsynced', return_value=False):
            pub = c.focus_public()
            self.assertGreater(pub['remaining'], 250)
            self.assertGreater(c.focus['ends_at'], time.time())

        # _synthesize_builtin_edge uses '--text=' flag-safe form & play() checks alarm_ringing
        st_src = self.st_path.read_text('utf-8')
        self.assertIn("f'--text={text}'", st_src)
        self.assertIn('_sync_enabled_clock(cfg)', st_src)
        self.assertIn("before.get('alarm_ringing')", st_src)

        with tempfile.TemporaryDirectory() as td:
            c2 = self.st.Companion.__new__(self.st.Companion)
            c2.stopping = False
            c2.voice_lock = asyncio.Lock()
            c2._night_view_active = lambda: False
            c2.media_busy = lambda: False
            c2.client_idle = mock.AsyncMock(return_value=True)
            pcm = pathlib.Path(td) / 'v.pcm'
            pcm.write_bytes(b'\x00' * 4800)
            c2.phrase_file = lambda _t, _v=None: pcm
            c2.music = mock.AsyncMock(return_value={'alarm_ringing': True})
            self.assertFalse(asyncio.run(c2.play('休息了')))


class TestAdminAndIntercomR87(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adm_path = _p('/usr/local/bin/thinksmart-admin.py')
        cls.adm = _load_py_clean('r87_admin', cls.adm_path)
        cls.ic_path = _p('/usr/local/bin/thinksmart-intercom.py')
        cls.ic = _load_py_clean('r87_intercom', cls.ic_path)

    def test_admin_ssrf_proxy_private_write_and_outbound_checks(self):
        # _clash_local_proxy returns None for local/private targets even if probe cached True
        with mock.patch.object(self.adm, '_clash_probe_cache', [time.monotonic(), True]):
            self.assertIsNone(self.adm._clash_local_proxy('http://127.0.0.1:8080/api'))
            self.assertIsNone(self.adm._clash_local_proxy('http://192.168.1.10/api'))
            self.assertIsNone(self.adm._clash_local_proxy('http://home.lan:8123/'))
            self.assertEqual(self.adm._clash_local_proxy('https://api.openai.com/v1'), 'http://127.0.0.1:7890')

        # check_safe_url + _verify_outbound_url block loopback protected ports & link-local/private DNS rebinds
        with self.assertRaises(self.adm.ApiError):
            self.adm.check_safe_url('http://127.0.0.1:8097/api', 'LLM')
        with self.assertRaises(self.adm.ApiError):
            self.adm.check_safe_url('http://169.254.169.254/latest', 'LLM')
        with mock.patch.object(self.adm.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 8097))]):
            with self.assertRaises(self.adm.ApiError):
                asyncio.run(self.adm._verify_outbound_url('http://rebind.example.com:8097/test', 'LLM'))
        with mock.patch.object(self.adm.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.10', 8123))]):
            info = asyncio.run(self.adm._verify_outbound_url('http://hass.example.com:8123', 'HASS'))
            self.assertTrue(info['local'])

        # write_private_text + write_client_cfg_patch symlink refusal & perms
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            sec_f = td_p / 'secret'
            self.adm.write_private_text(sec_f, 'topsecret\n')
            self.assertEqual(sec_f.stat().st_mode & 0o777, 0o600)
            victim = td_p / 'victim'
            victim.write_text('keep', encoding='utf-8')
            sym_s = td_p / 'sym_secret'
            sym_s.symlink_to(victim)
            with self.assertRaises(self.adm.ApiError):
                self.adm.write_private_text(sym_s, 'evil')
            self.assertEqual(victim.read_text('utf-8'), 'keep')

            cfg_p = td_p / 'client.json'
            sym_cfg = td_p / 'sym_cfg.json'
            sym_cfg.symlink_to(victim)
            with mock.patch.object(self.adm, 'CLIENT_CFG', cfg_p):
                self.adm.write_client_cfg_patch({'a': 1})
                self.assertEqual(json.loads(cfg_p.read_text('utf-8')), {'a': 1})
                self.assertEqual(cfg_p.stat().st_mode & 0o777, 0o644)
            with mock.patch.object(self.adm, 'CLIENT_CFG', sym_cfg):
                with self.assertRaises(self.adm.ApiError):
                    self.adm.write_client_cfg_patch({'a': 2})

        # Ensure all active test_settings outbound branches call _verify_outbound_url (12 after removing HASS/RAGFlow/VLLM)
        adm_src = self.adm_path.read_text('utf-8')
        self.assertGreaterEqual(adm_src.count('await _verify_outbound_url('), 12)
        self.assertIn("if cand_p and nz_info.get('local'):", adm_src)
        self.assertIn('_RUAMEL_MUTATE_SCRIPT', adm_src)
        self.assertIn('read_limited', adm_src)

    def test_intercom_tunnel_spoof_prevention_and_relay_retry(self):
        self.assertTrue(self.ic._via_local_tunnel(None))
        self.assertTrue(self.ic._via_local_tunnel('127.0.0.1'))
        self.assertTrue(self.ic._via_local_tunnel('::1'))
        self.assertFalse(self.ic._via_local_tunnel('192.168.1.50'))

        # Legacy 1-arg default works; LAN peer cannot claim 18443 tunnel Origin or spoof XFH
        self.assertTrue(self.ic._msg_origin_ok({'host': '127.0.0.1:18091', 'origin': 'https://tun.example.com:18443', 'sec-fetch-site': 'same-origin'}))
        self.assertFalse(self.ic._msg_origin_ok(
            {'host': '127.0.0.1:18091', 'origin': 'https://tun.example.com:18443', 'sec-fetch-site': 'same-origin'},
            peer='192.168.1.50',
        ))
        self.assertFalse(self.ic._msg_origin_ok(
            {'host': '127.0.0.1:18091', 'x-forwarded-host': 'evil.lan:18091', 'origin': 'http://evil.lan:18091'},
            peer='192.168.1.50',
        ))

        # handle_messages 304 cache miss / epoch change retry without If-None-Match
        self.ic._MSG_RELAY_EPOCH = 5
        self.ic._MSG_RELAY_CACHE.update(up_etag='"old-up"', etag='"old-cli"', body=b'{"messages":[]}')

        class _FakeResp:
            def __init__(self, status, data=b'', etag=''):
                self.status = status
                self._data = data
                self.headers = {'ETag': etag} if etag else {}
                self.content = mock.MagicMock()
                self.content.read = mock.AsyncMock(return_value=data)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        calls = []

        def _fake_get(url, params=None, headers=None):
            calls.append(dict(headers or {}))
            if 'If-None-Match' in (headers or {}):
                # Simulate concurrent POST bumping epoch while in-flight
                self.ic._MSG_RELAY_EPOCH += 1
                self.ic._MSG_RELAY_CACHE.update(up_etag='', etag='', body=b'')
                return _FakeResp(304)
            return _FakeResp(200, json.dumps({'messages': [{'id': '0123456789ab', 'text': 'hi', 'at': 1.0}]}).encode('utf-8'), etag='"new-up"')

        fake_sess = mock.MagicMock()
        fake_sess.get = _fake_get

        req = make_mocked_request('GET', '/api/messages', headers={'X-Intercom-Msg': '1', 'Host': '127.0.0.1:18091'})
        with mock.patch.object(self.ic, '_app_item', return_value=fake_sess):
            resp = asyncio.run(self.ic.handle_messages(req))
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(calls), 2)
        self.assertIn('If-None-Match', calls[0])
        self.assertNotIn('If-None-Match', calls[1])

        # handle_message_voice 28s timeout, handle_tutor_mobile 36s/20s timeout & X-Tutor-Entry-Base only via tunnel
        ic_src = self.ic_path.read_text('utf-8')
        self.assertIn('timeout=aiohttp.ClientTimeout(total=28)', ic_src)
        self.assertIn('timeout=_ah.ClientTimeout(total=36 if body is not None else 20)', ic_src)
        self.assertIn('if via_tunnel and xfp == "https" and entry_host', ic_src)
        self.assertIn('len(self.all_ws) >= 12', ic_src)


class TestFrontendScriptsR87(unittest.TestCase):
    def test_frontend_guards_and_preserved_invariants(self):
        ho = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        for tok in ('orGoSeq', 'orRaf1', 'cancelOrRaf', 'document.hidden', 'orSetMode', 'nativeRaf(() => nativeRaf(() => {'):
            self.assertIn(tok, ho)

        home_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('r.isConnected && !r.disabled', home_html)
        self.assertIn('d = Math.min(d, new Date(y, m, 0).getDate())', home_html)

        mon_html = _p('/usr/local/share/thinksmart-monitor/index.html').read_text('utf-8')
        self.assertIn('r.isConnected && !r.disabled', mon_html)

        ko = _p('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js').read_text('utf-8')
        self.assertIn('pendingOpenAt', ko)
        self.assertIn('tickDueAt = 0', ko)

        qq_html = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        for tok in (
            'radioLoading = false', 'mineReqSeq++', 'loadingMine = false',
            '!state.ambient', 'progBase.playing = false', 'syncMusicTimers()',
            'ackedOpenCmdTab', '!isPageActive()',
        ):
            self.assertIn(tok, qq_html)

        mob_html = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('function jsAttrStr', mob_html)
        self.assertIn('curPg.check_error', mob_html)

        tut_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('function encUriJs', tut_html)
        self.assertIn('function jsAttrStr', tut_html)
        self.assertIn('expectBase', tut_html)
        self.assertGreaterEqual(tut_html.count('ddClear'), 4)
        self.assertIn('scheduleOrientPoll(30)', tut_html)
        self.assertIn("if (orientMode === 'landscape' && orientSensor !== null) return;", tut_html)
        self.assertIn('boardDirTxt()', tut_html)

        ic_src = _p('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
        self.assertIn('?video=0', ic_src)
        self.assertIn('!_embedHidden() || inCall || listenOnly', ic_src)
        self.assertIn('&yearly=1', ic_src)


if __name__ == '__main__':
    unittest.main()
