#!/opt/thinksmart-addons/venv/bin/python
"""Round-1 Deep System Audit Regression Suite (2026-11-25):
Verifies security, concurrency, timezone/NaN, UI overlay/CSS, audio arbitration,
and hardware/system hardening fixes across xiaozhi-client, web/v2, home_overlay,
kiosk, xiaozhi-hub, admin, qqmusic, home, study, monitor, tutor, intercom, and system.
"""
import importlib.util
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

CLIENT_ROOT = Path("/opt/thinksmart/client/current")
CLIENT_PY = str(CLIENT_ROOT / ".venv/bin/python")
HUB_ROOT = Path("/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server")
HUB_PY = str(HUB_ROOT / ".venv/bin/python")
OVERLAY_JS = Path("/usr/local/share/thinksmart-monitor/home_overlay.js")
MONITOR_HTML = Path("/usr/local/share/thinksmart-monitor/index.html")
MONITOR_PY = Path("/usr/local/bin/thinksmart-monitor.py")
HOME_HTML = Path("/usr/local/share/thinksmart-home/index.html")
HOME_PY = Path("/usr/local/bin/thinksmart-home.py")
QQ_PY = Path("/usr/local/bin/thinksmart-qqmusic.py")
STUDY_PY = Path("/usr/local/bin/thinksmart-study.py")
TUTOR_PY = Path("/usr/local/bin/thinksmart-tutor.py")
INTERCOM_PY = Path("/usr/local/bin/thinksmart-intercom.py")
ADMIN_PY = Path("/usr/local/bin/thinksmart-admin.py")
ADMIN_HTML = Path("/usr/local/share/thinksmart-admin/index.html")
KIOSK_PY = Path("/usr/local/bin/thinksmart-kiosk.py")
KEYBOARD_JS = Path("/usr/local/share/thinksmart-kiosk/keyboard.js")
AIRPLAY_PY = Path("/usr/local/bin/thinksmart-airplay.py")
PROX_PY = Path("/usr/local/bin/vcnl4200-proximity-daemon.py")
UPDATE_SBIN = Path("/usr/local/sbin/thinksmart-update")
REVIEW_SBIN = Path("/usr/local/sbin/thinksmart-review")


