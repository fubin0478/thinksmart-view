#!/opt/thinksmart-addons/venv/bin/python
"""Step 2 Verification Suite (2026-11-25):
1. Unified Admin (:8097) full takeover of Xiaozhi controls, dynamic VPN relay CIDRs, and service maintenance actions.
2. Strict Gemini model disablement guardrail across xiaozhi-hub (/kiosk/llm + llm_router) and thinksmart-admin.
3. Legacy /v1 & test blob cleanup in xiaozhi-client, /api/restart idle safety, settings card order, and html.ts-light daytime theme fusion.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


def _load_mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Step2UnifiedDesignTests(unittest.IsolatedAsyncioTestCase):
    def test_hub_gemini_guardrail_blocks_kiosk_dropdown_and_router(self):
        hub_py = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python'
        probe = r"""
import asyncio, os, sys
sys.path.insert(0, '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
from core.agent import llm_router
from core.admin.admin_handler import AdminHandler

cfg = {
    'selected_module': {'LLM': 'DeepSeekLLM'},
    'LLM': {
        'DeepSeekLLM': {'type': 'openai', 'base_url': 'https://api.deepseek.com', 'model_name': 'deepseek-chat', 'api_key': 'sk-test-ds'},
        'OpenAILLM': {'type': 'openai', 'base_url': 'https://api.openai.com/v1', 'model_name': 'gpt-6.1-terra', 'api_key': 'sk-test-oa'},
        'GeminiAPI': {'type': 'openai', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/', 'model_name': 'gemini-4-hydrogen', 'api_key': 'sk-test-gem'},
    },
}
os.environ.pop('THINKSMART_ENABLE_GEMINI', None)
assert not llm_router.gemini_enabled(cfg)
assert llm_router.model_ready(cfg, 'DeepSeekLLM') is True
assert llm_router.model_ready(cfg, 'OpenAILLM') is True
assert llm_router.model_ready(cfg, 'GeminiAPI') is False

ah = object.__new__(AdminHandler)
ah._blocking = lambda fn, *a, **k: asyncio.sleep(0, result=fn(*a, **k))
ah._claude_install = lambda m, version=False: {'installed': False}
km = asyncio.run(ah._kiosk_models(cfg, device='dev-test'))
vendors = [o['vendor'] for o in km['options']]
assert 'gemini' not in vendors, vendors
assert set(vendors) == {'deepseek', 'openai'}, vendors

# Also verify live /kiosk/llm endpoint on :16688 has zero gemini option
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:16688/kiosk/llm?device=probe', timeout=3) as r:
    live_km = json.loads(r.read().decode('utf-8'))
live_vendors = [o.get('vendor') for o in (live_km.get('options') or [])]
assert 'gemini' not in live_vendors, live_vendors
print('HUB_GEMINI_GUARD_OK', live_vendors)
"""
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            r = subprocess.run([hub_py, '-B', '-c', probe], cwd=td, capture_output=True, text=True, timeout=25)
        self.assertEqual(r.returncode, 0, msg=r.stderr + '\n' + r.stdout)
        self.assertIn('HUB_GEMINI_GUARD_OK', r.stdout)

        # Verify :16688/admin static page carries unified admin takeover banner
        hub_idx = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/admin/static/index.html').read_text(encoding='utf-8')
        self.assertIn('id="tsUnifiedBanner"', hub_idx)
        self.assertIn(':18091/admin/', hub_idx)

    async def test_admin_gemini_guardrail_and_native_service_controls(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            td_p = pathlib.Path(td)
            hub_d = td_p / 'hub'
            (hub_d / 'data').mkdir(parents=True)
            (hub_d / 'config.yaml').write_text('selected_module:\n  LLM: DeepSeekLLM\nLLM:\n  DeepSeekLLM:\n    type: openai\n', encoding='utf-8')
            (hub_d / 'data' / '.config.yaml').write_text(
                'selected_module:\n  LLM: DeepSeekLLM\nLLM:\n  DeepSeekLLM:\n    type: openai\n    base_url: https://api.deepseek.com\n    model_name: deepseek-chat\n    api_key: sk-ds-123456\n',
                encoding='utf-8',
            )

            # Mock client (:8090) & hub (:16688) servers
            state_box = {'client_state': 'listening', 'restarted_client': 0, 'restarted_hub': 0, 'reloaded_hub': 0}
            mock_cli = web.Application()

            async def m_events(req):
                resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
                await resp.prepare(req)
                snap = {
                    'version': '1.0.0-self',
                    'status': {'state': state_box['client_state'], 'mic': True, 'brightness': {'level': 80, 'dimmed': False}, 'screen_off': {'enabled': True, 'minutes': 5}, 'sleep': {'enabled': False, 'start': '22:00', 'end': '07:00'}, 'volume': 50},
                    'update': {'enabled': False, 'self_managed': True},
                    'alarms': {'items': [], 'ringing': None},
                    'bluetooth': {'enabled': True, 'devices': []},
                }
                await resp.write(f'event: snapshot\ndata: {json.dumps(snap)}\n\n'.encode())
                return resp

            async def m_restart(req):
                if state_box['client_state'] != 'idle':
                    return web.json_response({'ok': False, 'error': 'busy'}, status=409)
                state_box['restarted_client'] += 1
                return web.json_response({'ok': True})

            async def m_health(_r):
                return web.json_response({'version': '1.0.0-self', 'audio': True, 'hub': True})

            async def m_net(_r):
                return web.json_response({'ip': '192.168.1.50'})

            mock_cli.router.add_get('/events', m_events)
            mock_cli.router.add_get('/api/health', m_health)
            mock_cli.router.add_get('/api/network', m_net)
            mock_cli.router.add_post('/api/restart', m_restart)

            mock_hub = web.Application()

            async def m_reload(_r):
                state_box['reloaded_hub'] += 1
                return web.json_response({'ok': True, 'results': {}})

            async def m_diag_get(_r):
                return web.json_response({'ok': True, 'hub_version': {'short': 'abc1234'}})

            async def m_diag_post(req):
                b = await req.json()
                if b.get('action') == 'restart_hub':
                    state_box['restarted_hub'] += 1
                    return web.json_response({'ok': True})
                return web.json_response({'ok': True})

            mock_hub.router.add_post('/kiosk/internal/reload', m_reload)
            mock_hub.router.add_get('/kiosk/internal/diag', m_diag_get)
            mock_hub.router.add_post('/kiosk/internal/diag', m_diag_post)

            async with TestServer(mock_cli) as cli_srv, TestServer(mock_hub) as hub_srv:
                env_ov = {
                    'ADMIN_STATE': str(td_p / 'admin_state'),
                    'ADMIN_HUB_DIR': str(hub_d),
                    'ADMIN_HUB_PY': '/nonexistent',
                    'ADMIN_HUB_API': f'http://127.0.0.1:{hub_srv.port}',
                    'ADMIN_CLIENT_API': f'http://127.0.0.1:{cli_srv.port}',
                    'ADMIN_CLIENT_CFG': str(td_p / 'client_cfg.json'),
                    'ADMIN_UPDATE_STATE': str(td_p / 'upd'),
                    'ADMIN_VPS_URL': 'https://relay.example.invalid:18443/admin/',
                    'THINKSMART_ENABLE_GEMINI': '0',
                }
                with mock.patch.dict(os.environ, env_ov):
                    adm = _load_mod('ts_adm_step2_t', '/usr/local/bin/thinksmart-admin.py')
                    app = adm.make_app(adm.Admin())
                    async with TestClient(TestServer(app)) as cl:
                        hdr = {'X-TS-Peer': '127.0.0.1', 'X-TS-Admin': '1'}
                        rs = await cl.get('/admin/api/session', headers={'X-TS-Peer': '127.0.0.1'})
                        sj = await rs.json()
                        self.assertEqual(sj['vps_url'], 'https://relay.example.invalid:18443/admin/')

                        # Blocking Gemini as active default LLM
                        rg = await cl.post('/admin/api/settings/llm', json={
                            'mode': 'api', 'vendor': 'gemini', 'keep_default': False,
                            'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/',
                            'model': 'gemini-4-hydrogen', 'api_key': 'sk-gem-block-123',
                        }, headers=hdr)
                        self.assertEqual(rg.status, 400)
                        self.assertIn('停用 Gemini', (await rg.json())['error'])

                        # Busy client blocks restart_client with 409
                        rb = await cl.post('/admin/api/settings/device', json={'action': 'restart_client'}, headers=hdr)
                        self.assertEqual(rb.status, 409)

                        # Idle client allows reload_hub, restart_client, restart_hub
                        state_box['client_state'] = 'idle'
                        r1 = await cl.post('/admin/api/settings/device', json={'action': 'reload_hub'}, headers=hdr)
                        self.assertEqual(r1.status, 200)
                        r2 = await cl.post('/admin/api/settings/device', json={'action': 'restart_client'}, headers=hdr)
                        self.assertEqual(r2.status, 200)
                        r3 = await cl.post('/admin/api/settings/device', json={'action': 'restart_hub'}, headers=hdr)
                        self.assertEqual(r3.status, 200)
                        self.assertEqual(state_box['reloaded_hub'], 1)
                        self.assertEqual(state_box['restarted_client'], 1)
                        self.assertEqual(state_box['restarted_hub'], 1)

    def test_client_legacy_cleanup_settings_order_and_light_theme(self):
        cli = pathlib.Path('/opt/thinksmart/client/current')
        self.assertFalse((cli / 'web' / 'index.html').exists())
        self.assertFalse((cli / 'web' / 'app.js').exists())
        self.assertFalse((cli / 'web' / 'app.css').exists())
        self.assertFalse((cli / 'web' / 'test').exists())

        idx = (cli / 'web' / 'v2' / 'index.html').read_text(encoding='utf-8')
        # Brightness & volume appear BEFORE #upd in settings panel
        self.assertLess(idx.index('id="brightRange"'), idx.index('id="upd"'))
        self.assertLess(idx.index('id="upd"'), idx.index('id="powerBtn"'))

        css = (cli / 'web' / 'v2' / 'app.css').read_text(encoding='utf-8')
        self.assertIn('html.ts-light {', css)
        self.assertIn('html.ts-light body {', css)
        self.assertIn('html.ts-light .sheet {', css)
        # Ensure our newly added light-theme block uses no forbidden fractional px or invalid font-weights
        light_part = css[css.index('html.ts-light {'):]
        self.assertEqual(re.findall(r'\d+\.\d+px', light_part), [])
        for fw in re.findall(r'font-weight:\s*(\d+)', light_part):
            self.assertIn(fw, ('400', '700'))

        adm_html = pathlib.Path('/usr/local/share/thinksmart-admin/index.html').read_text(encoding='utf-8')
        self.assertIn('id="btnReloadHubSvc"', adm_html)
        self.assertIn('id="btnRestartClientSvc"', adm_html)
        self.assertIn('id="btnRestartHubSvc"', adm_html)
        self.assertNotIn('GeminiAPI = gemini', adm_html)


if __name__ == '__main__':
    unittest.main()
