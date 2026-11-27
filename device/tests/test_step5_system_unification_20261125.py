#!/opt/thinksmart-addons/venv/bin/python
"""Step 5 System-Wide Feature Merge & Debloat Regression Suite (2026-11-25):
1. Settings sheet (#settings) deduplicated: read-only alarm list / alarm volume / tones merged into #alarmShortcutCard (#openAlarmBoardBtn -> Home 闹钟留言 panel); conflicting legacy sleep hours (#sleepSwitch / self.sleep_mode.*) & Grok report pill (#reportPill) removed; Bluetooth Speaker (#btCard) preserved.
2. Offline welcome page (welcome.html) & overlay (home_overlay.js) cleaned: legacy OTA update UI & QR tutorial removed, standalone /display iframe prewarming retired.
3. Voice Music Tools Unified: bedtime sleep-session start/stop/extend merged into self.music.play & self.music.sleep_timer; UnifiedToolHandler deduplicates play_music schema when self.music.play is present (~1,000 prompt tokens saved per turn).
4. Lunar & Holiday Calendar Unified: get_lunar (get_time.py) queries ThinkSmart Home (:8095/api/home/calendar) for authoritative State Council statutory holidays & makeup workdays.
5. Unused Third-Party Extensions Pruned (~633 KB / 78 files): Grok Bot + reports, ESP32 device_iot, remote mcp_endpoint, voiceprint, HomeAssistant, RAGFlow, Chinanews RSS, client sleep.py, and upstream dev tests removed across Hub, Client & Admin (while keeping VLLM & Bluetooth intact).
"""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

HUB_SRV = Path("/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server")
CLIENT_ROOT = Path("/opt/thinksmart/client/current")
CLIENT_PY = str(CLIENT_ROOT / ".venv/bin/python")
HUB_PY = str(HUB_SRV / ".venv/bin/python")