def _run_py(py_bin: str, code: str, cwd: str = "/tmp", timeout: int = 60):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run(
        [py_bin, "-B", "-c", code],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return r.returncode, r.stdout, r.stderr


class TestClientWebAndCoreRound1(unittest.TestCase):
    def test_01_client_web_security_middleware_and_validation(self):
        code = f"""
import asyncio, sys
from unittest import mock
sys.path.insert(0, {str(CLIENT_ROOT)!r})
from aiohttp.test_utils import make_mocked_request, TestClient, TestServer
from thinksmart_client import web as w

assert w._is_loopback_host("127.0.0.1")
assert w._is_loopback_host("localhost")
assert w._is_loopback_host("::1")
assert not w._is_loopback_host("192.168.1.88")
assert not w._is_loopback_host("evil.example")

assert w._local_host_ok(make_mocked_request("GET", "/", headers={{"Host": "127.0.0.1:8090"}}))
assert w._local_host_ok(make_mocked_request("GET", "/", headers={{"Host": "localhost:8090"}}))
assert w._local_host_ok(make_mocked_request("GET", "/", headers={{"Host": "[::1]:8090"}}))
assert not w._local_host_ok(make_mocked_request("GET", "/", headers={{"Host": "192.168.1.88:8090"}}))
assert not w._local_host_ok(make_mocked_request("GET", "/", headers={{"Host": "evil.example"}}))

req_null = make_mocked_request("POST", "/api/state", headers={{"Host": "127.0.0.1:8090", "Origin": "null"}})
assert not w._local_origin_ok(req_null)
req_cross = make_mocked_request("POST", "/api/state", headers={{"Host": "127.0.0.1:8090", "Sec-Fetch-Site": "cross-site"}})
assert not w._local_origin_ok(req_cross)
req_ok = make_mocked_request("POST", "/api/state", headers={{"Host": "127.0.0.1:8090", "Origin": "http://127.0.0.1:8090", "Sec-Fetch-Site": "same-origin"}})
assert w._local_origin_ok(req_ok)

async def check():
    app = w.make_app(mock.MagicMock(cfg=dict(port=8090)))
    assert app._client_max_size == 32768
    async with TestClient(TestServer(app)) as cli:
        r_form = await cli.post("/api/say", data={{"text": "hi"}}, headers={{"Host": f"127.0.0.1:{{cli.port}}", "Origin": f"http://127.0.0.1:{{cli.port}}"}})
        assert r_form.status == 415, r_form.status
        assert r_form.headers.get("X-Content-Type-Options") == "nosniff"
        r_bad_host = await cli.get("/api/state", headers={{"Host": "192.168.1.88:8090"}})
        assert r_bad_host.status == 403
        r_idx = await cli.get("/", headers={{"Host": f"127.0.0.1:{{cli.port}}"}})
        assert r_idx.status == 200
        csp = r_idx.headers.get("Content-Security-Policy", "")
        assert "frame-src 'self' about: http://127.0.0.1:* http://localhost:*" in csp, csp
        r_nan = await cli.post("/api/alarms/create", data=b'{{"x": NaN}}', headers={{"Host": f"127.0.0.1:{{cli.port}}", "Content-Type": "application/json"}})
        assert r_nan.status == 400
asyncio.run(check())
print("OK")
"""
        rc, out, err = _run_py(CLIENT_PY, code)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "OK")

    def test_02_client_config_alarms_workdays_and_guards(self):
        code = f"""
import datetime, stat, sys, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, {str(CLIENT_ROOT)!r})
from thinksmart_client import config as cfg_mod, alarms as al_mod, workdays as wd_mod, settings_api as sa_mod

orig_path = cfg_mod.PATH
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "config.json"
    sym = Path(td) / "sym.json"
    sym.symlink_to(p)
    try:
        cfg_mod.PATH = p
        cfg_mod.save(a=1)
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o644, oct(mode)
        cfg_mod.PATH = sym
        try:
            cfg_mod.save(a=2)
            raise AssertionError("symlink save must fail")
        except (OSError, ValueError):
            pass
    finally:
        cfg_mod.PATH = orig_path

assert al_mod.LOCAL_TZ == ZoneInfo("Asia/Shanghai")
with tempfile.TemporaryDirectory() as td:
    ap = Path(td) / "alarms.json"
    ap.write_text(
        '{{"version": 1, "next_id": 4, "items": ['
        '{{"id": 1, "at": NaN, "kind": "alarm"}}, '
        '{{"id": 2, "at": 1800000000, "kind": "alarm", "snooze": Infinity}}, '
        '{{"id": 3, "at": 1800000000, "kind": "alarm", "snooze": null}}'
        '], "missed": []}}',
        encoding="utf-8",
    )
    al = al_mod.Alarms(directory=td)
    assert [x["id"] for x in al.items] == [3], al.items

with tempfile.TemporaryDirectory() as td:
    ep = Path(td) / "extra.json"
    ep.write_text('{{"holidays": ["2099-01-02"], "workdays": ["2099-01-03"]}}', encoding="utf-8")
    w = wd_mod.Workdays(extra_path=ep)
    assert not w(datetime.date(2099, 1, 2))
    assert w(datetime.date(2099, 1, 3))

assert sa_mod.MAX_BODY == 16384
print("OK")
"""
        rc, out, err = _run_py(CLIENT_PY, code)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "OK")

    def test_03_client_app_bt_device_wake_verify_source_guards(self):
        app_src = (CLIENT_ROOT / "thinksmart_client/app.py").read_text(encoding="utf-8")
        bt_src = (CLIENT_ROOT / "thinksmart_client/bt.py").read_text(encoding="utf-8")
        dev_src = (CLIENT_ROOT / "thinksmart_client/device.py").read_text(encoding="utf-8")
        wv_src = (CLIENT_ROOT / "thinksmart_client/wake_verify.py").read_text(encoding="utf-8")
        sa_src = (CLIENT_ROOT / "thinksmart_client/settings_api.py").read_text(encoding="utf-8")
        self.assertIn("_intercom_active", app_src)
        self.assertIn("reconnect_idle", app_src)
        self.assertIn("preview_lock", app_src)
        self.assertIn("trust_env=False", app_src)
        self.assertIn("_send_lock", bt_src)
        self.assertIn("force=False", dev_src)
        self.assertIn("ProxyHandler({})", wv_src)
        self.assertIn("_reject_json_const", sa_src)


