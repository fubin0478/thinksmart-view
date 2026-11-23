#!/opt/thinksmart-addons/venv/bin/python
"""Regression suite for R90 streamlining & optimization (7 approved items).

Supports STAGE_ROOT env override (defaults to '/') so it can run both pre-deploy
against /tmp/r90_stage and post-deploy inside /opt/thinksmart-addons/.
"""
import asyncio
import importlib.util
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import unittest
from unittest import mock

STAGE_ROOT = pathlib.Path(os.environ.get('STAGE_ROOT', '/'))


def _p(abs_path: str) -> pathlib.Path:
    rel = abs_path.lstrip('/')
    cand = STAGE_ROOT / rel
    return cand if cand.exists() else pathlib.Path(abs_path)


def _load_py_clean(mod_name: str, path: pathlib.Path, strip_names=()):
    if '/usr/local/bin' not in sys.path:
        sys.path.insert(0, '/usr/local/bin')
    src = path.read_text(encoding='utf-8')
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    for k in strip_names:
        mod.__dict__.pop(k, None)
    return mod


class TestR90Streamlining(unittest.TestCase):
    def test_01_socket_units_and_service_restart_policy(self):
        adm_sock = _p('/etc/systemd/system/thinksmart-admin.socket').read_text('utf-8')
        tut_sock = _p('/etc/systemd/system/thinksmart-tutor.socket').read_text('utf-8')
        self.assertIn('ListenStream=127.0.0.1:8097', adm_sock)
        self.assertIn('NoDelay=true', adm_sock)
        self.assertIn('WantedBy=sockets.target', adm_sock)
        self.assertIn('ListenStream=127.0.0.1:8096', tut_sock)
        self.assertIn('NoDelay=true', tut_sock)
        self.assertIn('WantedBy=sockets.target', tut_sock)

        adm_svc = _p('/etc/systemd/system/thinksmart-admin.service').read_text('utf-8')
        tut_svc = _p('/etc/systemd/system/thinksmart-tutor.service').read_text('utf-8')
        self.assertIn('Restart=on-failure', adm_svc)
        self.assertNotIn('Restart=always', adm_svc)
        self.assertIn('NoNewPrivileges=yes', adm_svc)
        self.assertIn('MemoryMax=220M', adm_svc)

        self.assertIn('Restart=on-failure', tut_svc)
        self.assertNotIn('Restart=always', tut_svc)
        self.assertIn('ReadWritePaths=-/run/user/1000', tut_svc)
        self.assertIn('ProtectKernelLogs=yes', tut_svc)
        self.assertIn('MemoryMax=200M', tut_svc)

    def test_02_admin_and_tutor_socket_activation_helpers(self):
        adm_path = _p('/usr/local/bin/thinksmart-admin.py')
        adm_src = adm_path.read_text('utf-8')
        self.assertIn("BIND = os.environ.get('ADMIN_BIND', '127.0.0.1')", adm_src)
        self.assertIn('ADMIN_IDLE_EXIT_SEC', adm_src)
        self.assertIn('web.SockSite', adm_src)
        adm = _load_py_clean('r90_admin', adm_path)
        self.assertIsNone(adm._systemd_listen_socket())
        a = adm.Admin()
        self.assertFalse(adm._admin_busy(a))
        a.open_clash_reqs.add(object())
        self.assertTrue(adm._admin_busy(a))

        tut_path = _p('/usr/local/bin/thinksmart-tutor.py')
        tut_src = tut_path.read_text('utf-8')
        self.assertIn("os.environ.get('TUTOR_WARM_GOLDEN', '0')", tut_src)
        self.assertIn('TUTOR_IDLE_EXIT_SEC', tut_src)
        self.assertIn('web.SockSite', tut_src)
        tut = _load_py_clean('r90_tutor', tut_path)
        self.assertIsNone(tut._systemd_listen_socket())
        with tempfile.TemporaryDirectory() as td:
            sig = pathlib.Path(td) / 'tutor-open.json'
            with mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(sig), 'TUTOR_STATE': td}):
                eng = tut.TutorEngine()
                self.assertFalse(tut._tutor_busy(eng))
                eng.bg_tasks.add(mock.Mock(done=lambda: False))
                self.assertTrue(tut._tutor_busy(eng))
                eng.trigger_open_ui('phone')
                self.assertTrue(sig.is_file())
                data = json.loads(sig.read_text('utf-8'))
                self.assertEqual(data['open_ui_seq'], 1)
                self.assertEqual(data['target'], 'phone')

    def test_03_zero_wake_overlay_js(self):
        js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn("'8096'", js)
        self.assertIn('pollTutorBadge', js)
        self.assertIn('!inTutorNow', js)
        self.assertIn('function handleTutorWake(tw)', js)
        self.assertIn("if (document.hidden || !homeOk || !forceCheck) return;", js)
        self.assertNotIn('tutorBadgeTimer = setTimeout(pollTutorBadge, stillNow', js)
        self.assertNotIn('tutorBadgeTimer = setTimeout(pollTutorBadge, 3600)', js)
        self.assertIn("? '?hotplug=1'", js)
        self.assertIn("12000 : 8000", js)

    def test_04_alt_tunnel_skipped_only_when_disabled_on_live_host(self):
        mon_path = _p('/usr/local/bin/thinksmart-monitor.py')
        mon_src = mon_path.read_text('utf-8')
        self.assertIn("multi-user.target.wants/thinksmart-intercom-tunnel-alt.service", mon_src)
        mon = _load_py_clean('r90_mon', mon_path)
        with tempfile.TemporaryDirectory() as td:
            cg = pathlib.Path(td) / 'cg'
            pr = pathlib.Path(td) / 'pr'
            rn = pathlib.Path(td) / 'rn'
            cg.mkdir(); pr.mkdir(); rn.mkdir()
            (cg / 'thinksmart-intercom-tunnel-alt.service').mkdir()
            (cg / 'thinksmart-intercom-tunnel-alt.service' / 'cgroup.procs').write_text('999\n', encoding='utf-8')
            links = mon.probe_local_links(cgroup_dir=cg, proc_dir=pr, run_dir=rn)
            self.assertEqual([x['id'] for x in links], ['tun_bak'])

    def test_05_study_merged_into_focus_and_messages_hidden_from_dock(self):
        apps_j = json.loads(_p('/usr/local/share/thinksmart-home/apps.json').read_text('utf-8'))
        apps = apps_j['apps']
        self.assertEqual(len(apps), 9)
        by_id = {a['id']: a for a in apps}
        self.assertEqual(by_id['focus']['title'], '学习专注')
        self.assertTrue(by_id['study'].get('hidden'))
        self.assertTrue(by_id['messages'].get('hidden'))
        ids = [a['id'] for a in apps]
        self.assertEqual(ids.index('tutor'), ids.index('server') + 1)

        home_h = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('if (!a.hidden)', home_h)
        self.assertIn('dock-5x2', home_h)
        self.assertIn('id="tpStudyLiveBar"', home_h)
        self.assertIn('id="tpStudyLiveBtn"', home_h)
        self.assertIn('id="trStudyBtn"', home_h)
        self.assertIn('id="sActMsgBtn"', home_h)
        self.assertIn('sideMsgHeader', home_h)
        self.assertIn('#dock .qk { grid-row: 2; grid-column: span 2;', home_h)

        idx_p = home_h.index('2026-11-20 Claude 竖版')
        p_css = home_h[idx_p:home_h.index('</style>', idx_p)]
        self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', p_css)) <= {'400', '700'})
        self.assertEqual(re.findall(r'\d+\.\d+px', p_css), [])
        sizes = [int(v) for v in re.findall(r'font-size:\s*(\d+)px', p_css)]
        self.assertTrue(sizes and min(sizes) >= 15)

    def test_06_tutor_ui_streamlined_and_strict_css(self):
        tut_h = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for sym in ('id="btnToggleCustomQ"', 'id="btnPhoneMore"', 'id="phoneAdvBox"',
                    'id="btnCamMore"', 'id="camAdvBox"', 'id="btnBoardMore"',
                    'id="sumMoreWrap"', 'id="btnToggleSumMore"', 'toggleSumMore', 'toggleBoardMore',
                    '#viewPhone:has(#phoneStage:not(.live)) .ph-btn-row'):
            self.assertIn(sym, tut_h)
        tut_css = tut_h[tut_h.index('<style>'):tut_h.index('</style>')]
        tut_css_nc = re.sub(r'/\*.*?\*/', '', tut_css, flags=re.S)
        self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', tut_css_nc)) <= {'400', '700'})
        self.assertEqual(re.findall(r'\d+\.\d+px', tut_css_nc), [])

        mob_h = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for sym in ('id="btnMobMore"', 'id="mobMoreTools"', 'toggleMobMore'):
            self.assertIn(sym, mob_h)
        mob_css = mob_h[mob_h.index('<style>'):mob_h.index('</style>')]
        mob_css_nc = re.sub(r'/\*.*?\*/', '', mob_css, flags=re.S)
        self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', mob_css_nc)) <= {'400', '700'})
        self.assertEqual(re.findall(r'\d+\.\d+px', mob_css_nc), [])

    def test_07_reduced_gravity_sensor_polling(self):
        ov_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('orStillN >= 12 ? 3500 : (orStillN >= 4 ? 1800 : 900)', ov_js)
        self.assertIn('orMode === \'auto\' ? autoMs : 12000', ov_js)

        ic_src = _p('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
        self.assertIn('await asyncio.sleep(0.5)', ic_src)
        self.assertIn('if self._pending_sensor_rot is not None or tick % 4 == 1:', ic_src)

        tut_h = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('orientCand ? 200 : 2500', tut_h)

        home_src = _p('/usr/local/bin/thinksmart-home.py').read_text('utf-8')
        self.assertIn('< 0.10:', home_src)
        self.assertIn('use_ttl = not os.environ.get', home_src)


class TestR90HomeTutorWakeHttp(unittest.IsolatedAsyncioTestCase):
    async def test_inbox_and_state_tutor_wake_etag(self):
        from aiohttp.test_utils import TestClient, TestServer
        home_path = _p('/usr/local/bin/thinksmart-home.py')
        home = _load_py_clean('r90_home', home_path)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            sig = td_p / 'tutor-open.json'
            v4l = td_p / 'v4l'
            usb_target = td_p / 'usb_dev' / '1-1.usb' / 'video'
            usb_target.mkdir(parents=True)
            v4l.mkdir()
            inbox_path = td_p / 'messages.json'
            cd_path = td_p / 'countdowns.json'
            with mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(sig), 'HOME_V4L2_DIR': str(v4l)}):
                app = home.make_app(inbox=home.Inbox(inbox_path), countdowns=home.CountdownStore(cd_path))
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': '127.0.0.1:8095'}
                    r1 = await cli.get('/api/home/inbox', headers=h)
                    self.assertEqual(r1.status, 200)
                    j1 = await r1.json()
                    self.assertEqual(j1['tutor_wake'], '0:')
                    r304 = await cli.get('/api/home/inbox', headers={**h, 'If-None-Match': r1.headers['ETag']})
                    self.assertEqual(r304.status, 304)

                    sig.write_text('{"ts":1}', encoding='utf-8')
                    r2 = await cli.get('/api/home/inbox', headers={**h, 'If-None-Match': r1.headers['ETag']})
                    self.assertEqual(r2.status, 200)
                    j2 = await r2.json()
                    self.assertTrue(j2['tutor_wake'].endswith(':'))
                    self.assertNotEqual(j2['tutor_wake'], '0:')

                    os.symlink(usb_target, v4l / 'video2')
                    r3 = await cli.get('/api/home/inbox', headers={**h, 'If-None-Match': r2.headers['ETag']})
                    self.assertEqual(r3.status, 200)
                    j3 = await r3.json()
                    self.assertTrue(j3['tutor_wake'].endswith(':video2'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