class TestStep5SystemUnification(unittest.TestCase):
    def test_01_pruned_extensions_and_preserved_core_modules(self):
        pruned = [
            HUB_SRV / "core/agent/grokbot_client.py",
            HUB_SRV / "core/agent/grokbot_session.py",
            HUB_SRV / "core/agent/reports.py",
            HUB_SRV / "core/admin/grokbot.py",
            HUB_SRV / "core/providers/llm/grok_bot",
            HUB_SRV / "core/providers/tools/device_iot",
            HUB_SRV / "core/handle/textHandler/iotMessageHandler.py",
            HUB_SRV / "core/providers/tools/mcp_endpoint",
            HUB_SRV / "core/utils/voiceprint_provider.py",
            HUB_SRV / "plugins_func/functions/get_news_from_chinanews.py",
            HUB_SRV / "plugins_func/functions/hass_init.py",
            HUB_SRV / "plugins_func/functions/hass_state.py",
            HUB_SRV / "plugins_func/functions/hass_play_music.py",
            HUB_SRV / "plugins_func/functions/search_from_ragflow.py",
            HUB_SRV / "tests",
            CLIENT_ROOT / "thinksmart_client/sleep.py",
            CLIENT_ROOT / "tests",
        ]
        for p in pruned:
            self.assertFalse(p.exists(), f"Expected pruned path to be removed: {p}")

        preserved = [
            CLIENT_ROOT / "thinksmart_client/bt.py",
            CLIENT_ROOT / "thinksmart_client/alarms.py",
            HUB_SRV / "plugins_func/functions/get_news_from_newsnow.py",
            HUB_SRV / "plugins_func/functions/get_weather.py",
            HUB_SRV / "plugins_func/functions/get_time.py",
            HUB_SRV / "plugins_func/functions/web_search.py",
            HUB_SRV / "plugins_func/functions/change_role.py",
        ]
        for p in preserved:
            self.assertTrue(p.exists(), f"Expected active core file preserved: {p}")

    def test_02_settings_dedup_welcome_clean_and_admin_form_pruning(self):
        cli_html = (CLIENT_ROOT / "web/v2/index.html").read_text(encoding="utf-8")
        cli_js = (CLIENT_ROOT / "web/v2/app.js").read_text(encoding="utf-8")
        welcome_html = Path("/usr/local/share/thinksmart-kiosk/welcome.html").read_text(encoding="utf-8")
        ov_js = Path("/usr/local/share/thinksmart-monitor/home_overlay.js").read_text(encoding="utf-8")
        home_html = Path("/usr/local/share/thinksmart-home/index.html").read_text(encoding="utf-8")
        adm_html = Path("/usr/local/share/thinksmart-admin/index.html").read_text(encoding="utf-8")

        # Settings panel deduplicated + Bluetooth speaker & AirPlay switch kept together
        self.assertIn('id="airplayCard"', cli_html)
        self.assertIn('id="airplaySwitch"', cli_html)
        self.assertIn('id="btCard"', cli_html)
        for gone_id in ("alarmVolRange", "alarmList", "tones", "sleepSwitch", "sleepTimes", "reportPill", "sleepChip",
                        "alarmShortcutCard", "displaySleepLinkCard", "netMonitorCard"):
            self.assertNotIn(f'id="{gone_id}"', cli_html)
        self.assertIn("__tsOpenHomeAlarms", cli_js)
        self.assertIn("__tsOpenHomeAlarms", ov_js)
        self.assertIn("d.openAlarms", home_html)
        self.assertNotIn("ensureBrightnessDom", ov_js)

        # Welcome page cleaned of legacy OTA updater / QR / old boot copy & redesigned in Home style
        for gone_w in ('id="upd"', "pollUpdate", "16688/admin", "qrcode.js", "接入小智 AI 网关", "正在准备主界面"):
            self.assertNotIn(gone_w, welcome_html)
        for keep_w in (":18091/admin/", "联想 View", "html.ts-portrait", "ts-rot270"):
            self.assertIn(keep_w, welcome_html)
        w_css = welcome_html.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertEqual(re.findall(r"\d+\.\d+px", w_css), [])
        for fw in re.findall(r"font-weight:\s*(\d+)", w_css):
            self.assertIn(fw, ("400", "700"))
        kiosk_src = Path("/usr/local/bin/thinksmart-kiosk.py").read_text(encoding="utf-8")
        for sym in ("BOOT_WAIT_SECONDS", "read_orient", "#0a101c", "wait_and_load"):
            self.assertIn(sym, kiosk_src)
        cli_unit = Path("/etc/systemd/system/thinksmart-client.service").read_text(encoding="utf-8")
        self.assertIn("After=network.target", cli_unit)
        self.assertNotIn("After=network-online.target", cli_unit)

        # Admin UI cleaned of unused third-party/legacy forms while keeping Bluetooth
        self.assertIn('id="devBtEn"', adm_html)
        self.assertIn("家庭管家 / 伴学导师 / 英语老师 / 故事姐姐", adm_html)
        for gone_adm in ("grokBotDetails", "haEnabled", "rfEnabled", "cnRssDefault", "mcpEndpointUrl", "devSleepEn", "btnPlayReports", "机车女友", "vllmUrl", "llmCliBox"):
            self.assertNotIn(gone_adm, adm_html)

        jsc = "/usr/bin/jsc"
        if os.path.isfile(jsc):
            for jf in (CLIENT_ROOT / "web/v2/app.js", Path("/usr/local/share/thinksmart-monitor/home_overlay.js")):
                r = subprocess.run([jsc, "-c", str(jf)], capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, f"JS syntax error in {jf}: {r.stderr} {r.stdout}")
            import tempfile
            w_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", welcome_html, flags=re.S)
            self.assertGreaterEqual(len(w_scripts), 2)
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=True) as tf:
                tf.write("\n;\n".join(w_scripts))
                tf.flush()
                r = subprocess.run([jsc, "-c", tf.name], capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, f"welcome.html JS syntax error: {r.stderr} {r.stdout}")

    def test_03_unified_music_tools_and_sleep_mode_removal(self):
        probe = r"""
import asyncio, json, sys
from aiohttp import web
sys.path.insert(0, '/opt/thinksmart/client/current')
from thinksmart_client import bridge, mcp

tool_names = [t['name'] for t in mcp.TOOLS]
assert 'self.sleep_mode.set' not in tool_names
assert 'self.sleep_mode.get' not in tool_names
assert 'self.music.play' in tool_names
assert 'self.music.sleep_timer' in tool_names

calls = []
async def run_test():
    app = web.Application()
    async def h_sleep_sess(req):
        b = await req.json()
        calls.append(('/api/music/sleep_session', b))
        if b.get('sound') == '周杰伦晴天':
            return web.json_response({'ok': False, 'error': 'not sleep sound'})
        return web.json_response({'ok': True, 'message': '已开启助眠睡眠模式'})
    async def h_play(req):
        b = await req.json()
        calls.append(('/api/music/play', b))
        return web.json_response({'ok': True, 'title': b.get('query'), 'source': 'QQ音乐'})
    async def h_sleep(req):
        b = await req.json()
        calls.append(('/api/music/sleep', b))
        return web.json_response({'ok': True, 'message': '30分钟后停止'})

    app.router.add_post('/api/music/sleep_session', h_sleep_sess)
    app.router.add_post('/api/music/play', h_play)
    app.router.add_post('/api/music/sleep', h_sleep)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    orig_call = bridge._call_qqmusic_api
    def redir_call(path, body):
        import urllib.request
        req = urllib.request.Request(
            f'http://127.0.0.1:{port}{path}',
            data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with bridge._NO_PROXY_OPENER.open(req, timeout=5) as r:
            return r.read().decode('utf-8')
    bridge._call_qqmusic_api = redir_call
    try:
        # 1) Bedtime start via self.music.play(query='我要睡觉了', sleep_minutes=45)
        r1 = json.loads(await bridge.dispatch_ecosystem_tool('self.music.play', {'query': '我要睡觉了', 'sleep_minutes': 45}))
        assert r1['ok'] is True
        assert calls[-1] == ('/api/music/sleep_session', {'action': 'start', 'sound': 'last', 'minutes': 45})

        # 2) Bedtime stop via self.music.play(query='关闭睡眠模式')
        r2 = json.loads(await bridge.dispatch_ecosystem_tool('self.music.play', {'query': '关闭睡眠模式'}))
        assert r2['ok'] is True
        assert calls[-1] == ('/api/music/sleep_session', {'action': 'stop'})

        # 3) Regular song + inline sleep timer falls back cleanly to /api/music/play + /api/music/sleep
        r3 = json.loads(await bridge.dispatch_ecosystem_tool('self.music.play', {'query': '周杰伦晴天', 'sleep_minutes': 30}))
        assert r3['ok'] is True and r3.get('sleep_message') == '30分钟后停止'

        # 4) Extend bedtime via self.music.sleep_timer(extend=True, minutes=20)
        r4 = json.loads(await bridge.dispatch_ecosystem_tool('self.music.sleep_timer', {'extend': True, 'minutes': 20}))
        assert r4['ok'] is True
        assert calls[-1] == ('/api/music/sleep_session', {'action': 'extend', 'minutes': 20})
    finally:
        bridge._call_qqmusic_api = orig_call
        await runner.cleanup()

asyncio.run(run_test())
print('CLIENT_MUSIC_UNIFIED_OK')
"""
        r = subprocess.run([CLIENT_PY, "-B", "-c", probe], capture_output=True, text=True, timeout=25)
        self.assertEqual(r.returncode, 0, f"Client unified music test failed:\n{r.stderr}\n{r.stdout}")
        self.assertIn("CLIENT_MUSIC_UNIFIED_OK", r.stdout)

    def test_04_hub_deduplicates_play_music_and_syncs_statutory_holidays(self):
        probe = r"""
import asyncio, json, sys, types
from datetime import datetime
from unittest.mock import patch
sys.path.insert(0, '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')

from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from plugins_func.functions import get_time as gt
from core.admin import catalog

# 1) Verify pruned plugins removed from catalog
cat_names = {p['name'] for p in catalog.PLUGINS}
assert cat_names == {'get_weather', 'get_news_from_newsnow', 'play_music', 'web_search', 'change_role'}, cat_names

# 2) Verify UnifiedToolHandler deduplicates play_music when self.music.play is present
fake_conn = types.SimpleNamespace(config={'Intent': {'function_call': {'functions': ['get_weather', 'play_music']}}})
uth = UnifiedToolHandler(fake_conn)
sample_descs = [
    {'type': 'function', 'function': {'name': 'get_weather'}},
    {'type': 'function', 'function': {'name': 'play_music'}},
    {'type': 'function', 'function': {'name': 'self.music.play'}},
]
uth.tool_manager.get_function_descriptions = lambda: list(sample_descs)
uth.tool_manager.get_supported_tool_names = lambda: ['get_weather', 'play_music', 'self.music.play']
uth.tool_manager.has_tool = lambda n: n == 'self.music.play'
filtered_names = [f['function']['name'] for f in uth.get_functions()]
assert 'play_music' not in filtered_names and 'self.music.play' in filtered_names, filtered_names
assert 'play_music' not in uth.current_support_functions()

# When self.music.play is NOT present, play_music remains available as fallback
uth.tool_manager.has_tool = lambda _n: False
uth.tool_manager.get_supported_tool_names = lambda: ['get_weather', 'play_music']
fallback_names = [f['function']['name'] for f in uth.get_functions()]
assert 'play_music' in fallback_names

# 3) Verify get_lunar incorporates Home statutory holiday & makeup workday schedule
fake_cal = {
    'month': {
        'year': 2026,
        'month': 10,
        'days': [
            {'date': '2026-10-01', 'hol': 'off', 'hol_name': '国庆节', 'weekend': False, 'cd': ['祖国华诞']},
            {'date': '2026-10-10', 'hol': 'work', 'hol_name': '国庆节', 'weekend': True},
        ],
    }
}
class FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, _n=-1): return json.dumps(fake_cal, ensure_ascii=False).encode('utf-8')

with patch.object(gt._NO_PROXY_OPENER, 'open', return_value=FakeResp()):
    res = gt.get_lunar(date='2026-10-01', query='放假调休安排')
    assert '国庆节放假休息' in res.result, res.result
    assert '2026-10-10(国庆节调休上班)' in res.result, res.result
    assert '祖国华诞' in res.result, res.result

print('HUB_DEDUP_AND_HOLIDAY_OK')
"""
        r = subprocess.run([HUB_PY, "-B", "-c", probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, f"Hub dedup & holiday test failed:\n{r.stderr}\n{r.stdout}")
        self.assertIn("HUB_DEDUP_AND_HOLIDAY_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
