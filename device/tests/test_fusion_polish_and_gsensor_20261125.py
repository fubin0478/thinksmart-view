#!/opt/thinksmart-addons/venv/bin/python
"""Regression suite (2026-11-25): BMA253 self-healing + wedge angle support + XiaoZhi-ThinkSmart final fusion polish."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

CLIENT_ROOT = pathlib.Path(os.environ.get("XZ_CLIENT", "/opt/thinksmart/client/current"))
HUB_SRV = pathlib.Path(os.environ.get("XZ_HUB_SRV", "/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server"))
WAKE_DAEMON = pathlib.Path(os.environ.get("WAKE_DAEMON", "/usr/local/bin/screen-wake-daemon.py"))
KIOSK_PY = pathlib.Path(os.environ.get("KIOSK_PY", "/usr/local/bin/thinksmart-kiosk.py"))
OVERLAY_JS = pathlib.Path(os.environ.get("OVERLAY_JS", "/usr/local/share/thinksmart-monitor/home_overlay.js"))
INTERCOM_PY = pathlib.Path(os.environ.get("INTERCOM_PY", "/usr/local/bin/thinksmart-intercom.py"))
ADMIN_PY = pathlib.Path(os.environ.get("ADMIN_PY", "/usr/local/bin/thinksmart-admin.py"))
HUB_PY = str(HUB_SRV / ".venv" / "bin" / "python")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestBma253AndWedgeAngles(unittest.TestCase):
    def test_01_bma253_freeze_recovery_and_cooldown(self):
        sw = _load("ts_wake_test_fusion", WAKE_DAEMON)
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            root = pathlib.Path(td)
            drv = root / "driver"
            i2c_dev = drv / "0-0018"
            iio = i2c_dev / "iio:device0"
            iio.mkdir(parents=True)
            unbind_f = drv / "unbind"
            bind_f = drv / "bind"
            unbind_f.write_text("")
            bind_f.write_text("")

            def set_xyz(x, y, z):
                for a, v in (("x", x), ("y", y), ("z", z)):
                    (iio / f"in_accel_{a}_raw").write_text(f"{v}\n")

            orig_drv = sw.I2C_ACCEL_DRIVER
            orig_dir = sw.ACCEL_DIR
            orig_res = sw._RESOLVED_ACCEL
            orig_hs = dict(sw._health_state)
            try:
                sw.I2C_ACCEL_DRIVER = drv
                sw.ACCEL_DIR = iio
                sw._RESOLVED_ACCEL = iio
                sw._health_state.update(val=None, since=0.0, count=0, last_recover=0.0)

                set_xyz(3, -494, 166)
                self.assertFalse(sw.check_accel_health((3, -494, 166), 100.0, sleep_fn=lambda _s: None))
                self.assertFalse(sw.check_accel_health((3, -494, 166), 112.0, sleep_fn=lambda _s: None))
                self.assertFalse(sw.check_accel_health((3, -494, 166), 124.0, sleep_fn=lambda _s: None))
                # 4th consecutive identical read at +38s triggers rebind
                self.assertTrue(sw.check_accel_health((3, -494, 166), 138.0, sleep_fn=lambda _s: None))
                self.assertEqual(unbind_f.read_text(), "0-0018\n")
                self.assertEqual(bind_f.read_text(), "0-0018\n")

                # Reset write markers and verify 60s cooldown blocks immediate second rebind
                unbind_f.write_text("")
                bind_f.write_text("")
                for i in range(4):
                    self.assertFalse(sw.check_accel_health((3, -494, 166), 140.0 + i * 15.0, sleep_fn=lambda _s: None))
                self.assertEqual(unbind_f.read_text(), "")
            finally:
                sw.I2C_ACCEL_DRIVER = orig_drv
                sw.ACCEL_DIR = orig_dir
                sw._RESOLVED_ACCEL = orig_res
                sw._health_state.update(orig_hs)

    def test_02_landscape_reclined_wedge_and_portrait_thresholds(self):
        # Verify home_overlay.js orClassify thresholds match both upright (~70deg) and reclined (~25deg) landscape
        js = OVERLAY_JS.read_text(encoding="utf-8")
        self.assertIn("if (Math.max(ax, ay) < 150 || Math.abs(ax - ay) < 100) return '';", js)

        # Verify thinksmart-kiosk.py read_orient uses the same thresholds
        kiosk_src = KIOSK_PY.read_text(encoding="utf-8")
        self.assertIn("if g < 400 or g > 640 or max(ax, ay) < 150 or abs(ax - ay) < 100:", kiosk_src)

        # Evaluate intercom's read_sensor_rot on reclined landscape (200, 5)
        ic_src = INTERCOM_PY.read_text(encoding="utf-8")
        self.assertIn("if max(ax, ay) < 150:", ic_src)
        fn = ic_src[ic_src.index("    def read_sensor_rot("):ic_src.index("    def orient_dict(")]
        ns = {}
        cls_src = "class Dummy:\n    sensor_rot = 90\n    def _resolve_accel_xy(self): return self._xy\n" + fn
        exec(cls_src, ns)  # noqa: S102
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            d = pathlib.Path(td)
            xp, yp = d / "x", d / "y"
            obj = ns["Dummy"]()
            obj._xy = (xp, yp)
            xp.write_text("200\n")
            yp.write_text("5\n")
            self.assertEqual(obj.read_sensor_rot(), 0)   # reclined landscape recovers from 90 -> 0!
            xp.write_text("3\n")
            yp.write_text("-494\n")
            self.assertEqual(obj.read_sensor_rot(), 90)
            xp.write_text("30\n")
            yp.write_text("-20\n")
            self.assertEqual(obj.read_sensor_rot(), 90)  # flat deadband keeps prior rotation


class TestFusionPolishAndDeadCleanup(unittest.TestCase):
    def test_03_emoji_disabled_and_native_prompts(self):
        hub_py = (CLIENT_ROOT / "thinksmart_client" / "hub.py").read_text(encoding="utf-8")
        self.assertIn("'emoji': False", hub_py)

        hub_prompt = (HUB_SRV / "hub-base-prompt.txt").read_text(encoding="utf-8")
        self.assertNotIn("<speaker_recognition>", hub_prompt)
        self.assertNotIn("hass tools", hub_prompt)
        self.assertIn("self.music.*", hub_prompt)
        self.assertIn("self.alarm.*", hub_prompt)
        self.assertIn("self.home.timer", hub_prompt)
        self.assertIn("self.study.focus", hub_prompt)

        pipe_txt = (HUB_SRV / "core/admin/pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("StackChan", pipe_txt)
        self.assertIn("direct_answer", pipe_txt)

    def test_04_weather_follows_home_city_and_admin_hotpush_and_pruned_bloat(self):
        script = r"""
