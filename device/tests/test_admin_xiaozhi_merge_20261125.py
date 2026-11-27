#!/opt/thinksmart-addons/venv/bin/python
"""2026-11-25 联想 View × 小麦融合后网页总后台调整回归测试。

覆盖：
1) 管理后台 HTML/CSS 规范（font-weight 400/700、整数 px 字号）、移除已裁减的火山/硅基 TTS 选项、
   小麦统一文案、闹钟留言导航、#wkSound / #cfgChatHistCard / #btnRebootDevice / #btnOpenAlarmMsgTab、JS 语法检查；
2) 本机唤醒提示音（wake_sound: zai / chime / off）读写保存、非法值拦截、以及热同步 client /api/settings；
3) 小麦近期对话记录 /admin/api/chat/history（Hub HTTP 与 chat.db SQLite 双通道降级读取）及按模型/全量清理 /admin/api/chat/clear + 防跨站校验；
4) 远程整机安全重启（action=reboot_device 对话/响铃/通话中 409 拦截，空闲下发 part=system action=reboot）；
5) 对讲页 /?embed=msg 闹钟在前・留言板在后（#alarmBox 先于 #msgBox）、JS 语法检查、以及 /api/alarms 读写/音量/铃声代理与 CSRF 校验。
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ADMIN_PY = os.environ.get('ADMIN_PY', '/usr/local/bin/thinksmart-admin.py')
ADMIN_HTML = os.environ.get('ADMIN_HTML', '/usr/local/share/thinksmart-admin/index.html')
INTERCOM_PY = os.environ.get('INTERCOM_PY', '/usr/local/bin/thinksmart-intercom.py')
ORIGIN = 'http://127.0.0.1'


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load(ADMIN_PY, 'ts_admin_merge_under_test')
IC = _load(INTERCOM_PY, 'ts_intercom_merge_under_test')


def _extract_scripts(html_text: str) -> str:
    return '\n;\n'.join(re.findall(r'<script[^>]*>([\s\S]*?)</script>', html_text))


def _check_js_syntax(js_code: str, tmp_dir: Path, label: str):
    js_path = tmp_dir / f'{label}.js'
    js_path.write_text(js_code, encoding='utf-8')
    for tool, args in (('jsc', ['-c', str(js_path)]), ('node', ['--check', str(js_path)])):
        if shutil.which(tool):
            p = subprocess.run([tool, *args], capture_output=True, text=True, timeout=10)
            if p.returncode != 0:
                raise AssertionError(f'{tool} syntax error in {label}: {p.stderr or p.stdout}')
            return


class TestAdminXiaozhiMerge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ts-adm-merge-'))
        t = self.tmp
        for d in (
            'state', 'ui', 'local', 'hub', 'usb', 'mihomo-ui/_nuxt',
            'xz-hub/data', 'xz-hub/models/SenseVoiceSmall',
            'home-state', 'study-state', 'tutor-state', 'mon-state', 'mon-share', 'update-state', 'client-sounds',
        ):
            (t / d).mkdir(parents=True)
        shutil.copy(ADMIN_HTML, t / 'ui' / 'index.html')
        (t / 'mihomo.yaml').write_text('mixed-port: 7890\nexternal-controller: 0.0.0.0:9090\nsecret: "k"\n', encoding='utf-8')
        (t / 'xz-hub/config.yaml').write_text(
            'server:\n  http_port: 16688\n'
            'selected_module:\n  LLM: DeepSeekLLM\n  TTS: EdgeTTS\n  ASR: SherpaXASR\n  Intent: function_call\n'
            'LLM:\n  DeepSeekLLM:\n    type: openai\n    url: https://api.deepseek.com\n    model_name: deepseek-chat\n    api_key: sk-test-123456\n'
            'TTS:\n  EdgeTTS:\n    type: edge\n    voice: zh-CN-XiaoxiaoNeural\n'
            'ASR:\n  SherpaXASR:\n    type: sherpa_onnx_asr\n    model_dir: models/SenseVoiceSmall\n    num_threads: 2\n',
            encoding='utf-8',
        )
        self._saved_a = {
            k: getattr(A, k) for k in (
                'STATE_DIR', 'UI_DIR', 'MUSIC_LOCAL', 'MUSIC_HUB', 'MUSIC_USB', 'USB_ASSUME_MOUNTED',
                'MIHOMO_API', 'MIHOMO_CFG', 'MIHOMO_UI', 'QQ_API', 'RESERVE_SYSTEM',
                'HUB_DIR', 'HUB_API', 'HOME_STATE', 'HOME_API', 'STUDY_STATE', 'STUDY_API',
                'TUTOR_STATE', 'TUTOR_API', 'MONITOR_STATE', 'MONITOR_SHARE',
                'MONITOR_API', 'CLIENT_API', 'INTERCOM_API', 'CLIENT_CFG', 'UPDATE_STATE',
                'QQ_CFG_DIR', 'CLIENT_SOUNDS_DIR',
            )
        }
        A.STATE_DIR, A.UI_DIR = t / 'state', t / 'ui'
        A.MUSIC_LOCAL, A.MUSIC_HUB, A.MUSIC_USB = t / 'local', t / 'hub', t / 'usb'
        A.USB_ASSUME_MOUNTED = True
        A.MIHOMO_API, A.MIHOMO_CFG, A.MIHOMO_UI = 'http://127.0.0.1:9', t / 'mihomo.yaml', t / 'mihomo-ui'
        A.RESERVE_SYSTEM = 0
        A.QQ_API = 'http://127.0.0.1:9'
        A.HUB_DIR, A.HUB_API = t / 'xz-hub', 'http://127.0.0.1:9'
        A.HOME_STATE, A.HOME_API = t / 'home-state', 'http://127.0.0.1:9'
        A.STUDY_STATE, A.STUDY_API = t / 'study-state', 'http://127.0.0.1:9'
        A.TUTOR_STATE, A.TUTOR_API = t / 'tutor-state', 'http://127.0.0.1:9'
        A.MONITOR_STATE, A.MONITOR_SHARE, A.MONITOR_API = t / 'mon-state', t / 'mon-share', 'http://127.0.0.1:9'
        A.CLIENT_API, A.INTERCOM_API = 'http://127.0.0.1:9', 'http://127.0.0.1:9'
        A.CLIENT_CFG = t / 'client-cfg' / 'config.json'
        A.UPDATE_STATE = t / 'update-state'
        A.QQ_CFG_DIR = t / 'qq-cfg'
        A.CLIENT_SOUNDS_DIR = t / 'client-sounds'

        self.admin = A.Admin()
        self.app = A.make_app(self.admin)
        self.srv = TestServer(self.app)
        await self.srv.start_server()
        self.cli = TestClient(self.srv)
        self._servers = []

    async def asyncTearDown(self):
        for s in self._servers:
            await s.close()
        await self.cli.close()
        await self.srv.close()
        for k, v in self._saved_a.items():
            setattr(A, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def fake(self, app):
        srv = TestServer(app)
        await srv.start_server()
        self._servers.append(srv)
        return str(srv.make_url('/')).rstrip('/')

    def hdr(self):
        return {'Origin': ORIGIN, 'X-TS-Admin': '1', 'Host': '127.0.0.1:8097'}

    async def test_html_css_js_and_xiaozhi_merge_markers(self):
        adm_html = Path(ADMIN_HTML).read_text(encoding='utf-8')
        css_m = re.search(r'<style>([\s\S]*?)</style>', adm_html)
        self.assertIsNotNone(css_m)
        css = css_m.group(1)
        for w in re.findall(r'font-weight:\s*(\d+)', css):
            self.assertIn(w, ('400', '700'))
        for px in re.findall(r'font(?:-size)?:\s*([\d.]+)px', css):
            self.assertNotIn('.', px)
        _check_js_syntax(_extract_scripts(adm_html), self.tmp, 'admin_index')

        # 已删除的火山 / 硅基 TTS 不应再出现在引擎选择网格里
        grid_m = re.search(r'<div class="v-grid" id="ttsModeGrid">([\s\S]*?)</div>', adm_html)
        self.assertIsNotNone(grid_m)
        grid_html = grid_m.group(1)
        self.assertNotIn('HuoshanDoubleStreamTTSV2', grid_html)
        self.assertNotIn('CosyVoiceSiliconflow', grid_html)
        for real_tts in ('IndexStreamTTS', 'EdgeTTS', 'OpenAITTS'):
            self.assertIn(f'data-tts="{real_tts}"', grid_html)

        for marker in (
            '<h2>小智主对话大模型</h2>',
            '<h2>小智近期对话记录</h2>',
            '<h2>小智角色人设提示词（Prompt）</h2>',
            '<h2>语音合成（TTS · 小智说话的声音）</h2>',
            'id="wkSound"',
            'id="cfgChatHistCard"',
            'id="chatHistTabs"',
            'id="btnClearModelChat"',
            'id="btnOpenAlarmMsgTab"',
            'id="btnRebootDevice"',
            "{ id: 'msg', t: '闹钟留言', s: '闹钟', i: 'msg', src: '/?embed=msg' }",
            '支持 10 家云端/兼容网关',
        ):
            self.assertIn(marker, adm_html)

        # Intercom 页面闹钟在前、留言板在后且 JS 合法
        ic_html = IC.HTML_PAGE
        _check_js_syntax(_extract_scripts(ic_html), self.tmp, 'intercom_index')
        self.assertLess(ic_html.index('id="alarmBox"'), ic_html.index('id="msgBox"'))
        self.assertIn('html[data-embed="cd"] #alarmBox', ic_html)
        self.assertIn('html[data-embed="call"] #alarmBox', ic_html)

    async def test_wake_sound_save_and_hot_push(self):
        pushed = []
        cli_app = web.Application()

        async def cli_settings(req):
            b = await req.json()
            pushed.append((req.headers.get('Origin'), b))
            return web.json_response({'ok': True})

        async def cli_events(req):
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
            await sr.prepare(req)
            await sr.write(b'event: snapshot\ndata: {"status":{"state":"idle","mic":true}}\n\n')
            await sr.write_eof()
            return sr

        cli_app.router.add_post('/api/settings', cli_settings)
        cli_app.router.add_get('/events', cli_events)
        A.CLIENT_API = await self.fake(cli_app)

        st0 = await (await self.cli.get('/admin/api/settings', headers=self.hdr())).json()
        self.assertEqual(st0['voice']['wakeup']['wake_sound'], 'zai')

        r_bad = await self.cli.post('/admin/api/settings/voice', json={
            'tts': {'selected': 'EdgeTTS'},
            'asr': {'selected': 'SherpaXASR'},
            'wakeup': {'wake_sound': 'invalid_tone'},
        }, headers=self.hdr())
        self.assertEqual(r_bad.status, 400)

        r_ok = await self.cli.post('/admin/api/settings/voice', json={
            'tts': {'selected': 'EdgeTTS'},
            'asr': {'selected': 'SherpaXASR'},
            'wakeup': {'wake_words': ['x iǎo m ài x iǎo m ài @小麦小麦'], 'wake_sound': 'chime', 'kws_threshold': 0.25, 'kws_gain_db': 10, 'follow_up_seconds': 10},
        }, headers=self.hdr())
        self.assertEqual(r_ok.status, 200, await r_ok.text())
        j_ok = await r_ok.json()
        self.assertEqual(j_ok['voice']['wakeup']['wake_sound'], 'chime')
        self.assertEqual(os.stat(A.CLIENT_CFG).st_mode & 0o777, 0o644)
        self.assertTrue(any(p[1].get('part') == 'assistant' and p[1]['values'].get('wake_sound') == 'chime' for p in pushed))

    async def test_chat_history_viewer_and_per_model_clear(self):
        db_p = self.tmp / 'xz-hub' / 'data' / 'chat.db'
        con = sqlite3.connect(str(db_p))
        con.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL,
                model TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                created REAL NOT NULL
            );
            CREATE TABLE device_memory (
                device TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL
            );
            CREATE TABLE memory_progress (
                device TEXT NOT NULL,
                model TEXT NOT NULL,
                upto INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (device, model)
            );
            """
        )
        now = time.time()
        con.execute("INSERT INTO messages(device,model,role,content,tool_calls,created) VALUES (?,?,?,?,?,?)",
                    ('thinksmart-1', 'DeepSeekLLM', 'user', json.dumps({'speaker': '爸爸', 'content': '今天天气怎么样'}), None, now - 30))
        con.execute("INSERT INTO messages(device,model,role,content,tool_calls,created) VALUES (?,?,?,?,?,?)",
                    ('thinksmart-1', 'DeepSeekLLM', 'assistant', '今天晴转多云，气温 18 度。……', None, now - 20))
        con.execute("INSERT INTO messages(device,model,role,content,tool_calls,created) VALUES (?,?,?,?,?,?)",
                    ('thinksmart-1', 'DeepSeekLLM', 'assistant', '', '[{"id":"tc1"}]', now - 25))
        con.execute("INSERT INTO messages(device,model,role,content,tool_calls,created) VALUES (?,?,?,?,?,?)",
                    ('thinksmart-1', 'OpenAILLM', 'user', '给我讲个故事', None, now - 10))
        con.execute("INSERT INTO messages(device,model,role,content,tool_calls,created) VALUES (?,?,?,?,?,?)",
                    ('thinksmart-1', 'OpenAILLM', 'assistant', '从前有座山……', None, now - 5))
        con.execute("INSERT INTO device_memory(device,summary,version,source,updated) VALUES (?,?,?,?,?)",
                    ('thinksmart-1', '家里住在杭州', 1, 'summary', now - 100))
        con.commit()
        con.close()

        # 1) SQLite 离线通道读取
        r_sq = await self.cli.get('/admin/api/chat/history?device=thinksmart-1&limit=50', headers=self.hdr())
        self.assertEqual(r_sq.status, 200)
        j_sq = await r_sq.json()
        self.assertEqual(j_sq['device'], 'thinksmart-1')
        self.assertEqual(j_sq['active_model'], 'DeepSeekLLM')
        by_e = {m['entry']: m for m in j_sq['models']}
        self.assertIn('DeepSeekLLM', by_e)
        self.assertIn('OpenAILLM', by_e)
        ds_msgs = by_e['DeepSeekLLM']['messages']
        self.assertEqual(len(ds_msgs), 2)  # tool_calls 行已过滤
        self.assertEqual(ds_msgs[0]['text'], '今天天气怎么样')
        self.assertTrue(ds_msgs[1]['interrupted'])
        self.assertEqual(ds_msgs[1]['text'], '今天晴转多云，气温 18 度。')

        # 2) Hub HTTP 在线通道读取
        hub_app = web.Application()
        hub_app.router.add_get('/kiosk/internal/memory', lambda _r: web.json_response({
            'ok': True,
            'devices': [{'device': 'thinksmart-1', 'models': [{'entry': 'DeepSeekLLM', 'count': 99}], 'memory': {'summary': 'x', 'version': 1}}],
        }))
        hub_app.router.add_get('/kiosk/chat/history', lambda _r: web.json_response({
            'ok': True,
            'models': {'DeepSeekLLM': [{'id': 77, 'role': 'user', 'text': '来自网关实录', 'time': now, 'interrupted': False}]},
        }))
        A.HUB_API = await self.fake(hub_app)
        r_hb = await self.cli.get('/admin/api/chat/history?device=thinksmart-1', headers=self.hdr())
        self.assertEqual(r_hb.status, 200)
        j_hb = await r_hb.json()
        self.assertEqual(j_hb['models'][0]['count'], 99)
        self.assertEqual(j_hb['models'][0]['messages'][0]['text'], '来自网关实录')

        # 3) CSRF 拦截 & 单模型清理（不影响其他模型与长期记忆）
        A.HUB_API = 'http://127.0.0.1:9'
        r_csrf = await self.cli.post('/admin/api/chat/clear', json={'device': 'thinksmart-1', 'model': 'OpenAILLM'}, headers={'Host': '127.0.0.1:8097'})
        self.assertEqual(r_csrf.status, 403)

        r_clr = await self.cli.post('/admin/api/chat/clear', json={'device': 'thinksmart-1', 'model': 'OpenAILLM'}, headers=self.hdr())
        self.assertEqual(r_clr.status, 200, await r_clr.text())
        self.assertEqual((await r_clr.json())['deleted'], 2)

        r_after = await self.cli.get('/admin/api/chat/history?device=thinksmart-1', headers=self.hdr())
        after_entries = [m['entry'] for m in (await r_after.json())['models']]
        self.assertEqual(after_entries, ['DeepSeekLLM'])

    async def test_remote_reboot_device_busy_guards(self):
        state_box = {'voice': 'speaking', 'in_call': False}
        reboot_calls = []

        cli_app = web.Application()

        async def cli_events(req):
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
            await sr.prepare(req)
            await sr.write(('event: snapshot\ndata: ' + json.dumps({'status': {'state': state_box['voice']}}) + '\n\n').encode())
            await sr.write_eof()
            return sr

        async def cli_settings(req):
            b = await req.json()
            reboot_calls.append(b)
            return web.json_response({'ok': True})

        cli_app.router.add_get('/events', cli_events)
        cli_app.router.add_post('/api/settings', cli_settings)
        A.CLIENT_API = await self.fake(cli_app)

        ic_app = web.Application()
        ic_app.router.add_get('/api/intercom/status', lambda _r: web.json_response({'ok': True, 'in_call': state_box['in_call']}))
        A.INTERCOM_API = await self.fake(ic_app)

        r1 = await self.cli.post('/admin/api/settings/device', json={'action': 'reboot_device'}, headers=self.hdr())
        self.assertEqual(r1.status, 409)

        state_box['voice'] = 'idle'
        state_box['in_call'] = True
        r2 = await self.cli.post('/admin/api/settings/device', json={'action': 'reboot_device'}, headers=self.hdr())
        self.assertEqual(r2.status, 409)

        state_box['in_call'] = False
        r3 = await self.cli.post('/admin/api/settings/device', json={'action': 'reboot_device'}, headers=self.hdr())
        self.assertEqual(r3.status, 200, await r3.text())
        self.assertEqual(reboot_calls, [{'part': 'system', 'values': {'action': 'reboot'}}])

    async def test_intercom_alarms_proxy_and_csrf(self):
        saved_ic_client = IC.CLIENT_API
        upstream_calls = []
        al_state = {
            'ok': True, 'ringing': False, 'volume': 75, 'tone': 'coffee',
            'tones': {'coffee': '咖啡时光', 'morning': '晨曦'},
            'missed': [],
            'items': [{'id': 11, 'kind': 'alarm', 'when': '07:00', 'repeat': 'daily', 'label': '起床'}],
        }

        cli_app = web.Application()

        async def al_get(req):
            et = '"al-test-1"'
            if req.headers.get('If-None-Match') == et:
                return web.Response(status=304, headers={'ETag': et})
            return web.json_response(al_state, headers={'ETag': et})

        async def al_post(req):
            b = await req.json()
            upstream_calls.append((req.path, req.headers.get('Origin'), b))
            return web.json_response({'ok': True, 'id': 12})

        cli_app.router.add_get('/api/alarms', al_get)
        for p in ('/api/alarms/create', '/api/alarms/delete', '/api/alarms/snooze', '/api/alarms/stop',
                  '/api/alarms/missed/clear', '/api/alarm-volume', '/api/alarm-tone'):
            cli_app.router.add_post(p, al_post)

        IC.CLIENT_API = await self.fake(cli_app)
        try:
            ic_app = web.Application()
            ic_app[IC.STUDY_SESSION_KEY] = aiohttp.ClientSession(trust_env=False)
            ic_app.router.add_get('/api/alarms', IC.handle_alarms_get)
            ic_app.router.add_post('/api/alarms', IC.handle_alarms_post)
            ic_srv = TestServer(ic_app)
            await ic_srv.start_server()
            ic_cli = TestClient(ic_srv)
            try:
                # 缺少 X-Intercom-Msg 被 403 拦截
                self.assertEqual((await ic_cli.get('/api/alarms')).status, 403)
                self.assertEqual((await ic_cli.post('/api/alarms', json={'action': 'stop'})).status, 403)

                hdr_ok = {'X-Intercom-Msg': '1', 'Host': '192.168.1.88:18091'}
                r_g = await ic_cli.get('/api/alarms', headers=hdr_ok)
                self.assertEqual(r_g.status, 200)
                self.assertEqual((await r_g.json())['items'][0]['label'], '起床')
                r_304 = await ic_cli.get('/api/alarms', headers={**hdr_ok, 'If-None-Match': '"al-test-1"'})
                self.assertEqual(r_304.status, 304)

                for body_p, exp_path in (
                    ({'action': 'create', 'kind': 'alarm', 'time': '08:00', 'label': '吃药', 'repeat': 'daily'}, '/api/alarms/create'),
                    ({'action': 'delete', 'id': 11}, '/api/alarms/delete'),
                    ({'action': 'snooze'}, '/api/alarms/snooze'),
                    ({'action': 'stop'}, '/api/alarms/stop'),
                    ({'action': 'clear_missed'}, '/api/alarms/missed/clear'),
                    ({'action': 'volume', 'volume': 60}, '/api/alarm-volume'),
                    ({'action': 'tone', 'tone': 'morning'}, '/api/alarm-tone'),
                ):
                    rp = await ic_cli.post('/api/alarms', json=body_p, headers=hdr_ok)
                    self.assertEqual(rp.status, 200, await rp.text())
                    self.assertEqual(upstream_calls[-1][0], exp_path)
                    self.assertEqual(upstream_calls[-1][1], IC.CLIENT_API)

                r_bad = await ic_cli.post('/api/alarms', json={'action': 'unknown'}, headers=hdr_ok)
                self.assertEqual(r_bad.status, 400)
            finally:
                await ic_app[IC.STUDY_SESSION_KEY].close()
                await ic_cli.close()
                await ic_srv.close()
        finally:
            IC.CLIENT_API = saved_ic_client


if __name__ == '__main__':
    unittest.main(verbosity=2)