class TestClientOverlayAndKioskRound1(unittest.TestCase):
    def test_01_overlay_css_zindex_and_voice_card_fixes(self):
        ov = OVERLAY_JS.read_text(encoding="utf-8")
        self.assertNotIn("18.5px", ov)
        self.assertNotIn("19.5px", ov)
        self.assertNotIn("font-weight:600", ov)
        self.assertNotIn("font-weight:800", ov)
        self.assertNotIn("brightness(.92)", ov)
        for col in ("#19253a", "#1a2840", "9200", "9205", "9210", "9820", "9850", "9900"):
            self.assertIn(col, ov)
        self.assertIn("closeBrightness(false); forceCloseSheets();", ov)
        self.assertIn("vcTurnTool", ov)

    def test_02_client_v2_and_kiosk_fixes(self):
        css = (CLIENT_ROOT / "web/v2/app.css").read_text(encoding="utf-8")
        html = (CLIENT_ROOT / "web/v2/index.html").read_text(encoding="utf-8")
        app_js = (CLIENT_ROOT / "web/v2/app.js").read_text(encoding="utf-8")
        set_js = (CLIENT_ROOT / "web/v2/settings.js").read_text(encoding="utf-8")
        kb_js = KEYBOARD_JS.read_text(encoding="utf-8")
        kiosk_py = KIOSK_PY.read_text(encoding="utf-8")

        self.assertIn("z-index: 26", css)
        self.assertIn('maxlength="63"', html)
        self.assertIn("invalid:", app_js)
        self.assertIn("pttEnd()", app_js)
        self.assertIn("inflight", set_js)
        self.assertIn("r.value = p", set_js)
        self.assertIn("visibility: hidden", kb_js)
        self.assertNotIn("update_status", kiosk_py)
        self.assertNotIn("update_request", kiosk_py)


class TestHubAndAdminRound1(unittest.TestCase):
    def test_01_hub_edge_kiosk_voice_pages_and_yaml(self):
        code = f"""
import sys, yaml
from pathlib import Path
sys.path.insert(0, {str(HUB_ROOT)!r})
from core.providers.tts.edge import _parse_edge_int
from core.admin.pages import _check_vendor_url
from core.admin import kiosk_voice

assert _parse_edge_int(0, 50) == 0
assert _parse_edge_int("0%", 50) == 0
assert _parse_edge_int("+50%", 0) == 50
assert _parse_edge_int("-15Hz", 0) == -15

_check_vendor_url("https://api.example.com/v1")
for bad in ("https://user:pass@api.example.com/v1", "http://0.0.0.0:8000", "http://169.254.169.254/latest", "http://224.0.0.1/"):
    try:
        _check_vendor_url(bad)
        raise AssertionError(f"Expected rejection for {{bad}}")
    except ValueError:
        pass

cfg = yaml.safe_load(Path({str(HUB_ROOT / 'config.yaml')!r}).read_text(encoding="utf-8"))
assert cfg["TTS"]["EdgeTTS"]["volume"] == "+50", cfg["TTS"]["EdgeTTS"]
assert "weather_city" in Path({str(HUB_ROOT / 'core/admin/kiosk_voice.py')!r}).read_text(encoding="utf-8")
print("OK")
"""
        rc, out, err = _run_py(HUB_PY, code)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "OK")

    def test_02_hub_source_and_admin_hardening(self):
        ah = (HUB_ROOT / "core/admin/admin_handler.py").read_text(encoding="utf-8")
        ch = (HUB_ROOT / "core/agent/chat_history.py").read_text(encoding="utf-8")
        cn = (HUB_ROOT / "core/connection.py").read_text(encoding="utf-8")
        ut = (HUB_ROOT / "core/providers/tools/unified_tool_handler.py").read_text(encoding="utf-8")
        ca = (HUB_ROOT / "core/agent/call_names.py").read_text(encoding="utf-8")
        adm = ADMIN_PY.read_text(encoding="utf-8")
        adm_html = ADMIN_HTML.read_text(encoding="utf-8")

        self.assertIn("recycle_idle", ah)
        self.assertIn("reconnect_idle", ah)
        self.assertIn("_PROXY_FWD_HEADERS", ah)
        self.assertIn("enabled", ch)
        self.assertIn("INVOCATION_ID", cn)
        self.assertIn("response.result", ut)
        self.assertNotIn("response.content", ut)
        self.assertIn("S_ISVTX", ca)
        self.assertIn("state_file_lock", adm)
        self.assertIn("btnSaveQqQuality", adm_html)
        self.assertIn("热重载", adm_html)


