#!/opt/thinksmart-addons/venv/bin/python -B
"""Round 2 full-system audit regression suite & permanent CSP/XFO/CORS guards (2026-11-26)."""
import asyncio
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path

os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
sys.dont_write_bytecode = True


def _load_mod(name: str, *candidates: str):
    for p in candidates:
        fp = Path(p)
        if fp.is_file():
            spec = importlib.util.spec_from_file_location(name, fp)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(f'Missing {name}: {candidates}')


def _read_first(*candidates: str) -> str:
    for p in candidates:
        fp = Path(p)
        if fp.is_file():
            return fp.read_text(encoding='utf-8')
    raise FileNotFoundError(candidates)


def _path_first(*candidates: str) -> Path:
    for p in candidates:
        fp = Path(p)
        if fp.exists():
            return fp
    raise FileNotFoundError(candidates)


class Round2AuditTests(unittest.TestCase):
    def test_01_kiosk_run_command_and_orient_poll(self):
        src = _read_first('/usr/local/bin/thinksmart-kiosk.py')
        self.assertIn('def run_command(handler, msg, progress):', src)
        self.assertIn("msg.get('cmd') != 'orient'", src)
        ns = {}
        # Compile just the pure function snippet
        fn_block = re.search(r'def run_command\(handler, msg, progress\):.*?\n\n\n', src, re.S)
        self.assertIsNotNone(fn_block)
        exec(fn_block.group(0), ns)
        rc = ns['run_command']
        self.assertEqual(rc(lambda m, p: {'ok': True, 'x': m['x']}, {'x': 7}, None), {'ok': True, 'x': 7})
        self.assertEqual(rc(lambda m, p: True, {}, None), {'ok': True})
        err_res = rc(lambda m, p: (_ for _ in ()).throw(RuntimeError('boom')), {}, None)
        self.assertFalse(err_res['ok'])
        self.assertEqual(err_res['reason'], 'failed')

    def test_02_gpu_watchdog_and_kiosk_mode_dash_b_regex(self):
        gpu_src = _read_first('/usr/local/bin/gpu-watchdog.py')
        m = re.search(r"KIOSK\s*=\s*'([^']+)'", gpu_src)
        self.assertIsNotNone(m)
        pat = m.group(1)
        self.assertRegex('/usr/bin/python3 /usr/local/bin/thinksmart-kiosk.py', pat)
        self.assertRegex('/usr/bin/python3 -B /usr/local/bin/thinksmart-kiosk.py', pat)
        self.assertNotRegex('/usr/bin/python3 /usr/local/bin/other.py', pat)
        km_src = _read_first('/usr/local/bin/kiosk-mode')
        self.assertIn('^/usr/bin/python3( -B)? /usr/local/bin/thinksmart-kiosk\\.py$', km_src)

    def test_03_home_overlays_city_and_portrait_scroll_cleanup(self):
        html = _read_first('/usr/local/share/thinksmart-home/index.html')
        m_wp = re.search(r'function openWeatherPage\(dayIdx = 0, city = \'\'\) \{([\s\S]*?)^function closeWeatherPage', html, re.M)
        self.assertIsNotNone(m_wp)
        self.assertIn('closeModal()', m_wp.group(1))
        self.assertIn('closeTimerPage()', m_wp.group(1))
        self.assertIn('closeCountdownPage()', m_wp.group(1))
        self.assertIn("wpCurCity = (typeof city === 'string' && city) ? city : homeCityName();", m_wp.group(1))
        m_tp = re.search(r'function openTimerPage\(view = \'setup\', tab = \'\'\) \{([\s\S]*?)^function closeTimerPage', html, re.M)
        self.assertIsNotNone(m_tp)
        self.assertIn('closeModal()', m_tp.group(1))
        self.assertIn('closeWeatherPage()', m_tp.group(1))
        self.assertIn('closeCountdownPage()', m_tp.group(1))
        m_cp = re.search(r'function openCountdownPage\(selectId = null, prefillDate = \'\'\) \{([\s\S]*?)^function closeCountdownPage', html, re.M)
        self.assertIsNotNone(m_cp)
        self.assertIn('closeModal()', m_cp.group(1))
        self.assertIn("d.openWeather) openWeatherPage(0, typeof d.city === 'string' ? d.city : '');", html)
        self.assertIn('window.tsStopPortraitScroll', html)
        m_sm = re.search(r'const stopMomentum = \(\) => \{([\s\S]*?)\n  \};', html)
        self.assertIsNotNone(m_sm)
        self.assertIn('ps = null;', m_sm.group(1))

    def test_04_qqmusic_overlay_does_not_block_on_sheets_or_boot_cover(self):
        js = _read_first('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js')
        self.assertNotIn("classList.contains('ts-xz-covered')", js)
        self.assertIn("classList.contains('ts-home')", js)
        self.assertIn("classList.contains('ts-homeload')", js)
        self.assertIn("getElementById('settings')", js)
        self.assertIn("getElementById('wifi')", js)
        self.assertIn("getElementById('sheet')", js)

    def test_05_home_overlay_no_tdz_and_no_double_postmessage_when_warm(self):
        js = _read_first('/usr/local/share/thinksmart-monitor/home_overlay.js')
        self.assertIn("isXZ && j?.cd_open_fresh", js)
        self.assertIn("isXZ && j?.cd_close_fresh", js)
        self.assertIn("if (!homeOk) setTimeout(sendOpenCd, 140)", js)
        self.assertIn("if (!homeOk) setTimeout(sendCloseCd, 140)", js)
        self.assertIn("if (!homeOk) setTimeout(send, 120)", js)
        self.assertIn("if (!homeOk) setTimeout(send, 140)", js)

    def test_06_csp_and_cors_contract_all_ports(self):
        client_web = _read_first('/opt/thinksmart/client/current/thinksmart_client/web.py')
        self.assertIn("frame-src 'self' about: http://127.0.0.1:* http://localhost:*", client_web)
        self.assertIn('https://*.qlogo.cn http://*.qlogo.cn', client_web)
        home_py = _read_first('/usr/local/bin/thinksmart-home.py')
        self.assertIn("frame-ancestors 'self' http://127.0.0.1:8090 http://localhost:8090", home_py)
        qq_py = _read_first('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn("frame-ancestors 'self' http://127.0.0.1:8090 http://localhost:8090", qq_py)
        mon_py = _read_first('/usr/local/bin/thinksmart-monitor.py')
        self.assertIn("frame-ancestors 'self' http://127.0.0.1:8090 http://localhost:8090", mon_py)
        tutor_py = _read_first('/usr/local/bin/thinksmart-tutor.py')
        self.assertIn("'http://127.0.0.1:8090'", tutor_py)
        self.assertIn("'http://localhost:8090'", tutor_py)

    def test_07_keyboard_portrait_ui_scroll(self):
        kb = _read_first('/usr/local/share/thinksmart-kiosk/keyboard.js')
        self.assertIn("classList.contains('portrait-ui')", kb)

    def test_08_wifi_forget_hides_buttons(self):
        app_js = _read_first('/opt/thinksmart/client/current/web/v2/app.js')
        self.assertIn("wifi.target.mode = 'done';", app_js)
        self.assertIn("t.mode === 'done'", app_js)

    def test_09_client_aec_hold_assistant_reload_and_wake_verify(self):
        client_root = str(_path_first('/opt/thinksmart/client/current'))
        if client_root not in sys.path:
            sys.path.insert(0, client_root)
        assistant = _load_mod('thinksmart_client_assistant_r2', f'{client_root}/thinksmart_client/assistant.py')
        wake_verify = _load_mod('thinksmart_client_wv_r2', f'{client_root}/thinksmart_client/wake_verify.py')
        app_src = _read_first(f'{client_root}/thinksmart_client/app.py')
        # aec_hold assigned after self.state = state
        m_st = re.search(r'def set_state\(self, state\):([\s\S]*?)def status\(', app_src)
        self.assertIsNotNone(m_st)
        self.assertLess(m_st.group(1).index('self.state = state'), m_st.group(1).index('self.audio.aec_hold'))
        # assistant.normalize accepts disk word via extra_known while still detecting change vs live cfg
        live_cfg = {'wake_words': ['n ǐ h ǎo x iǎo zh ì @你好小智'], 'wake_sound': 'zai', 'wake_verify': '',
                    'kws_threshold': 0.25, 'kws_score': 2.0, 'kws_gain_db': 10, 'kws_paths': 8, 'follow_up_seconds': 10}
        disk_word = 'custom_w@自定唤醒词'
        patch = assistant.normalize(live_cfg, {'wake_words': [disk_word]}, extra_known=[disk_word])
        self.assertEqual(patch.get('wake_words'), [disk_word])
        # Single-char wake_verify name must never auto-match partial utterances
        m = wake_verify.Matcher('小', slack=2.0)
        ok, _ = m.judge('我想睡觉')
        self.assertFalse(ok)

    def test_10_hub_prompt_reload_and_kiosk_voice_inf_guard(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        ah_src = _read_first(f'{hub_root}/core/admin/admin_handler.py')
        self.assertIn('self._blocking(conn._init_prompt_enhancement)', ah_src)
        self.assertNotIn('conn.dialogue.update_system_message(new_prompt)', ah_src)
        hub_py = f'{hub_root}/.venv/bin/python'
        probe = (
            "from core.admin import kiosk_voice as kv\n"
            "for bad in (float('inf'), float('-inf'), float('nan')):\n"
            "    for fn in (lambda: kv._want_int('超时', bad, 1, 100), lambda: kv._want_float('阈值', bad, 0.1, 0.9)):\n"
            "        try:\n"
            "            fn()\n"
            "            raise AssertionError('expected ValueError')\n"
            "        except ValueError:\n"
            "            pass\n"
            "print('KV_INF_GUARD_OK')\n"
        )
        r = subprocess.run([hub_py, '-B', '-c', probe], cwd=hub_root, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('KV_INF_GUARD_OK', r.stdout)

    def test_11_intercom_wan_dir_forwarding_and_snooze_toast(self):
        ic_src = _read_first('/usr/local/bin/thinksmart-intercom.py')
        self.assertIn("已延后 10 分钟再响", ic_src)
        self.assertNotIn("已延后 5 分钟再响", ic_src)
        self.assertIn('request["ts_allow_wan_dir"] = allow_wan_dir', ic_src)
        self.assertIn('po_port in (None, 443, 18443)', ic_src)
        bare = re.findall(r'_msg_origin_ok\(hdr_l,\s*request\.remote\)', ic_src)
        self.assertEqual(bare, [], f'Found bare _msg_origin_ok calls: {bare}')

    def test_12_admin_backup_alarms_monitor_secrets_ports_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env_backup = dict(os.environ)
            try:
                os.environ['ADMIN_STATE_DIR'] = str(root / 'admin')
                os.environ['ADMIN_HUB_DIR'] = str(root / 'hub')
                os.environ['ADMIN_HOME_STATE'] = str(root / 'home')
                os.environ['ADMIN_STUDY_STATE'] = str(root / 'study')
                os.environ['ADMIN_TUTOR_STATE'] = str(root / 'tutor')
                os.environ['ADMIN_MONITOR_STATE'] = str(root / 'monitor')
                os.environ['ADMIN_QQ_CFG_DIR'] = str(root / 'qq')
                os.environ.pop('ADMIN_CLIENT_CFG', None)
                os.environ['ADMIN_CLIENT_ALARMS'] = str(root / 'alarms' / 'alarms.json')
                adm = _load_mod('ts_admin_r2_a', '/usr/local/bin/thinksmart-admin.py')
                # Default CLIENT_ALARMS_PATH when ADMIN_CLIENT_CFG absent & ADMIN_CLIENT_ALARMS unset
                os.environ.pop('ADMIN_CLIENT_ALARMS', None)
                adm_def = _load_mod('ts_admin_r2_def', '/usr/local/bin/thinksmart-admin.py')
                self.assertEqual(str(adm_def.CLIENT_ALARMS_PATH), '/home/ubuntu/.local/share/thinksmart-client/alarms/alarms.json')

                # Prepare sample files including client token, monitor config, hub headers secret
                (root / 'hub' / 'data').mkdir(parents=True)
                (root / 'hub' / 'data' / '.config.yaml').write_text(
                    'LLM:\n  ExtLLM:\n    base_url: https://example.com\n    headers:\n      Authorization: Bearer tok-existing-999\n      X-App: demo\n',
                    encoding='utf-8',
                )
                (root / 'monitor').mkdir(parents=True)
                (root / 'monitor' / 'config.json').write_text(json.dumps({'limit_gb': 120}), encoding='utf-8')
                adm.CLIENT_CFG = root / 'client' / 'config.json'
                adm.CLIENT_CFG.parent.mkdir(parents=True)
                adm.CLIENT_CFG.write_text(json.dumps({'token': 'secret-cli-tok-888', 'wake_sound': 'zai'}), encoding='utf-8')
                adm.CLIENT_ALARMS_PATH = root / 'alarms' / 'alarms.json'
                adm.CLIENT_ALARMS_PATH.parent.mkdir(parents=True)
                adm.CLIENT_ALARMS_PATH.write_text(json.dumps({'items': [{'id': 'a1'}]}), encoding='utf-8')

                blob, _ = adm.build_backup_archive('sanitized')
                with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tf:
                    names = set(tf.getnames())
                    self.assertIn('monitor/config.json', names)
                    self.assertIn('client/config.json', names)
                    self.assertIn('client/alarms.json', names)
                    cli_san = json.loads(tf.extractfile('client/config.json').read().decode('utf-8'))
                    self.assertNotIn('token', cli_san)
                    self.assertEqual(cli_san.get('wake_sound'), 'zai')
                    hub_san = adm._parse_yaml_bytes(tf.extractfile('hub/config.sanitized.yaml').read())
                    self.assertNotIn('Authorization', str(hub_san))

                # _merge_keep_secrets must preserve omitted Authorization header when restoring sanitized
                merged_h = adm._merge_keep_secrets(
                    {'LLM': {'ExtLLM': {'base_url': 'https://example.com'}}},
                    {'LLM': {'ExtLLM': {'base_url': 'https://example.com', 'headers': {'Authorization': 'Bearer tok-existing-999', 'X-App': 'old'}}}},
                )
                self.assertEqual(merged_h['LLM']['ExtLLM']['headers']['Authorization'], 'Bearer tok-existing-999')

                # same_origin_ok rejects default vs explicit cross-port on non-loopback while keeping http://127.0.0.1 test compat
                req_bad = types.SimpleNamespace(headers={'Origin': 'https://example.com', 'Host': 'example.com:8097', 'X-Forwarded-Proto': 'https'})
                req_good = types.SimpleNamespace(headers={'Origin': 'https://example.com', 'Host': 'example.com:443', 'X-Forwarded-Proto': 'https'})
                req_lb = types.SimpleNamespace(headers={'Origin': 'http://127.0.0.1', 'Host': '127.0.0.1:8097'})
                self.assertFalse(adm.same_origin_ok(req_bad))
                self.assertTrue(adm.same_origin_ok(req_good))
                self.assertTrue(adm.same_origin_ok(req_lb))
            finally:
                os.environ.clear()
                os.environ.update(env_backup)

        adm_src = _read_first('/usr/local/bin/thinksmart-admin.py')
        self.assertIn("await self._notify_hub_reload([", adm_src)
        for _sec_tok in ("'LLM'", "'TTS'", "'ASR'", "'VAD'", "'Intent'"):
            self.assertIn(_sec_tok, adm_src)
        self.assertIn("'keep_wakeup_yaml': True", adm_src)
        unit_src = _read_first('/etc/systemd/system/thinksmart-admin.service')
        self.assertIn('/home/ubuntu/.local/share/thinksmart-client', unit_src)
        bk_src = _read_first('/usr/local/sbin/thinksmart-backup')
        self.assertIn('RESTORE_BACKUP_DIR', bk_src)
        self.assertIn('CLIENT_ALARMS_PATH', bk_src)

    def test_13_qqmusic_airplay_mirror_flag_filename(self):
        qq_src = _read_first('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('uxplay-mirroring-active', qq_src)
        self.assertNotIn("'uxplay-mirror-active'", qq_src)
        self.assertNotIn('/uxplay-mirror-active', qq_src)

    def test_14_tutor_allows_loopback_cross_site_fetch(self):
        from aiohttp.test_utils import TestClient, TestServer
        tutor = _load_mod('ts_tutor_r2', '/usr/local/bin/thinksmart-tutor.py')

        async def _probe():
            app = tutor.make_app()
            async with TestClient(TestServer(app)) as cli:
                r1 = await cli.get('/api/tutor/badge', headers={'Host': f'127.0.0.1:{tutor.PORT}', 'Origin': 'http://127.0.0.1:8090', 'Sec-Fetch-Site': 'cross-site'})
                r2 = await cli.get('/api/tutor/badge', headers={'Host': f'127.0.0.1:{tutor.PORT}', 'Origin': 'https://evil.example', 'Sec-Fetch-Site': 'cross-site'})
                return r1.status, r1.headers.get('Access-Control-Allow-Origin'), r2.status

        st1, acao1, st2 = asyncio.run(_probe())
        self.assertEqual(st1, 200)
        self.assertEqual(acao1, 'http://127.0.0.1:8090')
        self.assertEqual(st2, 403)

    def test_15_sudoers_sync_and_install_contract(self):
        su_p = _path_first('/etc/sudoers.d/thinksmart-airplay-tune')
        st = su_p.stat()
        self.assertEqual(oct(st.st_mode & 0o777), '0o440')
        self.assertEqual((st.st_uid, st.st_gid), (0, 0))
        if os.geteuid() == 0:
            self.assertIn('NOPASSWD: /usr/local/sbin/thinksmart-airplay-tune *', su_p.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
