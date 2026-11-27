#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R89: Round 10 Deep Non-Xiaozhi Audit & Regression Tests."""
import ast
import asyncio
import datetime as dt
import importlib.machinery
import importlib.util
import json
import math
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

E = os.environ.get
ROOT = pathlib.Path(E('R89_ROOT') or E('STAGE_ROOT') or '/')


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


class TestHomeAndMonitorR89(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = _load_py_clean('r89_home', _p('/usr/local/bin/thinksmart-home.py'))
        cls.mon = _load_py_clean('r89_monitor', _p('/usr/local/bin/thinksmart-monitor.py'))

    def test_inbox_finite_guard_fast_catchup_and_unread_inclusion(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'inbox.json'
            now = time.time()
            bad_items = [
                {'id': 'a1b2c3d4e5f6', 'text': 'nan at', 'at': float('nan')},
                {'id': 'b1b2c3d4e5f6', 'text': 'old repeat', 'at': now - 100, 'tag': 'remind', 'due': 1000.0, 'repeat': 'daily', 'source': 'phone'},
            ]
            p.write_text(json.dumps(bad_items).replace('NaN', 'NaN'), 'utf-8')
            inb = self.home.Inbox(p)
            self.assertEqual(len(inb.items), 1)
            t0 = time.monotonic()
            fired = inb.fire_due()
            elapsed = time.monotonic() - t0
            self.assertEqual(len(fired), 1)
            self.assertLess(elapsed, 0.5)
            later = [m for m in inb.items if not m.get('fired') and m.get('due')]
            self.assertEqual(len(later), 1)
            self.assertGreater(later[0]['due'], time.time())

            # Unread older message remains included in /api/home/state msgs['recent'] when 3 newer acked phone messages exist
            inb.ack(fired[0]['id'])
            inb.add('Old unread phone msg', source='phone')
            for i in range(3):
                m_i = inb.add(f'Phone msg {i}', source='phone')
                inb.ack(m_i['id'])
            self.assertEqual(len(inb.unread()), 1)

    def test_home_http_unread_nan_and_countdown_validation(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            inb = self.home.Inbox(tdp / 'inbox.json')
            m_unr = inb.add('妈妈还没收到', source='phone')
            for i in range(3):
                r_m = inb.add(f'Phone msg {i}', source='phone')
                inb.ack(r_m['id'])
            cds = self.home.CountdownStore(tdp / 'cds.json')

            async def _run():
                with mock.patch.object(self.home, '_ACCEL_CACHE', {'dir': None, 'checked_at': 0.0, 'sample': None, 'sample_dir': None, 'sample_at': 0.0}), \
                     mock.patch.object(self.home, '_get_json', mock.AsyncMock(return_value={})):
                    app = self.home.make_app(inbox=inb, countdowns=cds)
                    srv = TestServer(app)
                    cli = TestClient(srv)
                    await cli.start_server()
                    try:
                        h = {'Host': '127.0.0.1:8095'}
                        hi = {'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'}

                        # Unread item must be in state messages.recent even behind 3 read messages
                        r_st = await cli.get('/api/home/state', headers=h)
                        self.assertEqual(r_st.status, 200)
                        st = await r_st.json()
                        self.assertEqual(st['messages']['unread'], 1)
                        self.assertTrue(any(m['id'] == m_unr['id'] for m in st['messages']['recent']))

                        # body_of rejects NaN / Infinity
                        r_nan = await cli.post('/api/home/countdowns', data=b'{"title":"test","kind":"yearly_solar","month":NaN,"day":1}', headers={**h, 'Content-Type': 'application/json'})
                        self.assertEqual(r_nan.status, 400)

                        # Countdown bool & 1e400 rejection return Chinese error
                        r_bool = await cli.post('/api/home/countdowns', json={'title': 'test', 'kind': 'yearly_solar', 'month': True, 'day': 1}, headers=h)
                        self.assertEqual(r_bool.status, 400)
                        r_inf = await cli.post('/api/home/countdowns', data=b'{"title":"test","kind":"yearly_solar","month":1e400,"day":1}', headers={**h, 'Content-Type': 'application/json'})
                        self.assertEqual(r_inf.status, 400)
                        j_inf = await r_inf.json()
                        self.assertNotIn('infinity', j_inf.get('error', '').lower())

                        # get_countdown_convert invalid query returns Chinese message
                        r_conv = await cli.get('/api/home/countdowns/convert?y=abc&m=1&d=1', headers=h)
                        self.assertEqual(r_conv.status, 400)
                        self.assertEqual((await r_conv.json()).get('error'), '日期参数无效')

                        # post_countdown_tool accepts Chinese lunar/solar strings without crashing
                        r_tl = await cli.post('/api/home/countdowns/tool', json={'action': 'add', 'title': '中秋家宴', 'kind': 'yearly_lunar', 'lunar_month': '八月', 'lunar_day': '十五'}, headers=hi)
                        self.assertEqual(r_tl.status, 200)
                        j_tl = await r_tl.json()
                        self.assertTrue(j_tl.get('ok'), j_tl)
                        self.assertEqual(j_tl['item']['lunar_month'], 8)
                        self.assertEqual(j_tl['item']['lunar_day'], 15)
                    finally:
                        await cli.close()

            asyncio.run(_run())

        hm_src = _p('/usr/local/bin/thinksmart-home.py').read_text('utf-8')
        self.assertIn(') < 0.10:', hm_src)

    def test_monitor_traffic_ledger_rollover_complete_and_unavailable_seed(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            srv_f = tdp / 'servers.json'
            srv_f.write_text(json.dumps([{'id': 4, 'name': 'Node4', 'traffic_reset_day': 1, 'traffic_limit_bytes': 1000000000000}]), 'utf-8')
            tr_f = tdp / 'traffic.json'
            tr_f.write_text(json.dumps({
                'server_id': 3,
                'local_rules': {
                    '4': {'server_id': 4, 'reset_day': 1, 'limit_bytes': 1000000000000, 'complete_from': '2026-10-01T00:00:00+08:00', 'mode': 'both'}
                },
            }), 'utf-8')
            with mock.patch.object(self.mon, 'DisplayControl', mock.MagicMock):
                st = self.mon.Monitor(assets=tdp, state_dir=tdp)
            # Cycle started 2026-11-01 00:00+08:00; last sample was 2026-10-31 23:30+08:00; now is 2026-11-10 (weeks into cycle)
            prev_cycle_start = dt.datetime(2026, 10, 1, 0, 0, tzinfo=self.mon.SH_TZ).isoformat()
            prev_at = dt.datetime(2026, 10, 31, 23, 30, tzinfo=self.mon.SH_TZ).timestamp()
            st._traffic_ledger = {'4': {'from': prev_cycle_start, 'acc_in': 100, 'acc_out': 100, 'last_in': 100, 'last_out': 100, 'complete': True, 'updated_at': prev_at}}
            now_late = dt.datetime(2026, 11, 10, 12, 0, tzinfo=self.mon.SH_TZ).timestamp()
            extra = st._build_local_extra_traffic(now_late)
            self.assertIn('4', extra)
            self.assertFalse(extra['4']['complete'])
            self.assertIsNone(extra['4']['remaining_bytes'])

            # Within 1h of rollover complete may be True, UNLESS prev_at is ahead of cycle start (negative delta)
            now_near = dt.datetime(2026, 11, 1, 0, 30, tzinfo=self.mon.SH_TZ).timestamp()
            self.assertTrue(st._build_local_extra_traffic(now_near)['4']['complete'])
            st._traffic_ledger['4']['updated_at'] = dt.datetime(2026, 11, 2, 0, 0, tzinfo=self.mon.SH_TZ).timestamp()
            self.assertFalse(st._build_local_extra_traffic(now_near)['4']['complete'])

            # build_voice_summary -> _fleet_traffic_sentence skips available=False seed entry without KeyError
            sent = self.mon.build_voice_summary(
                {
                    'servers': [{'id': 4, 'name': 'Node4', 'alive': True, 'traffic_reset_day': 1, 'traffic_limit_bytes': 1000000000000}],
                    'traffic': {'available': False},
                    'extra_traffic': {'4': {'available': False, 'server_id': 4, 'limit_bytes': 1000000000000, 'reset_day': 1}},
                    'carriers': [],
                },
                query='各服务器月流量',
                focus='traffic',
            )
            self.assertIsInstance(sent, str)

    def test_frontend_overlay_home_monitor_contracts(self):
        ho = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('const hdrs = (orEtag && !orCand) ?', ho)
        self.assertIn("forceCloseSheets();   // 闹钟（#ring）盖在当前页面上面，停下后还在原来的页面", ho)
        self.assertIn("'If-None-Match': orEtag", ho)
        hm = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('const ss = sleepNow();', hm)
        mon_h = _p('/usr/local/share/thinksmart-monitor/index.html').read_text('utf-8')
        self.assertIn("text($('dmQuotaVal'),'待同步 / '+lTxt);", mon_h)
        self.assertIn('er?.available!==false&&er?.limit_bytes>0', mon_h)


class TestDaemonsAndUsbR89(unittest.TestCase):
    def test_vcnl4200_reset_bus_cooldown_honored(self):
        px = _load_py_clean('r89_prox', _p('/usr/local/bin/vcnl4200-proximity-daemon.py'))
        obj = object.__new__(px.ProximitySensor)
        obj.bus = None
        obj.bus_failed = False
        obj.als_enabled = True
        obj.sensor_enabled = True
        obj._sudo = mock.MagicMock()
        obj._bus = mock.MagicMock()
        obj._reset_bus(5.0)
        self.assertIsNone(obj.i2c_get(0x08))
        self.assertFalse(obj.i2c_set(0x03, 0x01))
        self.assertFalse(obj.i2c_set_word(0x03, 0x08CA))
        obj._sudo.assert_not_called()
        obj._bus.assert_not_called()

    def test_watchdog_airplay_blanker_udev_sources(self):
        gpu_src = _p('/usr/local/bin/gpu-watchdog.py').read_text('utf-8')
        self.assertIn('except subprocess.SubprocessError', gpu_src)
        ib_src = _p('/usr/local/bin/idle-blanker.py').read_text('utf-8')
        self.assertIn('((had_preview or mode_explicitly_saved) and read_als() is None)', ib_src)
        ap_src = _p('/usr/local/bin/thinksmart-airplay.py').read_text('utf-8')
        self.assertIn('if launch_ino and launch_ino != tuned_ino:', ap_src)

        with tempfile.TemporaryDirectory() as td:
            ap = _load_py_clean('r89_ap', _p('/usr/local/bin/thinksmart-airplay.py'))
            lnk = pathlib.Path(td) / 'uxplay-audio-active'
            lnk.symlink_to('/tmp/nonexistent-target')
            with mock.patch.object(ap, '_audio_flag_path', return_value=lnk):
                ap.set_audio_active(False)
            self.assertFalse(lnk.is_symlink())

        rules = _p('/etc/udev/rules.d/99-usb-automount.rules').read_text('utf-8')
        self.assertIn('ACTION=="add|change"', rules)
        self.assertIn('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', rules)
        self.assertIn('0xef', rules)

        wd_mod = _load_script('r89_usb_wd', _p('/usr/local/sbin/thinksmart-usb-watchdog'))
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            role_f = root / 'role'
            usb1_d = root / 'usb1'
            mnt_d = root / 'mnt_usb'
            lock_f = root / 'usb.lock'
            fake_mode = root / 'fake_usb_mode.sh'
            mnt_d.mkdir()
            role_f.write_text('host\n', 'utf-8')
            fake_mode.write_text('#!/bin/sh\n[ "$1" = "device" ] && exit 1\nexit 0\n', 'utf-8')
            fake_mode.chmod(0o755)
            dog = wd_mod.UsbWatchdog(
                role_path=str(role_f), usb1_path=str(usb1_d), usb_mode_bin=str(fake_mode),
                mnt_dir=str(mnt_d), lock_path=str(lock_f), cooldown_sec=0.0, settle_sec=0.0, role_gap_sec=0.0,
            )
            ok = dog.recover('HC died')
            self.assertFalse(ok)
            self.assertTrue(bool(dog._dead_kmsg_pending))


class TestTutorAdminSetScriptsR89(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adm = _load_py_clean('r89_admin', _p('/usr/local/bin/thinksmart-admin.py'))
        cls.tut = _load_py_clean('r89_tutor', _p('/usr/local/bin/thinksmart-tutor.py'))
        cls.sk = _load_script('r89_set_keys', _p('/usr/local/sbin/thinksmart-set-search-keys'))

    def test_set_search_keys_creates_missing_yaml_and_web_search_section(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            bak = tdp / 'bak'
            cfg1 = tdp / 'missing.yaml'
            self.sk.update_config_keys(str(cfg1), {'metaso_key': 'mk-test-1234', 'amap_key': 'ak-test-5678'}, backup_dir=str(bak), owner_user='')
            txt1 = cfg1.read_text('utf-8')
            self.assertIn("plugins:\n  web_search:\n", txt1)
            self.assertIn("    metaso_key: 'mk-test-1234'\n", txt1)
            self.assertIn("    amap_key: 'ak-test-5678'\n", txt1)
            self.assertTrue(txt1.endswith('\n'))

            cfg2 = tdp / 'sparse.yaml'
            cfg2.write_text('plugins: {}\nLLM:\n  selected: OpenAILLM\n', 'utf-8')
            self.sk.update_config_keys(str(cfg2), {'metaso_key': 'mk-new-9999'}, backup_dir=str(bak), owner_user='')
            txt2 = cfg2.read_text('utf-8')
            self.assertIn("plugins:\n  web_search:\n    metaso_key: 'mk-new-9999'\nLLM:\n", txt2)

    def test_set_admin_password_rejects_eof_and_keeps_invariants(self):
        pw_sh = _p('/usr/local/sbin/thinksmart-set-admin-password').read_text('utf-8')
        self.assertIn('if ! IFS= read -r PW && [ -z "$PW" ];', pw_sh)
        self.assertIn('LC_ALL=C.UTF-8', pw_sh)
        self.assertIn('setpriv --reuid=ubuntu', pw_sh)

    def test_admin_finite_float_and_json_body(self):
        self.assertEqual(self.adm._int_or('inf', 7, 1, 10), 7)
        self.assertEqual(self.adm._int_or(float('nan'), 7, 1, 10), 7)
        self.assertEqual(self.adm._int_or(True, 7, 1, 10), 7)
        with self.assertRaises(ValueError):
            self.adm._finite_float('nan')
        with self.assertRaises(ValueError):
            self.adm._finite_float('inf')
        with self.assertRaises(ValueError):
            self.adm._finite_float(True)

        async def _run():
            req = make_mocked_request('POST', '/admin/api/test')
            with mock.patch.object(self.adm, 'read_limited', mock.AsyncMock(return_value=b'{"speed": NaN}')), \
                 self.assertRaises(self.adm.ApiError):
                await self.adm.json_body(req)
        asyncio.run(_run())

        adm_html = _p('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertGreaterEqual(adm_html.count('if (seq !== pv.seq) return;'), 3)

    def test_tutor_rotate_progress_json_and_mobile_embed(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            with mock.patch.object(self.tut, 'STATE_DIR', tdp), \
                 mock.patch.object(self.tut, 'PREFS_PATH', tdp / 'prefs.json'):
                cam = self.tut.UVCCameraManager()
                cam.rotate = 0
                self.assertEqual(cam.set_rotate('bad'), 90)
                self.assertEqual(cam.set_rotate(True), 180)
                self.assertEqual(cam.set_rotate(float('inf')), 270)

                psm = self.tut.PhoneStudioManager()
                psm.rotate = 0
                self.assertEqual(psm.set_rotate('bad'), 90)
                self.assertEqual(psm.set_rotate(True), 180)

                eng = object.__new__(self.tut.TutorEngine)
                eng.sessions = {}
                eng._session_by_id = {}
                eng.active_session_id = 's1'
                eng.sessions['s1'] = {'id': 's1', 'seq': 1, 'progress': {'weak_step': 0}}
                eng._get_or_hydrate_session = lambda sid: eng.sessions.get(sid)
                eng.public_state = lambda: {}
                res = eng.update_progress('s1', {'reset_run': True, 'current_step': 'bad', 'weak_step': True, 'solo_sec': float('inf')})
                self.assertTrue(res['ok'])
                self.assertEqual(eng.sessions['s1']['progress']['current_step'], 1)
                self.assertEqual(eng.sessions['s1']['progress']['weak_step'], 0)

        tut_py = _p('/usr/local/bin/thinksmart-tutor.py').read_text('utf-8')
        self.assertIn('retrieved = False', tut_py)
        mob_html = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('window.tsEmbedSetActive', mob_html)
        tut_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('let camNextTimer = null;', tut_html)


class TestQqMusicIntercomStudyR89(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq = _load_py_clean('r89_qq', _p('/usr/local/bin/thinksmart-qqmusic.py'), strip_names=('engine',))
        cls.ic = _load_py_clean('r89_ic', _p('/usr/local/bin/thinksmart-intercom.py'))
        cls.st = _load_py_clean('r89_st', _p('/usr/local/bin/thinksmart-study.py'))

    def test_qqmusic_musicu_code_cookies_sleep_and_bounds(self):
        self.assertEqual(self.qq._musicu_code({'code': 2001}, {'code': 0}), 2001)
        self.assertEqual(self.qq._musicu_code({'code': -2}, {'code': 0}), -2)
        self.assertEqual(self.qq._musicu_code({'code': 0}, {'code': 0}), 0)
        self.assertEqual(self.qq._musicu_code({}, {'code': 0}), 0)

        with mock.patch.object(self.qq, 'load_config', return_value={'uin': '12345', 'qm_keyst': 'NEWMK', 'cookies_str': 'uin=12345; qm_keyst=; qqmusic_key='}):
            hdrs = self.qq._qq_headers()
            self.assertIn('qm_keyst=NEWMK', hdrs['Cookie'])
            self.assertIn('qqmusic_key=NEWMK', hdrs['Cookie'])

        with mock.patch.object(self.qq, 'CONFIG_PATH', pathlib.Path('/nonexistent/cfg.json')), \
             mock.patch.object(self.qq, 'SLEEP_STATE_PATH', pathlib.Path('/nonexistent/r89_sleep.json')), \
             mock.patch.object(self.qq, '_write_sleep_state'):
            with tempfile.TemporaryDirectory() as td_q:
                tdqp = pathlib.Path(td_q)
                with mock.patch.object(self.qq, 'SLEEP_STATE_PATH', tdqp / 'sleep.json'):
                    eng = object.__new__(self.qq.QQMusicEngine)
                    eng._lock = mock.MagicMock()
                    eng._lock.__enter__ = lambda s=None: eng._lock
                    eng._lock.__exit__ = lambda s=None, *_a: None
                    eng.volume = 25
                    eng.playing = False
                    eng.paused = True
                    eng.index = -1
                    eng.queue = []
                    eng.queue_seq = 1
                    eng._cached_q_seq = 0
                    eng._sleep_state = {'sleep_volume': 25}
                    eng._preview = {'prev_volume': 65, 'volume': 25, 'label': '雨夜壁炉'}
                    eng.sleep_mode = 'time'
                    eng.sleep_deadline = time.monotonic() + 30.0
                    eng.sleep_total = 1
                    eng.sleep_tracks_left = 0
                    eng.sleep_fade = 2.0
                    eng.sleep_night = False
                    eng.sleep_started_mono = time.monotonic()
                    eng.sleep_ends_wall = time.time() + 30.0
                    eng.ambient_preset = 'rain'
                    eng.ambient_layer = None
                    eng.set_volume = lambda v: setattr(eng, 'volume', v)
                    eng.set_ambient = lambda *_a, **_k: {'ok': True}
                    eng._sleep_session_end = mock.MagicMock()

                    # Cancel during preview must keep preview intact
                    r_can = eng.set_sleep(cancel=True)
                    self.assertTrue(r_can['ok'])
                    self.assertIsNotNone(eng._preview)
                    # Valid sleep set replaces preview and saves prev_volume into restore
                    r_set = eng.set_sleep(minutes=15)
                    self.assertTrue(r_set['ok'])
                    self.assertIsNone(eng._preview)
                    self.assertEqual(eng._sleep_state['restore']['day_volume'], 65)

                    # Bad seek / extend / preview inputs never raise OverflowError / bool coercion
                    self.assertFalse(eng.seek(10 ** 400)['ok'])
                    r_ext_bool = eng.sleep_session_extend(True)
                    self.assertTrue(r_ext_bool['ok'])
                    self.assertIn('再放 30 分钟', r_ext_bool['message'])
                    eng.sleep_mode = None
                    eng._preview = None
                    res_pv = eng.sleep_preview('雨声', volume=True, seconds=float('nan'))
                    self.assertTrue(res_pv.get('ok'), res_pv)
                    self.assertEqual(eng._preview['volume'], 25)
                    self.assertEqual(res_pv['preview']['remaining'], 30)

        qq_html = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        self.assertIn('`${s.mode}:${s.total}:${s.remaining}:${s.tracks_left}`', qq_html)

    def test_intercom_stop_call_shielded_and_kiosk_parts(self):
        srv = object.__new__(self.ic.IntercomServer)
        srv.in_call = True
        srv.call_owner = 'c1'
        srv.ui_proc = None
        srv.spk_proc = None
        srv.ui_task = None
        srv.saved_ambient = None
        srv.saved_ambient_owned = False
        srv.saved_spk_mute = False
        srv.saved_spk_vol = '45%'
        events = []

        with tempfile.TemporaryDirectory() as td:
            cflag = pathlib.Path(td) / 'intercom-call-active'
            cflag.write_text('{}', 'utf-8')
            mflag = pathlib.Path(td) / 'uxplay-mirroring-active'

            async def _slow_mic():
                await asyncio.sleep(0.05)
                events.append('mic')

            async def _slow_vol(v):
                await asyncio.sleep(0.05)
                events.append(('vol', v, cflag.exists()))

            async def _bcast():
                events.append('bcast')

            srv.stop_mic_if_idle = _slow_mic
            srv.set_speaker_volume = _slow_vol
            srv.set_speaker_mute = mock.AsyncMock()
            srv.wake_screen = lambda hold=False: events.append(('wake', hold))
            srv.set_webkit_paused = lambda paused: events.append(('pause', paused))
            srv.broadcast_status = _bcast

            async def _run():
                with mock.patch.object(self.ic, 'INTERCOM_CALL_FLAG', cflag), \
                     mock.patch.object(self.ic, 'MIRROR_FLAG_PATH', mflag), \
                     mock.patch.object(self.ic.aiohttp, 'ClientSession') as m_sess:
                    m_sess.return_value.__aenter__ = mock.AsyncMock(return_value=mock.MagicMock())
                    m_sess.return_value.__aexit__ = mock.AsyncMock(return_value=False)
                    t = asyncio.create_task(srv._stop_call(fast=True))
                    await asyncio.sleep(0.01)
                    t.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await t
                    self.assertFalse(cflag.exists())

            asyncio.run(_run())
            self.assertIn(('vol', '45%', True), events)
            self.assertIn(('wake', False), events)
            self.assertIn(('pause', False), events)

        ic_src = _p('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
        self.assertIn('function bindVideoTrackEnd(', ic_src)
        self.assertIn('for p in parts[:4]', ic_src)

    def test_study_focus_unsynced_and_parent_peer_guard(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = pathlib.Path(td)
            comp = object.__new__(self.st.Companion)
            comp.last = None
            comp.focus = {
                'phase': 'focus',
                'minutes': 25,
                'break_minutes': 5,
                'started_at': 1000.0,
                'ends_at': 2500.0,
                'ends_mono': time.monotonic() + 600.0,
                'with_study': True,
                'unsynced': False,
            }
            with mock.patch.object(self.st, '_boot_clock_unsynced', return_value=False):
                comp.focus_public()
            self.assertTrue(comp.focus.get('unsynced'))

            async def _run():
                with mock.patch.object(self.st, 'STATE', tdp), \
                     mock.patch.object(self.st, 'CONFIG_PATH', tdp / 'config.json'), \
                     mock.patch.object(self.st, 'FOCUS_PATH', tdp / 'focus.json'), \
                     mock.patch.object(self.st, 'LOG_DIR', tdp / 'log'):
                    (tdp / 'log').mkdir(exist_ok=True)
                    app = self.st.app_for(comp)
                    app.cleanup_ctx.clear()
                    app.on_startup.clear()
                    app.on_cleanup.clear()
                    app.on_shutdown.clear()
                    srv = TestServer(app)
                    cli = TestClient(srv)
                    await cli.start_server()
                    try:
                        # Loopback peer succeeds with localhost host
                        r_ok = await cli.get('/api/parent/state', headers={'Host': 'localhost:8094'})
                        self.assertEqual(r_ok.status, 200)
                    finally:
                        await cli.close()
                    # Verify global remote rejected directly via middleware even with Host: localhost:8094
                    mw = app.middlewares[0]
                    tr_ext = mock.MagicMock()
                    tr_ext.get_extra_info.return_value = ('8.8.8.8', 44444)
                    req_ext = make_mocked_request('GET', '/api/parent/state', headers={'Host': 'localhost:8094'}, transport=tr_ext)
                    with self.assertRaises(self.st.web.HTTPForbidden):
                        await mw(req_ext, lambda _r: self.st.web.json_response({'ok': True}))

            asyncio.run(_run())
            st_src = _p('/usr/local/bin/thinksmart-study.py').read_text('utf-8')
            self.assertIn('def _is_trusted_peer(peer)', st_src)
            self.assertIn('if not _is_trusted_peer(request.remote)', st_src)


if __name__ == '__main__':
    unittest.main()