class TestServicesAndSystemRound1(unittest.TestCase):
    def test_01_qqmusic_security_sleep_volume_and_arbitration(self):
        code = f"""
import asyncio, importlib.util, tempfile
from pathlib import Path
from unittest import mock
from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location("ts_qq", {str(QQ_PY)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

with tempfile.TemporaryDirectory() as td:
    td_p = Path(td)
    with mock.patch.object(mod, "SESSION_PATH", td_p / "sess.json"), \\
         mock.patch.object(mod, "_write_sleep_state", lambda *_: None), \\
         mock.patch.object(mod.QQMusicEngine, "_read_initial_volume", lambda self: None):
        eng = mod.QQMusicEngine()
        for bad in (float("nan"), float("inf"), float("-inf"), 10.5, True, False, 4, 61):
            r = eng.sleep_set_volume(bad)
            assert not r.get("ok"), (bad, r)

        eng.sleep_mode = "time"
        eng.sleep_session = {{"mode": "time", "sound": {{"kind": "ambient"}}}}
        eng.ambient_preset = "rain"
        eng.control("pause", source="intercom")
        assert eng.sleep_mode == "time" and eng.sleep_session is not None

async def check():
    app = mod.make_app()
    async with TestClient(TestServer(app)) as cli:
        r_ok = await cli.get("/status?lite=1", headers={{"Host": f"127.0.0.1:{{cli.port}}"}})
        assert r_ok.status != 403, r_ok.status
        r_lan_host = await cli.get("/status?lite=1", headers={{"Host": "192.168.1.88:8091"}})
        assert r_lan_host.status == 403, r_lan_host.status
asyncio.run(check())
print("OK")
"""
        rc, out, err = _run_py("/opt/thinksmart-addons/venv/bin/python", code)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "OK")

        qq_src = QQ_PY.read_text(encoding="utf-8")
        self.assertIn("web.TCPSite(runner, '127.0.0.1', 8091)", qq_src)
        self.assertIn("prev_bt is not True", qq_src)

    def test_02_home_monitor_study_tutor_intercom_and_system(self):
        home_html = HOME_HTML.read_text(encoding="utf-8")
        home_css = home_html.split("<style>", 1)[1].split("</style>", 1)[0]
        for bad_px in ("1.5px", "1.6px", "2.6px"):
            self.assertNotIn(bad_px, home_css)
        self.assertIn("calRetryT", home_html)
        self.assertIn("lastWeatherCity", home_html)
        self.assertIn("23, 59, 59", home_html)

        self.assertIn("_state_file_lock", HOME_PY.read_text(encoding="utf-8"))
        mon_src = MONITOR_PY.read_text(encoding="utf-8")
        self.assertIn("return hn_clean in ('127.0.0.1', 'localhost', '::1')", mon_src)
        mon_html = MONITOR_HTML.read_text(encoding="utf-8")
        self.assertIn("ps = null", mon_html)
        self.assertIn("tsStopPortraitScroll", mon_html)

        self.assertIn("_config_lock_cm", STUDY_PY.read_text(encoding="utf-8"))
        tutor_src = TUTOR_PY.read_text(encoding="utf-8")
        self.assertIn("O_NOFOLLOW", tutor_src)
        self.assertIn("/kiosk/tts", tutor_src)
        self.assertIn("/kiosk/asr", tutor_src)

        ic_src = INTERCOM_PY.read_text(encoding="utf-8")
        self.assertIn('"source": "intercom"', ic_src)
        self.assertIn("invocation:thinksmart-tutor.service", ic_src)

        self.assertIn('"source":"airplay"', AIRPLAY_PY.read_text(encoding="utf-8"))
        self.assertIn("O_NOFOLLOW", PROX_PY.read_text(encoding="utf-8"))
        self.assertIn("O_NOFOLLOW", UPDATE_SBIN.read_text(encoding="utf-8"))
        rev_src = REVIEW_SBIN.read_text(encoding="utf-8")
        self.assertIn("/var/lib/thinksmart-admin/", rev_src)
        self.assertIn("/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/", rev_src)


if __name__ == "__main__":
    unittest.main()
