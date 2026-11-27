#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-24 Round 1 deep-check regression suite.

Covers all non-XiaoZhi bug fixes, reliability hardening, and resource optimizations:
1. Tutor & Admin: TUTOR_IDLE_EXIT_SEC=420, raw capture preserves rotate, CoWriteMonitor write dedup,
   thinksmart-admin.service ReadWritePaths wakeup_words, _probe_qq ?omit_heavy=1 & offline missed_count.
2. Home & Monitor: parse_repeat natural Chinese variants, remind_due day='today' roll-forward,
   post_music USB-unplugged open=False + message vs empty-queue open=True, get_orient (x+8)>>4 ETag,
   Monitor.received_wall is None on init, compute_badge traffic >=95%/85%, home/monitor/overlay UI checks.
3. QQ Music & AirPlay: lazy _get_np(), _evict_safe_ffmpeg_url, control resume/next/jump missing-USB
   mixed-queue skipping, /api/music/radio/emblem/{id}.svg & ?omit_heavy=1 & ?lite=1 ETag/304 & queue emblem URLs,
   _vol_synced_xz SSE guard, thinksmart-airplay-tune rps_sock_flow_entries & airplay qqmusic-yield-airplay.
4. Intercom, Kiosk & Daemons: _msg_origin_ok allow_wan_dir & port 443 fallback, _cam_backoff_until guard,
   ALL_FRAMES keyboard injection, idle-blanker ALS_WAKE_COOL=4.0 / RENDER_RECHECK=2.0 / 3-arg drain,
   screen-wake & vcnl4200 fresh-ALS wake when MODE_PATH exists vs fallback when absent, usb-automount ntfs3.
