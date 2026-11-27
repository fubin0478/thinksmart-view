#!/opt/thinksmart-addons/venv/bin/python
"""Step 4 regression suite (2026-11-25):
1. Pruned 31 verified-dead legacy Xiaozhi files (ESP32 bind_code WAVs, call_device, alibl_endpoint, performance_tester, upstream installer scripts).
2. Unified Weather: xiaozhi-hub get_weather queries ThinkSmart Home (:8095) weather service first for 100% consistent spoken + visual weather, falling back to Open-Meteo for uncached locations.
3. Family-safe Personas: change_role replaces toy roles ("机车女友"/"好奇小男孩") with ThinkSmart family roles (家庭管家/伴学导师/英语老师/故事姐姐).
4. Unified Single Admin Entry: :16688/admin static page redirects cleanly to :18091/admin/.
5. Unified Timer / Reminder / Alarm: MCP schema prioritizes self.home.timer & self.home.reminder, live timer calls bridge to Home timer, client exposes /api/alarms with ETag, and Home /api/home/state surfaces active alarms.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

HUB_ROOT = Path("/home/ubuntu/xiaozhi-hub-server")
HUB_SRV = HUB_ROOT / "main" / "xiaozhi-server"
CLIENT_ROOT = Path("/opt/thinksmart/client/current")
CLIENT_PY = str(CLIENT_ROOT / ".venv" / "bin" / "python")
HUB_PY = str(HUB_SRV / ".venv" / "bin" / "python")


class TestStep4XiaozhiDeepMerge(unittest.TestCase):
    def test_01_legacy_bloat_files_pruned_and_core_assets_preserved(self):
        pruned = [
            HUB_SRV / "config" / "assets" / "bind_code",
            HUB_SRV / "config" / "assets" / "bind_code.wav",
            HUB_SRV / "config" / "assets" / "bind_not_found.wav",
            HUB_SRV / "plugins_func" / "functions" / "call_device.py",
            HUB_SRV / "core" / "utils" / "alibl_endpoint.py",
            HUB_SRV / "performance_tester.py",
            HUB_SRV / "performance_tester",
            HUB_ROOT / "scripts" / "install.ps1",
            HUB_ROOT / "scripts" / "start.ps1",
            HUB_ROOT / "scripts" / "uninstall.ps1",
            HUB_ROOT / "scripts" / "install.sh",
            HUB_ROOT / "scripts" / "uninstall.sh",
            HUB_ROOT / "scripts" / "autostart.sh",
            HUB_ROOT / "scripts" / "doctor.py",
            HUB_ROOT / "scripts" / "install_deps.py",
        ]
        for p in pruned:
            self.assertFalse(p.exists(), f"Legacy bloat should be deleted: {p}")

        preserved = [
            HUB_SRV / "config" / "assets" / "wakeup_words_short.wav",
            HUB_SRV / "config" / "assets" / "tts_notify.mp3",
            HUB_ROOT / "scripts" / "start.sh",
        ]
        for p in preserved:
            self.assertTrue(p.exists(), f"Active asset/stub must remain: {p}")

    def test_02_hub_weather_unifies_with_home_and_personas_cleaned(self):
        script = r"""
import asyncio, json, os, sys, types
from aiohttp import web
sys.path.insert(0, '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')

from plugins_func.functions import change_role as cr
from plugins_func.functions import get_weather as gw

# 1. Verify toy personas removed and ThinkSmart family roles work
assert '机车女友' not in cr.prompts and '好奇小男孩' not in cr.prompts
assert set(cr.prompts.keys()) == {'家庭管家', '伴学导师', '英语老师', '故事姐姐'}

changed = []
fake_conn = types.SimpleNamespace(
    change_system_prompt=lambda p: changed.append(p),
    logger=cr.logger,
    config={'plugins': {'get_weather': {'location': ''}}},
    client_ip='127.0.0.1',
)
r_ok = cr.change_role(fake_conn, '伴学模式', '小麦')
assert '伴学导师' in r_ok.response and len(changed) == 1 and '苏格拉底' in changed[0]
r_bad = cr.change_role(fake_conn, '机车女友', '小智')
assert '家庭管家' in r_bad.response and len(changed) == 1

