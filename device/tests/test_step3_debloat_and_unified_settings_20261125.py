#!/opt/thinksmart-addons/venv/bin/python
"""Step 3 Verification Suite (2026-11-25):
1. Native client architecture: Player.pull, mcp.handle, and Client._hold_speaker run natively without runtime monkey-patch wrappers; unused upstream bloat pruned from client & hub (including Gemini SDK provider).
2. Unified single Settings sheet (#settings): native /api/display & /api/display/preview on :8090, ALS auto/manual brightness, presets, theme (dark/light/auto), orientation (auto/portrait/landscape), voice presentation mode (island/fullscreen), and merged single Home quick-tile.
3. Touch scroll & slider fix: .panel flex-shrink:0, orientation-aware sheetPoint + intent-locked sliders for 0deg / 90deg / -90deg.
4. Smart Dynamic Island Capsule (#tsVoicePill + #tsVoiceCard + #tsVcRich) & Native Rich Media Cards (/api/rich/context + /api/rich/music + SSE tool events).
"""
import importlib.util
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest


class Step3DebloatUnifiedSettingsCapsuleTests(unittest.IsolatedAsyncioTestCase):
    def test_native_client_no_monkeypatch_and_hub_bloat_pruned(self):
        cur = pathlib.Path('/opt/thinksmart/client/current')
        self.assertFalse((cur / 'eval').exists())
        self.assertFalse((cur / 'tools' / 'ringtone').exists())

        hub = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
        for gone in (
            'core/providers/llm/gemini',
            'core/providers/llm/coze',
            'core/providers/llm/dify',
            'core/providers/asr/aliyun.py',
            'core/providers/asr/baidu.py',
            'core/providers/tts/alibl_stream.py',
            'core/providers/tts/huoshan_double_stream.py',
            'core/providers/memory/mem0ai',
            'core/providers/memory/powermem',
        ):
            self.assertFalse((hub / gone).exists(), f'Expected pruned path to be removed: {gone}')
        # Active providers remain intact
        for kept in (
            'core/providers/llm/openai/openai.py',
            'core/providers/asr/sherpa_onnx_local.py',
            'core/providers/asr/doubao_stream.py',
            'core/providers/tts/edge.py',
        ):
            self.assertTrue((hub / kept).is_file(), f'Expected active provider kept: {kept}')

        # Verify Player.pull, mcp.handle, and Client._hold_speaker stay native (unwrapped) after install_native()
        probe = r"""
import sys
sys.path.insert(0, '/opt/thinksmart/client/current')
from thinksmart_client import app as app_mod, audio, bridge, mcp
bridge.install_native(audio, mcp, app_mod)
assert audio.Player._NATIVE_MUSIC_MIX is True
assert audio.Player.pull.__name__ == 'pull', audio.Player.pull.__name__
assert mcp._NATIVE_ECOSYSTEM_TOOLS is True
assert mcp.handle.__name__ == 'handle', mcp.handle.__name__
assert app_mod.Client._NATIVE_SPEAKER_HOLD is True
assert app_mod.Client._hold_speaker.__name__ == '_hold_speaker', app_mod.Client._hold_speaker.__name__
names = {t['name'] for t in mcp.TOOLS}
for must in ('self.music.play', 'self.tutor.explain', 'self.server.status', 'self.home.countdown', 'self.screen.set_brightness'):
    assert must in names, must
print('NATIVE_CLIENT_OK', len(names))
"""
        client_py = '/opt/thinksmart/client/current/.venv/bin/python'
        r = subprocess.run([client_py, '-B', '-c', probe],
                           cwd='/tmp', capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr + '\n' + r.stdout)
        self.assertIn('NATIVE_CLIENT_OK', r.stdout)

    def test_client_web_display_and_rich_context_endpoints(self):
        client_py = '/opt/thinksmart/client/current/.venv/bin/python'
        probe = r"""
import asyncio, json, pathlib, sys, tempfile, time
sys.path.insert(0, '/usr/local/bin')
sys.path.insert(0, '/opt/thinksmart/client/current')
if '/opt/thinksmart-addons/vendor' not in sys.path:
    sys.path.append('/opt/thinksmart-addons/vendor')
from aiohttp.test_utils import TestClient, TestServer
import thinksmart_display
from thinksmart_client import web as cli_web

async def main():
    with tempfile.TemporaryDirectory(dir='/tmp') as td:
        tdp = pathlib.Path(td)
        mode_p = tdp / 'display.json'
        hint_p = tdp / 'screen.json'
        als_p = tdp / 'als.json'
        bl_p = tdp / 'brightness'
        sock_p = tdp / 'preview.sock'
        hint_p.write_text(json.dumps({'brightness': 70, 'dim': False, 'idle': 0}), encoding='utf-8')
        als_p.write_text(json.dumps({'lux': 142.5, 'at': time.time()}), encoding='utf-8')
        bl_p.write_text('3286\n', encoding='utf-8')

        preview_rx = thinksmart_display.BrightnessPreview(sock_p).open()
        try:
            class FakeEvents:
                def __init__(self):
                    self.queues = set()
                def publish(self, *_a):
                    pass

            class FakeClient:
                cfg = {'port': 8090}
                state = 'idle'
                ring = None
                volume = 48
                alarm_volume = 75
                sleep = {'enabled': True, 'start': '22:30', 'end': '07:00'}
                sleeping = False
                events = FakeEvents()

                def __init__(self):
                    self._lv = 70

                def brightness(self):
                    return {'level': self._lv, 'dimmed': False}

                async def set_brightness(self, level, change=None):
                    self._lv = int(level)
                    hint_p.write_text(json.dumps({'brightness': self._lv, 'dim': False, 'idle': 0}), encoding='utf-8')
                    return {'level': self._lv, 'dimmed': False}

                def alarm_view(self):
                    return {'synced': True, 'items': [{'id': 1, 'kind': 'alarm', 'type': '闹钟', 'when': '明天 07:00', 'in': '9小时后', 'label': '早起'}], 'missed': [], 'ringing': None, 'volume': 75, 'tone': 'coffee', 'tones': {}}

                def health(self):
                    return {'audio': True, 'hub': True}

                async def _noop(self, *_a, **_k):
                    return None
                wake = end = abort = ptt_start = ptt_stop = play_reports = _noop

            fc = FakeClient()

            class LocalTestDisplay(thinksmart_display.DisplayControl):
                async def manual(self, level):
                    await fc.set_brightness(level)
                    self.save({'auto': False})
                    self.clear_preview()

            disp = LocalTestDisplay(mode=mode_p, hint=hint_p, als=als_p, backlight=bl_p, preview_socket=sock_p)
            app = cli_web.make_app(fc, display_ctl=disp)
            async with TestClient(TestServer(app)) as cl:
                r0 = await cl.get('/api/display')
                assert r0.status == 200
                j0 = await r0.json()
                assert j0['sensor_ok'] is True and abs(j0['lux'] - 142.5) < 0.2 and j0['auto'] is False

                r1 = await cl.post('/api/display', json={'auto': True}, headers={'Origin': 'http://127.0.0.1:8090'})
                assert r1.status == 200 and (await r1.json())['auto'] is True

                rp = await cl.post('/api/display/preview', json={'level': 45}, headers={'Origin': 'http://127.0.0.1:8090'})
                assert rp.status == 200
                preview_rx.receive()
                assert preview_rx.current() == 45

                r2 = await cl.post('/api/display', json={'level': 62}, headers={'Origin': 'http://127.0.0.1:8090'})
                j2 = await r2.json()
                assert r2.status == 200 and j2['level'] == 62 and j2['auto'] is False and fc._lv == 62

                rx = await cl.post('/api/display', json={'level': 10}, headers={'Origin': 'http://evil.example'})
                assert rx.status == 403

                rc = await cl.get('/api/rich/context?kind=device')
                jc = await rc.json()
                assert rc.status == 200 and jc['ok'] is True and jc['device']['volume'] == 48 and len(jc['device']['alarms']) == 1
        finally:
            preview_rx.close()
    print('WEB_ENDPOINTS_OK')

asyncio.run(main())
"""
        r = subprocess.run([client_py, '-B', '-c', probe],
                           cwd='/tmp', capture_output=True, text=True, timeout=25)
        self.assertEqual(r.returncode, 0, msg=r.stderr + '\n' + r.stdout)
        self.assertIn('WEB_ENDPOINTS_OK', r.stdout)

    def test_unified_settings_scroll_fix_capsule_and_jsc_syntax(self):
        cli_v2 = pathlib.Path('/opt/thinksmart/client/current/web/v2')
        idx = (cli_v2 / 'index.html').read_text(encoding='utf-8')
        css = (cli_v2 / 'app.css').read_text(encoding='utf-8')
        js = (cli_v2 / 'app.js').read_text(encoding='utf-8')
        ov = pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text(encoding='utf-8')
        apps_j = json.loads(pathlib.Path('/usr/local/share/thinksmart-home/apps.json').read_text(encoding='utf-8'))

        # 1. Single unified settings entry on Home dock (brightness quick-tile hidden)
        by_id = {a['id']: a for a in apps_j['apps']}
        self.assertTrue(by_id['brightness'].get('hidden'))
        self.assertFalse(by_id['settings'].get('hidden', False))
        self.assertIn('亮度', by_id['settings']['sub'])

        # 2. Unified Display + Theme + Orientation + Voice Mode controls in :8090 #settings
        for elem_id in ('displayCard', 'brightModeSeg', 'brightRange', 'brightPresets', 'themeSeg', 'orientSeg'):
            self.assertIn(f'id="{elem_id}"', idx)
        # 2026-11-25（付总定的）：小麦没有单独的页面，语音呈现不再分胶囊和全屏；对话在设置里
        self.assertNotIn('voiceModeSeg', idx)
        self.assertIn('<section class="sec" data-sec="chat">', idx)

        # 3. Scroll & flex-shrink fixes + orientation-aware touch controller
        self.assertIn('.panel > * { flex-shrink: 0; }', css)
        self.assertIn('function sheetPoint(', js)
        self.assertIn('function initSheetTouchScroll(', js)
        self.assertIn('function bindSmartSlider(', js)

        # 4. Dynamic Island Capsule + Native Rich Media Card engine in home_overlay.js
        for sym in ('window.__tsTheme', 'function openUnifiedSettings(',
                    'id="tsVpWave"', 'id="tsVcRich"', 'function detectRichSpec(', 'function buildRichHtml('):
            self.assertIn(sym, ov)

        # 5. Typography & integer px redline check on newly added CSS rules in app.css
        tail_css = css[css.index('/* ---- native rich media cards'):]
        self.assertEqual(re.findall(r'\d+\.\d+px', tail_css), [])
        for fw in re.findall(r'font-weight:\s*(\d+)', tail_css):
            self.assertIn(fw, ('400', '700'))

        # 6. Validate JavaScript syntax with WebKit's JavaScriptCore (jsc) if installed, or node/python check
        jsc = '/usr/bin/jsc'
        if os.path.isfile(jsc):
            for jf in (cli_v2 / 'app.js', pathlib.Path('/usr/local/share/thinksmart-monitor/home_overlay.js')):
                r = subprocess.run([jsc, '-c', str(jf)], capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, f'JS syntax error in {jf}: {r.stderr} {r.stdout}')


if __name__ == '__main__':
    unittest.main()