import asyncio, os, sys, types
from aiohttp import web
sys.path.insert(0, '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
from plugins_func.functions import get_weather as gw

fake_conn = types.SimpleNamespace(
    config={'plugins': {'get_weather': {'location': '广州'}}},
    client_ip='127.0.0.1',
)

async def run_check():
    app = web.Application()
    payload = {
        'ok': True, 'city': '杭州', 'cities': ['杭州'], 'updated': 1764044400,
        'now': {'temp': '4', 'feels': '1', 'humidity': '35', 'text': '晴', 'wind_dir': '西北风', 'wind_scale': '2'},
        'daily': [{'date': '2026-11-25', 'text': '晴', 'text_night': '晴', 'min': '-5', 'max': '6'}],
        'hourly': [],
    }
    app.router.add_get('/api/home/weather', lambda _r: web.json_response(payload))
    app.router.add_get('/api/home/weather/detail', lambda _r: web.json_response(payload))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    os.environ['THINKSMART_HOME_WEATHER_URL'] = f'http://127.0.0.1:{port}'
    try:
        rep = await gw.home_weather_report(fake_conn, None)
        assert rep and '杭州（主页同源实况）' in rep, rep
    finally:
        os.environ.pop('THINKSMART_HOME_WEATHER_URL', None)
        await runner.cleanup()

asyncio.run(run_check())
print('WEATHER_SYNC_OK')
"""
        r = subprocess.run([HUB_PY, "-B", "-c", script], cwd="/tmp", capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("WEATHER_SYNC_OK", r.stdout)

        adm = ADMIN_PY.read_text(encoding="utf-8")
        self.assertIn("CLIENT_API + '/api/settings'", adm)
        self.assertIn("机身离线唤醒词与灵敏度已实时热生效", adm)

        gone_paths = [
            pathlib.Path("/usr/local/share/thinksmart-kiosk/test.html"),
            CLIENT_ROOT / "web" / "sounds" / "wake.wav",
            CLIENT_ROOT / "deploy" / "thinksmart-client.service",
            CLIENT_ROOT / "web" / "logos" / "anthropic.svg",
            CLIENT_ROOT / "web" / "logos" / "lmstudio.svg",
            CLIENT_ROOT / "web" / "logos" / "moonshot.svg",
            CLIENT_ROOT / "web" / "logos" / "ollama.svg",
            HUB_SRV / "core" / "providers" / "asr" / "fun_local.py",
            HUB_SRV / "core" / "providers" / "llm" / "ollama" / "ollama.py",
        ]
        for gp in gone_paths:
            self.assertFalse(gp.exists(), f"Dead legacy file must remain deleted: {gp}")

        cfg_yaml = (HUB_SRV / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("ASR: SherpaXASR", cfg_yaml)
        self.assertNotIn("type: fun_local", cfg_yaml)
        self.assertNotIn("OllamaLLM:", cfg_yaml)


if __name__ == "__main__":
    unittest.main(verbosity=1)