# 2. Verify get_weather reads ThinkSmart Home (:8095) weather first, and returns None for uncached cities
async def run_weather_check():
    app = web.Application()
    base_payload = {
        'ok': True,
        'city': '测试城',
        'cities': ['测试城', '北京'],
        'updated': 1764044400,
        'now': {'temp': '6', 'feels': '3', 'humidity': '42', 'text': '晴', 'wind_dir': '西北风', 'wind_scale': '3'},
        'air': {'aqi': '32', 'category': '优', 'pollutants': [{'name': 'PM2.5', 'value': '14'}]},
        'daily': [
            {'date': '2026-11-25', 'text': '晴', 'text_night': '多云', 'min': '-4', 'max': '8', 'sunrise': '07:28', 'sunset': '17:09'},
            {'date': '2026-11-26', 'text': '多云', 'text_night': '多云', 'min': '-5', 'max': '6'},
        ],
        'hourly': [
            {'time': '14:00', 'temp': '7', 'pop': '10', 'text': '晴'},
            {'time': '15:00', 'temp': '6', 'pop': '0', 'text': '晴'},
        ],
    }
    bj_payload = {
        **base_payload,
        'city': '北京',
        'now': {'temp': '9', 'feels': '7', 'humidity': '35', 'text': '多云', 'wind_dir': '北风', 'wind_scale': '2'},
        'hourly': [{'time': '18:00', 'temp': '5', 'pop': '65', 'text': '小雨'}],
    }
    async def h_weather(_req):
        return web.json_response(base_payload)
    async def h_detail(req):
        c = req.query.get('city', '')
        if c == '北京':
            return web.json_response(bj_payload)
        if c == '测试城':
            return web.json_response(base_payload)
        return web.Response(status=400, text='unsupported city')

    app.router.add_get('/api/home/weather', h_weather)
    app.router.add_get('/api/home/weather/detail', h_detail)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    os.environ['THINKSMART_HOME_WEATHER_URL'] = f'http://127.0.0.1:{port}'
    try:
        res_local = await gw.get_weather(fake_conn)
        assert '测试城（主页同源实况）' in res_local.result, res_local.result
        assert '6°C' in res_local.result and 'AQI 32' in res_local.result and 'PM2.5 14' in res_local.result
        assert '日出 07:28' in res_local.result and '基本不会下雨' in res_local.result

        res_bj = await gw.get_weather(fake_conn, location='北京市')
        assert '北京（主页同源实况）' in res_bj.result and '9°C' in res_bj.result
        assert '18:00可能下雨' in res_bj.result

        miss = await gw.home_weather_report(fake_conn, '伦敦')
        assert miss is None
    finally:
        os.environ.pop('THINKSMART_HOME_WEATHER_URL', None)
        await runner.cleanup()

asyncio.run(run_weather_check())
print('HUB_STEP4_OK')
"""
        r = subprocess.run([HUB_PY, "-B", "-c", script], capture_output=True, text=True, timeout=45)
        self.assertEqual(r.returncode, 0, f"Hub step4 test failed:\n{r.stderr}\n{r.stdout}")
        self.assertIn("HUB_STEP4_OK", r.stdout)

    def test_03_hub_admin_redirects_to_unified_18091_admin(self):
        idx_p = HUB_SRV / "core" / "admin" / "static" / "index.html"
        txt = idx_p.read_text(encoding="utf-8")
        self.assertLess(len(txt.encode("utf-8")), 8192)
        self.assertIn(":18091/admin/", txt)
        self.assertIn("location.replace", txt)

    def test_04_client_mcp_timer_bridge_and_alarms_api_and_ui_cleanup(self):
        app_js = (CLIENT_ROOT / "web" / "v2" / "app.js").read_text(encoding="utf-8")
        idx_html = (CLIENT_ROOT / "web" / "v2" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("UPD_STAGE", app_js)
        self.assertNotIn("gemini-2.5-flash", app_js)
        self.assertIn("放首周杰伦的歌", app_js)
        self.assertIn("开始专注 25 分钟", app_js)
        self.assertIn('id="upd"', idx_html)
        self.assertNotIn("Grok Bot", idx_html)

        script = r"""
import asyncio, json, sys, types
from aiohttp.test_utils import TestClient, TestServer
sys.path.insert(0, '/opt/thinksmart/client/current')

from thinksmart_client import app as cli_app, bridge, mcp, web as cli_web

assert not hasattr(cli_app, 'REPORT_POLL') and cli_app.MODEL_POLL == 25
alarm_desc = next(t['description'] for t in mcp.TOOLS if t['name'] == 'self.alarm.create')
assert 'self.home.timer' in alarm_desc and 'self.home.reminder' in alarm_desc

