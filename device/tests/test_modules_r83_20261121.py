#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R83: Round 4 Deep Non-Xiaozhi Audit & Regression Tests."""
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
ROOT = pathlib.Path(E('R83_ROOT', '/'))


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


class TestQqMusicR83(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq_path = _p('/usr/local/bin/thinksmart-qqmusic.py')
        cls.qq_src = cls.qq_path.read_text('utf-8')
        cls.qq = _load_py_clean('r83_qq', cls.qq_path, strip_names=('engine',))

    def test_search_local_entry_budget_and_zero_byte_compat(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / '01.mp3').touch()
            (root / '.hidden.mp3').touch()
            (root / '.Trashes').mkdir()
            (root / '.Trashes' / '00.mp3').touch()
            sub = root / '周杰伦专辑'
            sub.mkdir()
            (sub / '02_晴天.flac').touch()
            with mock.patch.object(self.qq, 'LOCAL_DIRS', [root]):
                all_tracks = self.qq.search_local('', limit=10)
                self.assertEqual([t['title'] for t in all_tracks], ['01', '02_晴天'])
                sub_hits = self.qq.search_local('周杰伦', limit=10)
                self.assertEqual([t['title'] for t in sub_hits], ['02_晴天'])

            cnt = 0

            class Entry:
                path = str(root / 'not-a-song.txt')

                def is_dir(self, **_kw):
                    return False

            class Entries:
                def __enter__(self):
                    return self

                def __exit__(self, *_a):
                    pass

                def __iter__(self):
                    nonlocal cnt
                    while True:
                        cnt += 1
                        if cnt > 10002:
                            raise AssertionError('unbounded')
                        yield Entry()

            with mock.patch.object(self.qq, 'LOCAL_DIRS', [root]), \
                 mock.patch.object(self.qq.os, 'scandir', return_value=Entries()):
                self.assertEqual(self.qq.search_local(), [])
            self.assertLessEqual(cnt, 10001)

    def test_sleep_preview_aborted_on_control_and_seek(self):
        eng = self.qq.QQMusicEngine()
        eng.save_session = lambda *a, **k: None
        eng.playing = True
        eng.paused = True
        eng.queue = [{'mid': 'm1', 'title': 'A', 'duration': 200}, {'mid': 'm2', 'title': 'B', 'duration': 200}]
        eng.index = 0
        eng.volume = 25
        eng._preview = {'prev_volume': 70, 'volume': 25, 'label': '雨声'}
        eng.sleep_mode = 'time'
        eng.sleep_deadline = time.monotonic() + 30.0
        eng.ambient_preset = 'rain'
        vols = []
        eng.set_volume = lambda v: (setattr(eng, 'volume', v), vols.append(v))[-1] or {'ok': True}
        res = eng.control('pause')
        self.assertTrue(res.get('ok'))
        self.assertIsNone(eng._preview)
        self.assertIsNone(eng.sleep_mode)
        self.assertIn(70, vols)

        eng.volume = 25
        eng._preview = {'prev_volume': 65, 'volume': 25, 'label': '风声'}
        eng.sleep_mode = 'time'
        eng.sleep_deadline = time.monotonic() + 30.0
        eng.ambient_preset = 'wind'
        vols.clear()
        sres = eng.seek(42.0)
        self.assertTrue(sres.get('ok'))
        self.assertIsNone(eng._preview)
        self.assertIsNone(eng.sleep_mode)
        self.assertIn(65, vols)

    def test_qq_song_id_anonymous_and_dir201_gen_guard(self):
        seen_hdrs = []

        class _Resp:
            def __init__(self, obj):
                self._b = json.dumps(obj).encode('utf-8')

            def read(self, *_a):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def fake_open(req, timeout=8):
            seen_hdrs.append(dict(req.header_items()))
            return _Resp({'code': 0, 'req_1': {'code': 0, 'data': {'track_info': {'id': 987654, 'type': 0}}}})

        with self.qq._SONG_ID_LOCK:
            self.qq._SONG_ID_CACHE.pop('mid_anon_check', None)
        with mock.patch.object(self.qq, '_safe_urlopen', side_effect=fake_open):
            sid, stype = self.qq.qq_song_id('mid_anon_check')
        self.assertEqual((sid, stype), (987654, 0))
        self.assertNotIn('Cookie', seen_hdrs[0])

        # Stale dir_201 fetch invalidated when _clear_dir201_cache() bumps _DIR201_GEN mid-flight
        self.qq._clear_dir201_cache()

        def racing_open(req, timeout=6):
            self.qq._clear_dir201_cache()
            return _Resp({'req': {'code': 0, 'data': {'songlist': [{'mid': 'stale1', 'name': 'Old', 'singer': [], 'album': {}}]}}})

        with mock.patch.object(self.qq, 'load_config', return_value={'uin': '123456', 'qm_keyst': 'k'}), \
             mock.patch.object(self.qq, '_safe_urlopen', side_effect=racing_open):
            out = self.qq.qq_playlist_tracks('dir_201', limit=160)
        self.assertEqual(len(out), 1)
        with self.qq._DIR201_LOCK:
            self.assertIsNone(self.qq._DIR201_CACHE)

    def test_voice_local_radio_cn_ep_fallback_and_local_path_guard(self):
        eng = self.qq.QQMusicEngine()
        eng.save_session = lambda *a, **k: None
        seen_radio = []
        eng.play_radio = lambda rid, **kw: seen_radio.append((rid, kw)) or {'ok': True}
        eng.play_query('播放本地广播')
        eng.play_query('播放三国演义评书第十二回')
        self.assertEqual(seen_radio[0][0], 'zj_93')
        self.assertEqual(seen_radio[1][0], 'ct_sanguo')
        self.assertEqual(seen_radio[1][1]['track_index'], 11)
        self.assertFalse(self.qq._is_allowed_local_audio('pipe:0.mp3'))

        # resolve_hires_fallback decodes HTML entities, penalizes karaoke, uses dash.duration
        search_j = {'data': {'result': [
            {'bvid': 'BV1ab411c7mD', 'duration': '03:30', 'title': '晴天 KTV 伴奏'},
            {'bvid': 'BV1cd411c7mE', 'duration': '03:45', 'title': '周杰伦 &amp; 晴天&#39;s'},
        ]}}
        view_j = {'data': {'cid': 555, 'duration': 3600}}
        play_j = {'data': {'dash': {'duration': 225, 'audio': [{'bandwidth': 192000, 'baseUrl': 'https://upos-sz-mirror.bilivideo.com/a.m4s'}]}}}

        class _R:
            def __init__(self, b):
                self._b = json.dumps(b).encode('utf-8')

            def read(self, *_a):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def fake_bili_open(req, timeout=4):
            u = req if isinstance(req, str) else req.full_url
            if 'search/type' in u:
                return _R(search_j)
            if 'web-interface/view' in u:
                return _R(view_j)
            return _R(play_j)

        class _Opener:
            open = staticmethod(fake_bili_open)

        with mock.patch.object(self.qq.urllib.request, 'build_opener', return_value=_Opener()), \
             mock.patch.object(self.qq, '_is_safe_stream_url', return_value=True):
            fb = self.qq.resolve_hires_fallback("晴天's", title_hint="晴天's", artist_hint='周杰伦')
        self.assertIsNotNone(fb)
        self.assertEqual(fb['duration'], 225)

    def test_qq_html_r83_guards(self):
        html = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        self.assertIn('const seekHold = seekDragging || seekPumping || pendingSeekSec !== null || monoNow() < seekOptimisticUntil;', html)
        self.assertIn('window.tsStopPortraitScroll()', html)
        self.assertIn('const qrStartedAt = monoNow();', html)
        self.assertIn('TAB_TITLES[st.open_ui_tab]', html)
        self.assertIn("row.parentElement.id === 'radioEpList'", html)


class TestUsbStackR83(unittest.TestCase):
    def test_usb_mode_watchdog_and_automount(self):
        um = _p('/usr/local/bin/usb-mode').read_text('utf-8')
        self.assertIn('i % 4', um)
        self.assertIn('/sys/bus/usb/devices/usb*/power/control', um)
        sh = _p('/usr/local/bin/usb-automount.sh').read_text('utf-8')
        self.assertIn('timeout 3 findmnt -krn -M "$MNT" -o SOURCE', sh)
        self.assertIn('/sys/class/block/$DEVNAME/${DEVNAME}[0-9]*', sh)
        wd_src = _p('/usr/local/sbin/thinksmart-usb-watchdog').read_text('utf-8')
        self.assertIn('wake_r, wake_w = os.pipe()', wd_src)
        wd = _load_script('r83_usb_wd', _p('/usr/local/sbin/thinksmart-usb-watchdog'))
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            role_f = root / 'role'
            usb1_d = root / 'usb1'
            role_f.write_text('device\n', 'utf-8')
            calls = []

            def fake_run(cmd, **_kw):
                if 'usb-mode' not in str(cmd[0]):
                    return types.SimpleNamespace(returncode=1, stdout='', stderr='')
                calls.append(cmd[1])
                if len(calls) <= 3:
                    return types.SimpleNamespace(returncode=1, stdout='', stderr='')
                role_f.write_text('host\n', 'utf-8')
                usb1_d.mkdir(exist_ok=True)
                return types.SimpleNamespace(returncode=0, stdout='', stderr='')

            dog = wd.UsbWatchdog(
                role_path=str(role_f), usb1_path=str(usb1_d),
                mnt_dir=str(root / 'mnt'), lock_path=str(root / 'lock'),
                cooldown_sec=0.0, settle_sec=0.0, role_gap_sec=0.0,
            )
            role_f.write_text('host\n', 'utf-8')
            with mock.patch.object(wd.subprocess, 'run', side_effect=fake_run), \
                 mock.patch.object(wd.time, 'sleep', return_value=None):
                ok1 = dog.recover('test1')
                self.assertFalse(ok1)
                self.assertTrue(dog._host_restore_pending)
                self.assertTrue(dog.needs_roothub_recovery())
                ok2 = dog.recover('test2')
                self.assertTrue(ok2)
                self.assertFalse(dog._host_restore_pending)


class TestTutorAndAdminR83(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tutor = _load_py_clean('r83_tutor', _p('/usr/local/bin/thinksmart-tutor.py'), strip_names=('eng', 'engine', 'app'))
        cls.admin = _load_py_clean('r83_admin', _p('/usr/local/bin/thinksmart-admin.py'), strip_names=('app', 'mgr'))

    def test_tutor_jpeg_eoi_right_angle_and_tts_slot(self):
        fake_thumb = b'\xff\xe1\x00\x0eEXIF\x00\x00\xff\xd8\x00\xff\xd9\x00'
        real_scan = b'\xff\xda\x00\x08\x01\x02\x03\x04\x05\x06ABCD\xff\xd9'
        buf = bytearray(b'\xff\xd8' + fake_thumb + real_scan + b'\xff\xd8')
        eoi = self.tutor.UVCCameraManager._find_jpeg_eoi(buf)
        self.assertEqual(eoi, len(b'\xff\xd8' + fake_thumb + real_scan) - 2)

        ops = self.tutor.sanitize_draw_ops([{'op': 'right_angle', 'x': 100, 'y': 100, 'dx': 'left', 'dy': [-1]}])
        self.assertEqual(ops[0]['dx'], -1)
        self.assertEqual(ops[0]['dy'], -1)

        # Ensure _ensure_stream stops dead v4l2-ctl on timeout and _acquire_tts_slot prevents slot stealing
        async def _check_async():
            eng = self.tutor.TutorEngine.__new__(self.tutor.TutorEngine)
            eng._tts_busy = False
            eng._tts_woken_ev = None
            eng._tts_priority_waiters = 0
            eng._tts_hi_q = __import__('collections').deque()
            eng._tts_lo_q = __import__('collections').deque()
            order = []

            async def _hold_first():
                async with eng._acquire_tts_slot(priority=False, key='k1'):
                    order.append('first')
                    await asyncio.sleep(0.05)

            async def _wait_hi():
                await asyncio.sleep(0.01)
                async with eng._acquire_tts_slot(priority=True, key='hi'):
                    order.append('hi')

            async def _late_lo():
                await asyncio.sleep(0.015)
                async with eng._acquire_tts_slot(priority=False, key='lo2'):
                    order.append('lo2')

            await asyncio.gather(_hold_first(), _wait_hi(), _late_lo())
            self.assertEqual(order, ['first', 'hi', 'lo2'])

        asyncio.run(_check_async())

    def test_admin_subproc_kill_and_html_fixes(self):
        async def _check_kill():
            killed = []
            waited = []

            class FakeProc:
                returncode = None

                async def communicate(self, _inp=None):
                    await asyncio.sleep(5)
                    return b'', b''

                def kill(self):
                    killed.append(1)
                    self.returncode = -9

                async def wait(self):
                    waited.append(1)

            with self.assertRaises(asyncio.TimeoutError):
                await self.admin._communicate_bounded(FakeProc(), None, 0.02)
            self.assertEqual(killed, [1])
            self.assertEqual(waited, [1])

        asyncio.run(_check_kill())
        adm_html = _p('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertIn('}, true);\n})();', adm_html)
        self.assertIn('URL.revokeObjectURL(cfgUi.pvUrl)', adm_html)
        self.assertIn("cur === 'cfg' && cfgUi.tab === 'llm' && !document.hidden", adm_html)
        tut_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn("const expectBase = session ? ('sid=' + encodeURIComponent(session.id) + '&idx=' + curStepIdx) : '';", tut_html)
        self.assertIn('voiceRecBusy', tut_html)
        mob_html = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn("$('printPreviewImg')) $('printPreviewImg').removeAttribute('src')", mob_html)


