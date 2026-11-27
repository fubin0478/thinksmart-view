#!/opt/thinksmart-addons/venv/bin/python
"""Step 1 Verification Suite (2026-11-25): Self-owned xiaozhi-client & xiaozhi-hub integration
and permanent disablement of all third-party online update mechanisms."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


def _load_mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Step1SelfOwnedXiaozhiTests(unittest.TestCase):
    def test_client_is_self_owned_native_bridge_without_pythonpath_injection(self):
        cur = pathlib.Path('/opt/thinksmart/client/current')
        self.assertTrue(cur.is_symlink())
        self.assertEqual(os.readlink(cur), '1.0.0-self')
        init_txt = (cur / 'thinksmart_client' / '__init__.py').read_text(encoding='utf-8')
        self.assertIn("1.0.0-self", init_txt)
        main_txt = (cur / 'thinksmart_client' / '__main__.py').read_text(encoding='utf-8')
        self.assertIn('bridge.install_native(', main_txt)
        self.assertTrue((cur / 'thinksmart_client' / 'bridge.py').is_file())
        self.assertFalse((cur / 'deploy' / 'bootstrap-updater').exists())
        self.assertFalse(pathlib.Path('/etc/systemd/system/thinksmart-client.service.d/aec-bridge.conf').exists())
        svc_txt = pathlib.Path('/etc/systemd/system/thinksmart-client.service').read_text(encoding='utf-8')
        self.assertNotIn('bootstrap-updater', svc_txt)

        br = pathlib.Path('/run/user/1000/qqmusic-bridge.json')
        self.assertTrue(br.is_file())
        br_data = json.loads(br.read_text(encoding='utf-8'))
        self.assertTrue(br_data.get('audio_hook'))
        self.assertTrue(br_data.get('voice_tools'))
        self.assertFalse(br_data.get('disabled'))
        self.assertEqual(br_data.get('xiaozhi'), '1.0.0-self')

    def test_client_updater_and_systemd_timers_permanently_disabled(self):
        upd = _load_mod('ts_client_upd_s1', '/opt/thinksmart/client/current/thinksmart_client/update.py')
        st = upd.read()
        self.assertTrue(st.get('self_managed'))
        self.assertFalse(st.get('enabled'))
        with tempfile.TemporaryDirectory() as td:
            upd.REQUESTS = pathlib.Path(td)
            self.assertFalse(upd.request('check'))
            self.assertFalse(upd.request('install', '9.9.9', '9.9.9'))
            self.assertEqual(list(pathlib.Path(td).iterdir()), [])

        conf_txt = pathlib.Path('/etc/thinksmart-update.conf').read_text(encoding='utf-8')
        self.assertIn('SOURCES=""', conf_txt)
        self.assertNotIn('justhalfbit', conf_txt)

        for unit in (
            'thinksmart-update-check.timer',
            'thinksmart-update-request.path',
            'thinksmart-update-boot.service',
        ):
            r_act = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True)
            self.assertNotEqual(r_act.stdout.strip(), 'active', unit)
            r_en = subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True, text=True)
            self.assertIn(r_en.stdout.strip(), ('disabled', 'masked', 'static', 'not-found'), f'{unit}: {r_en.stdout}')

    def test_hub_runs_native_merged_modules_with_clean_git_and_disabled_updater(self):
        dropin = pathlib.Path('/home/ubuntu/.config/systemd/user/xiaozhi-hub.service.d/thinksmart-overrides.conf').read_text(encoding='utf-8')
        self.assertIn('UMask=0077', dropin)
        self.assertNotIn('xiaozhi-hub-overrides', dropin)

        hub_root = pathlib.Path('/home/ubuntu/xiaozhi-hub-server')
        r_st = subprocess.run(
            ['git', '-c', f'safe.directory={hub_root}', '-C', str(hub_root), 'status', '--porcelain', '--untracked-files=no'],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(r_st.stdout.strip(), '')

        r_rem = subprocess.run(
            ['git', '-c', f'safe.directory={hub_root}', '-C', str(hub_root), 'remote'],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(r_rem.stdout.strip(), '')

        hub_py = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python'
        probe = (
            "import sys; sys.path.insert(0, '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'); "
            "from core.admin import updater; import plugins_func.functions.play_music as pm; "
            "assert '/xiaozhi-server/' in pm.__file__ and '8091' in open(pm.__file__, encoding='utf-8').read(); "
            "u = updater.Updater(); "
            "v = u.version(); assert v.get('supported') is False and v['source']['url'] == ''; "
            "c = u.check(); assert c['state'] == 'self_managed'; "
            "print('HUB_SELF_OWNED_OK')"
        )
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            r_p = subprocess.run([hub_py, '-B', '-c', probe], cwd=td, capture_output=True, text=True, timeout=20)
        self.assertEqual(r_p.returncode, 0, msg=r_p.stderr + '\n' + r_p.stdout)
        self.assertIn('HUB_SELF_OWNED_OK', r_p.stdout)

        st_file = pathlib.Path('/run/user/1000/xiaozhi-hub-overrides.json')
        self.assertTrue(st_file.is_file())
        st_json = json.loads(st_file.read_text(encoding='utf-8'))
        self.assertEqual(st_json.get('mode'), 'native_merged')
        self.assertEqual(len(st_json.get('modules', {})), 16)


if __name__ == '__main__':
    unittest.main()