async def run_client_checks():
    routed_calls = []
    created_alarms = []

    async def fake_dispatch(name, args):
        routed_calls.append((name, args))
        return '已在主页开始 5 分钟倒计时'

    orig_dispatch = bridge.dispatch_ecosystem_tool
    bridge.dispatch_ecosystem_tool = fake_dispatch
    try:
        # Live connected client -> pure timer routes to Home timer
        live_c = types.SimpleNamespace(
            hub=types.SimpleNamespace(connected=True),
            create_alarm=lambda **kw: created_alarms.append(kw),
        )
        rep1 = await mcp.handle({
            'id': 1, 'method': 'tools/call',
            'params': {'name': 'self.alarm.create', 'arguments': {'kind': 'timer', 'minutes': 5, 'label': '煮鸡蛋'}},
        }, client=live_c)
        d1 = json.loads(rep1['result']['content'][0]['text'])
        assert d1['success'] and d1['routed'] == 'self.home.timer' and d1['seconds'] == 300
        assert len(routed_calls) == 1 and routed_calls[0][1]['label'] == '煮鸡蛋'
        assert len(created_alarms) == 0

        # Offline unit-test client -> falls through to local create_alarm
        async def fake_create_alarm(**kw):
            created_alarms.append(kw)
            return {'id': 7, 'type': '倒计时', 'label': kw.get('label', ''), 'when': '12:35', 'in': '5 分钟后', 'repeat': ''}
        off_c = types.SimpleNamespace(
            hub=types.SimpleNamespace(connected=False),
            create_alarm=fake_create_alarm,
        )
        rep2 = await mcp.handle({
            'id': 2, 'method': 'tools/call',
            'params': {'name': 'self.alarm.create', 'arguments': {'kind': 'timer', 'minutes': 5, 'label': '测试'}},
        }, client=off_c)
        d2 = json.loads(rep2['result']['content'][0]['text'])
        assert d2['success'] and d2['id'] == 7 and len(created_alarms) == 1
    finally:
        bridge.dispatch_ecosystem_tool = orig_dispatch

    # Verify GET /api/alarms + ETag 304
    async def _noop(*_a, **_kw):
        return {'ok': True}

    class FakeClient:
        cfg = {'port': 8090}
        state = 'idle'
        ring = None
        volume = 55
        alarm_volume = 70
        sleep = {'enabled': False, 'start': '22:00', 'end': '07:00'}
        sleeping = False
        events = types.SimpleNamespace(queues=set(), publish=lambda *a: None)
        wake = end = abort = ptt_start = ptt_stop = play_reports = _noop
        def snapshot(self):
            return {'status': {'state': 'idle'}, 'models': {}, 'thread': {'entry': None, 'messages': []}}
        def brightness(self):
            return {'level': 65, 'dimmed': False}
        def alarm_view(self):
            return {
                'synced': True, 'ringing': None, 'missed': [], 'volume': 70, 'tone': 'coffee',
                'tones': {'coffee': '咖啡时光'},
                'items': getattr(self, '_items', [{'id': 2, 'kind': 'alarm', 'type': '闹钟', 'label': '早起', 'at': 1764025200, 'when': '明天 07:00', 'in': '18 小时后', 'repeat': '每天', 'snoozed': False}]),
            }
        async def create_alarm(self, kind, label='', say='', repeat=None, **when):
            item = {'id': 9, 'kind': kind, 'type': '闹钟', 'label': label, 'at': 1764027000, 'when': f"明天 {when.get('time', '07:30')}", 'in': '18 小时后', 'repeat': '每个工作日' if repeat else ''}
            self._items = [item]
            return item
        def health(self):
            return {'audio': True, 'hub': True}

    wapp = cli_web.make_app(FakeClient(), display_ctl=None)
    async with TestClient(TestServer(wapp)) as tc:
        r = await tc.get('/api/alarms')
        assert r.status == 200
        etag = r.headers.get('ETag')
        assert etag
        j = await r.json()
        assert j['ok'] and len(j['items']) == 1 and j['items'][0]['label'] == '早起' and j['volume'] == 70
        r304 = await tc.get('/api/alarms', headers={'If-None-Match': etag})
        assert r304.status == 304

        rc = await tc.post('/api/alarms/create', json={'kind': 'alarm', 'time': '07:30', 'repeat': 'workdays', 'label': '上学'})
        assert rc.status == 200
        jc = await rc.json()
        assert jc['ok'] and jc['item']['label'] == '上学'