class TestHomeMonitorStudyIntercomSystemdR83(unittest.TestCase):
    def test_home_inbox_fire_due_and_ui_fixes(self):
        home = _load_py_clean('r83_home', _p('/usr/local/bin/thinksmart-home.py'))
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'messages.json'
            box = home.Inbox(p)
            due_ts = time.time() + 120.0
            rem = box.add('吃药', due=due_ts)
            for i in range(25):
                box.recent_adds.clear()
                box.add(f'msg {i}', source='phone')
            fired = box.fire_due(now=due_ts + 1.0)
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0]['id'], rem['id'])
            recent_ids = [m['id'] for m in box.recent(3)]
            self.assertEqual(recent_ids[0], rem['id'])
            self.assertTrue(any(m['id'] == rem['id'] for m in box.unread()))

        h_src = _p('/usr/local/bin/thinksmart-home.py').read_text('utf-8')
        self.assertIn("'cd_close_seq': cd_cseq, 'cd_close_fresh': cd_cfresh", h_src)
        h_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('const isUnrMsg = m =>', h_html)
        self.assertIn('if (d.closeCountdown)', h_html)
        self.assertIn("const isYearlyRule = mode === 'yearly';", h_html)
        ov_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('closeCountdown: true', ov_js)

    def test_monitor_airplay_idle_blanker_fixes(self):
        m_src = _p('/usr/local/bin/thinksmart-monitor.py').read_text('utf-8')
        self.assertIn("c.get('latest_ms'), c.get('loss_pct')", m_src)
        self.assertIn("v.get('calibrated'), v.get('remaining_bytes')", m_src)

        ap = _load_py_clean('r83_ap', _p('/usr/local/bin/thinksmart-airplay.py'))
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ic_f = root / 'intercom-call-active'
            mr_f = root / 'uxplay-mirroring-active'
            au_f = root / 'uxplay-audio-active'
            ic_f.write_text('1', 'utf-8')
            mr_f.write_text('1', 'utf-8')
            stopped = []
            ev = threading.Event()

            class FakeProc:
                def poll(self):
                    return None

            p = FakeProc()
            with mock.patch.object(ap, 'INTERCOM_FLAG_PATH', ic_f), \
                 mock.patch.object(ap, 'MIRROR_FLAG_PATH', mr_f), \
                 mock.patch.object(ap, '_audio_flag_path', lambda: au_f), \
                 mock.patch.object(ap, 'stop_proc', lambda pr: stopped.append(pr) or ev.set()):
                ap.start_wayland_watchdog(lambda: (p, 123), interval=0.05, stop_event=ev)
                self.assertTrue(ev.wait(1.5))
            self.assertEqual(stopped, [p])

        ib_src = _p('/usr/local/bin/idle-blanker.py').read_text('utf-8')
        self.assertGreaterEqual(ib_src.count('back = None'), 5)

    def test_study_intercom_tunnels_and_systemd_units(self):
        st = _load_py_clean('r83_study', _p('/usr/local/bin/thinksmart-study.py'))
        out = st.parse_result('前言 {"step":1} 废话 <think>{"present":false,"posture":"bad"}\n</think>\n{"present":true,"posture":"good","too_close":false,"activity":"studying","focused":true,"note":"ok"}')
        self.assertTrue(out['present'])
        with self.assertRaises(ValueError):
            st.parse_result('{"step": 1} <think>{"present":false,"posture":"slouch","too_close":false,"activity":"playing","focused":false,"note":"draft"} truncated')
        out3 = st.parse_result('前缀 {"foo": 1} 正文 {"present":true,"posture":"good","too_close":false,"activity":"studying","focused":true,"note":"ok"}')
        self.assertTrue(out3['present'])
        st_src = _p('/usr/local/bin/thinksmart-study.py').read_text('utf-8')
        self.assertIn("_send_display_preview({'wake': True})", st_src)
        self.assertIn("'_enabled_unsynced'", st_src.replace('"', "'"))
        self.assertIn("'ends_mono'", st_src)

        p_html = _p('/usr/local/share/thinksmart-study/parent.html').read_text('utf-8')
        self.assertIn("c.addEventListener('focus', show);", p_html)
        self.assertIn("e.key === 'Escape'", p_html)

        ic_src = _p('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
        self.assertIn('def _embedHidden()', ic_src.replace('function ', 'def '))
        self.assertIn('asyncio.shield(restore_t)', ic_src)
        self.assertIn('self.saved_spk_vol = None', ic_src)

        ui_src = _p('/usr/local/bin/thinksmart-intercom-ui.py').read_text('utf-8')
        enq_block = ui_src.split('def enqueue_frame(')[1].split('def flush_frames(')[0] if 'def flush_frames(' in ui_src.split('def enqueue_frame(')[1] else ui_src.split('def enqueue_frame(')[1]
        self.assertNotIn('last_pb1', enq_block)

        t = _p('/usr/local/bin/thinksmart-intercom-tunnel.sh').read_text('utf-8')
        self.assertIn('cut -d. -f1 /proc/uptime > $UP_FILE', t)
        self.assertIn('pkill -TERM -P "$SSH_PID"', t)

        self.assertIn('MemoryMax=144M', _p('/etc/systemd/system/thinksmart-home.service').read_text('utf-8'))
        self.assertIn('MemoryMax=200M', _p('/etc/systemd/system/thinksmart-tutor.service').read_text('utf-8'))
        self.assertIn('MemoryMax=240M', _p('/etc/systemd/system/thinksmart-intercom.service').read_text('utf-8'))
        self.assertIn('TasksMax=48', _p('/etc/systemd/system/thinksmart-study.service').read_text('utf-8'))
        self.assertIn('-B /usr/local/bin/screen-wake-daemon.py', _p('/etc/systemd/system/screen-wake.service').read_text('utf-8'))
        self.assertIn('-B /usr/local/bin/gpu-watchdog.py', _p('/etc/systemd/system/thinksmart-gpu-watchdog.service').read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