"""
import ast
import asyncio
import base64
import datetime as dt
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import sys
sys.dont_write_bytecode = True
if '/usr/local/bin' not in sys.path:
    sys.path.insert(0, '/usr/local/bin')
if os.path.isdir('/opt/thinksmart-addons/vendor') and '/opt/thinksmart-addons/vendor' not in sys.path:
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _load_mod(name, path, strip_main=True):
    src = pathlib.Path(path).read_text('utf-8')
    if strip_main:
        tree = ast.parse(src, filename=str(path))
        tree.body = [
            n for n in tree.body
            if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                    and getattr(n.test.left, 'id', '') == '__name__')
            and not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'engine' for t in n.targets))
        ]
        ast.fix_missing_locations(tree)
        code = compile(tree, str(path), 'exec')
        mod = types.ModuleType(name)
        mod.__file__ = str(path)
        exec(code, mod.__dict__)
        return mod
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR8TutorAdmin(unittest.TestCase):
    def test_tutor_config_and_source_invariants(self):
        tut_src = pathlib.Path('/usr/local/bin/thinksmart-tutor.py').read_text('utf-8')
        self.assertIn("os.environ.get('TUTOR_IDLE_EXIT_SEC', '420')", tut_src)
        self.assertIn('self._last_state_written', tut_src)
        self.assertIn('_rot_cache', tut_src)
        # UVCCameraManager.capture_jpeg(raw_untransformed=True) no longer mutates rotate in-place
        tut = _load_mod('r8_tut', '/usr/local/bin/thinksmart-tutor.py')
        uvc = tut.UVCCameraManager.__new__(tut.UVCCameraManager)
        uvc.rotate = 270
        uvc.zoom = 1.0
        uvc.bright_boost = False
        self.assertEqual(uvc.rotate, 270)

    def test_admin_unit_and_code(self):
        unit = pathlib.Path('/etc/systemd/system/thinksmart-admin.service').read_text('utf-8')
        self.assertIn('wakeup_words', unit)
        adm_src = pathlib.Path('/usr/local/bin/thinksmart-admin.py').read_text('utf-8')
        self.assertIn('/api/music/status?omit_heavy=1', adm_src)
        self.assertIn("'missed_count': 0", adm_src)
        self.assertIn("entry = 'vps' if vpn else ('wan' if wan_req else 'lan')", adm_src)
        adm_html = pathlib.Path('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertIn(": (curEntry === 'vps'", adm_html)
        self.assertNotIn("curEntry === 'vps' || window.isSecureContext", adm_html)


class TestR8HomeMonitor(unittest.TestCase):
    def test_parse_repeat_and_remind_due(self):
        hm = _load_mod('r8_hm', '/usr/local/bin/thinksmart-home.py')
        self.assertEqual(hm.parse_repeat('每个工作日'), ('workdays', None))
        self.assertEqual(hm.parse_repeat('周一至周五'), ('workdays', None))
        self.assertEqual(hm.parse_repeat('每周一到周五'), ('workdays', None))
        self.assertEqual(hm.parse_repeat('每日'), ('daily', None))
        self.assertEqual(hm.parse_repeat('每礼拜', [1, 3]), ('weekly', [1, 3]))
        # 20:00 local time asking day='today' time='07:30' with repeat=True rolls forward +1d
        now_dt = dt.datetime(2026, 11, 24, 20, 0, 0, tzinfo=hm.LOCAL_TZ)
        due = hm.remind_due(day='today', time_s='07:30', now=now_dt.timestamp(), repeat=True)
        self.assertIsNotNone(due)
        due_dt = dt.datetime.fromtimestamp(due, hm.LOCAL_TZ)
        self.assertEqual((due_dt.year, due_dt.month, due_dt.day, due_dt.hour, due_dt.minute), (2026, 11, 25, 7, 30))

    def test_home_html_and_monitor_fixes(self):
        hh = pathlib.Path('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn("'st-unread', 'st-cd'", hh)
        self.assertIn('!t.is_live && !t.missing', hh)
        self.assertIn('async function prefetchOtherWpCities()', hh)
        self.assertIn('html.tskb-open .modal.cd-edit', hh)

        ho = pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn("'If-None-Match'] = inboxEtag", ho)
        ko = pathlib.Path('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js').read_text('utf-8')
        xz_chunk = ko.split('function applyMusicStatus')[0].split('const xzActive =')[1]
        self.assertNotIn("ringing", xz_chunk)

        mh = pathlib.Path('/usr/local/share/thinksmart-monitor/index.html').read_text('utf-8')
        self.assertIn('.network-strip .c-src { display: inline-flex; }', mh)
        self.assertIn('else if(d.received_at===null){lastGoodTraffic=null;', mh)

    def test_monitor_cold_start_and_traffic_badge(self):
        with tempfile.TemporaryDirectory() as td:
            mon_mod = _load_mod('r8_mon', '/usr/local/bin/thinksmart-monitor.py')
            m = mon_mod.Monitor.__new__(mon_mod.Monitor)
            # Check source for received_wall = None and 3600.0 idle flush + safe viewed_recently without last_view
            src = pathlib.Path('/usr/local/bin/thinksmart-monitor.py').read_text('utf-8')
            self.assertIn('self.received_wall = None', src)
            self.assertIn('flush_interval = 60.0 if self.viewed_recently() else 3600.0', src)
            self.assertFalse(m.viewed_recently())
            m.assets = pathlib.Path(td)
            m.seed = [{'id': 3, 'name': 'US', 'sort': 1, 'alive': True}]
            m._seed_fallback = list(m.seed)
            m.servers = [{'id': 3, 'name': 'US', 'alive': True, 'disk_used': 10, 'disk_total': 100, 'mem_used': 10, 'mem_total': 100}]
            m.received_at = time.monotonic()
            m.received_wall = time.time()
            m.carriers = []
            m._carriers_updated_mono = 0.0
            m.open_ui_seq = 0
            m._open_ui_mono = 0.0
            m._open_ui_cmd = ''
            m.traffic_config = {'server_id': 3}
            m._servers_cfg_path = pathlib.Path(td) / 'servers.json'
            m._servers_cfg_sig = None
            m._servers_cfg_checked = 0.0
            m._traffic_cfg_path = pathlib.Path(td) / 'traffic.json'
            m._traffic_cfg_sig = None
            m._traffic_cfg_checked = 0.0
            m._traffic_ledger = {}
            m.extra_traffic = {}
            m._reload_servers_config = lambda force=False: None
            m._reload_traffic_config = lambda force=False: None
            m._runtime_override = lambda: None
            future_to = '2099-01-01T00:00:00Z'
            # Traffic quota >= 95% must NOT alert Home dock badge (test_server_20261112 invariant)
            m.traffic = {'available': True, 'complete': True, 'limit_bytes': 1000, 'used_bytes': 970, 'to': future_to}
            b97 = m.compute_badge()
            self.assertEqual(b97['text'], '')
            self.assertFalse(b97['amber'])
            self.assertFalse(b97['bad'])
            m.servers[0]['disk_used'] = 96
            b_dsk = m.compute_badge()
            self.assertEqual(b_dsk['text'], '磁盘96%')
            self.assertTrue(b_dsk['bad'])


class TestR8QQMusicAirPlay(unittest.TestCase):
    def test_lazy_numpy_and_cdn_evict_and_control(self):
        qq = _load_mod('r8_qq', '/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIsNone(qq._np)
        self.assertFalse(qq._np_tried)
        with qq._SAFE_FFMPEG_URL_LOCK:
            qq._SAFE_FFMPEG_URL_CACHE[('http://edge.example/live.aac', ())] = (time.monotonic(), 'http://cdn.example/1.aac')
        qq._evict_safe_ffmpeg_url('http://edge.example/live.aac')
        with qq._SAFE_FFMPEG_URL_LOCK:
            self.assertFalse(any(k[0] == 'http://edge.example/live.aac' for k in qq._SAFE_FFMPEG_URL_CACHE))

        # 6 missing USB tracks + 1 remote track: resume/next jump directly to playable track 6
        e = qq.QQMusicEngine.__new__(qq.QQMusicEngine)
        e._lock = threading.RLock()
        e.queue = [{'mid': f'u{i}', 'is_local': True, 'url': f'/mnt/usb/{i}.flac', 'title': f'U{i}'} for i in range(6)]
        e.queue.append({'mid': 'ol', 'is_local': False, 'url': None, 'title': 'OnlineSong'})
        e.index = 0
        e.ducked = False
        e.playing, e.paused, e.just_started = True, True, False
        e._proc = e._proc2 = e._pacat = None
        e._gen, e._starting_gen, e._fail_streak = 0, -1, 0
        e.elapsed_base, e.play_started_mono = 0.0, 0.0
        e.lyrics, e.lyrics_seq = [], 0
        e.ambient_preset = e.ambient_layer = None
        e.sleep_session = e.sleep_mode = e._preview = None
        e.play_mode = 'loop'
        e._clear_duck_except_home_locked = lambda: None
        e._stop_proc_locked = lambda: False
        e._start_current_track = lambda sec, gen: None
        e._send_aec_flush = lambda: None
        e._sleep_session_end = lambda why: None
        e._pause_bluetooth_if_streaming = lambda: None
        e._snapshot_radio_progress_locked = lambda: None
        e.save_session = lambda *a, **k: None
        orig_usb = qq._usb_mounted
        try:
            qq._usb_mounted = lambda: False
            r = e.control('resume')
            self.assertTrue(r['ok'])
            self.assertEqual(e.index, 6)
            # jump to missing USB track returns clear error
            rj = e.control('jump', index=2)
            self.assertFalse(rj['ok'])
            self.assertIn('U盘', rj['error'])
            # next on all-missing queue returns False without spinning
            e.queue = e.queue[:3]
            e.index = 0
            rn = e.control('next')
            self.assertFalse(rn['ok'])
            self.assertEqual(e.index, 0)
        finally:
            qq._usb_mounted = orig_usb

    def test_emblem_route_omit_heavy_and_airplay(self):
        qq = _load_mod('r8_qq2', '/usr/local/bin/thinksmart-qqmusic.py')
        self.assertTrue(qq.RADIO_STATIONS[0]['cover'].startswith('data:image/svg+xml;base64,'))
        with tempfile.TemporaryDirectory() as td:
            qq.CONFIG_PATH = pathlib.Path(td) / 'cfg.json'
            qq.SESSION_PATH = pathlib.Path(td) / 'sess.json'
            qq.AMBIENT_DIR = pathlib.Path(td) / 'amb'
            with mock.patch.object(qq.QQMusicEngine, '_load_session', lambda self: None):
                eng = qq.QQMusicEngine()
            eng.queue = [qq._radio_live_track(qq.RADIO_STATIONS[0])] * 10
            eng.queue_seq = 5
            st = eng.status(lite=False)
            self.assertEqual(st['queue'][0]['cover'], f"/api/music/radio/emblem/{qq.RADIO_STATIONS[0]['id']}.svg")
            self.assertEqual(st['queue'][0]['thumb'], '')

            async def go():
                qq.engine = eng
                app = qq.make_app()
                app.on_startup.clear()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    r_svg = await client.get(f"/api/music/radio/emblem/{qq.RADIO_STATIONS[0]['id']}.svg")
                    self.assertEqual(r_svg.status, 200)
                    self.assertEqual(r_svg.content_type, 'image/svg+xml')
                    etag = r_svg.headers['ETag']
                    r_304 = await client.get(f"/api/music/radio/emblem/{qq.RADIO_STATIONS[0]['id']}.svg", headers={'If-None-Match': etag})
                    self.assertEqual(r_304.status, 304)

                    r_oh = await client.get('/api/music/status?omit_heavy=1')
                    j_oh = await r_oh.json()
                    self.assertNotIn('queue', j_oh)
                    self.assertNotIn('lyrics', j_oh)
                    self.assertIn('play_mode', j_oh)

                    r_lite = await client.get('/api/music/status?lite=1')
                    self.assertEqual(r_lite.status, 200)
                    et_l = r_lite.headers.get('ETag')
                    self.assertTrue(et_l)
                    r_l304 = await client.get('/api/music/status?lite=1', headers={'If-None-Match': et_l})
                    self.assertEqual(r_l304.status, 304)
                finally:
                    await client.close()
            asyncio.run(go())

        tune = pathlib.Path('/usr/local/sbin/thinksmart-airplay-tune').read_text('utf-8')
        self.assertIn('/proc/sys/net/core/rps_sock_flow_entries', tune)
        ap = pathlib.Path('/usr/local/bin/thinksmart-airplay.py').read_text('utf-8')
        self.assertIn('qqmusic-yield-airplay', ap)
        mic = pathlib.Path('/usr/local/bin/thinksmart-mic').read_text('utf-8')
        self.assertIn('audioop.tomono', mic)
        self.assertIn('skip_full', mic)

    def test_refresh_token_fallback_after_pskey_expiry(self):
        qq = _load_mod('r8_qq_rt', '/usr/local/bin/thinksmart-qqmusic.py')
        qq._LAST_REFRESH_FAIL = (None, 0.0)
        qq._LAST_REFRESH_OK = (None, 0.0)
        qq._DEAD_PSKEY = None
        qq._clear_dir201_cache()

        class _Resp:
            def __init__(self, data=None, hdrs=None):
                self._b = json.dumps(data).encode('utf-8') if data is not None else b''
                self.headers = hdrs or {}
            def read(self, *_a):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _ExpiredPskeyOpener:
            def open(self, req, timeout=5):
                return _Resp(hdrs={'Location': 'https://xui.ptlogin2.qq.com/error'})

        seen_payloads = []
        def fake_safe_open(req, timeout=5):
            seen_payloads.append(json.loads(req.data.decode('utf-8')))
            return _Resp({'req': {'code': 0, 'data': {
                'musickey': 'NEW_VIP_MK_9988',
                'refresh_token': 'NEW_RT_7766',
                'access_token': 'NEW_AT_5544',
                'refresh_key': 'NEW_RK_3322',
                'musickeyCreateTime': 1795500000,
            }}})

        cfg = {
            'uin': '7000244',
            'qm_keyst': 'OLD_MK',
            'cookies_str': 'uin=7000244; p_skey=EXPIRED_PSKEY; psrf_qqopenid=OID1234; psrf_qqrefresh_token=RT_OLD; qm_keyst=OLD_MK; qqmusic_key=OLD_MK',
        }
        saved = []
        with mock.patch.object(qq, '_has_only_global_ips', return_value=True), \
             mock.patch.object(qq, 'load_config', side_effect=lambda: dict(cfg)), \
             mock.patch.object(qq, 'save_config', side_effect=lambda d: saved.append(dict(d))), \
             mock.patch.object(qq.urllib.request, 'build_opener', return_value=_ExpiredPskeyOpener()), \
             mock.patch.object(qq, '_safe_urlopen', side_effect=fake_safe_open):
            ok = qq._refresh_musickey_from_cookies()
        self.assertTrue(ok)
        self.assertEqual(len(seen_payloads), 1)
        self.assertEqual(seen_payloads[0]['req']['param']['refresh_token'], 'RT_OLD')
        self.assertEqual(saved[-1]['qm_keyst'], 'NEW_VIP_MK_9988')
        self.assertIn('psrf_qqrefresh_token=NEW_RT_7766', saved[-1]['cookies_str'])
        self.assertIn('qqmusic_key=NEW_VIP_MK_9988', saved[-1]['cookies_str'])

        # Post-logout abort guard on refresh_token path
        logged_out = {'uin': '', 'qm_keyst': '', 'cookies_str': ''}
        seq = [dict(cfg), logged_out]
        saved_abort = []
        with mock.patch.object(qq, '_has_only_global_ips', return_value=True), \
             mock.patch.object(qq, 'load_config', side_effect=lambda: seq.pop(0)), \
             mock.patch.object(qq, 'save_config', side_effect=lambda d: saved_abort.append(dict(d))), \
             mock.patch.object(qq.urllib.request, 'build_opener', return_value=_ExpiredPskeyOpener()), \
             mock.patch.object(qq, '_safe_urlopen', side_effect=fake_safe_open):
            self.assertFalse(qq._refresh_musickey_from_cookies())
        self.assertEqual(saved_abort, [])


class TestR8IntercomKioskDaemons(unittest.TestCase):
    def test_intercom_wan_dir_origin_and_camera_backoff(self):
        ic = _load_mod('r8_ic', '/usr/local/bin/thinksmart-intercom.py')
        # Strict default still rejects direct public HTTP origin
        self.assertFalse(ic._msg_origin_ok(
            {'host': '1.2.3.4:18091', 'origin': 'http://1.2.3.4:18091'}, peer='5.6.7.8'
        ))
        # Direct WAN authenticated session allows same Host+Port
        self.assertTrue(ic._msg_origin_ok(
            {'host': '1.2.3.4:18091', 'origin': 'http://1.2.3.4:18091'}, peer='5.6.7.8', allow_wan_dir=True
        ))
        self.assertFalse(ic._msg_origin_ok(
            {'host': '1.2.3.4:18091', 'origin': 'http://evil.example:18091'}, peer='5.6.7.8', allow_wan_dir=True
        ))
        srv = ic.IntercomServer()
        srv._cam_backoff_until = time.monotonic() + 30.0
        with mock.patch.object(ic.asyncio, 'create_subprocess_exec') as sp:
            asyncio.run(srv._ensure_camera_running())
            sp.assert_not_called()

    def test_kiosk_daemons_and_usb(self):
        kiosk_src = pathlib.Path('/usr/local/bin/thinksmart-kiosk.py').read_text('utf-8')
        self.assertIn('UserContentInjectedFrames.ALL_FRAMES', kiosk_src)
        self.assertIn('ensure_retry_timer', kiosk_src)
        kiosk_svc = pathlib.Path('/etc/systemd/system/thinksmart-kiosk.service').read_text('utf-8')
        self.assertNotIn('ExecStopPre=', kiosk_svc)
        self.assertIn('ExecStop=', kiosk_svc)
        self.assertIn(r'thinksmart-kiosk\\.py', kiosk_svc)
        tun_sh = pathlib.Path('/usr/local/bin/thinksmart-intercom-tunnel.sh').read_text('utf-8')
        self.assertIn('INTERCOM_REMOTE_HOST:-', tun_sh)
        self.assertIn('INTERCOM_REMOTE_PORT:-', tun_sh)
        self.assertIn('INTERCOM_SOCKS_PROXY:-', tun_sh)

        ib_src = pathlib.Path('/usr/local/bin/idle-blanker.py').read_text('utf-8')
        self.assertIn('ALS_WAKE_COOL    = 4.0', ib_src)
        self.assertIn('RENDER_RECHECK   = 2.0', ib_src)
        self.assertIn('poll_wait = RENDER_RECHECK', ib_src)
        self.assertIn('inotify_init1', ib_src)

        sw = _load_mod('r8_sw', '/usr/local/bin/screen-wake-daemon.py')
        self.assertEqual(sw.IDLE_ON_SECONDS, 1.5)
        px = _load_mod('r8_px', '/usr/local/bin/vcnl4200-proximity-daemon.py')
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            for tag, mod, als_attr in (('sw', sw, 'ALS_PATH'), ('px', px, 'ALS_RESULT')):
                root = base / tag
                root.mkdir()
                mod.HINT_PATH = root / 'hint.json'
                mod.MODE_PATH = root / 'mode.json'
                mod.USER_BRIGHTNESS_PATH = root / 'ub'
                mod.LEGACY_USER_BRIGHTNESS_PATH = root / 'ub_old'
                mod.NIGHT_VIEW_PATH = root / 'nv'
                mod.SUNRISE_PATH = root / 'sr'
                mod.NIGHT_TEST = False
                setattr(mod, als_attr, root / 'als.json')
                mod.HINT_PATH.write_text(json.dumps({'brightness': 90}))
                mod.USER_BRIGHTNESS_PATH.write_text('3900\n')
                # MODE_PATH missing -> daytime fallback (>3000)
                day_ts = dt.datetime(2026, 11, 24, 14, 0).timestamp()
                self.assertGreater(mod.read_wake_brightness(day_ts), 3000)
                # MODE_PATH exists with auto=True and fresh dark-room ALS (lux=0.1 -> ~1400) -> <=1600
                mod.MODE_PATH.write_text(json.dumps({'auto': True, 'anchor': 90}))
                getattr(mod, als_attr).write_text(json.dumps({'lux': 0.1, 'at': day_ts - 2.0}))
                self.assertLessEqual(mod.read_wake_brightness(day_ts), 1600)

        usb_src = pathlib.Path('/usr/local/bin/usb-automount.sh').read_text('utf-8')
        self.assertIn('mount -t ntfs3 -o "$OPTIONS"', usb_src)
        self.assertIn('mounted=1', usb_src)



    def test_fs2_runtime_hygiene(self):
        import base64, subprocess
        sysctl_txt = pathlib.Path('/etc/sysctl.d/90-airplay-tcp.conf').read_text('utf-8')
        self.assertIn('net.core.rps_sock_flow_entries=4096', sysctl_txt)

        mic_path = pathlib.Path('/usr/local/bin/thinksmart-mic')
        mic_txt = mic_path.read_text('utf-8')
        self.assertIn('audioop.tomono', mic_txt)
        ast.parse(mic_txt, filename=str(mic_path))
        hdr_lines = []
        in_hdr = False
        for ln in mic_txt.splitlines():
            if ln.strip() == 'import warnings':
                in_hdr = True
            if in_hdr:
                hdr_lines.append(ln)
                if 'import audioop' in ln:
                    break
        self.assertTrue(hdr_lines and any('import audioop' in x for x in hdr_lines))
        py_bin = '/usr/bin/python3' if os.path.exists('/usr/bin/python3') else sys.executable
        res = subprocess.run(
            [py_bin, '-W', 'default', '-c', '\n'.join(hdr_lines)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr.strip(), '')

        hub_dir = '/home/ubuntu/xiaozhi-hub-server'
        if os.path.isdir(os.path.join(hub_dir, '.git')):
            gr = subprocess.run(
                ['git', '-c', f'safe.directory={hub_dir}', '-C', hub_dir, 'status', '--porcelain', '--untracked-files=no'],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(gr.returncode, 0, gr.stderr)
            self.assertEqual(gr.stdout.strip(), '')



    def test_fs3_tas5782m_escape_and_diagnose_wake(self):
        tas_path = pathlib.Path('/home/ubuntu/.config/systemd/user/tas5782m-preinit.service')
        if tas_path.exists():
            tas_txt = tas_path.read_text('utf-8')
            self.assertIn('$${n#alsa_output.platform-c051000.sound-card}', tas_txt)
            self.assertNotIn('"${n#', tas_txt)

        dw = '/usr/local/bin/diagnose-wake.sh'
        self.assertTrue(os.path.isfile(dw) and os.access(dw, os.X_OK))

    def test_fs4_tutor_reasoning_stream_and_qq_refresh_cooldown(self):
        tut = _load_mod('r8_tut_fs4', '/usr/local/bin/thinksmart-tutor.py')

        class _FakeStreamContent:
            def __init__(self, chunks):
                self._chunks = list(chunks)

            async def readany(self):
                if not self._chunks:
                    return b''
                delay, raw = self._chunks.pop(0)
                if delay > 0:
                    await asyncio.sleep(delay)
                return raw

        class _FakePostCM:
            def __init__(self, status, chunks):
                self.status = status
                self.content = _FakeStreamContent(chunks)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        def _sse_line(delta_obj):
            return ('data: ' + json.dumps({'choices': [{'delta': delta_obj}]}, ensure_ascii=False) + '\n\n').encode('utf-8')

        steps_payload = [
            {'step': 1, 'title': '审题找条件', 'say': '已知两直角边分别是三和四。', 'board': 'AC=3, BC=4', 'quiz': {'mode': 'choice', 'q': '先求哪条边？', 'options': ['斜边AB', '高AC'], 'ans': 0}},
            {'step': 2, 'title': '勾股定理算斜边', 'say': '用勾股定理算出斜边。', 'board': 'AB² = 3² + 4²', 'quiz': {'mode': 'choice', 'q': '三的平方加四的平方等于？', 'options': ['25', '7'], 'ans': 0}},
            {'step': 3, 'title': '面积法列式', 'say': '算斜边上的高。', 'board': '3×4 = 5×CD，CD = □', 'quiz': {'mode': 'input', 'q': 'CD 等于几？', 'ans': '2.4'}},
            {'step': 4, 'title': '核对复盘', 'say': '所以斜边是五，高是二点四。', 'board': 'AB=5, CD=2.4', 'quiz': None},
        ]
        ndjson_chunk = ''.join(json.dumps(s, ensure_ascii=False) + '\n' for s in steps_payload)

        with tempfile.TemporaryDirectory() as td:
            tp = pathlib.Path(td)
            cfg_p = tp / 'hub.json'
            cfg_p.write_text(json.dumps({
                'selected_module': {'LLM': 'DeepSeekLLM'},
                'LLM': {
                    'DeepSeekLLM': {'base_url': 'http://127.0.0.1:19999/v1', 'api_key': 'k-ds', 'model_name': 'deepseek-v4.1-flash-sg'},
                    'OpenAILLM': {'base_url': 'http://127.0.0.1:19999/v1', 'api_key': 'k-oa', 'model_name': 'gpt-6.1-terra'},
                },
            }), encoding='utf-8')
            saved = {k: getattr(tut, k) for k in ('STATE_DIR', 'SNAP_DIR', 'HIST_PATH', 'PREFS_PATH', 'HUB_CFG_PATH')}
            had_sig = 'TUTOR_OPEN_SIGNAL' in os.environ
            old_sig = os.environ.get('TUTOR_OPEN_SIGNAL')
            try:
                tut.STATE_DIR = tp / 'state'
                tut.SNAP_DIR = tut.STATE_DIR / 'snaps'
                tut.HIST_PATH = tut.STATE_DIR / 'history.json'
                tut.PREFS_PATH = tut.STATE_DIR / 'prefs.json'
                tut.HUB_CFG_PATH = cfg_p
                os.environ['TUTOR_OPEN_SIGNAL'] = str(tp / 'open.json')

                async def _run_tutor():
                    # 1) Reasoning tokens keep stream alive past _FIRST_TOKEN_TIMEOUT_FAST without false failover
                    called_a = []
                    eng_a = tut.TutorEngine()
                    eng_a.tts_http = None

                    def post_a(url, json=None, **_kw):
                        called_a.append((json or {}).get('model'))
                        chunks = [
                            (0.06, _sse_line({'reasoning_content': '先看直角三角形'})),
                            (0.06, _sse_line({'extra_fields': {'reasoning_content': '勾股定理三四五'}})),
                            (0.06, _sse_line({'reasoning_content': '再等面积法'})),
                            (0.05, _sse_line({'content': ndjson_chunk})),
                            (0.0, b'data: [DONE]\n\n'),
                        ]
                        return _FakePostCM(200, chunks)

                    eng_a.http = types.SimpleNamespace(post=post_a)
                    with mock.patch.object(tut, '_FIRST_TOKEN_TIMEOUT_FAST', 0.12), \
                         mock.patch.object(tut, '_FIRST_CONTENT_TIMEOUT_FAST', 2.0):
                        sess_a = eng_a.start_live_problem(
                            question='已知直角三角形两直角边为3和4，求斜边与高',
                            mode='fast', open_ui=False, silent_tts=True,
                        )
                        for _ in range(40):
                            await asyncio.sleep(0.05)
                            pub_a = eng_a.public_session(sess_a['id'])
                            if pub_a['status'] in ('ready', 'error'):
                                break
                    for t in list(eng_a.bg_tasks):
                        t.cancel()
                        try:
                            await t
                        except BaseException:
                            pass
                    pub_a = eng_a.public_session(sess_a['id'])
                    self.assertEqual(pub_a['status'], 'ready', pub_a.get('error'))
                    self.assertEqual(called_a, ['deepseek-v4.1-flash-sg'])
                    self.assertEqual(eng_a.sessions[sess_a['id']]['metrics']['model'], 'deepseek-v4.1-flash-sg')
                    self.assertGreaterEqual(len([s for s in pub_a['steps'] if s.get('step', 0) > 0]), 4)

                    # 2) Reasoning stall with zero content past _FIRST_CONTENT_TIMEOUT_FAST cleanly fails over
                    called_b = []
                    eng_b = tut.TutorEngine()
                    eng_b.tts_http = None

                    def post_b(url, json=None, **_kw):
                        m = (json or {}).get('model')
                        called_b.append(m)
                        if m == 'deepseek-v4.1-flash-sg':
                            chunks = [(0.05, _sse_line({'reasoning_content': f'loop{i}'})) for i in range(12)]
                            return _FakePostCM(200, chunks)
                        return _FakePostCM(200, [(0.01, _sse_line({'content': ndjson_chunk})), (0.0, b'data: [DONE]\n\n')])

                    eng_b.http = types.SimpleNamespace(post=post_b)
                    with mock.patch.object(tut, '_FIRST_TOKEN_TIMEOUT_FAST', 0.08), \
                         mock.patch.object(tut, '_FIRST_CONTENT_TIMEOUT_FAST', 0.14):
                        sess_b = eng_b.start_live_problem(
                            question='已知直角三角形两直角边为3和4，求斜边与高',
                            mode='fast', open_ui=False, silent_tts=True,
                        )
                        for _ in range(50):
                            await asyncio.sleep(0.05)
                            pub_b = eng_b.public_session(sess_b['id'])
                            if pub_b['status'] in ('ready', 'error'):
                                break
                    for t in list(eng_b.bg_tasks):
                        t.cancel()
                        try:
                            await t
                        except BaseException:
                            pass
                    pub_b = eng_b.public_session(sess_b['id'])
                    self.assertEqual(pub_b['status'], 'ready', pub_b.get('error'))
                    self.assertEqual(called_b, ['deepseek-v4.1-flash-sg', 'gpt-6.1-terra'])

                asyncio.run(_run_tutor())
            finally:
                for k, v in saved.items():
                    setattr(tut, k, v)
                if had_sig:
                    os.environ['TUTOR_OPEN_SIGNAL'] = old_sig
                else:
                    os.environ.pop('TUTOR_OPEN_SIGNAL', None)

        # Part B: QQ Music refresh success cooldown & _DEAD_PSKEY skip
        qq = _load_mod('r8_qq_fs4', '/usr/local/bin/thinksmart-qqmusic.py')
        qq._LAST_REFRESH_FAIL = (None, 0.0)
        qq._LAST_REFRESH_OK = (None, 0.0)
        qq._DEAD_PSKEY = None
        qq._clear_dir201_cache()

        class _Resp:
            def __init__(self, data=None, hdrs=None):
                self._b = json.dumps(data).encode('utf-8') if data is not None else b''
                self.headers = hdrs or {}
            def read(self, *_a):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        state = {
            'uin': '7000244',
            'qm_keyst': 'OLD_MK',
            'cookies_str': 'uin=7000244; p_skey=DEAD_PS; psrf_qqopenid=OID1; psrf_qqrefresh_token=RT1; qm_keyst=OLD_MK; qqmusic_key=OLD_MK',
        }
        saves = []
        counts = {'auth': 0, 'rt': 0}

        class _DeadPsOpener:
            def open(self, req, timeout=5):
                counts['auth'] += 1
                return _Resp(hdrs={'Location': 'https://xui.ptlogin2.qq.com/error'})

        def fake_save(d):
            state.clear()
            state.update(dict(d))
            saves.append(d.get('qm_keyst'))

        def fake_rt_open(req, timeout=5):
            counts['rt'] += 1
            n = counts['rt']
            return _Resp({'req': {'code': 0, 'data': {
                'musickey': f'NEW_MK_{n}',
                'refresh_token': f'NEW_RT_{n}',
                'access_token': f'NEW_AT_{n}',
                'refresh_key': f'NEW_RK_{n}',
                'musickeyCreateTime': 1795500000 + n,
            }}})

        with mock.patch.object(qq, '_has_only_global_ips', return_value=True), \
             mock.patch.object(qq, 'load_config', side_effect=lambda: dict(state)), \
             mock.patch.object(qq, 'save_config', side_effect=fake_save), \
             mock.patch.object(qq.urllib.request, 'build_opener', return_value=_DeadPsOpener()), \
             mock.patch.object(qq, '_safe_urlopen', side_effect=fake_rt_open):
            # 1) First call tries expired p_skey -> falls back to refresh_token
            self.assertTrue(qq._refresh_musickey_from_cookies())
            self.assertEqual(counts, {'auth': 1, 'rt': 1})
            self.assertEqual(saves, ['NEW_MK_1'])
            self.assertEqual(qq._DEAD_PSKEY, 'DEAD_PS')

            # 2) Immediate second call blocked by 300s _LAST_REFRESH_OK cooldown
            self.assertFalse(qq._refresh_musickey_from_cookies())
            self.assertEqual(counts, {'auth': 1, 'rt': 1})
            self.assertEqual(saves, ['NEW_MK_1'])

            # 3) After cooldown ages past 300s, skips _DEAD_PSKEY p_skey POST and goes straight to refresh_token
            qq._LAST_REFRESH_OK = (qq._LAST_REFRESH_OK[0], time.monotonic() - 301.0)
            self.assertTrue(qq._refresh_musickey_from_cookies())
            self.assertEqual(counts, {'auth': 1, 'rt': 2})
            self.assertEqual(saves, ['NEW_MK_1', 'NEW_MK_2'])

    def test_fs5_tutor_post_chat_once_low_effort_and_print_timeout(self):
        tut_src = pathlib.Path('/usr/local/bin/thinksmart-tutor.py').read_text('utf-8')
        alt_seg = tut_src[tut_src.index('async def generate_alt_method'):tut_src.index('async def get_exam_rubric')]
        self.assertIn("timeout_s=32.0", alt_seg)
        ps_seg = tut_src[tut_src.index('async def prepare_print_sheet'):tut_src.index('def _read_open_ui_seq_floor')]
        self.assertIn("timeout_s=22.0", ps_seg)
        self.assertIn("timeout=24.0", ps_seg)

        tut = _load_mod('r8_tut_fs5', '/usr/local/bin/thinksmart-tutor.py')

        class _FakeJsonCM:
            def __init__(self, reply_text):
                self.status = 200
                self.content = None
                self._raw = json.dumps({'choices': [{'message': {'content': reply_text}}]}, ensure_ascii=False).encode('utf-8')

            async def read(self):
                return self._raw

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as td:
            tp = pathlib.Path(td)
            cfg_p = tp / 'hub.json'
            cfg_p.write_text(json.dumps({
                'selected_module': {'LLM': 'DeepSeekLLM'},
                'LLM': {
                    'DeepSeekLLM': {'base_url': 'http://127.0.0.1:19999/v1', 'api_key': 'k-ds', 'model_name': 'deepseek-v4.1-flash-sg'},
                    'OpenAILLM': {'base_url': 'http://127.0.0.1:19999/v1', 'api_key': 'k-oa', 'model_name': 'gpt-6.1-terra'},
                },
            }), encoding='utf-8')
            saved = {k: getattr(tut, k) for k in ('STATE_DIR', 'SNAP_DIR', 'HIST_PATH', 'PREFS_PATH', 'HUB_CFG_PATH')}
            had_sig = 'TUTOR_OPEN_SIGNAL' in os.environ
            old_sig = os.environ.get('TUTOR_OPEN_SIGNAL')
            try:
                tut.STATE_DIR = tp / 'state'
                tut.SNAP_DIR = tut.STATE_DIR / 'snaps'
                tut.HIST_PATH = tut.STATE_DIR / 'history.json'
                tut.PREFS_PATH = tut.STATE_DIR / 'prefs.json'
                tut.HUB_CFG_PATH = cfg_p
                os.environ['TUTOR_OPEN_SIGNAL'] = str(tp / 'open.json')

                eng = tut.TutorEngine()
                # Preserve _resolve_llm_chain & _iter_llm_targets empty extra for deepseek
                for kind in ('fast', 'deep', 'aux'):
                    for m_n, ex_c in eng._resolve_llm_chain(kind)[2]:
                        if m_n.startswith('deepseek'):
                            self.assertEqual(ex_c, {})
                    for _, _, m_t, ex_t in eng._iter_llm_targets(kind):
                        if m_t.startswith('deepseek'):
                            self.assertEqual(ex_t, {})

                async def _run_fs5():
                    seen_bodies = []

                    def fake_post(url, json=None, **_kw):
                        b = dict(json or {})
                        seen_bodies.append(b)
                        user_msg = str((b.get('messages') or [{}])[-1].get('content') or '')
                        if '"items"' in user_msg:
                            reply = json_reply
                        else:
                            reply = '{"ok": true}'
                        return _FakeJsonCM(reply)

                    json_reply = json.dumps({'items': [
                        {'title': '改编1', 'question': '解新方程 x^2 - 9 = 0。', 'ans_check': 'x = ±3'},
                        {'title': '改编2', 'question': '解新方程 3x + 2 = 14。', 'ans_check': 'x = 4'},
                    ]}, ensure_ascii=False)
                    eng.http = types.SimpleNamespace(post=fake_post)

                    await eng._post_chat_once('aux', [{'role': 'user', 'content': 'hi'}])
                    self.assertEqual(seen_bodies[-1].get('model'), 'deepseek-v4.1-flash-sg')
                    self.assertEqual(seen_bodies[-1].get('reasoning_effort'), 'low')

                    await eng._post_chat_once('deep', [{'role': 'user', 'content': 'hi'}])
                    self.assertEqual(seen_bodies[-1].get('model'), 'deepseek-v4.1-flash-sg')
                    self.assertNotIn('reasoning_effort', seen_bodies[-1])

                    eng._ensure_mistakes_loaded()
                    eng.mistakes.extend([
                        {'id': 'mb_fs5_1', 'title': '方程1', 'question': '解方程 x^2 - 4 = 0。', 'subject': 'math', 'error_tag': '计算失误', 'graduated': False, 'steps': []},
                        {'id': 'mb_fs5_2', 'title': '方程2', 'question': '解方程 2x + 3 = 11。', 'subject': 'math', 'error_tag': '移项符号', 'graduated': False, 'steps': []},
                    ])
                    sheet = await eng.prepare_print_sheet(ids=['mb_fs5_1', 'mb_fs5_2'], sheet_mode='desensitized')
                    self.assertTrue(sheet.get('ok'))
                    self.assertEqual(len(sheet['items']), 2)
                    self.assertIn('x² - 9 = 0', sheet['items'][0]['question'])
                    self.assertIn('3x + 2 = 14', sheet['items'][1]['question'])
                    self.assertEqual(seen_bodies[-1].get('reasoning_effort'), 'low')

                asyncio.run(_run_fs5())
            finally:
                for k, v in saved.items():
                    setattr(tut, k, v)
                if had_sig:
                    os.environ['TUTOR_OPEN_SIGNAL'] = old_sig
                else:
                    os.environ.pop('TUTOR_OPEN_SIGNAL', None)


    def test_fs6_polya_shield_voice_timeout_and_yaml_cache(self):
        import re
        from aiohttp.test_utils import TestClient, TestServer

        # 1) Polya timeout & frontend AbortController alignment & CSafeLoader
        tut_src = pathlib.Path('/usr/local/bin/thinksmart-tutor.py').read_text('utf-8')
        pol_seg = tut_src[tut_src.index('async def get_polya_reduction'):tut_src.index('async def inspect_board_scratch')]
        self.assertIn('timeout_s=32.0', pol_seg)
        self.assertIn('CSafeLoader', tut_src)
        self.assertFalse(os.path.exists('/usr/local/bin/__pycache__'), 'stale /usr/local/bin/__pycache__ must not exist')

        idx_html = pathlib.Path('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        expected_min_ms = {
            '/api/tutor/alt_method': 45000,
            '/api/tutor/polya_simplify': 45000,
            '/api/tutor/foundation': 36000,
            '/api/tutor/exam_rubric': 36000,
            '/api/tutor/archetype_map': 36000,
            '/api/tutor/feynman': 34000,
            '/api/tutor/ask': 36000,
            '/api/tutor/check_scratch': 38000,
            '/api/tutor/cowrite/variant': 48000,
            '/api/tutor/mistakes/retest': 48000,
            '/api/tutor/mistakes/sprint_start': 46000,
            '/api/tutor/mistakes/sprint_next': 46000,
            '/api/tutor/variant': 50000,
        }
        for ep, min_ms in expected_min_ms.items():
            m_ep = re.search(re.escape(ep) + r"[\s\S]{1,420}?(\d{5})\s*\)", idx_html)
            self.assertIsNotNone(m_ep, f'missing api() call for {ep}')
            self.assertGreaterEqual(int(m_ep.group(1)), min_ms, f'timeout too short for {ep}: {m_ep.group(1)}')

        # 2) AEC bridge _call_tutor_tool timeout (27s for LLM/vision actions, 12s for pure open)
        sc_mod = _load_mod('r8_sc_fs6', '/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py')
        seen_touts = []

        class _FakeUrlResp:
            def read(self, *_a):
                return b'{"ok": true, "text": "done"}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def _fake_open(req, timeout=None):
            seen_touts.append(timeout)
            return _FakeUrlResp()

        with mock.patch.object(sc_mod._NO_PROXY_OPENER, 'open', side_effect=_fake_open):
            sc_mod._call_tutor_tool({'action': 'open'})
            sc_mod._call_tutor_tool({'action': 'open_phone'})
            sc_mod._call_tutor_tool({'action': 'explain', 'question': '伴写巡检'})
            sc_mod._call_tutor_tool({'action': 'camera_snap', 'question': '检查整页作业'})
            sc_mod._call_tutor_tool({'action': 'mistake_retest'})
        self.assertEqual(seen_touts, [12, 12, 27, 27, 27])

        # 3) Admin _load_yaml_file CSafeLoader + stat cache + deepcopy isolation + invalidation
        adm = _load_mod('r8_adm_fs6', '/usr/local/bin/thinksmart-admin.py')
        with tempfile.TemporaryDirectory() as td_a:
            yp = pathlib.Path(td_a) / 'sample.yaml'
            yp.write_text('LLM:\n  OpenAILLM:\n    model_name: gpt-test-1\n', encoding='utf-8')
            adm._YAML_FILE_CACHE.clear()
            with mock.patch.object(adm._yaml, 'load', wraps=adm._yaml.load) as spy_load:
                d1 = adm._load_yaml_file(yp)
                d1['LLM']['OpenAILLM']['model_name'] = 'MUTATED'
                d2 = adm._load_yaml_file(yp)
                self.assertEqual(d2['LLM']['OpenAILLM']['model_name'], 'gpt-test-1')
                self.assertEqual(spy_load.call_count, 1)
                yp.write_text('LLM:\n  OpenAILLM:\n    model_name: gpt-test-2\n', encoding='utf-8')
                d3 = adm._load_yaml_file(yp)
                self.assertEqual(d3['LLM']['OpenAILLM']['model_name'], 'gpt-test-2')
                self.assertEqual(spy_load.call_count, 2)

        # 4) Tutor mobile variant_one & voice api_tool background shielding
        tut = _load_mod('r8_tut_fs6', '/usr/local/bin/thinksmart-tutor.py')
        with tempfile.TemporaryDirectory() as td_t:
            tp = pathlib.Path(td_t)
            saved = {k: getattr(tut, k) for k in ('STATE_DIR', 'SNAP_DIR', 'HIST_PATH', 'PREFS_PATH', 'HUB_CFG_PATH')}
            had_sig = 'TUTOR_OPEN_SIGNAL' in os.environ
            old_sig = os.environ.get('TUTOR_OPEN_SIGNAL')
            try:
                tut.STATE_DIR = tp / 'state'
                tut.SNAP_DIR = tut.STATE_DIR / 'snaps'
                tut.HIST_PATH = tut.STATE_DIR / 'history.json'
                tut.PREFS_PATH = tut.STATE_DIR / 'prefs.json'
                tut.HUB_CFG_PATH = tp / 'hub.json'
                tut.HUB_CFG_PATH.write_text('{}', encoding='utf-8')
                os.environ['TUTOR_OPEN_SIGNAL'] = str(tp / 'open.json')

                async def _run_shield_tests():
                    eng = tut.TutorEngine()
                    app = tut.make_app(eng)
                    orig_wait = asyncio.wait

                    async def _fast_soft_wait(fs, timeout=None, **kw):
                        if timeout in (12.2, 21.5):
                            timeout = 0.05
                        return await orig_wait(fs, timeout=timeout, **kw)

                    done_var = asyncio.Event()

                    async def _slow_var(iid, **_kw):
                        await asyncio.sleep(0.14)
                        done_var.set()
                        return {'ok': True, 'session': {'id': 's_var_bg', 'title': '变式', 'status': 'ready', 'silent_start': True}}

                    done_chk = asyncio.Event()

                    async def _slow_chk(**_kw):
                        await asyncio.sleep(0.14)
                        done_chk.set()
                        return {'ok': True, 'note': '已核2题', 'cowrite': eng.cowrite.public_state()}

                    eng.cowrite.variant_from_item = _slow_var
                    eng.cowrite.check_now = _slow_chk

                    async with TestClient(TestServer(app)) as cli:
                        with mock.patch.object(tut.asyncio, 'wait', side_effect=_fast_soft_wait):
                            r1 = await cli.post(
                                '/api/tutor/mobile/upload',
                                json={'action': 'cowrite_remote_ctl', 'sub_action': 'variant_one', 'id': 'cw_1'},
                                headers={'X-TS-Internal': '1'},
                            )
                            j1 = await r1.json()
                            self.assertTrue(j1['ok'] and j1.get('pending'))
                            await asyncio.wait_for(done_var.wait(), timeout=1.0)

                            r2 = await cli.post(
                                '/api/tutor/tool',
                                json={'action': 'explain', 'question': '伴写巡检'},
                                headers={'X-TS-Internal': '1'},
                            )
                            j2 = await r2.json()
                            self.assertTrue(j2['ok'] and j2.get('pending'))
                            await asyncio.wait_for(done_chk.wait(), timeout=1.0)

                asyncio.run(_run_shield_tests())
            finally:
                for k, v in saved.items():
                    setattr(tut, k, v)
                if had_sig:
                    os.environ['TUTOR_OPEN_SIGNAL'] = old_sig
                else:
                    os.environ.pop('TUTOR_OPEN_SIGNAL', None)


    def test_slim1_kiosk_cache_journald_and_logger_bounds(self):
        import ast
        import shutil
        import stat

        ksrc = pathlib.Path('/usr/local/bin/thinksmart-kiosk.py').read_text(encoding='utf-8')
        tree = ast.parse(ksrc)
        fn_node = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == 'prune_webkit_cache'
        )
        main_node = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == 'main'
        )
        main_calls = [
            c.func.id for c in ast.walk(main_node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]
        self.assertIn('prune_webkit_cache', main_calls)

        ns = {'os': os, 'shutil': shutil, 'stat': stat}
        mod_ast = ast.Module(body=[fn_node], type_ignores=[])
        exec(compile(mod_ast, '/usr/local/bin/thinksmart-kiosk.py', 'exec'), ns)
        prune_fn = ns['prune_webkit_cache']

        with tempfile.TemporaryDirectory() as td:
            data_dir = pathlib.Path(td) / 'data'
            cache_dir = pathlib.Path(td) / 'cache'
            (data_dir / 'cookies.sqlite').parent.mkdir(parents=True)
            (data_dir / 'cookies.sqlite').write_bytes(b'keep_cookie')
            blobs = cache_dir / 'WebKitCache' / 'Version 17' / 'Blobs'
            blobs.mkdir(parents=True)
            (blobs / 'small.bin').write_bytes(b'x' * (2 * 1024 * 1024))
            self.assertFalse(prune_fn(str(cache_dir), {'CACHE_MAX_MB': '4'}))
            self.assertTrue((blobs / 'small.bin').exists())

            (blobs / 'big.bin').write_bytes(b'y' * (3 * 1024 * 1024))
            self.assertTrue(prune_fn(str(cache_dir), {'CACHE_MAX_MB': '4'}))
            self.assertFalse((cache_dir / 'WebKitCache').exists())
            self.assertEqual((data_dir / 'cookies.sqlite').read_bytes(), b'keep_cookie')

            outside = pathlib.Path(td) / 'outside'
            outside.mkdir()
            (outside / 'keep.bin').write_bytes(b'z' * (6 * 1024 * 1024))
            os.symlink(str(outside), str(cache_dir / 'WebKitCache'))
            self.assertFalse(prune_fn(str(cache_dir), {'CACHE_MAX_MB': '4'}))
            self.assertTrue((outside / 'keep.bin').exists())

        jconf = pathlib.Path('/etc/systemd/journald.conf.d/thinksmart.conf').read_text(encoding='utf-8')
        self.assertIn('SystemMaxUse=20M', jconf)
        self.assertIn('SystemMaxFileSize=4M', jconf)
        self.assertIn('MaxRetentionSec=7day', jconf)

        loverride = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/config/logger.py').read_text(encoding='utf-8')
        self.assertIn('rotation="2 MB"', loverride)
        self.assertIn('retention=2', loverride)
        self.assertIn('compression="gz"', loverride)
        self.assertNotIn('diagnose=True', loverride)
        self.assertEqual(loverride.count('diagnose=False'), 2)
        self.assertFalse(os.path.exists('/usr/bin/meta-backup'))


    def test_slim2_webkit_mem_mihomo_filter_chatdb_and_emmc(self):
        import ast
        import subprocess

        # 1) WebKit2 MemoryPressureSettings configured in thinksmart-kiosk.py
        ksrc = pathlib.Path('/usr/local/bin/thinksmart-kiosk.py').read_text(encoding='utf-8')
        ktree = ast.parse(ksrc)
        fn_node = next(
            n for n in ktree.body
            if isinstance(n, ast.FunctionDef) and n.name == 'configure_webkit_memory'
        )
        main_node = next(
            n for n in ktree.body
            if isinstance(n, ast.FunctionDef) and n.name == 'main'
        )
        main_calls = [
            c.func.id for c in ast.walk(main_node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]
        self.assertIn('configure_webkit_memory', main_calls)

        recorded = {}

        class _FakeMP:
            def set_memory_limit(self, v): recorded['limit'] = v
            def set_conservative_threshold(self, v): recorded['cons'] = v
            def set_strict_threshold(self, v): recorded['strict'] = v
            def set_kill_threshold(self, v): recorded['kill'] = v
            def set_poll_interval(self, v): recorded['poll'] = v

        class _FakeMPCls:
            @staticmethod
            def new():
                return _FakeMP()

        class _FakeWDM:
            @staticmethod
            def set_memory_pressure_settings(mp):
                recorded['applied'] = isinstance(mp, _FakeMP)

        class _FakeWebKit2:
            MemoryPressureSettings = _FakeMPCls
            WebsiteDataManager = _FakeWDM

        ns = {'WebKit2': _FakeWebKit2}
        exec(compile(ast.Module(body=[fn_node], type_ignores=[]), '/usr/local/bin/thinksmart-kiosk.py', 'exec'), ns)
        self.assertTrue(ns['configure_webkit_memory']({}))
        self.assertEqual(recorded['limit'], 480)
        self.assertEqual(recorded['cons'], 0.45)
        self.assertEqual(recorded['strict'], 0.70)
        self.assertEqual(recorded['kill'], 0.0)
        self.assertEqual(recorded['poll'], 30.0)
        self.assertTrue(recorded['applied'])

        # 2) Admin idle exit tightened to 300s
        asrc = pathlib.Path('/usr/local/bin/thinksmart-admin.py').read_text(encoding='utf-8')
        self.assertIn("os.environ.get('ADMIN_IDLE_EXIT_SEC', '300')", asrc)

        # 3) Mihomo journald info/debug filter active in systemd
        out = subprocess.check_output(['systemctl', 'show', '-p', 'LogFilterPatterns', 'mihomo.service'], text=True)
        self.assertIn('level=(info|debug)', out)

        # 4) Kernel dirty writeback, APT GzipIndexes, and logrotate compression
        wb = pathlib.Path('/etc/sysctl.d/95-emmc-writeback.conf').read_text(encoding='utf-8')
        self.assertIn('vm.dirty_writeback_centisecs = 1500', wb)
        self.assertIn('vm.dirty_expire_centisecs = 4500', wb)
        apt_c = pathlib.Path('/etc/apt/apt.conf.d/99-thinksmart-slim.conf').read_text(encoding='utf-8')
        self.assertIn('Acquire::GzipIndexes "true";', apt_c)
        self.assertIn('Keep-Downloaded-Packages "false";', apt_c)
        lr = pathlib.Path('/etc/logrotate.conf').read_text(encoding='utf-8')
        self.assertIn('\ncompress\n', lr)

        # 5) Xiaozhi chat_history override: WAL autocheckpoint & safe prune_old
        ch_file = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/agent/chat_history.py')
        ch_src = ch_file.read_text(encoding='utf-8')
        self.assertIn('PRAGMA wal_autocheckpoint=256', ch_src)
        self.assertIn('def prune_old(', ch_src)

        ch_tree = ast.parse(ch_src)
        cls_node = next(n for n in ch_tree.body if isinstance(n, ast.ClassDef) and n.name == 'ChatDB')
        import sqlite3
        import threading
        from typing import Dict, List, Optional
        c_ns = {
            'Path': pathlib.Path, 'sqlite3': sqlite3, 'threading': threading,
            'time': __import__('time'), 'json': json, 'os': os,
            'Dict': Dict, 'List': List, 'Optional': Optional,
            '_row': lambda r: {'id': r[0]},
        }
        exec(compile(ast.Module(body=[cls_node], type_ignores=[]), str(ch_file), 'exec'), c_ns)
        ChatDB = c_ns['ChatDB']
        with tempfile.TemporaryDirectory() as td:
            cdb = ChatDB(pathlib.Path(td) / 'chat.db')
            try:
                ids = [cdb.insert('d1', 'm1', 'user', f'msg-{i}', None, None) for i in range(10)]
                # Summarized only up to ids[3] (4th msg), keep_recent=4 -> keep at least 6..9, and never delete > ids[3]
                deleted = cdb.prune_old('d1', 'm1', keep_recent=4, max_id=ids[3])
                self.assertEqual(deleted, 4)
                rem = [r['id'] for r in cdb.recent('d1', 'm1', 20)]
                self.assertEqual(rem, ids[4:])
                # Unsummarized protection: if max_id=0, nothing is deleted even if > keep_recent
                self.assertEqual(cdb.prune_old('d1', 'm1', keep_recent=2, max_id=0), 0)
            finally:
                cdb.close()


    def test_slim3_intercom_accesslog_coredump_arena_and_cgroups(self):
        # 1) All 7 thinksmart aiohttp daemons suppress per-request access_log spam
        isrc = pathlib.Path('/usr/local/bin/thinksmart-intercom.py').read_text(encoding='utf-8')
        self.assertIn('web.AppRunner(app, access_log=None, shutdown_timeout=3.0)', isrc)

        # 2) Multi-threaded Python daemons set 1 MiB thread stack_size
        for fp in (
            '/usr/local/bin/thinksmart-home.py',
            '/usr/local/bin/thinksmart-qqmusic.py',
            '/usr/local/bin/thinksmart-admin.py',
            '/usr/local/bin/thinksmart-tutor.py',
            '/usr/local/bin/thinksmart-kiosk.py',
        ):
            self.assertIn('threading.stack_size(1048576)', pathlib.Path(fp).read_text(encoding='utf-8'), fp)

        # 3) System & user managers disable core dumps and cap glibc MALLOC_ARENA_MAX=2
        for cf in (
            '/etc/systemd/system.conf.d/99-thinksmart-slim.conf',
            '/etc/systemd/user.conf.d/99-thinksmart-slim.conf',
        ):
            txt = pathlib.Path(cf).read_text(encoding='utf-8')
            self.assertIn('DumpCore=no', txt)
            self.assertIn('DefaultLimitCORE=0', txt)
            self.assertIn('MALLOC_ARENA_MAX=2', txt)

        # 4) rtkit-daemon quiet drop-in & cgroup memory bounds on client + hub
        rt = pathlib.Path('/etc/systemd/system/rtkit-daemon.service.d/quiet.conf').read_text(encoding='utf-8')
        self.assertIn('LogLevelMax=notice', rt)
        tc = pathlib.Path('/etc/systemd/system/thinksmart-client.service.d/slim.conf').read_text(encoding='utf-8')
        self.assertIn('PYTHONDONTWRITEBYTECODE=1', tc)
        self.assertIn('MemoryHigh=220M', tc)
        self.assertIn('MemoryMax=320M', tc)
        xh_p = pathlib.Path('/home/ubuntu/.config/systemd/user/xiaozhi-hub.service.d/slim.conf')
        xh = xh_p.read_text(encoding='utf-8')
        self.assertEqual(xh_p.stat().st_uid, 1000)
        self.assertIn('PYTHONDONTWRITEBYTECODE=1', xh)
        self.assertIn('MemoryHigh=384M', xh)
        self.assertIn('MemoryMax=560M', xh)
        self.assertFalse(pathlib.Path('/usr/local/lib/thinksmart-aec-bridge/__pycache__').exists())


    def test_slim4_qqmusic_ssl_reuse_idle_trim_and_kiosk_pagecache(self):
        # 1) QQ Music reuses a single verified SSLContext across _safe_urlopen HTTPS calls
        qm = _load_mod('r8_qm_slim4', '/usr/local/bin/thinksmart-qqmusic.py')
        op1 = qm._get_safe_opener()
        op2 = qm._get_safe_opener()
        self.assertIs(op1, op2)
        self.assertIsNotNone(qm._SAFE_OPENER_SSL_CTX)
        # Monkeypatch path on urllib.request.urlopen still works cleanly
        called = []
        with mock.patch.object(qm.urllib.request, 'urlopen', side_effect=lambda req, timeout=6: called.append((req, timeout)) or 'ok'):
            self.assertEqual(qm._safe_urlopen('http://example.invalid', timeout=3), 'ok')
        self.assertEqual(len(called), 1)

        # 2) Idle heap trim helper present & works across all 4 always-on daemons
        qm._LAST_TRIM_MONO = 0.0
        self.assertTrue(qm._trim_idle_heap())
        self.assertFalse(qm._trim_idle_heap())  # rate-limited within 30s
        for fp in (
            '/usr/local/bin/thinksmart-monitor.py',
            '/usr/local/bin/thinksmart-intercom.py',
            '/usr/local/bin/thinksmart-study.py',
        ):
            self.assertIn('def _trim_idle_heap()', pathlib.Path(fp).read_text(encoding='utf-8'), fp)

        # 3) Kiosk single-page BFCache/AppCache disabled & -B flag in launcher
        ksrc = pathlib.Path('/usr/local/bin/thinksmart-kiosk.py').read_text(encoding='utf-8')
        self.assertIn('settings.set_enable_page_cache(False)', ksrc)
        self.assertIn('settings.set_enable_offline_web_application_cache(False)', ksrc)
        kasrc = pathlib.Path('/usr/local/bin/thinksmart-kiosk-app').read_text(encoding='utf-8')
        self.assertIn('/usr/bin/python3 -B /usr/local/bin/thinksmart-kiosk.py', kasrc)
        self.assertTrue(any(pathlib.Path('/var/backups').glob('thinksmart-*')))

    def test_slim5_hub_client_heap_trim_and_httpx_aiohttp_ssl_reuse(self):
        # 1) Global systemd manager drop-ins enforce both MALLOC_ARENA_MAX=2 and PYTHONDONTWRITEBYTECODE=1
        for cfg_p in (
            '/etc/systemd/system.conf.d/99-thinksmart-slim.conf',
            '/etc/systemd/user.conf.d/99-thinksmart-slim.conf',
        ):
            txt = pathlib.Path(cfg_p).read_text(encoding='utf-8')
            self.assertIn('MALLOC_ARENA_MAX=2', txt)
            self.assertIn('PYTHONDONTWRITEBYTECODE=1', txt)

        # 2) Post-boot & session-close heap reclamation hooks in xiaozhi-hub & thinksmart-client bridges
        hub_app = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/app.py').read_text(encoding='utf-8')
        self.assertIn('def _trim_hub_heap()', hub_app)
        self.assertIn('hub-postboot-trim', hub_app)
        conn_src = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/connection.py').read_text(encoding='utf-8')
        self.assertIn('def _trim_conn_heap()', conn_src)
        self.assertIn('_trim_conn_heap()', conn_src)
        aec_sc = pathlib.Path('/opt/thinksmart/client/current/thinksmart_client/bridge.py').read_text(encoding='utf-8')
        self.assertIn('def _trim_client_heap()', aec_sc)
        self.assertIn('qqmusic-bridge-trim', aec_sc)

        # 3) Shared SSLContext reuse in active httpx hub plugins (and mock-compatibility preserved)
        for rel in (
            'get_news_from_newsnow.py',
            'web_search.py',
        ):
            fp = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/plugins_func/functions') / rel
            src = fp.read_text(encoding='utf-8')
            self.assertIn('def _get_ssl_ctx()', src, rel)
            self.assertIn('_ORIG_ASYNC_CLIENT', src, rel)

        # 4) Monitor, Home, and Study reuse SSLContext + bounded TCPConnector, and Home has _trim_idle_heap
        mon = _load_mod('r8_mon_slim5', '/usr/local/bin/thinksmart-monitor.py')
        self.assertIs(mon._get_ssl_ctx(), mon._get_ssl_ctx())
        hm = _load_mod('r8_home_slim5', '/usr/local/bin/thinksmart-home.py')
        self.assertIs(hm._get_ssl_ctx(), hm._get_ssl_ctx())
        hm._LAST_TRIM_MONO = 0.0
        self.assertTrue(hm._trim_idle_heap())
        self.assertFalse(hm._trim_idle_heap())
        st = _load_mod('r8_study_slim5', '/usr/local/bin/thinksmart-study.py')
        self.assertIs(st._get_ssl_ctx(), st._get_ssl_ctx())


    def test_r9_self_service_dictation_and_photo_grading(self):
        # 1) Voice MCP bridge exposes action='dictation' on self.tutor.explain
        sc_mod = _load_mod('r8_aec_dict9', '/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py')
        tutor_tool = next(t for t in sc_mod.MUSIC_TOOLS if t['name'] == 'self.tutor.explain')
        act_enum = tutor_tool['inputSchema']['properties']['action']['enum']
        self.assertIn('dictation', act_enum)
        self.assertIn('自助听写', tutor_tool['description'])

        # 2) UI files contain dictation views & obey strict 400/700 font-weight + integer px rules
        idx_html = pathlib.Path('/usr/local/share/thinksmart-tutor/index.html').read_text(encoding='utf-8')
        mob_html = pathlib.Path('/usr/local/share/thinksmart-tutor/mobile.html').read_text(encoding='utf-8')
        for sym in ('id="tabDictation"', 'id="viewDictation"', 'id="dictAudio"', 'window.stopDictationPlayback', 'window.gradeDictationByCamera'):
            self.assertIn(sym, idx_html)
        for msym in ('id="mobDictCard"', 'window.mobStartDictText', 'window.mobStartDictImage', 'window.mobGradeDictImage'):
            self.assertIn(msym, mob_html)

        # 3) Parser & full end-to-end async HTTP + VLM + TTS + Mistake closed-loop in isolated temp dir
        tut = _load_mod('r8_tut_dict9', '/usr/local/bin/thinksmart-tutor.py')
        parsed = tut.parse_dictation_items(
            '今天听写作业：\n1. 光合作用、蜿蜒  锲而不舍（镂刻不停，比喻锲而不舍有恒心）\n'
            '英语：environment (n. 自然环境), take care of 照顾；well-known：adj. 著名的'
        )
        self.assertEqual([x['word'] for x in parsed], ['光合作用', '蜿蜒', '锲而不舍', 'environment', 'take care of', 'well-known'])
        self.assertNotIn('锲而不舍', parsed[2]['hint'])  # anti-spoiler scrubbed!
        self.assertEqual(parsed[3]['lang'], 'en')
        self.assertEqual(parsed[3]['hint'], 'n. 自然环境')
        self.assertEqual(parsed[4]['hint'], '照顾')

        import subprocess
        from aiohttp.test_utils import TestClient, TestServer
        valid_jpg = subprocess.check_output([
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=ivory:s=320x240',
            '-frames:v', '1', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ])
        b64_jpg = 'data:image/jpeg;base64,' + base64.b64encode(valid_jpg).decode('ascii')

        with tempfile.TemporaryDirectory() as td:
            tp = pathlib.Path(td)
            saved = {k: getattr(tut, k) for k in ('STATE_DIR', 'SNAP_DIR', 'HIST_PATH', 'PREFS_PATH')}
            had_sig = 'TUTOR_OPEN_SIGNAL' in os.environ
            old_sig = os.environ.get('TUTOR_OPEN_SIGNAL')
            try:
                tut.STATE_DIR = tp / 'state'
                tut.SNAP_DIR = tut.STATE_DIR / 'snaps'
                tut.HIST_PATH = tut.STATE_DIR / 'history.json'
                tut.PREFS_PATH = tut.STATE_DIR / 'prefs.json'
                tut.STATE_DIR.mkdir(parents=True, exist_ok=True)
                os.environ['TUTOR_OPEN_SIGNAL'] = str(tp / 'open.json')

                async def _run_dict_flow():
                    app = tut.make_app()
                    eng = app[tut._TUTOR_ENGINE_KEY]

                    async def _fake_synth(text: str, priority: bool = False) -> bytes:
                        return tut._pcm_to_wav(b'\x01\x00' * 1200)

                    eng.synthesize_wav = _fake_synth

                    async with TestServer(app) as srv, TestClient(srv) as cli:
                        H = {'Host': f'127.0.0.1:{srv.port}'}
                        H_INT = {'Host': f'127.0.0.1:{srv.port}', 'X-TS-Internal': '1'}

                        # A) Start dictation from text -> anti-spoiler masks words in /api/tutor/state
                        r_st = await cli.post('/api/tutor/dictation/start', headers=H, json={
                            'text': '锲而不舍（有恒心）、environment (n. 环境)、人迹罕至',
                            'interval_sec': 5,
                            'repeat_count': 2,
                            'en_mode': 'both',
                        })
                        j_st = await r_st.json()
                        self.assertTrue(j_st['ok'])
                        dc = j_st['dictation']
                        self.assertEqual(dc['total_count'], 3)
                        self.assertFalse(dc['revealed'])
                        self.assertEqual(dc['items'][0]['word'], '')
                        self.assertIn('□', dc['items'][0]['mask'])
                        self.assertIn('_', dc['items'][1]['mask'])

                        # B) English prompt modes & WAV TTS endpoint
                        self.assertIn('environment', eng.dictation.spoken_text_for(1, 1))
                        await cli.post('/api/tutor/dictation/control', headers=H, json={'action': 'config', 'en_mode': 'zh_prompt'})
                        sp_zh_p = eng.dictation.spoken_text_for(1, 1)
                        self.assertIn('请默写英文', sp_zh_p)
                        self.assertNotIn('environment', sp_zh_p)
                        r_wav = await cli.get('/api/tutor/dictation/tts?idx=1&pass=1', headers=H)
                        self.assertEqual(r_wav.status, 200)
                        self.assertEqual((await r_wav.read())[:4], b'RIFF')

                        # C) Grade handwritten sheet via mobile upload -> saves dc_*.jpg snap & marks ok/wrong/missing
                        fake_grade_json = json.dumps({
                            'summary': '整体字迹很认真，订正两个易错点就满分啦！',
                            'items': [
                                {'idx': 1, 'status': 'wrong', 'written': '契而不舍', 'note': '漏写了左边的金字旁'},
                                {'idx': 2, 'status': 'wrong', 'written': 'enviroment', 'note': '中间漏了字母 n'},
                                {'idx': 3, 'status': 'ok', 'written': '人迹罕至', 'note': ''},
                            ],
                        }, ensure_ascii=False)
                        with mock.patch.object(eng, '_post_chat_once', new=mock.AsyncMock(return_value=fake_grade_json)):
                            r_mg = await cli.post('/api/tutor/mobile/upload', headers=H_INT, json={
                                'action': 'dictation_grade',
                                'page_b64': b64_jpg,
                            })
                            j_mg = await r_mg.json()
                        self.assertTrue(j_mg['ok'])
                        self.assertTrue(j_mg['opened_phone_view'])
                        self.assertEqual(eng.open_ui_target, 'dictation')
                        self.assertEqual(j_mg['dictation']['phase'], 'graded')
                        self.assertEqual(j_mg['dictation']['ok_count'], 1)
                        self.assertEqual(j_mg['dictation']['wrong_count'], 2)
                        self.assertEqual(j_mg['dictation']['accuracy_pct'], 33)

                        # Snapshot image is accessible through /api/tutor/snap/<name>
                        pub_graded = eng.dictation.public_state()
                        self.assertTrue(pub_graded['snap_url'].startswith('/api/tutor/snap/dc_'))
                        r_snap = await cli.get(pub_graded['snap_url'], headers=H)
                        self.assertEqual(r_snap.status, 200)

                        # D) Enroll wrong words into mistake_book -> non-spoiler question + interactive quiz
                        r_em = await cli.post('/api/tutor/dictation/enroll_mistakes', headers=H, json={})
                        j_em = await r_em.json()
                        self.assertEqual(j_em['enrolled_count'], 2)
                        self.assertEqual(len(eng.mistakes), 2)
                        en_mb = next(m for m in eng.mistakes if m['dict_word'] == 'environment')
                        self.assertNotIn('environment', en_mb['question'])
                        self.assertEqual(en_mb['steps'][-1]['quiz']['ans'], 'environment')

                        # E) Retry wrong words only -> resets to 2-item 'ready' session with answers masked again
                        r_rw = await cli.post('/api/tutor/dictation/control', headers=H, json={'action': 'retry_wrong'})
                        j_rw = await r_rw.json()
                        self.assertEqual(j_rw['dictation']['total_count'], 2)
                        self.assertEqual(j_rw['dictation']['phase'], 'ready')
                        self.assertFalse(j_rw['dictation']['revealed'])

                        # F) Pull from mistakes pack & grade all 'ok' twice -> advances mistake to graduated=True!
                        r_mp = await cli.post('/api/tutor/dictation/start', headers=H, json={'pack_id': 'mistakes'})
                        self.assertEqual((await r_mp.json())['dictation']['total_count'], 2)
                        all_ok_json = json.dumps({
                            'summary': '全部订正全对！',
                            'items': [
                                {'idx': 1, 'status': 'ok', 'written': eng.dictation.items[0]['word'], 'note': ''},
                                {'idx': 2, 'status': 'ok', 'written': eng.dictation.items[1]['word'], 'note': ''},
                            ],
                        }, ensure_ascii=False)
                        with mock.patch.object(eng, '_post_chat_once', new=mock.AsyncMock(return_value=all_ok_json)):
                            await cli.post('/api/tutor/dictation/grade', headers=H, json={'image_b64': b64_jpg})
                            self.assertTrue(all(int(m['stage']) == 1 and not m['graduated'] for m in eng.mistakes))
                            await cli.post('/api/tutor/dictation/grade', headers=H, json={'image_b64': b64_jpg})
                            self.assertTrue(all(int(m['stage']) == 2 and m['graduated'] for m in eng.mistakes))

                        # G) Textbook photo extraction + Voice MCP /api/tutor/tool action='dictation'
                        ext_json = json.dumps({
                            'title': '七上第一单元词表',
                            'items': [{'word': '酝酿', 'hint': '造酒发酵，比喻事前讨论准备'}, {'word': '嘹亮', 'hint': '声音清晰响亮'}],
                        }, ensure_ascii=False)
                        with mock.patch.object(eng, '_post_chat_once', new=mock.AsyncMock(return_value=ext_json)):
                            r_ext = await cli.post('/api/tutor/mobile/upload', headers=H_INT, json={
                                'action': 'dictation_start',
                                'page_b64': b64_jpg,
                            })
                            j_ext = await r_ext.json()
                        self.assertEqual(j_ext['dictation']['total_count'], 2)

                        r_vt = await cli.post('/api/tutor/tool', headers=H_INT, json={
                            'action': 'dictation',
                            'question': '帮我报听写：渊博、人迹罕至、library 图书馆',
                        })
                        j_vt = await r_vt.json()
                        self.assertTrue(j_vt['ok'])
                        self.assertEqual(eng.dictation.public_state()['total_count'], 3)
                        self.assertEqual(eng.open_ui_target, 'dictation')

                asyncio.run(_run_dict_flow())
            finally:
                for k, v in saved.items():
                    setattr(tut, k, v)
                if had_sig:
                    os.environ['TUTOR_OPEN_SIGNAL'] = old_sig
                else:
                    os.environ.pop('TUTOR_OPEN_SIGNAL', None)


if __name__ == '__main__':
    unittest.main(verbosity=1)