asyncio.run(run_client_checks())
print('CLIENT_STEP4_OK')
"""
        r = subprocess.run([CLIENT_PY, "-B", "-c", script], capture_output=True, text=True, timeout=45)
        self.assertEqual(r.returncode, 0, f"Client step4 test failed:\n{r.stderr}\n{r.stdout}")
        self.assertIn("CLIENT_STEP4_OK", r.stdout)

    def test_05_home_state_and_alarms_board_integration(self):
        import importlib.util
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        # Verify dock tile "闹钟留言" and Alarms-before-Messages order in Home HTML
        apps = json.loads(Path("/usr/local/share/thinksmart-home/apps.json").read_text(encoding="utf-8"))["apps"]
        msg_tile = next(a for a in apps if a["id"] == "messages")
        self.assertFalse(msg_tile.get("hidden", False))
        self.assertEqual(msg_tile["title"], "闹钟留言")
        self.assertEqual(msg_tile["icon"], "alarm")

        home_html = Path("/usr/local/share/thinksmart-home/index.html").read_text(encoding="utf-8")
        self.assertLess(home_html.index('id="mpTabAlarm"'), home_html.index('id="mpTabMsg"'))
        self.assertLess(home_html.index('id="mpAlarmPane"'), home_html.index('id="mpMsgPane"'))
        self.assertIn("dock-6x1", home_html)

        spec = importlib.util.spec_from_file_location("ts_home_s4", "/usr/local/bin/thinksmart-home.py")
        hm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hm)

        async def run_home_alarm_bridge():
            fake_cli = web.Application()
            store = [{"id": 1, "kind": "alarm", "type": "闹钟", "label": "起床", "at": 1764025200, "when": "明天 07:00", "repeat": "每个工作日"}]
            async def _fake_alarms(_r):
                return web.json_response({
                    "ok": True, "synced": True, "ringing": False, "volume": 75, "tone": "coffee",
                    "tones": {"coffee": "咖啡时光"}, "missed": [], "items": list(store),
                })
            async def _fake_create(req):
                b = await req.json()
                it = {"id": 2, "kind": "alarm", "type": "闹钟", "label": b.get("label", ""), "at": 1764027000, "when": f"明天 {b.get('time', '07:30')}", "repeat": ""}
                store.append(it)
                return web.json_response({"ok": True, "item": it})
            async def _fake_delete(req):
                b = await req.json()
                store[:] = [x for x in store if x["id"] != int(b["id"])]
                return web.json_response({"ok": True})
            fake_cli.router.add_get("/api/alarms", _fake_alarms)
            fake_cli.router.add_post("/api/alarms/create", _fake_create)
            fake_cli.router.add_post("/api/alarms/delete", _fake_delete)
            runner = web.AppRunner(fake_cli)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]

            with tempfile.TemporaryDirectory() as td:
                td_p = Path(td)
                with patch.dict(os.environ, {"HOME_CLIENT_URL": f"http://127.0.0.1:{port}"}), \
                     patch.object(hm, "CLIENT_API", f"http://127.0.0.1:{port}"), \
                     patch.object(hm, "STATE", td_p), \
                     patch.object(hm, "MSG_PATH", td_p / "messages.json"), \
                     patch.object(hm, "COUNTDOWN_PATH", td_p / "countdowns.json"):
                    happ = hm.make_app()
                    async with TestClient(TestServer(happ)) as tc:
                        resp = await tc.get("/api/home/state", headers={"Host": "127.0.0.1:8095"})
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        self.assertEqual(len(data.get("alarms") or []), 1)
                        self.assertEqual(data["alarms"][0]["label"], "起床")

                        ra = await tc.get("/api/home/alarms", headers={"Host": "127.0.0.1:8095"})
                        ja = await ra.json()
                        self.assertTrue(ja["ok"])
                        self.assertEqual(ja["volume"], 75)

                        rc = await tc.post("/api/home/alarms", json={"action": "create", "kind": "alarm", "time": "06:40", "label": "上学"}, headers={"Host": "127.0.0.1:8095"})
                        jc = await rc.json()
                        self.assertTrue(jc["ok"])
                        self.assertEqual(jc["item"]["label"], "上学")

                        rd = await tc.post("/api/home/alarms", json={"action": "delete", "id": 1}, headers={"Host": "127.0.0.1:8095"})
                        self.assertEqual(rd.status, 200)
                        st2 = await (await tc.get("/api/home/state", headers={"Host": "127.0.0.1:8095"})).json()
                        self.assertEqual([x["label"] for x in st2["alarms"]], ["上学"])
            await runner.cleanup()

        asyncio.run(run_home_alarm_bridge())


if __name__ == "__main__":
    unittest.main()
