#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-20 R61: USB Hub / U-Disk / DocCam hotplug self-healing & auto-rescan tests.
- thinksmart-usb-watchdog: detects 'HC died' / dead xHCI lines, ignores normal USB logs, enforces cooldown,
  ignores non-host USB role, detects missing usb1 root hub, and cycles usb-mode device -> host cleanly.
- thinksmart-qqmusic.py: usb_mount_sig() O(1) st_dev mount detection + GET /api/music/status exposes usb_sig.
- thinksmart-qqmusic/index.html: auto-rescans playlists & highlights U-disk when st.usb_sig changes.
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')
from aiohttp.test_utils import AioHTTPTestCase

E = os.environ.get
WATCHDOG_PATH = E('WATCHDOG_BIN', '/usr/local/sbin/thinksmart-usb-watchdog')
WATCHDOG_SVC_PATH = pathlib.Path(E('WATCHDOG_SVC', '/etc/systemd/system/thinksmart-usb-watchdog.service'))
QQ_PY_PATH = E('QQ_PY', '/usr/local/bin/thinksmart-qqmusic.py')
QQ_HTML_PATH = pathlib.Path(E('QQ_HTML', '/usr/local/share/thinksmart-qqmusic/index.html'))


def _load_script(name: str, path: str):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


wd_mod = _load_script('ts_usb_watchdog_test', WATCHDOG_PATH)


def _load_qq_no_main(path: str):
    import ast
    import types
    src = pathlib.Path(path).read_text('utf-8')
    tree = ast.parse(src)
    tree.body = [
        n for n in tree.body
        if not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'engine' for t in n.targets))
        and not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and getattr(n.test.left, 'id', '') == '__name__')
    ]
    mod = types.ModuleType('ts_qq_usb_test')
    mod.__file__ = path
    exec(compile(tree, path, 'exec'), mod.__dict__)
    return mod


qq_mod = _load_qq_no_main(QQ_PY_PATH)


class TestUsbWatchdog(unittest.TestCase):
    def test_dead_kmsg_classification(self):
        self.assertTrue(wd_mod.is_dead_kmsg('3,881,999,-;xhci-hcd xhci-hcd.1.auto: HC died; cleaning up\n'))
        self.assertTrue(wd_mod.is_dead_kmsg('3,880,998,-;xhci-hcd xhci-hcd.1.auto: xHCI host controller not responding, assume dead\n'))
        self.assertFalse(wd_mod.is_dead_kmsg('6,890,1005,-;usb 1-1.3: new high-speed USB device number 4 using xhci-hcd\n'))
        self.assertFalse(wd_mod.is_dead_kmsg('4,850,900,-;xhci-hcd xhci-hcd.1.auto: WARN: HC couldn\'t access mem fast enough\n'))

    def test_recovery_lifecycle_and_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            role_f = root / 'role'
            usb1_d = root / 'usb1'
            mnt_d = root / 'mnt_usb'
            lock_f = root / 'usb.lock'
            fake_mode = root / 'fake_usb_mode.sh'
            calls_log = root / 'calls.txt'
            mnt_d.mkdir()
            role_f.write_text('host\n', 'utf-8')
            fake_mode.write_text(f'#!/bin/sh\necho "$1" >> "{calls_log}"\n', 'utf-8')
            fake_mode.chmod(0o755)

            dog = wd_mod.UsbWatchdog(
                role_path=str(role_f),
                usb1_path=str(usb1_d),
                usb_mode_bin=str(fake_mode),
                mnt_dir=str(mnt_d),
                lock_path=str(lock_f),
                cooldown_sec=10.0,
                settle_sec=0.0,
                role_gap_sec=0.0,
            )
            # usb1 missing while role is host -> needs_roothub_recovery True
            self.assertTrue(dog.needs_roothub_recovery())
            self.assertTrue(dog.recover('HC died'))
            self.assertEqual(calls_log.read_text('utf-8').splitlines(), ['device', 'host'])

            # Immediate duplicate call blocked by cooldown
            self.assertFalse(dog.recover('HC died duplicate'))
            self.assertEqual(calls_log.read_text('utf-8').splitlines(), ['device', 'host'])

            # Non-host role is never touched
            dog.last_recover_mono = -999.0
            role_f.write_text('device\n', 'utf-8')
            self.assertFalse(dog.needs_roothub_recovery())
            self.assertFalse(dog.recover('HC died while device'))
            self.assertEqual(calls_log.read_text('utf-8').splitlines(), ['device', 'host'])

    def test_service_runs_in_host_mount_namespace(self):
        svc = WATCHDOG_SVC_PATH.read_text('utf-8')
        self.assertIn('ExecStart=/usr/bin/python3 -B /usr/local/sbin/thinksmart-usb-watchdog', svc)
        self.assertNotIn('PrivateTmp=', svc)
        self.assertNotIn('ProtectSystem=', svc)


class TestQqMusicUsbHotplug(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._td.name)
        self.html_f = root / 'index.html'
        self.html_f.write_text('<!doctype html><title>qq</title>', 'utf-8')
        eng = mock.MagicMock()
        eng.status.side_effect = lambda lite=False, qseq=None, lseq=None: {
            'ok': True, 'playing': False, 'lite': lite
        }
        self._p1 = mock.patch.object(qq_mod, 'UI_HTML_PATH', self.html_f)
        self._p2 = mock.patch.object(qq_mod, 'engine', eng, create=True)
        self._p1.start()
        self._p2.start()
        app = qq_mod.make_app()
        app.on_startup.clear()
        app.on_cleanup.clear()
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self._p2.stop()
        self._p1.stop()
        self._td.cleanup()

    async def test_usb_mount_sig_and_status_payload(self):
        # Same st_dev as parent -> '0' (unmounted)
        fake_unmounted = mock.MagicMock(st_dev=10, st_ino=2, st_mtime_ns=1000)
        fake_parent = mock.MagicMock(st_dev=10, st_ino=1, st_mtime_ns=900)
        with mock.patch.object(pathlib.Path, 'stat', side_effect=[fake_unmounted, fake_parent]):
            self.assertEqual(qq_mod.usb_mount_sig(), '0')

        # Different st_dev from parent -> mounted signature
        fake_mounted = mock.MagicMock(st_dev=42, st_ino=1, st_mtime_ns=123456789)
        with mock.patch.object(pathlib.Path, 'stat', side_effect=[fake_mounted, fake_parent]):
            self.assertEqual(qq_mod.usb_mount_sig(), '42:1:123456789')

        h = {'Host': '127.0.0.1:8091'}
        with mock.patch.object(qq_mod, 'usb_mount_sig', return_value='42:1:999'):
            r = await self.client.get('/api/music/status', headers=h)
            self.assertEqual(r.status, 200)
            j = await r.json()
            self.assertEqual(j.get('usb_sig'), '42:1:999')

            rl = await self.client.get('/api/music/status?lite=1', headers=h)
            jl = await rl.json()
            self.assertNotIn('usb_sig', jl)

    def test_ui_hotplug_rescan_logic(self):
        html = QQ_HTML_PATH.read_text('utf-8')
        for sym in ('let lastUsbSig = null;', "typeof st.usb_sig === 'string'", "selectedPlId = 'usb'", 'loadMine(true);'):
            self.assertIn(sym, html)


if __name__ == '__main__':
    unittest.main()
