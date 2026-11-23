#!/opt/thinksmart-addons/venv/bin/python
"""2026-11-20 Claude：管理后台回归测试（统一管理面板）。

覆盖：thinksmart-admin（登录、限速、防跨站、歌曲上传下载删除恢复改名移动、在大屏播放、Clash 代理白名单与密钥、
WebSocket、完整面板静态文件）、thinksmart-intercom 的 /admin/ 转发（流式上传下载、Set-Cookie、WebSocket）、
三个手机页的嵌入模式。全部用临时目录和假服务，不碰真 mihomo、不放音乐、不出声。

路径可用环境变量换成暂存副本：ADMIN_PY ADMIN_HTML INTERCOM_PY STUDY_HTML TUTOR_MOBILE_HTML ADMIN_UNIT
"""
import asyncio
import base64
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ADMIN_PY = os.environ.get('ADMIN_PY', '/usr/local/bin/thinksmart-admin.py')
ADMIN_HTML = os.environ.get('ADMIN_HTML', '/usr/local/share/thinksmart-admin/index.html')
INTERCOM_PY = os.environ.get('INTERCOM_PY', '/usr/local/bin/thinksmart-intercom.py')
STUDY_HTML = os.environ.get('STUDY_HTML', '/usr/local/share/thinksmart-study/parent.html')
TUTOR_MOBILE_HTML = os.environ.get('TUTOR_MOBILE_HTML', '/usr/local/share/thinksmart-tutor/mobile.html')
ADMIN_UNIT = os.environ.get('ADMIN_UNIT', '/etc/systemd/system/thinksmart-admin.service')
QQ_PY = os.environ.get('QQ_PY', '/usr/local/bin/thinksmart-qqmusic.py')
QQ_HTML = os.environ.get('QQ_HTML', '/usr/local/share/thinksmart-qqmusic/index.html')

ID3 = b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'\x00' * 2000
FLAC = b'fLaC\x00\x00\x00\x22' + b'\x00' * 2000
WAV = b'RIFF\x24\x08\x00\x00WAVEfmt ' + b'\x00' * 2000
ORIGIN = 'http://127.0.0.1'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = load(ADMIN_PY, 'ts_admin_under_test')


class Base(unittest.IsolatedAsyncioTestCase):
    """每个测试一套临时目录：状态、页面、本机音乐、小智音乐、U 盘、mihomo 配置和面板。"""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ts-admin-test-'))
        t = self.tmp
        for d in ('state', 'ui', 'local', 'hub', 'usb', 'mihomo-ui/_nuxt',
                  'xz-hub/data', 'xz-hub/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17',
                  'home-state', 'study-state', 'tutor-state', 'mon-state', 'mon-share', 'update-state', 'client-sounds'):
            (t / d).mkdir(parents=True)
        shutil.copy(ADMIN_HTML, t / 'ui' / 'index.html')
        (t / 'mihomo.yaml').write_text('mixed-port: 7890\nexternal-controller: 0.0.0.0:9090\nsecret: "s3cret-key"\n')
        (t / 'xz-hub/config.yaml').write_text(
            'server:\n  http_port: 16688\n'
            'selected_module:\n  LLM: DeepSeekLLM\n  TTS: EdgeTTS\n  ASR: SherpaXASR\n  Intent: function_call\n'
            'LLM:\n  DeepSeekLLM:\n    type: openai\n    url: https://api.deepseek.com\n    model_name: deepseek-chat\n    api_key: 你的key\n'
            '  OpenAILLM:\n    type: openai\n    base_url: https://api.openai.com/v1\n    model_name: gpt-5-mini\n    api_key: 你的key\n'
            'TTS:\n  EdgeTTS:\n    type: edge\n    voice: zh-CN-XiaoxiaoNeural\n'
            'ASR:\n  SherpaXASR:\n    type: sherpa_onnx_asr\n    model_dir: models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17\n    num_threads: 2\n'
            'Intent:\n  function_call:\n    type: function_call\n    functions:\n      - get_weather\n      - play_music\n      - web_search\n',
            encoding='utf-8',
        )
        (t / 'mon-share/servers.json').write_text('[{"id":1,"name":"HK-1"},{"id":2,"name":"US-1"}]', encoding='utf-8')
        self._saved = {k: getattr(A, k) for k in (
            'STATE_DIR', 'UI_DIR', 'MUSIC_LOCAL', 'MUSIC_HUB', 'MUSIC_USB', 'USB_ASSUME_MOUNTED',
            'MIHOMO_API', 'MIHOMO_CFG', 'MIHOMO_UI', 'QQ_API', 'RESERVE_SYSTEM',
            'HUB_DIR', 'HUB_API', 'HOME_STATE', 'HOME_API', 'STUDY_STATE', 'STUDY_API',
            'TUTOR_STATE', 'TUTOR_API', 'MONITOR_STATE', 'MONITOR_SHARE',
            'MONITOR_API', 'CLIENT_API', 'INTERCOM_API', 'CLIENT_CFG', 'UPDATE_STATE',
            'METASO_TEST_URL', 'AMAP_TEST_URL', 'SILICONFLOW_TTS_URL', 'NEWSNOW_API_URL',
            'OPEN_METEO_GEO_URL', 'OPEN_METEO_FORECAST_URL', 'CHINANEWS_DEFAULT_RSS', 'QWEATHER_PROTO',
            'CLAUDE_CLI_HINT', 'QQ_CFG_DIR', 'CLIENT_SOUNDS_DIR',
        )}
        A.CLAUDE_CLI_HINT = '__NONE__'
        A.OPEN_METEO_GEO_URL = 'http://127.0.0.1:9'
        A.OPEN_METEO_FORECAST_URL = 'http://127.0.0.1:9'
        A.CHINANEWS_DEFAULT_RSS = 'http://127.0.0.1:9'
        A.QQ_CFG_DIR = t / 'qq-cfg'
        A.CLIENT_SOUNDS_DIR = t / 'client-sounds'
        A.STATE_DIR, A.UI_DIR = t / 'state', t / 'ui'
        A.MUSIC_LOCAL, A.MUSIC_HUB, A.MUSIC_USB = t / 'local', t / 'hub', t / 'usb'
        A.USB_ASSUME_MOUNTED = True
        A.MIHOMO_CFG, A.MIHOMO_UI = t / 'mihomo.yaml', t / 'mihomo-ui'
        A.RESERVE_SYSTEM = 0
        A.QQ_API = 'http://127.0.0.1:9'
        A.HUB_DIR = t / 'xz-hub'
        A.HUB_API = 'http://127.0.0.1:9'
        A.HOME_STATE, A.HOME_API = t / 'home-state', 'http://127.0.0.1:9'
        A.STUDY_STATE, A.STUDY_API = t / 'study-state', 'http://127.0.0.1:9'
        A.TUTOR_STATE, A.TUTOR_API = t / 'tutor-state', 'http://127.0.0.1:9'
        A.MONITOR_STATE, A.MONITOR_SHARE = t / 'mon-state', t / 'mon-share'
        A.MONITOR_API, A.CLIENT_API, A.INTERCOM_API = 'http://127.0.0.1:9', 'http://127.0.0.1:9', 'http://127.0.0.1:9'
        A.CLIENT_CFG = t / 'client-cfg' / 'config.json'
        A.UPDATE_STATE = t / 'update-state'
        os.environ.pop('CREDENTIALS_DIRECTORY', None)
        self.fakes = []
        self.admin = A.Admin()
        self.client = TestClient(TestServer(A.make_app(self.admin)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for f in self.fakes:
            await f.close()
        for k, v in self._saved.items():
            setattr(A, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hdr(self, **kw):
        h = {'Origin': ORIGIN, 'X-TS-Admin': '1'}
        h.update(kw)
        return h

    async def login(self, pw='good-pass-1', username='admin'):
        sys_stdin = sys.stdin
        line = f'{username}:{pw}\n' if username != 'admin' else f'{pw}\n'
        sys.stdin = io.StringIO(line)
        try:
            with open(os.devnull, 'w') as dn:
                so = sys.stdout
                sys.stdout = dn
                try:
                    self.assertEqual(A.cli_set_password(), 0)
                finally:
                    sys.stdout = so
        finally:
            sys.stdin = sys_stdin
        r = await self.client.post('/admin/api/login', json={'username': username, 'password': pw, 'remember': True}, headers=self.hdr())
        self.assertEqual(r.status, 200, await r.text())
        return r

    async def fake(self, app):
        srv = TestServer(app)
        await srv.start_server()
        self.fakes.append(srv)
        return f'http://127.0.0.1:{srv.port}'


class AuthTests(Base):
    async def test_page_headers_and_redirect(self):
        r = await self.client.get('/admin/')
        self.assertEqual(r.status, 200)
        self.assertIn("frame-ancestors 'none'", r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['X-Frame-Options'], 'DENY')
        self.assertIn('管理后台', await r.text())
        r = await self.client.get('/admin', allow_redirects=False)
        self.assertIn(r.status, (301, 308))
        self.assertEqual(r.headers['Location'], '/admin/')

    async def test_open_by_default_then_optional_password(self):
        # 用户：和远程可视一样，默认不用密码，只靠 VPS 节点 / 局域网限制
        j = await (await self.client.get('/admin/api/session')).json()
        self.assertTrue(j['logged_in'])
        self.assertFalse(j['password_on'])
        r = await self.client.get('/admin/api/songs')
        self.assertEqual(r.status, 200)
        r = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'x'}, headers=self.hdr())
        self.assertEqual(r.status, 409)
        await self.login('good-pass-1')
        raw = (A.STATE_DIR / 'auth.json').read_text()
        self.assertNotIn('good-pass-1', raw)
        self.assertEqual(oct((A.STATE_DIR / 'auth.json').stat().st_mode & 0o777), '0o600')
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/admin/'))
        self.assertIn('ts_admin', cookie)
        j = await (await self.client.get('/admin/api/session')).json()
        self.assertTrue(j['logged_in'] and j['password_on'])
        self.client.session.cookie_jar.clear()
        j = await (await self.client.get('/admin/api/session')).json()
        self.assertFalse(j['logged_in'])
        self.assertEqual((await self.client.get('/admin/api/songs')).status, 401)
        # 空行 = 取消密码，回到默认
        sys_stdin, so = sys.stdin, sys.stdout
        sys.stdin = io.StringIO('\n')
        sys.stdout = open(os.devnull, 'w')
        try:
            self.assertEqual(A.cli_set_password(), 0)
        finally:
            sys.stdout.close()
            sys.stdin, sys.stdout = sys_stdin, so
        self.assertFalse((A.STATE_DIR / 'auth.json').exists())
        self.assertEqual((await self.client.get('/admin/api/songs')).status, 200)

    async def test_only_vps_tunnel_or_lan(self):
        for peer, want in (('127.0.0.1', 200), ('192.168.1.23', 200), ('10.1.2.3', 200), ('172.16.42.2', 200),
                           ('8.8.8.8', 403), ('2001:db8::1', 403), ('garbage', 403)):
            r = await self.client.get('/admin/api/songs', headers={'X-TS-Peer': peer})
            self.assertEqual(r.status, want, peer)
        self.assertTrue(A.peer_ok('::ffff:192.168.1.9'))
        self.assertFalse(A.peer_ok('::ffff:1.2.3.4'))

    async def test_cookie_flags(self):
        r = await self.login()
        sc = r.headers.get('Set-Cookie', '')
        self.assertIn('HttpOnly', sc)
        self.assertIn('SameSite=Strict', sc)
        self.assertIn('Path=/admin', sc)
        self.assertNotIn('Secure', sc)
        r = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'good-pass-1'},
                                   headers=self.hdr(**{'X-Forwarded-Proto': 'https'}))
        cookies = r.headers.getall('Set-Cookie', [])
        self.assertIn('Secure', cookies[0])
        self.assertTrue(any('ts_wan=' in c and 'Path=/' in c and 'Secure' in c for c in cookies), cookies)

    async def test_wrong_password_and_rate_limit(self):
        await self.login('good-pass-1')
        for _ in range(5):
            r = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'nope'}, headers=self.hdr(**{'X-Real-IP': '9.9.9.9'}))
            self.assertEqual(r.status, 403)
        r = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'good-pass-1'}, headers=self.hdr(**{'X-Real-IP': '9.9.9.9'}))
        self.assertEqual(r.status, 429)
        r = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'good-pass-1'}, headers=self.hdr(**{'X-Real-IP': '8.8.8.8'}))
        self.assertEqual(r.status, 200)

    async def test_cross_site_writes_refused(self):
        await self.login()
        r = await self.client.post('/admin/api/songs/mkdir', json={'root': 'local', 'name': 'x'})
        self.assertEqual(r.status, 403)
        r = await self.client.post('/admin/api/songs/mkdir', json={'root': 'local', 'name': 'x'}, headers={'Origin': 'http://evil.example'})
        self.assertEqual(r.status, 403)
        r = await self.client.post('/admin/api/songs/mkdir', json={'root': 'local', 'name': 'x'}, headers=self.hdr(**{'Sec-Fetch-Site': 'cross-site'}))
        self.assertEqual(r.status, 403)
        self.assertFalse((A.MUSIC_LOCAL / 'x').exists())

    async def test_api_needs_login_once_password_set(self):
        await self.login()
        self.client.session.cookie_jar.clear()
        for path in ('/admin/api/songs', '/admin/api/clash/proxies', '/admin/api/music/status'):
            r = await self.client.get(path)
            self.assertEqual(r.status, 401, path)
        r = await self.client.get('/admin/clash-ui/', allow_redirects=False)
        self.assertEqual(r.status, 302)

    async def test_change_password_and_logout_all(self):
        await self.login('good-pass-1')
        r = await self.client.post('/admin/api/password', json={'old': 'bad', 'new': 'another-pass'}, headers=self.hdr())
        self.assertEqual(r.status, 403)
        r = await self.client.post('/admin/api/password', json={'old': 'good-pass-1', 'new': 'another-pass'}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        self.assertTrue((await (await self.client.get('/admin/api/session')).json())['logged_in'])   # 本机保持登录
        r = await self.client.post('/admin/api/logout', json={'all': True}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        self.assertEqual(self.admin.auth.count(), 0)
        r = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'another-pass'}, headers=self.hdr())
        self.assertEqual(r.status, 200)


class SongTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        t = self.tmp
        (t / 'local' / 'a.mp3').write_bytes(ID3)
        (t / 'local' / '儿歌').mkdir()
        (t / 'local' / '儿歌' / 'b.flac').write_bytes(FLAC)
        (t / 'local' / '.hidden.mp3').write_bytes(ID3)
        (t / 'local' / 'note.txt').write_text('x')
        os.symlink(t / 'usb', t / 'local' / 'usb')                 # 和设备上一样：Music/usb -> /mnt/usb
        (t / 'hub' / 'c.mp3').write_bytes(ID3)
        (t / 'usb' / 'd.wav').write_bytes(WAV)
        self.played = []

        async def play_tracks(req):
            self.played.append(await req.json())
            return web.json_response({'ok': True})

        async def control(req):
            self.played.append({'control': await req.json()})
            return web.json_response({'ok': True})

        async def status(_req):
            return web.json_response({'playing': True, 'paused': False, 'track': {'title': 'a', 'artist': '本地音乐', 'file': '/x/a.mp3'}})
        qq = web.Application()
        qq.router.add_post('/api/music/play_tracks', play_tracks)
        qq.router.add_post('/api/music/control', control)
        qq.router.add_get('/api/music/status', status)
        A.QQ_API = await self.fake(qq)
        await self.login()

    async def songs(self):
        r = await self.client.get('/admin/api/songs')
        self.assertEqual(r.status, 200)
        return await r.json()

    async def upload(self, data, name, root='local', folder='', size=None):
        q = {'root': root, 'dir': folder, 'name': name, 'size': str(len(data) if size is None else size)}
        return await self.client.post('/admin/api/songs/upload', params=q, data=data,
                                      headers=self.hdr(**{'Content-Type': 'application/octet-stream'}))

    def leftovers(self):
        return [p for p in self.tmp.rglob('*') if p.name.endswith('.part')]

    async def test_list_skips_hidden_links_and_other_files(self):
        j = await self.songs()
        got = sorted((f['root'], f['path']) for f in j['files'])
        self.assertEqual(got, [('hub', 'c.mp3'), ('local', 'a.mp3'), ('local', '儿歌/b.flac'), ('usb', 'd.wav')])
        self.assertEqual(j['folders']['local'], ['儿歌'])
        self.assertTrue(j['roots']['usb']['ok'])

    async def test_upload_ok_and_conflict_renamed(self):
        r = await self.upload(ID3, '新歌.mp3', folder='儿歌')
        j = await r.json()
        self.assertEqual(r.status, 200, j)
        self.assertEqual((self.tmp / 'local' / '儿歌' / '新歌.mp3').read_bytes(), ID3)
        r = await self.upload(ID3, '新歌.mp3', folder='儿歌')
        j = await r.json()
        self.assertTrue(j['renamed'])
        self.assertEqual(j['name'], '新歌（2）.mp3')
        r = await self.upload(WAV, 'w.wav', root='usb')
        self.assertEqual(r.status, 200)
        self.assertTrue((self.tmp / 'usb' / 'w.wav').exists())
        self.assertEqual(self.leftovers(), [])

    async def test_upload_refusals_leave_nothing(self):
        r = await self.upload(b'\x00' * 3000, 'fake.mp3')
        self.assertEqual(r.status, 415)
        r = await self.upload(ID3, 'short.mp3', size=len(ID3) + 100)
        self.assertEqual(r.status, 400)
        r = await self.upload(ID3, 'x.exe')
        self.assertEqual(r.status, 415)
        r = await self.upload(ID3, 'x.mp3', root='hub')
        self.assertEqual(r.status, 400)
        r = await self.upload(ID3, 'x.mp3', folder='../hub')
        self.assertEqual(r.status, 400)
        r = await self.upload(ID3, 'x.mp3', folder='usb')            # 符号链接不跟
        self.assertEqual(r.status, 400)
        A.RESERVE_SYSTEM = 1 << 60
        r = await self.upload(ID3, 'big.mp3')
        self.assertEqual(r.status, 507)
        self.assertEqual(self.leftovers(), [])
        names = {p.name for p in (self.tmp / 'local').iterdir() if p.name != '.incoming'}
        self.assertEqual(names, {'a.mp3', '儿歌', '.hidden.mp3', 'note.txt', 'usb'})
        self.assertEqual([p.name for p in (self.tmp / 'local' / '.incoming').iterdir()], [])

    async def test_upload_name_is_sanitized(self):
        r = await self.upload(ID3, '../evil:name?.mp3')
        j = await r.json()
        self.assertEqual(r.status, 200, j)
        self.assertNotIn('/', j['path'])
        self.assertTrue((self.tmp / 'local' / j['name']).is_file())

    async def test_download_range_and_path_safety(self):
        r = await self.client.get('/admin/api/songs/file', params={'root': 'local', 'path': '儿歌/b.flac', 'dl': '1'})
        self.assertEqual(r.status, 200)
        self.assertIn("filename*=UTF-8''b.flac", r.headers['Content-Disposition'])
        self.assertEqual(await r.read(), FLAC)
        r = await self.client.get('/admin/api/songs/file', params={'root': 'local', 'path': 'a.mp3'}, headers={'Range': 'bytes=0-2'})
        self.assertEqual(r.status, 206)
        self.assertEqual(await r.read(), b'ID3')
        for bad in ('../hub/c.mp3', '/etc/passwd', 'usb/d.wav', '.hidden.mp3', 'note.txt', ''):
            r = await self.client.get('/admin/api/songs/file', params={'root': 'local', 'path': bad})
            self.assertIn(r.status, (400, 404), bad)

    async def test_delete_restore_purge(self):
        r = await self.client.post('/admin/api/songs/delete', json={'root': 'local', 'path': 'a.mp3'}, headers=self.hdr())
        tid = (await r.json())['id']
        self.assertFalse((self.tmp / 'local' / 'a.mp3').exists())
        self.assertTrue((self.tmp / 'local' / '.thinksmart-trash' / f'{tid}.deleted').is_file())
        j = await self.songs()
        self.assertNotIn('a.mp3', [f['path'] for f in j['files']])
        self.assertEqual([x['id'] for x in j['trash']], [tid])
        r = await self.client.post('/admin/api/songs/restore', json={'id': tid}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        self.assertEqual((self.tmp / 'local' / 'a.mp3').read_bytes(), ID3)
        r = await self.client.post('/admin/api/songs/delete', json={'root': 'usb', 'path': 'd.wav'}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        r = await self.client.post('/admin/api/songs/purge', json={'all': True}, headers=self.hdr())
        self.assertEqual((await r.json())['removed'], 1)
        self.assertFalse((self.tmp / 'usb' / '.thinksmart-trash').exists())
        self.assertEqual((await self.songs())['trash'], [])

    async def test_old_trash_purged(self):
        tdir = self.tmp / 'hub' / '.thinksmart-trash'
        tdir.mkdir()
        old = f'{int(time.time()) - 8 * 86400}-0000abcd.deleted'
        (tdir / old).write_bytes(ID3)
        new = f'{int(time.time())}-0000beef.deleted'
        (tdir / new).write_bytes(ID3)
        self.assertEqual(self.admin.trash.purge(), 1)
        self.assertFalse((tdir / old).exists())
        self.assertTrue((tdir / new).exists())

    async def test_rename_move_mkdir_rmdir(self):
        r = await self.client.post('/admin/api/songs/rename', json={'root': 'local', 'path': 'a.mp3', 'name': '好听'}, headers=self.hdr())
        self.assertEqual((await r.json())['name'], '好听.mp3')
        r = await self.client.post('/admin/api/songs/rename', json={'root': 'local', 'path': '好听.mp3', 'name': '好听.flac'}, headers=self.hdr())
        self.assertEqual(r.status, 400)
        (self.tmp / 'local' / 'z.mp3').write_bytes(ID3)
        r = await self.client.post('/admin/api/songs/rename', json={'root': 'local', 'path': 'z.mp3', 'name': '好听.mp3'}, headers=self.hdr())
        self.assertEqual(r.status, 409)
        r = await self.client.post('/admin/api/songs/mkdir', json={'root': 'local', 'name': '睡前'}, headers=self.hdr())
        self.assertEqual((await r.json())['dir'], '睡前')
        r = await self.client.post('/admin/api/songs/move', json={'root': 'local', 'path': 'z.mp3', 'dir': '睡前'}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        self.assertTrue((self.tmp / 'local' / '睡前' / 'z.mp3').exists())
        r = await self.client.post('/admin/api/songs/rmdir', json={'root': 'local', 'dir': '睡前'}, headers=self.hdr())
        self.assertEqual(r.status, 409)
        r = await self.client.post('/admin/api/songs/move', json={'root': 'local', 'path': '睡前/z.mp3', 'dir': ''}, headers=self.hdr())
        r = await self.client.post('/admin/api/songs/rmdir', json={'root': 'local', 'dir': '睡前'}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        r = await self.client.post('/admin/api/songs/mkdir', json={'root': 'local', 'name': '../x'}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        self.assertTrue((self.tmp / 'local' / '.._x').exists() or (self.tmp / 'local' / '_x').exists())

    async def test_play_on_screen_sends_local_queue(self):
        (self.tmp / 'local' / '儿歌' / 'a2.mp3').write_bytes(ID3)
        r = await self.client.post('/admin/api/songs/play', json={'root': 'local', 'path': '儿歌/b.flac'}, headers=self.hdr())
        self.assertEqual(r.status, 200, await r.text())
        body = self.played[-1]
        self.assertFalse(body['open_ui'])
        urls = [t['url'] for t in body['tracks']]
        self.assertEqual(urls, [str(self.tmp / 'local' / '儿歌' / 'a2.mp3'), str(self.tmp / 'local' / '儿歌' / 'b.flac')])
        self.assertEqual(body['index'], 1)
        self.assertTrue(all(t['is_local'] for t in body['tracks']))

    async def test_batch_delete_and_restore(self):
        items = [{'root': 'local', 'path': 'a.mp3'}, {'root': 'local', 'path': '儿歌/b.flac'}, {'root': 'usb', 'path': 'd.wav'},
                 {'root': 'local', 'path': 'missing.mp3'}]
        r = await self.client.post('/admin/api/songs/delete', json={'items': items}, headers=self.hdr())
        j = await r.json()
        self.assertEqual(r.status, 200, j)
        self.assertEqual(j['deleted'], 3)
        self.assertEqual(len(j['ids']), 3)
        self.assertEqual(len(j['failed']), 1)
        for rel in ('local/a.mp3', 'local/儿歌/b.flac', 'usb/d.wav'):
            self.assertFalse((self.tmp / rel).exists(), rel)
        self.assertEqual(len((await self.songs())['trash']), 3)
        r = await self.client.post('/admin/api/songs/restore', json={'ids': j['ids']}, headers=self.hdr())
        k = await r.json()
        self.assertEqual(k['restored'], 3, k)
        for rel in ('local/a.mp3', 'local/儿歌/b.flac', 'usb/d.wav'):
            self.assertTrue((self.tmp / rel).exists(), rel)
        self.assertEqual((await self.songs())['trash'], [])

    async def wait_job(self, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = (await (await self.client.get('/admin/api/songs/import')).json())['job']
            if j and j['state'] != 'running':
                return j
            await asyncio.sleep(0.1)
        self.fail('import job did not finish')

    async def test_import_from_usb_into_fixed_folder(self):
        u = self.tmp / 'usb'
        (u / 'a.mp3').write_bytes(ID3)                        # 本机已有同名同大小 → 跳过
        (u / '合集').mkdir()
        (u / '合集' / 'x.flac').write_bytes(FLAC)            # 子文件夹里的歌 → 进本机音乐文件夹（不带子文件夹）
        (self.tmp / 'local' / 'z.mp3').write_bytes(ID3 + b'1')
        (u / 'z.mp3').write_bytes(ID3)                        # 同名不同大小 → 存成（2）
        r = await self.client.post('/admin/api/songs/import', json={'all': True}, headers=self.hdr())
        self.assertEqual(r.status, 200, await r.text())
        job = await self.wait_job()
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual((job['imported'], job['skipped'], job['failed_count']), (3, 1, 0), job)
        loc = self.tmp / 'local'
        self.assertEqual((loc / 'x.flac').read_bytes(), FLAC)
        self.assertEqual((loc / 'd.wav').read_bytes(), WAV)
        self.assertEqual((loc / 'z（2）.mp3').read_bytes(), ID3)
        self.assertEqual((loc / 'a.mp3').read_bytes(), ID3)
        self.assertTrue((u / '合集' / 'x.flac').exists(), 'import copies, never moves')
        self.assertEqual(self.leftovers(), [])
        r = await self.client.post('/admin/api/songs/import', json={'paths': ['../local/a.mp3', 'nope.mp3']}, headers=self.hdr())
        self.assertEqual(r.status, 404)

    async def test_import_space_and_cancel(self):
        A.RESERVE_SYSTEM = 1 << 60
        (self.tmp / 'usb' / 'big.mp3').write_bytes(ID3)
        r = await self.client.post('/admin/api/songs/import', json={'paths': ['big.mp3']}, headers=self.hdr())
        self.assertEqual(r.status, 507)
        A.RESERVE_SYSTEM = 0
        with open(self.tmp / 'usb' / 'huge.flac', 'wb') as f:
            f.write(b'fLaC')
            f.truncate(120 * 1024 * 1024)
        r = await self.client.post('/admin/api/songs/import', json={'paths': ['huge.flac', 'd.wav']}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        r2 = await self.client.post('/admin/api/songs/import', json={'paths': ['d.wav']}, headers=self.hdr())
        self.assertEqual(r2.status, 409)                     # 一次只跑一批
        await self.client.post('/admin/api/songs/import/cancel', json={}, headers=self.hdr())
        job = await self.wait_job()
        self.assertIn(job['state'], ('cancelled', 'done'))
        if job['state'] == 'cancelled':
            self.assertFalse((self.tmp / 'local' / 'huge.flac').exists())
        self.assertEqual(self.leftovers(), [])

    async def test_batch_play_keeps_order_and_upload_temp_is_hidden(self):
        (self.tmp / 'usb' / 'e.mp3').write_bytes(ID3)
        items = [{'root': 'usb', 'path': 'e.mp3'}, {'root': 'local', 'path': 'a.mp3'}, {'root': 'local', 'path': 'gone.mp3'}]
        r = await self.client.post('/admin/api/songs/play', json={'items': items}, headers=self.hdr())
        self.assertEqual((await r.json())['count'], 2)
        body = self.played[-1]
        self.assertEqual([t['url'] for t in body['tracks']], [str(self.tmp / 'usb' / 'e.mp3'), str(self.tmp / 'local' / 'a.mp3')])
        self.assertEqual(body['index'], 0)
        r = await self.upload(ID3, '新歌.mp3')
        self.assertEqual(r.status, 200)
        self.assertTrue((self.tmp / 'local' / '.incoming').is_dir())
        self.assertEqual(list((self.tmp / 'local' / '.incoming').iterdir()), [])
        names = [f['name'] for f in (await self.songs())['files']]
        self.assertNotIn('.incoming', names)

    async def test_music_control_whitelist(self):
        r = await self.client.post('/admin/api/music/control', json={'action': 'open'}, headers=self.hdr())
        self.assertEqual(r.status, 400)
        r = await self.client.post('/admin/api/music/control', json={'action': 'pause'}, headers=self.hdr())
        self.assertEqual(r.status, 200)
        self.assertEqual(self.played[-1]['control']['action'], 'pause')
        r = await self.client.get('/admin/api/music/status')
        j = await r.json()
        self.assertTrue(j['playing'] and j['local'])


class ClashTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seen = []

        @web.middleware
        async def need_key(request, handler):
            self.seen.append({'path': request.raw_path, 'auth': request.headers.get('Authorization'), 'method': request.method})
            if request.headers.get('Authorization') != 'Bearer s3cret-key':
                return web.json_response({'message': 'Unauthorized'}, status=401)
            return await handler(request)

        async def ok(request):
            body = await request.read()
            if (request.method == 'PUT' and request.raw_path.startswith('/proxies/')) or request.method == 'PATCH':
                return web.Response(status=204)          # 和 mihomo 一样：切节点、改模式回 204
            return web.json_response({'path': request.raw_path, 'body': body.decode() if body else ''})

        async def traffic(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            for i in range(2):
                await ws.send_str(json.dumps({'up': i, 'down': i * 10}))
            await ws.close()
            return ws
        m = web.Application(middlewares=[need_key])
        m.router.add_get('/traffic', traffic)
        m.router.add_route('*', '/{tail:.*}', ok)
        A.MIHOMO_API = await self.fake(m)
        self.admin.secret = A.mihomo_secret()
        (self.tmp / 'mihomo-ui' / 'index.html').write_text('<html>panel</html>')
        (self.tmp / 'mihomo-ui' / '200.html').write_text('<html>spa</html>')
        (self.tmp / 'mihomo-ui' / '_nuxt' / 'a.js').write_text('console.log(1)')
        (self.tmp / 'mihomo-ui' / 'sw.js').write_text('self.x=1')
        await self.login()

    async def test_secret_read_and_forwarded(self):
        self.assertEqual(A.mihomo_secret(), 's3cret-key')
        r = await self.client.get('/admin/api/clash/proxies')
        self.assertEqual(r.status, 200)
        self.assertEqual(self.seen[-1]['auth'], 'Bearer s3cret-key')
        r = await self.client.get('/admin/api/clash/version?token=from-browser&x=1')
        j = await r.json()
        self.assertIn('x=1', j['path'])
        self.assertNotIn('token', j['path'])

    async def test_encoded_node_name_kept(self):
        name = '%E9%A6%99%E6%B8%AF%2F01'
        r = await self.client.put(f'/admin/api/clash/proxies/{name}', json={'name': 'x'}, headers=self.hdr())
        self.assertEqual(r.status, 204)
        self.assertEqual(self.seen[-1]['path'], f'/proxies/{name}')

    async def test_dangerous_calls_refused(self):
        bad = [('POST', '/admin/api/clash/restart', None), ('POST', '/admin/api/clash/upgrade', None),
               ('POST', '/admin/api/clash/upgrade/ui', None), ('GET', '/admin/api/clash/debug/pprof', None),
               ('PUT', '/admin/api/clash/configs', {'path': '/tmp/evil.yaml'}), ('PUT', '/admin/api/clash/configs', {'payload': 'x'}),
               ('PATCH', '/admin/api/clash/configs', {'allow-lan': True}), ('PATCH', '/admin/api/clash/configs', {'mode': 'evil'}),
               ('PUT', '/admin/api/clash/proxies/%2E%2E', {'name': 'x'})]
        n = len(self.seen)
        for method, path, body in bad:
            r = await self.client.request(method, path, json=body, headers=self.hdr())
            self.assertIn(r.status, (403, 404), f'{method} {path}')
        self.assertEqual(len(self.seen), n, 'nothing should reach mihomo')
        r = await self.client.patch('/admin/api/clash/configs', json={'mode': 'global'}, headers=self.hdr())
        self.assertEqual(r.status, 204)
        r = await self.client.put('/admin/api/clash/configs?force=true', json={'path': '', 'payload': ''}, headers=self.hdr())
        self.assertEqual(r.status, 200)

    async def test_wrong_secret_reported(self):
        self.admin.secret = 'wrong'
        (self.tmp / 'mihomo.yaml').write_text('secret: also-wrong\n')
        r = await self.client.get('/admin/api/clash/proxies')
        self.assertEqual(r.status, 502)
        self.assertIn('密钥', (await r.json())['error'])

    async def test_ws_bridge(self):
        ws = await self.client.ws_connect('/admin/api/clash/traffic?token=x', headers={'Origin': ORIGIN})
        got = [json.loads((await ws.receive()).data) for _ in range(2)]
        await ws.close()
        self.assertEqual(got[1]['down'], 10)
        self.assertEqual(self.seen[-1]['auth'], 'Bearer s3cret-key')
        self.assertNotIn('token', self.seen[-1]['path'])

    async def test_full_panel_files(self):
        r = await self.client.get('/admin/clash-ui/')
        self.assertEqual(await r.text(), '<html>panel</html>')
        self.assertIn("frame-ancestors 'self'", r.headers['Content-Security-Policy'])
        r = await self.client.get('/admin/clash-ui/config.js')
        self.assertIn("'/api/clash'", await r.text())
        r = await self.client.get('/admin/clash-ui/sw.js')
        self.assertEqual(r.status, 404)
        r = await self.client.get('/admin/clash-ui/_nuxt/a.js')
        self.assertEqual(r.status, 200)
        r = await self.client.get('/admin/clash-ui/proxies')
        self.assertEqual(await r.text(), '<html>spa</html>')
        for bad in ('/admin/clash-ui/../mihomo.yaml', '/admin/clash-ui/%2e%2e/mihomo.yaml', '/admin/clash-ui/missing.js'):
            r = await self.client.get(bad)
            self.assertIn(r.status, (403, 404), bad)
            self.assertNotIn('s3cret', await r.text())


class IntercomProxyTests(unittest.IsolatedAsyncioTestCase):
    """intercom 的 /admin/ 转发：用假的管理后台，看流式上传下载、Cookie 和 WebSocket 是否原样过去。"""

    @classmethod
    def setUpClass(cls):
        cls.I = load(INTERCOM_PY, 'ts_intercom_admin_test')

    async def asyncSetUp(self):
        I = self.I
        self.got = {}

        async def echo(request):
            n = 0
            async for chunk in request.content.iter_chunked(65536):
                n += len(chunk)
            self.got = {'path': request.raw_path, 'cookie': request.headers.get('Cookie'), 'host': request.headers.get('X-TS-Orig-Host'),
                        'origin': request.headers.get('Origin'), 'len': n, 'method': request.method}
            resp = web.json_response({'ok': True, 'len': n})
            resp.headers.add('Set-Cookie', 'a=1; Path=/admin')
            resp.headers.add('Set-Cookie', 'b=2; Path=/admin')
            return resp

        async def big(_request):
            return web.Response(body=b'x' * (3 * 1024 * 1024), content_type='audio/flac',
                                headers={'Content-Disposition': "attachment; filename*=UTF-8''a.flac"})

        async def wsecho(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                await ws.send_str('echo:' + msg.data)
            return ws
        fake = web.Application(client_max_size=64 * 1024 * 1024)
        fake.router.add_get('/admin/big', big)
        fake.router.add_get('/admin/ws', wsecho)
        fake.router.add_route('*', '/admin/{tail:.*}', echo)
        self.fake = TestServer(fake)
        await self.fake.start_server()
        self._api = I.ADMIN_API
        I.ADMIN_API = f'http://127.0.0.1:{self.fake.port}'

        @web.middleware
        async def guard(request, handler):
            if not I._msg_origin_ok({k.lower(): v for k, v in request.headers.items()}):
                raise web.HTTPForbidden()
            return await handler(request)
        app = web.Application(client_max_size=768 * 1024, middlewares=[guard])
        app[I.ADMIN_SESSION_KEY] = aiohttp.ClientSession(trust_env=False, auto_decompress=False)

        async def _close(a):
            await a[I.ADMIN_SESSION_KEY].close()
        app.on_cleanup.append(_close)
        app.router.add_get('/admin', I.handle_admin_redirect)
        app.router.add_route('*', '/admin/{tail:.*}', I.handle_admin)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.fake.close()
        self.I.ADMIN_API = self._api

    async def test_get_keeps_raw_path_cookie_and_host(self):
        r = await self.client.get('/admin/api/clash/proxies/%E9%A6%99%2F1?x=1', headers={'Cookie': 'ts_admin=abc'})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.got['path'], '/admin/api/clash/proxies/%E9%A6%99%2F1?x=1')
        self.assertEqual(self.got['cookie'], 'ts_admin=abc')
        self.assertTrue(self.got['host'])
        self.assertEqual(r.headers.getall('Set-Cookie'), ['a=1; Path=/admin', 'b=2; Path=/admin'])

    async def test_big_upload_streams_past_intercom_limit(self):
        data = os.urandom(5 * 1024 * 1024)
        r = await self.client.post('/admin/api/songs/upload?name=a.mp3', data=data, headers={'Origin': ORIGIN.replace('http://127.0.0.1', 'http://127.0.0.1')})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.got['len'], len(data))
        self.assertEqual(self.got['method'], 'POST')

    async def test_big_download(self):
        r = await self.client.get('/admin/big')
        body = await r.read()
        self.assertEqual(len(body), 3 * 1024 * 1024)
        self.assertIn('filename*', r.headers['Content-Disposition'])

    async def test_websocket_bridge(self):
        ws = await self.client.ws_connect('/admin/ws')
        await ws.send_str('hi')
        msg = await ws.receive()
        await ws.close()
        self.assertEqual(msg.data, 'echo:hi')

    def test_peer_rule_and_forwarded_headers(self):
        I = self.I
        for ip in ('127.0.0.1', '192.168.1.23', '10.0.0.8', '172.16.42.2', '::1'):
            self.assertTrue(I._peer_ok(ip), ip)
        for ip in ('8.8.8.8', '203.0.113.10', '2001:db8:1::1', '', None, 'x'):
            self.assertFalse(I._peer_ok(ip), ip)
        hdr = {'Host': '192.168.1.100:18091', 'X-Real-IP': '1.2.3.4', 'X-Forwarded-For': '1.2.3.4', 'X-Forwarded-Proto': 'https',
               'Cookie': 'a=1', 'X-TS-Peer': 'spoof', 'X-TS-Orig-Host': 'spoof'}
        lan = I._admin_fwd_headers(hdr, '192.168.1.23')          # 局域网直连：不信它自己带的转发头
        self.assertNotIn('X-Real-IP', lan)
        self.assertNotIn('X-Forwarded-Proto', lan)
        self.assertEqual(lan['X-TS-Peer'], '192.168.1.23')
        self.assertEqual(lan['X-TS-Orig-Host'], '192.168.1.100:18091')
        self.assertEqual(lan['Cookie'], 'a=1')
        tun = I._admin_fwd_headers(hdr, '127.0.0.1')              # 隧道来的：保留 nginx 加的访问者 IP
        self.assertEqual(tun['X-Real-IP'], '1.2.3.4')
        self.assertEqual(tun['X-TS-Peer'], '127.0.0.1')

    async def test_cross_site_refused_and_down_is_502(self):
        r = await self.client.post('/admin/api/login', json={}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status, 403)
        await self.fake.close()
        r = await self.client.get('/admin/api/session')
        self.assertEqual(r.status, 502)
        r = await self.client.get('/admin', allow_redirects=False)
        self.assertIn(r.status, (301, 308))


class PageTests(unittest.TestCase):
    @staticmethod
    def css_of(html):
        return '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', html, re.S))

    def test_admin_page(self):
        s = Path(ADMIN_HTML).read_text(encoding='utf-8')
        css = self.css_of(s)
        for w in re.findall(r'font-weight:\s*(\d+)', css):
            self.assertIn(w, ('400', '700'))
        for px in re.findall(r'font(?:-size)?:\s*([\d.]+)px', css):
            self.assertNotIn('.', px)
        self.assertIsNone(re.search(r'(src|href)="https?://', s), 'no external files')
        for token in ('selToggle', 'api/songs/import', 'restoreAll', 'webkitGetAsEntry', 'local_dir', 'confirmSheet',
                      "'X-TS-Admin'", '/?embed=call', '/?embed=msg', '/?embed=cd', '/study/?embed=1', '/tutor/?embed=1',
                      'api/songs/upload', 'tsCallBusy', 'tsSetTheme', 'api/clash/', 'clash-ui/', '撤销', "localStorage.getItem('ts-theme')",
                      'password_on', 'https://203.0.113.10:18443/admin/', 'https://198.51.100.20:18443/admin/', ':18091/admin/', 'isSecureContext',
                      'api/settings', 'cfgPane-llm', 'cfgPane-voice', 'cfgPane-apis', 'cfgPane-study', 'cfgPane-clash', 'cfgPane-extra',
                      'ttsSyncStudy', 'btnPreviewTts', 'btnTestWs', 'syncTtsPvBtn', 'saveLlmDraft',
                      'btnFetchModels', 'ccliModel', 'chMaxMsgs', 'vadSilenceMs', 'wkThreshold', 'asrHwText',
                      'intentPluginBox', 'stBurstFrames', 'btnSaveDevice', 'api/settings/device'):
            self.assertIn(token, s)
        self.assertNotIn('data-view="home"', s)
        self.assertNotIn('data-view="clash"', s)
        self.assertNotIn('ovGrid', s)

    def test_phone_pages_embed_mode(self):
        p = Path(STUDY_HTML).read_text(encoding='utf-8')
        m = Path(TUTOR_MOBILE_HTML).read_text(encoding='utf-8')
        for page, hide in ((p, 'html[data-embed] .theme { display: none; }'), (m, 'html[data-embed] #btnTheme { display: none; }')):
            self.assertIn("get('embed') === '1'", page)
            self.assertIn('window.tsSetTheme', page)
            self.assertIn(hide, page)

    def test_intercom_admin_entry(self):
        s = Path(INTERCOM_PY).read_text(encoding='utf-8')
        for token in ('ADMIN_API = "http://127.0.0.1:8097"', 'app.router.add_route("*", "/admin/{tail:.*}", handle_admin)',
                      'if not _peer_ok(request.remote):', 'web.TCPSite(runner, "0.0.0.0", 18091)',
                      'app.router.add_get("/api/intercom/status", handle_intercom_status)', '"X-Frame-Options": "SAMEORIGIN"',
                      "connect-src 'self' ws: wss:; frame-ancestors 'self'", 'out = _fr(out)', 'out_hdrs = _fr(out_hdrs)', 'globals().get("_admin_frameable")',
                      "document.documentElement.dataset.embed = em", 'window.tsCallBusy', 'window.tsSetTheme',
                      "if (!/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) connectWS();"):
            self.assertIn(token, s)
        self.assertEqual(s.count("&& !/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) connectWS();"), 2)
        self.assertNotIn("frame-ancestors 'none'\"", s.split('async def handle_ws')[0].split('async def handle_index')[1])

    def test_music_page_follows_local_folder(self):
        q = Path(QQ_PY).read_text(encoding='utf-8')
        self.assertIn('def local_lib_sig() -> str:', q)
        self.assertIn("payload['local_sig'] = local_lib_sig()", q)
        self.assertIn("def search_local(query: str = '', limit: int = 50) -> list:", q)
        h = Path(QQ_HTML).read_text(encoding='utf-8')
        self.assertIn("typeof st.local_sig === 'string'", h)
        self.assertIn('loadMine(true);', h.split("typeof st.local_sig === 'string'")[1][:600])

    def test_unit_file(self):
        u = Path(ADMIN_UNIT).read_text(encoding='utf-8')
        for token in ('User=ubuntu', 'LoadCredential=mihomo.yaml:/etc/mihomo/config.yaml', 'ProtectSystem=strict',
                      'ProtectHome=read-only', 'ReadWritePaths=-/home/ubuntu/Music', '-/mnt/usb',
                      '-/home/ubuntu/.config/thinksmart-client', 'StateDirectory=thinksmart-admin',
                      'NoNewPrivileges=yes', 'MemoryMax='):
            self.assertIn(token, u)
        self.assertIn("BIND = os.environ.get('ADMIN_BIND', '127.0.0.1')", Path(ADMIN_PY).read_text(encoding='utf-8'))




class Round64AdminAuditTests(Base):
    """R64 deep audit regression tests: idempotent re-import across（2）, startup .part cleanup,
    same-origin Fetch Metadata DELETE without Origin, worker-src CSP, duplicate batch delete, & BrokenPipe safety."""

    async def test_second_import_skips_numbered_conflict_copy(self):
        await self.login()
        u = self.tmp / 'usb'
        loc = self.tmp / 'local'
        (loc / 'z.mp3').write_bytes(ID3 + b'1')
        (u / 'z.mp3').write_bytes(ID3)
        r1 = await self.client.post('/admin/api/songs/import', json={'all': True}, headers=self.hdr())
        self.assertEqual(r1.status, 200)
        for _ in range(40):
            j1 = (await (await self.client.get('/admin/api/songs/import')).json())['job']
            if j1 and j1['state'] != 'running':
                break
            await asyncio.sleep(0.05)
        self.assertEqual((j1['imported'], j1['skipped']), (1, 0))
        self.assertTrue((loc / 'z（2）.mp3').is_file())

        # Second import must detect z（2）.mp3 with matching size and skip instead of creating z（3）.mp3!
        r2 = await self.client.post('/admin/api/songs/import', json={'all': True}, headers=self.hdr())
        self.assertEqual(r2.status, 200)
        for _ in range(40):
            j2 = (await (await self.client.get('/admin/api/songs/import')).json())['job']
            if j2 and j2['state'] != 'running':
                break
            await asyncio.sleep(0.05)
        self.assertEqual((j2['imported'], j2['skipped']), (0, 1))
        self.assertFalse((loc / 'z（3）.mp3').exists())

    async def test_startup_cleans_recent_leftover_part_files_and_dedupes_delete(self):
        await self.login()
        inc = self.tmp / 'local' / '.incoming'
        inc.mkdir(exist_ok=True)
        stale = inc / '.upload-deadbeef.part'
        stale.write_bytes(b'partial')
        A.cleanup_partials(0)
        self.assertFalse(stale.exists())

        (self.tmp / 'local' / 'dup.mp3').write_bytes(ID3)
        r = await self.client.post('/admin/api/songs/delete', json={
            'items': [{'root': 'local', 'path': 'dup.mp3'}, {'root': 'local', 'path': 'dup.mp3'}]
        }, headers=self.hdr())
        j = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(j['deleted'], 1)
        self.assertEqual(j['failed'], [])

    async def test_metacubexd_worker_csp_and_same_origin_delete_without_origin(self):
        await self.login()
        (self.tmp / 'mihomo-ui' / 'index.html').write_text('<html>ui</html>')
        r_ui = await self.client.get('/admin/clash-ui/')
        self.assertIn("worker-src 'self' blob:", r_ui.headers.get('Content-Security-Policy', ''))

        # Simulate Firefox/Safari same-origin DELETE without Origin header (only Sec-Fetch-Site: same-origin)
        m = web.Application()
        m.router.add_delete('/connections', lambda _req: web.Response(status=204))
        A.MIHOMO_API = await self.fake(m)
        r_del = await self.client.delete('/admin/api/clash/connections', headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(r_del.status, 204)
        # Cross-site Sec-Fetch-Site is still strictly 403
        r_bad = await self.client.delete('/admin/api/clash/connections', headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r_bad.status, 403)




class Round65AdminAuditTests(Base):
    """R65 second-pass deep audit regression tests:
    - Subfolder upload/rename/delete/restore updates MUSIC_LOCAL mtime so qqmusic local_lib_sig() refreshes
    - Exact GET /admin/api/clash root probe + PATCH /admin/api/clash/rules/disable validation + config.js stale endpointList self-heal
    - Ambient sleep sound surfaced in /admin/api/music/status
    - Embedded intercom call/cd never auto-acks unread screen replies when #msgBox is hidden
    - Offline session boot error uses connBox instead of falsely prompting for password"""

    async def test_subfolder_mutations_bump_local_root_mtime(self):
        await self.login()
        sub = self.tmp / 'local' / '儿歌'
        sub.mkdir()
        (self.tmp / 'local' / '.thinksmart-trash').mkdir()
        song = sub / 'sub1.mp3'
        song.write_bytes(ID3)
        os.utime(self.tmp / 'local', (1700000000, 1700000000))
        t0 = (self.tmp / 'local').stat().st_mtime_ns

        # Rename inside subfolder must bump top-level local mtime
        r = await self.client.post('/admin/api/songs/rename',
                                   json={'root': 'local', 'path': '儿歌/sub1.mp3', 'name': 'sub2.mp3'},
                                   headers=self.hdr())
        self.assertEqual(r.status, 200)
        t1 = (self.tmp / 'local').stat().st_mtime_ns
        self.assertGreater(t1, t0)

        # Delete from subfolder (even when .thinksmart-trash already exists) must bump top-level local mtime
        os.utime(self.tmp / 'local', (1700000000, 1700000000))
        t_before_del = (self.tmp / 'local').stat().st_mtime_ns
        rd = await self.client.post('/admin/api/songs/delete',
                                    json={'root': 'local', 'path': '儿歌/sub2.mp3'},
                                    headers=self.hdr())
        self.assertEqual(rd.status, 200)
        tid = (await rd.json())['id']
        self.assertGreater((self.tmp / 'local').stat().st_mtime_ns, t_before_del)

        # Restore back into subfolder must also bump top-level local mtime
        os.utime(self.tmp / 'local', (1700000000, 1700000000))
        t_before_rest = (self.tmp / 'local').stat().st_mtime_ns
        rr = await self.client.post('/admin/api/songs/restore', json={'id': tid}, headers=self.hdr())
        self.assertEqual(rr.status, 200)
        self.assertGreater((self.tmp / 'local').stat().st_mtime_ns, t_before_rest)

    async def test_clash_exact_root_and_rules_disable_and_config_self_heal(self):
        await self.login()
        seen = []
        async def mihomo_handler(req):
            body = await req.read()
            seen.append((req.method, req.raw_path, body.decode() if body else ''))
            if req.raw_path == '/':
                return web.json_response({'hello': 'mihomo'})
            return web.Response(status=204)

        m = web.Application()
        m.router.add_route('*', '/{tail:.*}', mihomo_handler)
        A.MIHOMO_API = await self.fake(m)

        # Exact /admin/api/clash (defaultBackendURL without trailing slash) works
        r_root = await self.client.get('/admin/api/clash')
        self.assertEqual(r_root.status, 200)
        self.assertEqual((await r_root.json())['hello'], 'mihomo')

        # Valid PATCH rules/disable allowed; invalid payload blocked
        r_ok = await self.client.patch('/admin/api/clash/rules/disable', json={'0': True, '12': False}, headers=self.hdr())
        self.assertEqual(r_ok.status, 204)
        r_bad = await self.client.patch('/admin/api/clash/rules/disable', json={'evil': 'x'}, headers=self.hdr())
        self.assertEqual(r_bad.status, 403)

        # config.js self-heals stale endpointList
        r_cfg = await self.client.get('/admin/clash-ui/config.js')
        cfg_txt = await r_cfg.text()
        self.assertIn("localStorage.removeItem('endpointList')", cfg_txt)

    async def test_ambient_sound_status_and_embed_ack_safety(self):
        await self.login()
        m = web.Application()
        m.router.add_get('/api/music/status', lambda _r: web.json_response({
            'playing': False, 'paused': False, 'track': None,
            'ambient': {'preset': 'rain', 'label': '夏夜细雨'}, 'elapsed': 12, 'volume': 40
        }))
        A.QQ_API = await self.fake(m)
        r = await self.client.get('/admin/api/music/status')
        j = await r.json()
        self.assertTrue(j['playing'])
        self.assertTrue(j['live'])
        self.assertEqual(j['title'], '夏夜细雨')

        # Verify page & intercom embed invariants
        adm_html = open(ADMIN_HTML, encoding='utf-8').read()
        for tok in ('showConnErr', 'bootSession', 'closeClashFull', 'clashStopTimers'):
            self.assertIn(tok, adm_html)
        ic_src = open(INTERCOM_PY, encoding='utf-8').read()
        self.assertIn("$('msgBox').offsetParent !== null", ic_src)
        self.assertIn('_msgVisibleInEmbed()', ic_src)
        self.assertIn('_cdVisibleInEmbed()', ic_src)


class SettingsConsoleTests(Base):
    """2026-11-20 统一「接口与模型」配置中心回归测试。
    覆盖：网页直接设密/改密/消密、5 大配置分组保存与密钥脱敏（留空保留原密钥、ruamel 保留注释）、
    小智网关热加载通知、MacASR 地址自动补齐、学习陪伴同步、SSRF 内网端口防护、7 种在线连通测试与语音试听。"""

    async def test_web_password_enable_change_and_remove(self):
        # 1) 默认未设密码时，可直接在网页启用密码并自动拿到登录会话 cookie
        r_set = await self.client.post('/admin/api/password', json={'new': 'first-web-pw'}, headers=self.hdr())
        self.assertEqual(r_set.status, 200, await r_set.text())
        self.assertTrue((await r_set.json())['password_on'])
        self.assertTrue((A.STATE_DIR / 'auth.json').is_file())
        # 当前浏览器已自动持有会话，仍能正常读取接口
        self.assertEqual((await self.client.get('/admin/api/settings')).status, 200)

        # 新设备无 cookie 时必须返回 401
        raw_jar = list(self.client.session.cookie_jar)
        self.client.session.cookie_jar.clear()
        self.assertEqual((await self.client.get('/admin/api/settings')).status, 401)

        # 重新登录后测试输错原密码取消失败、输对原密码成功取消密码
        await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'first-web-pw'}, headers=self.hdr())
        r_bad_rm = await self.client.post('/admin/api/password', json={'old': 'wrong-pw', 'remove': True}, headers=self.hdr())
        self.assertEqual(r_bad_rm.status, 403)
        r_ok_rm = await self.client.post('/admin/api/password', json={'old': 'first-web-pw', 'remove': True}, headers=self.hdr())
        self.assertEqual(r_ok_rm.status, 200)
        self.assertFalse((await r_ok_rm.json())['password_on'])
        self.assertFalse((A.STATE_DIR / 'auth.json').exists())
        self.client.session.cookie_jar.clear()
        self.assertEqual((await self.client.get('/admin/api/settings')).status, 200)

    async def test_settings_llm_secret_masking_comment_preservation_and_reload(self):
        reloads = []
        hub_m = web.Application()
        async def handle_reload(req):
            self.assertEqual(req.headers.get('X-TS-Internal-Reload'), '1')
            self.assertNotIn('Origin', req.headers)
            reloads.append(await req.json())
            return web.json_response({'ok': True, 'results': {'LLM': {'applied': True}}})
        hub_m.router.add_post('/kiosk/internal/reload', handle_reload)
        A.HUB_API = await self.fake(hub_m)

        custom_yaml = A.HUB_DIR / 'data' / '.config.yaml'
        custom_yaml.write_text('# keep-user-comment-2026\nprompt: 你是小智\n', encoding='utf-8')
        (A.TUTOR_STATE / 'prefs.json').write_text(json.dumps({'phone_stage': {'seq': 9, 'active_id': 'p1'}}), encoding='utf-8')

        secret_key = 'sk-super-secret-deepseek-9876'
        r = await self.client.post('/admin/api/settings/llm', json={
            'vendor': 'deepseek',
            'base_url': 'https://api.deepseek.com',
            'model': 'deepseek-v4.1-flash',
            'api_key': secret_key,
            'prompt': '你是家里温暖的小智助手',
            'tutor': {
                'fast_model': 'gpt-6.1-terra',
                'deep_model': 'gpt-6.1-sol',
                'backup_model': 'deepseek-v4.1-flash',
                'mobile_entry_url': 'https://203.0.113.10:18443/tutor/',
            },
        }, headers=self.hdr())
        self.assertEqual(r.status, 200, await r.text())
        j = await r.json()
        raw_resp = json.dumps(j)
        self.assertNotIn(secret_key, raw_resp)     # 严禁回显完整密钥
        ds_v = next(v for v in j['llm']['vendors'] if v['id'] == 'deepseek')
        self.assertTrue(ds_v['key_set'])
        self.assertEqual(ds_v['key_hint'], 'sk-s••••9876')
        self.assertEqual(len(reloads), 1)
        self.assertEqual(reloads[0]['sections'], ['LLM'])
        self.assertTrue(reloads[0]['reset_device_llm'])

        # 再次保存且 api_key 留空 + prompt 清空 + base_url 带 /chat/completions 后缀：
        # 原密钥与 phone_stage、YAML 注释均保留，自定义 prompt 被删除，base_url 自动去掉 /chat/completions
        qq_m = web.Application()
        qq_m.router.add_get('/api/music/status', lambda _r: web.json_response({
            'user_profile': {'userId': '10001', 'nickname': '用户', 'isVip': True}, 'quality_level': 'flac'
        }))
        A.QQ_API = await self.fake(qq_m)

        r2 = await self.client.post('/admin/api/settings/llm', json={
            'vendor': 'deepseek',
            'base_url': 'https://api.deepseek.com/v1/chat/completions',
            'model': 'deepseek-chat',
            'api_key': '',
            'prompt': '   ',
            'tutor': {'fast_model': 'gpt-6.1-terra', 'deep_model': 'gpt-6.1-sol', 'backup_model': 'deepseek-chat'},
        }, headers=self.hdr())
        self.assertEqual(r2.status, 200)
        j2 = await r2.json()
        # 保存后返回值必须带上 qqmusic 在线状态，防止前端把 QQ 音乐状态刷成“服务未响应”
        self.assertTrue(j2['extra']['qqmusic']['online'])
        self.assertTrue(j2['extra']['qqmusic']['vip'])
        ds_v2 = next(v for v in j2['llm']['vendors'] if v['id'] == 'deepseek')
        self.assertEqual(ds_v2['base_url'], 'https://api.deepseek.com/v1')
        disk_txt = custom_yaml.read_text(encoding='utf-8')
        self.assertIn(secret_key, disk_txt)
        self.assertNotIn('你是家里温暖的小智助手', disk_txt)
        if A.HUB_PY.is_file():
            self.assertIn('# keep-user-comment-2026', disk_txt)
        prefs_after = json.loads((A.TUTOR_STATE / 'prefs.json').read_text(encoding='utf-8'))
        self.assertEqual(prefs_after['phone_stage']['seq'], 9)
        self.assertEqual(prefs_after['tutor_models']['backup'], 'deepseek-chat')

    async def test_settings_voice_apis_study_extra_and_ssrf_guard(self):
        # 0) 验证 2026-11 最新模型备选清单 + 未装 ClaudeCLI 时拒绝误切 + 已装 ClaudeCLI 模式切换与恢复 Intent
        r_init = await self.client.get('/admin/api/settings')
        j_init = await r_init.json()
        v_map = {v['id']: v['models'] for v in j_init['llm']['vendors']}
        self.assertIn('gpt-6.2-astra', v_map['openai'])
        self.assertIn('claude-sonnet-5-5', v_map['claude'])
        self.assertIn('gemini-4-hydrogen', v_map['gemini'])
        self.assertIn('qwen4-max', v_map['qwen'])
        self.assertIn('glm-5', v_map['glm'])
        self.assertIn('kimi-k3.1-pro', v_map['kimi'])
        self.assertIn('grok-4.20', v_map['grok'])
        self.assertFalse(j_init['llm']['claude_cli']['installed'])

        # 未安装 claude 二进制时，严禁把主对话切到 ClaudeCLI 导致小智静音失败
        r_no_cli = await self.client.post('/admin/api/settings/llm', json={
            'mode': 'cli', 'vendor': 'claude_cli',
            'claude_cli': {'model': 'sonnet', 'effort': 'low', 'level': 'read'},
            'tutor': {'fast_model': 'gpt-6.1-terra', 'deep_model': 'gpt-6.2-sol', 'backup_model': 'deepseek-v4.1-flash'},
        }, headers=self.hdr())
        self.assertEqual(r_no_cli.status, 400)
        r_test_nocli = await self.client.post('/admin/api/settings/test', json={'kind': 'claude_cli'}, headers=self.hdr())
        self.assertEqual(r_test_nocli.status, 404)

        # 放入模拟可执行 claude 脚本后，检测与模式切换均应通过
        fake_claude = self.tmp / 'fake-claude'
        fake_claude.write_text('#!/bin/sh\necho "Claude Code 2.1.4"\n', encoding='utf-8')
        fake_claude.chmod(0o755)
        A.CLAUDE_CLI_HINT = str(fake_claude)
        r_test_cli = await self.client.post('/admin/api/settings/test', json={'kind': 'claude_cli'}, headers=self.hdr())
        self.assertEqual(r_test_cli.status, 200, await r_test_cli.text())
        self.assertIn('2.1.4', (await r_test_cli.json())['message'])

        r_cli = await self.client.post('/admin/api/settings/llm', json={
            'mode': 'cli',
            'vendor': 'claude_cli',
            'claude_cli': {
                'model': 'sonnet', 'effort': 'medium', 'level': 'edit', 'web': True, 'workdir': '~/work',
                'first_reply_filler': '稍等', 'background_after_seconds': 45, 'turn_timeout_seconds': 900,
                'stall_timeout_seconds': 0, 'idle_timeout_seconds': 180, 'resume_window_minutes': 0,
                'progress_reply': '后台继续处理中',
            },
            'chat_history': {
                'enabled': True, 'max_messages': 16, 'max_tokens': 12000, 'display_limit': 150, 'summary': True,
                'summary_min': 8, 'summary_chars': 900, 'summary_llm': 'OpenAILLM',
            },
            'call_names': {'enabled': True, 'reply': '来啦'},
            'tutor': {'fast_model': 'gpt-6.1-terra', 'deep_model': 'gpt-6.2-sol', 'backup_model': 'deepseek-v4.1-flash'},
        }, headers=self.hdr())
        self.assertEqual(r_cli.status, 200, await r_cli.text())
        j_cli = await r_cli.json()
        self.assertEqual(j_cli['llm']['mode'], 'cli')
        self.assertTrue(j_cli['llm']['claude_cli']['installed'])
        self.assertEqual(j_cli['llm']['claude_cli']['level'], 'edit')
        self.assertTrue(j_cli['llm']['claude_cli']['web'])
        self.assertEqual(j_cli['llm']['claude_cli']['background_after_seconds'], 45)
        self.assertEqual(j_cli['llm']['claude_cli']['turn_timeout_seconds'], 900)
        self.assertEqual(j_cli['llm']['claude_cli']['stall_timeout_seconds'], 0)
        self.assertEqual(j_cli['llm']['claude_cli']['idle_timeout_seconds'], 180)
        self.assertEqual(j_cli['llm']['claude_cli']['resume_window_minutes'], 0)
        self.assertEqual(j_cli['llm']['claude_cli']['progress_reply'], '后台继续处理中')
        self.assertEqual(j_cli['llm']['chat_history']['max_messages'], 16)
        self.assertEqual(j_cli['llm']['chat_history']['max_tokens'], 12000)
        self.assertEqual(j_cli['llm']['chat_history']['display_limit'], 150)
        self.assertEqual(j_cli['llm']['chat_history']['summary_min'], 8)
        self.assertEqual(j_cli['llm']['chat_history']['summary_chars'], 900)
        self.assertEqual(j_cli['llm']['chat_history']['summary_llm'], 'OpenAILLM')
        self.assertEqual(j_cli['llm']['call_names']['reply'], '来啦')
        merged_cli, _ = A.load_hub_configs()
        self.assertEqual(merged_cli['selected_module']['Intent'], 'nointent')
        self.assertEqual(merged_cli['admin']['api_intent'], 'function_call')

        # 模拟设备空闲时遗留的语音临时模型覆盖（agent_sessions.json），验证 keep_default 保留覆盖、切换默认自动清除覆盖 + 每厂商 temperature/max_tokens 增删
        ag_sess = A.HUB_DIR / 'data' / 'agent_sessions.json'
        ag_sess.write_text(json.dumps({'last_model': {'aa:bb:cc:dd:ee:01': {'model': 'DeepSeekLLM', 'last_active': 1794532964}}}), encoding='utf-8')
        j_ov_chk = await (await self.client.get('/admin/api/settings')).json()
        self.assertEqual(j_ov_chk['llm']['device_override']['model'], 'DeepSeekLLM')

        # keep_default=True：单独保存非默认厂商参数（如 Gemini 含 temperature/max_tokens），不改默认模型、也不清除临时覆盖
        r_keep = await self.client.post('/admin/api/settings/llm', json={
            'mode': 'api', 'vendor': 'gemini', 'keep_default': True,
            'base_url': 'http://192.168.1.50:8000/v1', 'model': 'gemini-4-hydrogen', 'api_key': 'sk-gem-keep-88',
            'temperature': 0.35, 'max_tokens': 4096,
        }, headers=self.hdr())
        self.assertEqual(r_keep.status, 200, await r_keep.text())
        j_keep = await r_keep.json()
        self.assertEqual(j_keep['llm']['selected_entry'], 'ClaudeCLI')
        gem_v = next(v for v in j_keep['llm']['vendors'] if v['id'] == 'gemini')
        self.assertEqual(gem_v['model'], 'gemini-4-hydrogen')
        self.assertTrue(gem_v['key_set'])
        self.assertAlmostEqual(gem_v['temperature'], 0.35, places=2)
        self.assertEqual(gem_v['max_tokens'], 4096)
        self.assertEqual(j_keep['llm']['device_override']['model'], 'DeepSeekLLM')

        tut_cam_calls = []
        tut_app = web.Application()
        async def tut_cam_ep(req):
            tut_cam_calls.append((req.path, await req.json()))
            return web.json_response({'ok': True})
        tut_app.router.add_post('/api/tutor/camera/rotate', tut_cam_ep)
        tut_app.router.add_post('/api/tutor/camera/ctrl', tut_cam_ep)
        A.TUTOR_API = await self.fake(tut_app)

        # 切回 API 默认模型后：Intent 自动恢复为 function_call、空闲 agent_sessions.json 临时覆盖被清空、留空 temperature/max_tokens 删除旧限额、讲题高拍仪参数落盘并热推送
        r_back = await self.client.post('/admin/api/settings/llm', json={
            'mode': 'api', 'vendor': 'custom', 'base_url': 'http://192.168.1.50:11434/v1', 'model': 'qwen4-max',
            'vendor_drafts': {
                'openai': {'base_url': 'http://192.168.1.50:8000/v1', 'model': 'gpt-6.2-sol', 'api_key': 'sk-oa-draft-99'},
                'gemini': {'base_url': 'http://192.168.1.50:8000/v1', 'model': 'gemini-4-hydrogen', 'temperature': '', 'max_tokens': ''},
            },
            'call_names': {
                'enabled': True,
                'reply': '我在，请讲',
                'models': 'DeepSeekLLM = deepseek, 深度求索\nOpenAILLM = gpt, 小g',
            },
            'tutor': {
                'fast_model': 'gpt-6.1-terra', 'deep_model': 'gpt-6.2-sol', 'backup_model': 'deepseek-v4.1-flash',
                'cam_rotate': 270, 'cam_zoom': 1.5, 'cam_bright': True,
            },
        }, headers=self.hdr())
        self.assertEqual(r_back.status, 200, await r_back.text())
        j_back = await r_back.json()
        self.assertIsNone(j_back['llm']['device_override'])
        self.assertEqual(j_back['llm']['tutor']['cam_rotate'], 270)
        self.assertAlmostEqual(j_back['llm']['tutor']['cam_zoom'], 1.5, places=1)
        self.assertTrue(j_back['llm']['tutor']['cam_bright'])
        self.assertIn(('/api/tutor/camera/rotate', {'rotate': 270}), tut_cam_calls)
        self.assertIn(('/api/tutor/camera/ctrl', {'zoom': 1.5, 'bright_boost': True}), tut_cam_calls)
        self.assertEqual(json.loads(ag_sess.read_text(encoding='utf-8')).get('last_model'), {})
        gem_v_cleared = next(v for v in j_back['llm']['vendors'] if v['id'] == 'gemini')
        self.assertEqual(gem_v_cleared['temperature'], '')
        self.assertEqual(gem_v_cleared['max_tokens'], '')
        merged_back, _ = A.load_hub_configs()
        self.assertEqual(merged_back['selected_module']['Intent'], 'function_call')
        self.assertEqual(merged_back['LLM']['OpenAILLM']['model_name'], 'gpt-6.2-sol')
        self.assertNotIn('max_tokens', merged_back['LLM'].get('GeminiAPI', {}))
        self.assertEqual(merged_back['call_names']['models']['OpenAILLM'], ['gpt', '小g'])

        # 长期家庭记忆档案：读写、版本乐观锁防覆盖（409）与清空
        r_mem1 = await self.client.post('/admin/api/settings/memory', json={
            'device': 'dev-home-1', 'summary': '家里孩子读初二，喜欢物理和科幻。', 'version': 0,
        }, headers=self.hdr())
        self.assertEqual(r_mem1.status, 200, await r_mem1.text())
        j_mem1 = await r_mem1.json()
        self.assertEqual(j_mem1['memory']['version'], 1)
        r_mem_conflict = await self.client.post('/admin/api/settings/memory', json={
            'device': 'dev-home-1', 'summary': '过期版本写入', 'version': 0,
        }, headers=self.hdr())
        self.assertEqual(r_mem_conflict.status, 409)
        j_after_mem = await (await self.client.get('/admin/api/settings')).json()
        self.assertIn('初二', j_after_mem['llm']['long_memory']['summary'])

        # 模拟真实 thinksmart-study.py (:8093/api/study/config 仅允许 6 个数值键，多传任何字符串键都会 400!)
        study_posts = []
        study_app = web.Application()
        study_allowed = {'interval': (30, 600), 'cooldown': (60, 3600), 'min_gap': (30, 1800),
                         'auto_off_hours': (0, 12), 'burst_frames': (1, 4), 'burst_gap': (1, 8)}
        async def study_cfg_ep(req):
            b = await req.json()
            for k, v in b.items():
                if k not in study_allowed or not isinstance(v, int) or isinstance(v, bool):
                    return web.json_response({'error': f'bad key {k}'}, status=400)
                lo, hi = study_allowed[k]
                if not (lo <= v <= hi):
                    return web.json_response({'error': f'out of range {k}={v}'}, status=400)
            study_posts.append(b)
            return web.json_response({'ok': True})
        study_app.router.add_post('/api/study/config', study_cfg_ep)
        A.STUDY_API = await self.fake(study_app)
        (A.STUDY_STATE / 'voice').mkdir(parents=True, exist_ok=True)
        stale_pcm = A.STUDY_STATE / 'voice' / 'stale_voice.pcm'
        stale_pcm.write_bytes(b'\x00' * 320)

        # 模拟真实 thinksmart-home.py (:8095/api/home/weather/detail?force=1)
        home_hits = []
        home_app = web.Application()
        async def home_weather_ep(req):
            home_hits.append(req.rel_url.path_qs)
            return web.json_response({'ok': True})
        home_app.router.add_get('/api/home/weather/detail', home_weather_ep)
        A.HOME_API = await self.fake(home_app)

        # 1) Voice: MacASR 自动补齐 + OpenAITTS 自建免 Key + 同步学习陪伴(清旧缓存且推纯数值) + 纠错词典 + 热词 + VAD双阈值 + 唤醒二次校验
        rv = await self.client.post('/admin/api/settings/voice', json={
            'sync_tts_to_study': True,
            'tts': {
                'selected': 'IndexStreamTTS',
                'prebuffer_count': 24,
                'timeout': 18,
                'index_stream': {'api_url': 'http://192.168.1.50:8080/tts', 'voice': 'xiaohe'},
                'openai_tts': {'api_url': 'http://192.168.1.50:8010/v1/audio/speech', 'model': 'Qwen3-TTS', 'voice': 'xiaohe', 'format': 'pcm', 'speed': 1.1, 'api_key': ''},
            },
            'asr': {
                'selected': 'MacASR',
                'mac': {
                    'base_url': 'http://192.168.1.50:8010', 'model_name': 'Qwen3-ASR-1.7B',
                    'language': 'auto', 'timeout': 10, 'connect_timeout': 2.0, 'retries': 2, 'use_proxy': False,
                    'prompt': '@default',
                },
                'sherpa': {'num_threads': 3},
                'hotwords': '北京, Claude Code\n小麦同学',
                'corrections': 'Claude Code = cloud code, 克拉德扣的\n大楷 -> 大咖\n',
            },
            'vad': {'silence_ms': 650, 'threshold': 0.45, 'threshold_low': 0.25},
            'session': {
                'no_voice_close_sec': 150, 'end_prompt_enabled': True, 'end_prompt_reply': '先休息啦，有事叫我。',
                'exit_commands': '退下吧, 再见啦\n先去忙吧',
                'enable_greeting': False, 'wakeup_cache': True, 'system_error_response': '网络稍慢，请再说一遍。',
            },
            'wakeup': {
                'wake_words': ['x iǎo m ài x iǎo m ài @小麦小麦', 'x iǎo m ài t óng x ué @小麦同学'],
                'kws_threshold': 0.16,
                'kws_gain_db': 12,
                'follow_up_seconds': 8,
                'wake_verify': True,
            },
        }, headers=self.hdr())
        self.assertEqual(rv.status, 200, await rv.text())
        jv = await rv.json()
        self.assertEqual(jv['voice']['asr']['mac']['base_url'], 'http://192.168.1.50:8010/v1/audio/transcriptions')
        self.assertEqual(jv['voice']['asr']['corrections'], {
            'Claude Code': ['cloud code', '克拉德扣的'],
            '大咖': ['大楷'],
        })
        self.assertEqual(jv['voice']['asr']['hotwords'], ['北京', 'Claude Code', '小麦同学'])
        self.assertEqual(jv['voice']['vad']['silence_ms'], 650)
        self.assertAlmostEqual(jv['voice']['vad']['threshold'], 0.45, places=2)
        self.assertAlmostEqual(jv['voice']['vad']['threshold_low'], 0.25, places=2)
        self.assertEqual(jv['voice']['session']['no_voice_close_sec'], 150)
        self.assertEqual(jv['voice']['session']['exit_commands'], ['退下吧', '再见啦', '先去忙吧'])
        self.assertFalse(jv['voice']['session']['enable_greeting'])
        self.assertTrue(jv['voice']['session']['wakeup_cache'])
        self.assertEqual(jv['voice']['session']['system_error_response'], '网络稍慢，请再说一遍。')
        self.assertEqual(jv['voice']['asr']['mac']['timeout'], 10)
        self.assertAlmostEqual(jv['voice']['asr']['mac']['connect_timeout'], 2.0, places=1)
        self.assertEqual(jv['voice']['asr']['mac']['retries'], 2)
        self.assertFalse(jv['voice']['asr']['mac']['use_proxy'])
        self.assertEqual(jv['voice']['asr']['mac']['prompt'], '@default')
        self.assertEqual(jv['voice']['asr']['sherpa']['num_threads'], 3)
        self.assertEqual(jv['voice']['tts']['openai_tts']['model'], 'Qwen3-TTS')
        self.assertEqual(jv['voice']['tts']['timeout'], 18)
        self.assertEqual(jv['voice']['tts']['prebuffer_count'], 24)
        self.assertEqual(jv['voice']['wakeup']['kws_threshold'], 0.16)
        self.assertEqual(jv['voice']['wakeup']['wake_verify'], 'hub')
        self.assertTrue(jv['voice']['wakeup']['hub_wakeup_synced'])
        self.assertEqual(len(jv['voice']['wakeup']['wake_words']), 2)
        self.assertIn('重启设备后生效', jv['note'])
        self.assertEqual(oct(A.CLIENT_CFG.stat().st_mode & 0o777), '0o644')
        merged_v, _ = A.load_hub_configs()
        self.assertIsInstance(merged_v['asr_corrections']['Claude Code'], list)
        self.assertIn('小麦小麦', merged_v['wakeup_words'])
        self.assertIn('嘿你好呀', merged_v['wakeup_words'])
        self.assertEqual(merged_v['ASR']['MacASR']['output_dir'], 'tmp/')
        self.assertEqual(merged_v['TTS']['OpenAITTS']['api_key'], 'local')
        st_cfg = json.loads((A.STUDY_STATE / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(st_cfg['tts_url'], 'http://192.168.1.50:8080/tts')
        self.assertEqual(st_cfg['voice'], 'xiaohe')
        self.assertFalse(stale_pcm.exists())   # TTS 变更时自动清理旧语音缓存
        self.assertEqual(len(study_posts), 1)  # :8093 收到合法的 6 个数值键并返回 200

        # 2) APIs: 和风天气域名清洗 + 触发 /api/home/weather/detail?force=1 + 技能插件勾选 + tool_call_timeout + HA/RAGFlow 接入
        ra = await self.client.post('/admin/api/settings/apis', json={
            'plugins_enabled': ['get_weather', 'play_music', 'change_role'],
            'tool_call_timeout': 45,
            'mcp_endpoint': 'wss://mcp.example.com/ws',
            'qweather': {
                'api_host': 'https://abc123xyz.re.qweatherapi.com/',
                'api_key': 'qw_secret_key_123456',
                'city': '北京',
                'extra_cities': ['上海', '广州'],
            },
            'web_search': {
                'enabled': False,
                'base_url': 'https://api.deepseek.com/anthropic/v1',
                'model': 'deepseek-v4-flash',
                'max_uses': 4,
                'api_key': 'ws-secret-0001',
                'metaso_key': 'mk-secret-0002',
                'amap_key': 'amap-secret-0003',
            },
            'news': {
                'news_sources': '澎湃新闻，财联社；IT之家',
                'pool_size': 90,   # 自动钳位到 40
                'batch_size': 4,
                'seen_days': 5,
                'detail_chars': 2500,
                'focus_keywords': '半导体, 英伟达, 黄金',
            },
            'home_assistant': {
                'enabled': True,
                'base_url': 'http://192.168.1.60:8123/',
                'api_key': 'ha-secret-token-9999',
                'devices': '书房灯,light.study;客厅空调,climate.living',
            },
            'ragflow': {
                'enabled': True,
                'base_url': 'http://192.168.1.60:9380/',
                'api_key': 'rf-secret-key-8888',
                'dataset_ids': 'ds_family_1, ds_manual_2',
                'description': '家庭设备说明书与常用资料',
            },
        }, headers=self.hdr())
        self.assertEqual(ra.status, 200, await ra.text())
        ja = await ra.json()
        self.assertEqual(ja['apis']['tool_call_timeout'], 45)
        self.assertEqual(ja['apis']['mcp_endpoint'], 'wss://mcp.example.com/ws')
        r_bad_mcp = await self.client.post('/admin/api/settings/apis', json={
            'mcp_endpoint': 'ws://127.0.0.1:9090/ws',
            'qweather': {'city': '北京'}, 'news': {'news_sources': '财联社'},
        }, headers=self.hdr())
        self.assertEqual(r_bad_mcp.status, 400)
        self.assertEqual(ja['apis']['qweather']['api_host'], 'abc123xyz.re.qweatherapi.com')
        self.assertEqual(ja['apis']['qweather']['extra_cities'], ['上海', '广州'])
        self.assertFalse(ja['apis']['web_search']['enabled'])
        self.assertIn('hass_get_state', ja['apis']['plugins']['enabled'])
        self.assertIn('search_from_ragflow', ja['apis']['plugins']['enabled'])
        self.assertTrue(ja['apis']['home_assistant']['enabled'])
        self.assertTrue(ja['apis']['home_assistant']['key_set'])
        self.assertIn('light.study', ja['apis']['home_assistant']['devices_text'])
        self.assertTrue(ja['apis']['ragflow']['enabled'])
        self.assertEqual(ja['apis']['ragflow']['dataset_ids_text'], 'ds_family_1, ds_manual_2')
        self.assertEqual(ja['apis']['news']['news_sources'], '澎湃新闻;财联社;IT之家')
        self.assertEqual(ja['apis']['news']['pool_size'], 40)
        self.assertEqual(ja['apis']['news']['batch_size'], 4)
        self.assertEqual(ja['apis']['news']['focus_keywords'], '半导体, 英伟达, 黄金')
        self.assertEqual(ja['summary']['search_count'], 3)
        self.assertIn('/api/home/weather/detail?force=1', home_hits)
        for raw_sec in ('qw_secret_key_123456', 'ws-secret-0001', 'mk-secret-0002', 'amap-secret-0003', 'ha-secret-token-9999', 'rf-secret-key-8888'):
            self.assertNotIn(raw_sec, json.dumps(ja))

        # 非法新闻源拦截 + 一键清除已存秘塔密钥 + 清空已读新闻去重记录
        ra_bad_nw = await self.client.post('/admin/api/settings/apis', json={
            'qweather': {'city': '北京'}, 'news': {'news_sources': '不存在的火星日报'},
        }, headers=self.hdr())
        self.assertEqual(ra_bad_nw.status, 400)
        ra_clr_mk = await self.client.post('/admin/api/settings/apis', json={
            'qweather': {'api_host': 'abc123xyz.re.qweatherapi.com', 'city': '北京'},
            'web_search': {'enabled': True, 'clear_metaso_key': True},
            'news': {'news_sources': '财联社;华尔街见闻'},
        }, headers=self.hdr())
        self.assertEqual(ra_clr_mk.status, 200)
        self.assertFalse((await ra_clr_mk.json())['apis']['web_search']['metaso_set'])
        (A.HUB_DIR / 'data' / 'newsnow_seen.json').write_text('{"dev":{"abc":123}}', encoding='utf-8')
        ra_seen = await self.client.post('/admin/api/settings/apis', json={'action': 'clear_news_seen'}, headers=self.hdr())
        self.assertEqual(ra_seen.status, 200)
        self.assertEqual(json.loads((A.HUB_DIR / 'data' / 'newsnow_seen.json').read_text(encoding='utf-8')), {})

        # 3) Study: 拒绝非法长度密钥，精确对齐 study.py 数值边界 (min_gap>=30, burst_frames<=4, burst_gap<=8)
        r_bad_sec = await self.client.post('/admin/api/settings/study', json={'secret': '1234'}, headers=self.hdr())
        self.assertEqual(r_bad_sec.status, 400)
        good_sec = 'ab' * 32
        rs = await self.client.post('/admin/api/settings/study', json={
            'vision_url': 'http://192.168.1.50:8084/study/v1',
            'secret': good_sec,
            'tts_url': '',
            'voice': 'zh-CN-XiaoxiaoNeural',
            'auto_off_hours': 4,
            'interval': 60,
            'cooldown': 300,
            'min_gap': 15,         # 自动钳位到 30
            'burst_frames': 5,     # 自动钳位到 4
            'burst_gap': 15,       # 自动钳位到 8
        }, headers=self.hdr())
        self.assertEqual(rs.status, 200, await rs.text())
        js = await rs.json()
        self.assertNotIn(good_sec, json.dumps(js))
        self.assertTrue(js['study']['secret_set'])
        self.assertEqual((js['study']['min_gap'], js['study']['burst_frames'], js['study']['burst_gap']), (30, 4, 8))
        self.assertEqual(len(study_posts), 2)
        self.assertEqual((study_posts[-1]['min_gap'], study_posts[-1]['burst_frames'], study_posts[-1]['burst_gap']), (30, 4, 8))
        self.assertEqual(oct((A.STUDY_STATE / 'secret').stat().st_mode & 0o777), '0o600')

        # 4) Extra + Device: 模拟真实客户端嵌套 SSE 结构 + socks5 代理自动转 http + 系统健康与在线升级 + 蓝牙配对控制
        re_ex = await self.client.post('/admin/api/settings/extra', json={
            'nezha_host': 'nezha.example.com',
            'nezha_proxy': 'socks5://127.0.0.1:7890',
        }, headers=self.hdr())
        self.assertEqual(re_ex.status, 200)
        j_ex = await re_ex.json()
        self.assertEqual(j_ex['extra']['nezha_host'], 'https://nezha.example.com')
        self.assertEqual(j_ex['extra']['nezha_proxy'], 'http://127.0.0.1:7890')

        (A.UPDATE_STATE / 'status.json').write_text(json.dumps({
            'current': '0.1.6', 'available': '0.1.7',
            'system': {'current': '0.2.1', 'available': None},
            'checked': 1794500000, 'job': {'state': 'idle', 'message': ''},
        }), encoding='utf-8')

        qq_state = {'quality_level': 'lossless', 'max_cache_mb': 200}
        qq_app2 = web.Application()
        qq_app2.router.add_get('/api/music/status', lambda _r: web.json_response({
            'user_profile': {'userId': '10001', 'nickname': '用户', 'isVip': True},
            'quality_level': qq_state['quality_level'], 'max_cache_mb': qq_state['max_cache_mb'],
        }))
        async def qq_qual_ep(req):
            b = await req.json()
            qq_state['quality_level'] = b['quality_level']
            qq_state['max_cache_mb'] = b['max_cache_mb']
            return web.json_response({'ok': True})
        qq_app2.router.add_post('/api/music/quality', qq_qual_ep)
        A.QQ_API = await self.fake(qq_app2)
        r_qq_q = await self.client.post('/admin/api/settings/extra', json={
            'action': 'qq_quality', 'quality_level': 'hq', 'max_cache_mb': 320,
        }, headers=self.hdr())
        self.assertEqual(r_qq_q.status, 200, await r_qq_q.text())
        j_qq_q = await r_qq_q.json()
        self.assertEqual(j_qq_q['extra']['qqmusic']['quality'], 'hq')
        self.assertEqual(j_qq_q['extra']['qqmusic']['max_cache_mb'], 320)

        dev_calls = []
        dark_room = {'on': False}
        mon_app = web.Application()
        async def mon_disp(req):
            if req.method == 'GET':
                # 模拟物理背光 sysfs 尚未刷完（仍停留在旧值 35），确保返回给前端的亮度以 POST 确认值为准不回弹
                return web.json_response({'auto': False, 'level': 35, 'lux': 185.4, 'sensor_ok': True})
            body = await req.json()
            self.assertEqual(len(body), 1)   # thinksmart_display 要求每次只传单个键
            self.assertEqual(req.headers.get('Origin'), A.MONITOR_API)
            dev_calls.append(('display', body))
            if body.get('auto') and dark_room['on']:
                return web.json_response({'error': '暂时没有光线读数，请先手动调整'}, status=400)
            return web.json_response({'ok': True, 'auto': bool(body.get('auto', False)), 'level': body.get('level', 72)})
        mon_app.router.add_route('*', '/api/display', mon_disp)

        cli_app = web.Application()
        async def cli_sse(req):
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
            await sr.prepare(req)
            # 与真实 thinksmart-client /events 完全一致的嵌套结构（status / alarms / bluetooth）
            snap = {
                'status': {
                    'state': 'idle',
                    'volume': 55,
                    'screen_off': {'enabled': True, 'minutes': 10},
                    'sleep': {'enabled': True, 'start': '22:30', 'end': '06:50'},
                },
                'alarms': {
                    'volume': 75, 'tone': 'morning',
                    'items': [{'id': 7, 'kind': 'alarm', 'when': '明天 07:00', 'label': '起床', 'repeat': 'workdays'}],
                    'missed': [{'id': 2}],
                    'ringing': {'id': 9},
                },
                'bluetooth': {
                    'enabled': False, 'powered': False, 'window': 0, 'name': 'ThinkSmart',
                    'devices': [{'address': 'AA:BB:CC:11:22:33', 'name': '我的 iPhone', 'connected': False, 'streaming': False}],
                },
            }
            await sr.write(f"event: snapshot\ndata: {json.dumps(snap)}\n\n".encode('utf-8'))
            await sr.write_eof()
            return sr
        async def cli_post(req):
            dev_calls.append((req.path, await req.json()))
            return web.json_response({'ok': True})
        cli_app.router.add_get('/events', cli_sse)
        cli_app.router.add_get('/api/health', lambda _r: web.json_response({'ok': True, 'audio': True, 'hub': True, 'state': 'idle'}))
        cli_app.router.add_get('/api/network', lambda _r: web.json_response({'ip': '192.168.1.100'}))
        for p in ('/api/volume', '/api/alarm-volume', '/api/alarm-tone', '/api/screen-off', '/api/sleep', '/api/brightness', '/api/bluetooth', '/api/history/clear', '/api/model', '/api/update/check', '/api/update/install', '/api/alarms/delete', '/api/alarms/stop', '/api/alarms/missed/clear'):
            cli_app.router.add_post(p, cli_post)

        A.MONITOR_API = await self.fake(mon_app)
        A.CLIENT_API = await self.fake(cli_app)

        rd = await self.client.post('/admin/api/settings/device', json={
            'auto_brightness': False,
            'brightness': 72,
            'volume': 55,
            'alarm_volume': 75,
            'alarm_tone': 'morning',
            'screen_off': {'enabled': True, 'minutes': 10},
            'sleep': {'enabled': True, 'start': '22:30', 'end': '06:50'},
            'bluetooth': False,
        }, headers=self.hdr())
        self.assertEqual(rd.status, 200, await rd.text())
        jd = await rd.json()
        self.assertTrue(jd['device']['online'])
        self.assertEqual(jd['device']['brightness'], 72)      # 不被滞后的 GET sysfs=35 拉回
        self.assertEqual(jd['device']['volume'], 55)          # 正确解析 status.volume
        self.assertEqual(jd['device']['alarm_volume'], 75)    # 正确解析 alarms.volume
        self.assertEqual(jd['device']['alarm_tone'], 'morning')
        self.assertFalse(jd['device']['bluetooth'])
        self.assertEqual(jd['device']['health']['ip'], '192.168.1.100')
        self.assertTrue(jd['device']['health']['audio'])
        self.assertEqual(jd['device']['update']['client_current'], '0.1.6')
        self.assertEqual(jd['device']['update']['client_available'], '0.1.7')
        self.assertEqual(jd['device']['update']['system_current'], '0.2.1')
        self.assertEqual(jd['device']['bluetooth_detail']['devices'][0]['name'], '我的 iPhone')
        self.assertEqual(jd['device']['alarms_detail']['items'][0]['label'], '起床')
        self.assertTrue(jd['device']['alarms_detail']['ringing'])
        self.assertEqual(jd['device']['alarms_detail']['missed_count'], 1)
        r_al_del = await self.client.post('/admin/api/settings/device', json={'action': 'alarm_ctl', 'delete_id': 7}, headers=self.hdr())
        self.assertEqual(r_al_del.status, 200)
        self.assertIn(('/api/alarms/delete', {'id': 7}), dev_calls)
        r_al_stp = await self.client.post('/admin/api/settings/device', json={'action': 'alarm_ctl', 'stop_ringing': True}, headers=self.hdr())
        self.assertEqual(r_al_stp.status, 200)
        self.assertIn(('/api/alarms/stop', {}), dev_calls)
        r_al_mis = await self.client.post('/admin/api/settings/device', json={'action': 'alarm_ctl', 'clear_missed': True}, headers=self.hdr())
        self.assertEqual(r_al_mis.status, 200)
        self.assertIn(('/api/alarms/missed/clear', {}), dev_calls)
        self.assertIn(('display', {'level': 72}), dev_calls)
        self.assertNotIn(('/api/brightness', {'level': 72}), dev_calls)   # 由 :8092 内部同步，避免并发写冲突
        self.assertIn(('/api/volume', {'volume': 55}), dev_calls)
        self.assertIn(('/api/bluetooth', {'enabled': False}), dev_calls)

        r_bt = await self.client.post('/admin/api/settings/device', json={'action': 'bt_ctl', 'window': True}, headers=self.hdr())
        self.assertEqual(r_bt.status, 200, await r_bt.text())
        self.assertIn(('/api/bluetooth', {'window': True}), dev_calls)
        r_upd_chk = await self.client.post('/admin/api/settings/device', json={'action': 'update_check'}, headers=self.hdr())
        self.assertEqual(r_upd_chk.status, 200)
        self.assertIn(('/api/update/check', {}), dev_calls)
        r_upd_ins = await self.client.post('/admin/api/settings/device', json={'action': 'update_install', 'version': '0.1.7'}, headers=self.hdr())
        self.assertEqual(r_upd_ins.status, 200)
        self.assertIn(('/api/update/install', {'version': '0.1.7', 'system': ''}), dev_calls)
        (A.HUB_DIR / 'data' / 'agent_sessions.json').write_text(json.dumps({'last_model': {'dev1': {'model': 'OpenAILLM'}}}), encoding='utf-8')
        r_rst_llm = await self.client.post('/admin/api/settings/device', json={'action': 'reset_device_llm'}, headers=self.hdr())
        self.assertEqual(r_rst_llm.status, 200)
        self.assertIn(('/api/model', {'kind': 'default'}), dev_calls)
        self.assertEqual(json.loads((A.HUB_DIR / 'data' / 'agent_sessions.json').read_text(encoding='utf-8'))['last_model'], {})

        dark_room['on'] = True
        rd_dark = await self.client.post('/admin/api/settings/device', json={
            'auto_brightness': True, 'brightness': 72, 'volume': 55, 'alarm_volume': 75, 'alarm_tone': 'morning',
            'screen_off': {'enabled': True, 'minutes': 10}, 'sleep': {'enabled': True, 'start': '22:30', 'end': '06:50'}, 'bluetooth': False,
        }, headers=self.hdr())
        self.assertEqual(rd_dark.status, 200)
        self.assertIn('暂时没有光线读数', (await rd_dark.json())['note'])

        rc = await self.client.post('/admin/api/settings/device', json={'action': 'clear_history', 'memory_only': True}, headers=self.hdr())
        self.assertEqual(rc.status, 200)
        self.assertIn(('/api/history/clear', {'memory_only': True}), dev_calls)

        # 4b) R74 小智网关内部诊断联动：最近识别实录、90秒呼名试录、唤醒/呼名旧语音缓存清理与小智中枢版本检查
        diag_calls = []
        learn_state = {'active': False, 'target': None, 'left': 0, 'need': 3, 'got': [], 'remaining': 0}
        hub_diag = web.Application()
        async def diag_get(req):
            if req.headers.get('X-TS-Internal-Reload') != '1':
                return web.json_response({'ok': False}, status=403)
            return web.json_response({
                'ok': True,
                'asr_recent': [{'time': 1794550000, 'device_id': 'dev1', 'raw': '克拉德扣的', 'text': 'Claude Code'}],
                'call_learning': dict(learn_state),
                'hub_version': {'short': 'abc1234', 'subject': 'hub main'},
                'hub_check': {'up_to_date': True, 'behind': 0, 'current': {'short': 'abc1234'}},
            })
        async def diag_post(req):
            if req.headers.get('X-TS-Internal-Reload') != '1':
                return web.json_response({'ok': False}, status=403)
            b = await req.json()
            diag_calls.append(b)
            act = b.get('action')
            if act == 'learn_start':
                learn_state.update({'active': True, 'target': b.get('target'), 'left': 3, 'remaining': 90})
                return web.json_response({'ok': True, 'call_learning': dict(learn_state)})
            if act == 'learn_stop':
                learn_state.update({'active': False, 'remaining': 0, 'got': [{'text': '嗨小鸡', 'alias': '小鸡'}]})
                return web.json_response({'ok': True, 'call_learning': dict(learn_state)})
            if act == 'clear_tts_cache':
                return web.json_response({'ok': True})
            if act == 'hub_update_check':
                return web.json_response({'ok': True, 'hub_check': {'up_to_date': True, 'behind': 0, 'current': {'short': 'abc1234'}}})
            return web.json_response({'ok': False}, status=400)
        hub_diag.router.add_get('/kiosk/internal/diag', diag_get)
        hub_diag.router.add_post('/kiosk/internal/diag', diag_post)
        hub_diag.router.add_post('/kiosk/internal/reload', lambda _r: web.json_response({'ok': True, 'results': {}}))
        hub_diag.router.add_get('/kiosk/internal/memory', lambda _r: web.json_response({'ok': True, 'devices': []}))
        A.HUB_API = await self.fake(hub_diag)

        j_diag = await (await self.client.get('/admin/api/settings')).json()
        self.assertEqual(j_diag['voice']['asr']['recent'][0]['raw'], '克拉德扣的')
        self.assertEqual(j_diag['device']['hub_version']['current']['short'], 'abc1234')

        r_bad_tgt = await self.client.post('/admin/api/settings/llm', json={'action': 'call_learn_start', 'target': 'UnknownLLM'}, headers=self.hdr())
        self.assertEqual(r_bad_tgt.status, 400)
        r_lstart = await self.client.post('/admin/api/settings/llm', json={'action': 'call_learn_start', 'target': 'GrokLLM'}, headers=self.hdr())
        self.assertEqual(r_lstart.status, 200, await r_lstart.text())
        self.assertTrue((await r_lstart.json())['call_learning']['active'])
        r_lstop = await self.client.post('/admin/api/settings/llm', json={'action': 'call_learn_stop'}, headers=self.hdr())
        self.assertEqual(r_lstop.status, 200)
        self.assertEqual((await r_lstop.json())['call_learning']['got'][0]['alias'], '小鸡')

        ww_cache = A.HUB_DIR / 'data' / '.wakeup_words.yaml'
        ww_cache.parent.mkdir(parents=True, exist_ok=True)
        ww_cache.write_text('cached: true\n', encoding='utf-8')
        cn_cache_dir = A.HUB_DIR / 'data' / 'call_reply_audio'
        cn_cache_dir.mkdir(parents=True, exist_ok=True)
        cn_opus = cn_cache_dir / 'reply.opus'
        cn_opus.write_bytes(b'OggS')
        r_clr_tts = await self.client.post('/admin/api/settings/voice', json={'action': 'clear_tts_cache'}, headers=self.hdr())
        self.assertEqual(r_clr_tts.status, 200, await r_clr_tts.text())
        self.assertFalse(ww_cache.exists())
        self.assertFalse(cn_opus.exists())
        self.assertIn({'action': 'clear_tts_cache'}, diag_calls)

        r_hub_upd = await self.client.post('/admin/api/settings/device', json={'action': 'hub_update_check'}, headers=self.hdr())
        self.assertEqual(r_hub_upd.status, 200, await r_hub_upd.text())
        self.assertIn('abc1234', (await r_hub_upd.json())['note'])

        # 5) SSRF 防护：严禁将接口指向本机受保护内部端口、链路本地元数据或非 HTTP 协议
        for bad_url in (
            'http://127.0.0.1:9090/v1',
            'http://127.0.0.1:18091/v1',
            'http://localhost:8097/v1',
            'http://169.254.169.254/latest',
            'file:///etc/passwd',
            'http://admin:123@example.com/v1',
        ):
            rb = await self.client.post('/admin/api/settings/llm', json={
                'vendor': 'custom', 'base_url': bad_url, 'model': 'qwen',
            }, headers=self.hdr())
            self.assertEqual(rb.status, 400, bad_url)

    async def test_live_connectivity_and_tts_preview_endpoints(self):
        up = web.Application()
        up.router.add_get('/v1/models', lambda _r: web.json_response({
            'data': [{'id': 'gpt-6.2-sol'}, {'id': 'gpt-6.1-terra'}, {'id': 'models/gemini-4-hydrogen'}]
        }))
        up.router.add_post('/v1/chat/completions', lambda _r: web.json_response({
            'choices': [{'message': {'content': '', 'reasoning_content': 'OK'}}]
        }))
        up.router.add_get('/geo/v2/city/lookup', lambda _r: web.json_response({
            'code': '200', 'location': [{'id': '101010100', 'name': '北京'}]
        }))
        up.router.add_get('/v7/weather/now', lambda _r: web.json_response({
            'code': '200', 'now': {'text': '晴', 'temp': '16', 'humidity': '30'}
        }))
        up.router.add_post('/anthropic/v1/messages', lambda _r: web.json_response({
            'content': [
                {'type': 'server_tool_use', 'name': 'web_search', 'input': {'query': '北京天气'}},
                {'type': 'text', 'text': '今天北京晴'},
            ]
        }))
        up.router.add_post('/metaso', lambda _r: web.json_response({'webpages': [{'title': 'news'}]}))
        up.router.add_get('/amap', lambda _r: web.json_response({
            'status': '1', 'geocodes': [{'formatted_address': '北京市朝阳区'}]
        }))
        up.router.add_get('/api/v1/service', lambda _r: web.json_response({
            'success': True, 'data': {'services': {'1': {'name': '联通'}, '2': {'name': '电信'}, '3': {'name': '移动'}}},
        }))
        up.router.add_get('/api/', lambda req: (
            web.json_response({'message': 'API running.'})
            if req.headers.get('Authorization') == 'Bearer ha-ok'
            else web.json_response({'message': 'Unauthorized'}, status=401)
        ))
        up.router.add_get('/api/v1/datasets', lambda req: (
            web.json_response({'code': 0, 'data': [{'id': 'ds1'}]})
            if req.headers.get('Authorization') == 'Bearer rf-ok'
            else web.json_response({'code': 102, 'message': 'bad key'})
        ))
        async def mac_asr_ep(req):
            ct = req.headers.get('Content-Type', '')
            self.assertIn('multipart/form-data', ct)
            raw_m = await req.read()
            self.assertIn(b'RIFF', raw_m)
            return web.json_response({'text': ''})

        expected_study_sec = bytes.fromhex('cd' * 32)
        async def study_vision_ep(req):
            env = await req.json()
            for k in ('v', 'ts', 'nonce', 'ct', 'tag'):
                if k not in env:
                    return web.json_response({'error': 'bad envelope'}, status=400)
            import base64 as _b64, hashlib as _hl, hmac as _hm
            mac_k = _hm.new(expected_study_sec, b'study-vision mac req', _hl.sha256).digest()
            nonce = _b64.b64decode(env['nonce'])
            ct_b = _b64.b64decode(env['ct'])
            tag_b = _b64.b64decode(env['tag'])
            good_tag = _hm.new(mac_k, b'v1|%d|' % int(env['ts']) + nonce + ct_b, _hl.sha256).digest()
            if not _hm.compare_digest(tag_b, good_tag):
                return web.json_response({'error': 'denied'}, status=403)
            return web.json_response({'ok': True})

        up.router.add_post('/v1/audio/transcriptions', mac_asr_ep)
        up.router.add_post('/study/v1', study_vision_ep)

        async def stream_tts(req):
            # 模拟 IndexStreamTTS 分 6 个 HTTP chunk 流式输出（防止首个分块 2~3 个字就被截断！）
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'application/octet-stream'})
            await sr.prepare(req)
            chunk = b'\x10\x00' * 10000     # 每块 20,000 字节，共 6 块 = 120,000 字节 (2.5 秒 24kHz PCM)
            for _ in range(6):
                await sr.write(chunk)
                await sr.drain()
                await asyncio.sleep(0.01)
            await sr.write_eof()
            return sr

        async def oa_tts_handler(req):
            body = await req.json()
            if body.get('response_format') == 'pcm':
                return web.Response(body=b'\x08\x00' * 24000, content_type='application/octet-stream')
            return web.Response(body=ID3 + b'\x00' * 800, content_type='audio/mpeg')

        up.router.add_post('/tts', stream_tts)
        up.router.add_post('/oa_tts', oa_tts_handler)
        up.router.add_post('/sf_tts', lambda _r: web.Response(body=ID3 + b'\x00' * 800, content_type='audio/mpeg'))
        up.router.add_get('/newsnow', lambda _r: web.json_response({'items': [{'title': 'A股三大指数集体收涨'}]}))
        base = await self.fake(up)
        host_port = base.split('://', 1)[1]

        A.METASO_TEST_URL = base + '/metaso'
        A.AMAP_TEST_URL = base + '/amap'
        A.SILICONFLOW_TTS_URL = base + '/sf_tts'
        A.NEWSNOW_API_URL = base + '/newsnow?id='
        A.QWEATHER_PROTO = 'http'

        # 0) LLM live models list fetch (/v1/models)
        r_mod = await self.client.post('/admin/api/settings/test', json={
            'kind': 'llm_models', 'vendor': 'openai', 'base_url': base, 'api_key': 'sk-test1234'
        }, headers=self.hdr())
        self.assertEqual(r_mod.status, 200, await r_mod.text())
        self.assertEqual((await r_mod.json())['models'], ['gpt-6.2-sol', 'gpt-6.1-terra', 'gemini-4-hydrogen'])

        # 1) LLM test (including reasoning_content fallback & /chat/completions suffix tolerance) + Tutor 3-tier test
        r1 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'llm', 'vendor': 'deepseek', 'base_url': base + '/v1/chat/completions', 'model': 'deepseek-reasoner', 'api_key': 'sk-test1234'
        }, headers=self.hdr())
        self.assertEqual(r1.status, 200, await r1.text())
        self.assertIn('OK', (await r1.json())['message'])
        await self.client.post('/admin/api/settings/llm', json={
            'mode': 'api', 'vendor': 'openai', 'base_url': base, 'model': 'gpt-6.1-terra', 'api_key': 'sk-oa-123',
            'vendor_drafts': {'deepseek': {'base_url': base, 'model': 'deepseek-v4.1-flash', 'api_key': 'sk-ds-456'}},
        }, headers=self.hdr())
        r_tut = await self.client.post('/admin/api/settings/test', json={
            'kind': 'tutor_llm', 'fast_model': 'gpt-6.1-terra', 'deep_model': 'gpt-6.1-sol', 'backup_model': 'deepseek-v4.1-flash'
        }, headers=self.hdr())
        self.assertEqual(r_tut.status, 200, await r_tut.text())
        self.assertIn('三档讲题模型全部就绪', (await r_tut.json())['message'])

        # 2) QWeather test
        r2 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'qweather', 'api_host': host_port, 'api_key': 'qw_key_123456', 'city': '北京'
        }, headers=self.hdr())
        self.assertEqual(r2.status, 200, await r2.text())
        self.assertIn('16°C', (await r2.json())['message'])

        # 3) Native Web Search + Metaso & AMap + News + Nezha + Home Assistant + RAGFlow tests
        r_ws = await self.client.post('/admin/api/settings/test', json={
            'kind': 'web_search', 'base_url': base + '/anthropic/v1', 'model': 'deepseek-v4-flash', 'api_key': 'sk-ws-1234'
        }, headers=self.hdr())
        self.assertEqual(r_ws.status, 200, await r_ws.text())
        r3 = await self.client.post('/admin/api/settings/test', json={'kind': 'metaso', 'metaso_key': 'mk-123456'}, headers=self.hdr())
        self.assertEqual(r3.status, 200)
        r4 = await self.client.post('/admin/api/settings/test', json={'kind': 'amap', 'amap_key': 'am-123456', 'city': '北京'}, headers=self.hdr())
        self.assertEqual(r4.status, 200)
        r_nw = await self.client.post('/admin/api/settings/test', json={'kind': 'news', 'news_sources': '财联社;华尔街见闻'}, headers=self.hdr())
        self.assertEqual(r_nw.status, 200, await r_nw.text())
        self.assertIn('A股三大指数', (await r_nw.json())['message'])
        r_nz = await self.client.post('/admin/api/settings/test', json={'kind': 'nezha', 'nezha_host': base, 'nezha_proxy': ''}, headers=self.hdr())
        self.assertEqual(r_nz.status, 200, await r_nz.text())
        self.assertIn('3 个', (await r_nz.json())['message'])
        r_ha = await self.client.post('/admin/api/settings/test', json={'kind': 'hass', 'base_url': base, 'api_key': 'ha-ok'}, headers=self.hdr())
        self.assertEqual(r_ha.status, 200, await r_ha.text())
        r_ha_bad = await self.client.post('/admin/api/settings/test', json={'kind': 'hass', 'base_url': base, 'api_key': 'ha-wrong'}, headers=self.hdr())
        self.assertEqual(r_ha_bad.status, 502)
        r_rf = await self.client.post('/admin/api/settings/test', json={'kind': 'ragflow', 'base_url': base, 'api_key': 'rf-ok'}, headers=self.hdr())
        self.assertEqual(r_rf.status, 200, await r_rf.text())
        r_rf_bad = await self.client.post('/admin/api/settings/test', json={'kind': 'ragflow', 'base_url': base, 'api_key': 'rf-wrong'}, headers=self.hdr())
        self.assertEqual(r_rf_bad.status, 502)

        # 4) ASR (SherpaXASR local, MacASR real multipart WAV POST, Doubao credentials) + Study Vision HMAC seal verification
        r5 = await self.client.post('/admin/api/settings/test', json={'kind': 'asr', 'selected': 'SherpaXASR'}, headers=self.hdr())
        self.assertEqual(r5.status, 200)
        r6 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'asr', 'selected': 'MacASR', 'base_url': base, 'model_name': 'Qwen3-ASR-1.7B'
        }, headers=self.hdr())
        self.assertEqual(r6.status, 200, await r6.text())
        r6_404 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'asr', 'selected': 'MacASR', 'base_url': base + '/wrong/v1/audio/transcriptions'
        }, headers=self.hdr())
        self.assertEqual(r6_404.status, 502)

        r_db_bad = await self.client.post('/admin/api/settings/test', json={
            'kind': 'asr', 'selected': 'DoubaoStreamASRV2', 'auth_mode': 'api_key', 'api_key': ''
        }, headers=self.hdr())
        self.assertEqual(r_db_bad.status, 400)
        r_db_ok = await self.client.post('/admin/api/settings/test', json={
            'kind': 'asr', 'selected': 'DoubaoStreamASRV2', 'auth_mode': 'api_key', 'api_key': 'db-key-123'
        }, headers=self.hdr())
        self.assertEqual(r_db_ok.status, 200)

        r7 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'study_vision', 'vision_url': base + '/study/v1', 'secret': 'cd' * 32
        }, headers=self.hdr())
        self.assertEqual(r7.status, 200, await r7.text())
        r7_wrong_sec = await self.client.post('/admin/api/settings/test', json={
            'kind': 'study_vision', 'vision_url': base + '/study/v1', 'secret': 'ef' * 32
        }, headers=self.hdr())
        self.assertEqual(r7_wrong_sec.status, 502)
        self.assertIn('密钥不匹配', (await r7_wrong_sec.json())['error'])

        # 5) Multi-chunk IndexStreamTTS preview -> MUST read all 6 chunks (120,000 PCM bytes + 44 WAV header = 120,044 bytes)!
        r8 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'tts_preview', 'selected': 'IndexStreamTTS', 'api_url': base + '/tts', 'voice': 'xiaohe', 'text': '你好，我是家里的小智语音助手，现在声音正常。'
        }, headers=self.hdr())
        self.assertEqual(r8.status, 200, await r8.text())
        j8 = await r8.json()
        self.assertEqual(j8['mime'], 'audio/wav')
        import base64 as _b64
        wav_raw = _b64.b64decode(j8['audio_b64'])
        self.assertEqual(wav_raw[:4], b'RIFF')
        self.assertEqual(len(wav_raw), 120044)
        self.assertAlmostEqual(j8['duration'], 2.5, places=1)

        # 6) OpenAITTS (both mp3 and keyless self-hosted raw pcm auto-wrapped into WAV!) & CosyVoiceSiliconflow direct preview
        r9_cloud_nokey = await self.client.post('/admin/api/settings/test', json={
            'kind': 'tts_preview', 'selected': 'OpenAITTS', 'api_url': 'https://api.openai.com/v1/audio/speech', 'api_key': ''
        }, headers=self.hdr())
        self.assertEqual(r9_cloud_nokey.status, 400)

        r9 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'tts_preview', 'selected': 'OpenAITTS', 'api_url': base + '/oa_tts', 'model': 'tts-1', 'voice': 'alloy', 'format': 'mp3', 'api_key': ''
        }, headers=self.hdr())
        self.assertEqual(r9.status, 200, await r9.text())
        self.assertEqual((await r9.json())['mime'], 'audio/mpeg')

        r9_pcm = await self.client.post('/admin/api/settings/test', json={
            'kind': 'tts_preview', 'selected': 'OpenAITTS', 'api_url': base + '/oa_tts', 'model': 'Qwen3-TTS', 'voice': 'xiaohe', 'format': 'pcm', 'speed': 1.1, 'api_key': ''
        }, headers=self.hdr())
        self.assertEqual(r9_pcm.status, 200, await r9_pcm.text())
        j9_pcm = await r9_pcm.json()
        self.assertEqual(j9_pcm['mime'], 'audio/wav')
        self.assertEqual(_b64.b64decode(j9_pcm['audio_b64'])[:4], b'RIFF')

        r10 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'tts_preview', 'selected': 'CosyVoiceSiliconflow', 'model': 'FunAudioLLM/CosyVoice2-0.5B', 'voice': 'alex', 'access_token': 'sf-tok'
        }, headers=self.hdr())
        self.assertEqual(r10.status, 200, await r10.text())
        self.assertEqual((await r10.json())['mime'], 'audio/mpeg')

        r11 = await self.client.post('/admin/api/settings/test', json={
            'kind': 'tts_preview', 'selected': 'HuoshanDoubleStreamTTSV2'
        }, headers=self.hdr())
        self.assertEqual(r11.status, 501)

    async def test_r75_round6_full_settings_parity_and_clash_toggles(self):
        # 1) ClaudeCLI 5 new fields (preserving 0 values) + preview_prompt
        fake_claude = self.tmp / 'bin' / 'claude-custom'
        fake_claude.parent.mkdir(parents=True, exist_ok=True)
        fake_claude.write_text('#!/bin/sh\necho "2.1.99 (Claude Code)"\n', encoding='utf-8')
        fake_claude.chmod(0o755)
        A.CLAUDE_CLI_HINT = ''

        r_tccli = await self.client.post('/admin/api/settings/test', json={
            'kind': 'claude_cli', 'cli_path': str(fake_claude),
        }, headers=self.hdr())
        self.assertEqual(r_tccli.status, 200, await r_tccli.text())
        self.assertIn('2.1.99', (await r_tccli.json())['message'])

        hub_r75 = web.Application()
        hub_r75.router.add_get('/kiosk/internal/diag', lambda _r: web.json_response({
            'ok': True, 'asr_recent': [], 'call_learning': {'active': False},
            'hub_version': {'short': '75f00d1'}, 'hub_check': None,
            'hub_checks': [{'name': 'ffmpeg', 'level': 'ok', 'detail': '/usr/bin/ffmpeg'}],
        }))
        async def diag_post_r75(req):
            b = await req.json()
            if b.get('action') == 'preview_prompt':
                return web.json_response({
                    'ok': True, 'template': 'hub-base-prompt.txt',
                    'rendered': '【已渲染】' + str(b.get('prompt') or ''),
                    'tools': 'get_weather, web_search',
                })
            return web.json_response({'ok': True})
        hub_r75.router.add_post('/kiosk/internal/diag', diag_post_r75)
        hub_r75.router.add_post('/kiosk/internal/reload', lambda _r: web.json_response({'ok': True, 'results': {}}))
        hub_r75.router.add_get('/kiosk/internal/memory', lambda _r: web.json_response({'ok': True, 'devices': []}))
        A.HUB_API = await self.fake(hub_r75)

        r_pv = await self.client.post('/admin/api/settings/llm', json={
            'action': 'preview_prompt', 'prompt': '你是温柔耐心的家庭伙伴',
        }, headers=self.hdr())
        self.assertEqual(r_pv.status, 200, await r_pv.text())
        jpv = await r_pv.json()
        self.assertIn('你是温柔耐心的家庭伙伴', jpv['rendered'])
        self.assertIn('web_search', jpv['tools'])

        r_ccli = await self.client.post('/admin/api/settings/llm', json={
            'mode': 'cli', 'vendor': 'claude_cli',
            'claude_cli': {
                'model': 'sonnet', 'effort': 'high', 'level': 'edit', 'web': True, 'workdir': '~/work',
                'first_reply_filler': '等一下', 'filler_delay_seconds': 0,
                'progress_interval_seconds': 0, 'background_after_seconds': 25, 'background_max_seconds': 0,
                'turn_timeout_seconds': 600, 'stall_timeout_seconds': 0, 'idle_timeout_seconds': 0,
                'resume_window_minutes': 0, 'resume_keywords': '接着上次聊, 继续刚才的话题',
                'cli_path': str(fake_claude), 'progress_reply': '还在忙',
            },
        }, headers=self.hdr())
        self.assertEqual(r_ccli.status, 200, await r_ccli.text())
        jc = (await r_ccli.json())['llm']['claude_cli']
        self.assertEqual(jc['filler_delay_seconds'], 0)
        self.assertEqual(jc['progress_interval_seconds'], 0)
        self.assertEqual(jc['background_max_seconds'], 0)
        self.assertEqual(jc['resume_keywords'], ['接着上次聊', '继续刚才的话题'])
        self.assertEqual(jc['cli_path_cfg'], str(fake_claude))
        self.assertTrue(jc['installed'])

        # 2) Voice kws_score + session.delete_audio
        r_vc = await self.client.post('/admin/api/settings/voice', json={
            'tts': {'selected': 'EdgeTTS', 'edge': {'voice': 'zh-CN-XiaoxiaoNeural'}},
            'asr': {'selected': 'SherpaXASR'},
            'session': {'delete_audio': False},
            'wakeup': {'kws_threshold': 0.22, 'kws_score': 2.8, 'kws_gain_db': 12, 'follow_up_seconds': 8},
        }, headers=self.hdr())
        self.assertEqual(r_vc.status, 200, await r_vc.text())
        jvc = (await r_vc.json())['voice']
        self.assertFalse(jvc['session']['delete_audio'])
        self.assertAlmostEqual(jvc['wakeup']['kws_score'], 2.8, places=1)

        # 3) VLLM config save + live multimodal PNG test + clear
        vllm_seen = []
        vllm_app = web.Application()
        async def vllm_chat(req):
            b = await req.json()
            vllm_seen.append(b)
            return web.json_response({'choices': [{'message': {'content': 'OK'}}]})
        vllm_app.router.add_post('/v1/chat/completions', vllm_chat)
        vllm_base = await self.fake(vllm_app)

        r_ap = await self.client.post('/admin/api/settings/apis', json={
            'qweather': {'api_host': '', 'city': '北京'},
            'web_search': {'enabled': True, 'base_url': 'https://api.deepseek.com/anthropic/v1', 'model': 'deepseek-v4-flash'},
            'news': {'news_sources': '财联社'},
            'vllm': {'base_url': vllm_base, 'model': 'gpt-6.1-terra', 'api_key': ''},
        }, headers=self.hdr())
        self.assertEqual(r_ap.status, 200, await r_ap.text())
        jap = (await r_ap.json())['apis']['vllm']
        self.assertTrue(jap['configured'])
        self.assertEqual(jap['entry'], 'OpenAIVLLM')
        self.assertEqual(jap['model'], 'gpt-6.1-terra')
        self.assertFalse(jap['key_set'])

        r_vt = await self.client.post('/admin/api/settings/test', json={
            'kind': 'vllm', 'base_url': '', 'model': '', 'api_key': '',
        }, headers=self.hdr())
        self.assertEqual(r_vt.status, 200, await r_vt.text())
        self.assertIn('OK', (await r_vt.json())['message'])
        self.assertEqual(vllm_seen[-1]['messages'][0]['content'][1]['type'], 'image_url')

        # 4) Study toggle + QQ Music radio_audio_mode / wake / QR login / logout + Device mic_on / snooze / play_reports
        st_calls, qq_calls, dv_calls = [], [], []
        st_app = web.Application()
        async def st_toggle(req):
            b = await req.json()
            st_calls.append(b)
            A.write_private_json(A.STUDY_STATE / 'config.json', {'enabled': bool(b.get('enabled'))})
            return web.json_response({'ok': True, 'enabled': bool(b.get('enabled'))})
        st_app.router.add_post('/api/study/toggle', st_toggle)
        A.STUDY_API = await self.fake(st_app)

        r_st = await self.client.post('/admin/api/settings/study', json={'action': 'toggle_study', 'enabled': True}, headers=self.hdr())
        self.assertEqual(r_st.status, 200, await r_st.text())
        self.assertTrue((await r_st.json())['study']['enabled'])
        self.assertEqual(st_calls, [{'enabled': True}])

        qq_state = {'logged_in': False, 'radio': 'hifi', 'wake': True, 'wake_time': '06:30'}
        qq_app = web.Application()
        qq_app.router.add_get('/api/music/status', lambda _r: web.json_response({
            'logged_in': qq_state['logged_in'], 'quality_level': 'lossless', 'max_cache_mb': 200,
            'user_profile': {'nickname': '会员账号', 'isVip': True} if qq_state['logged_in'] else None,
        }))
        async def qq_sleep_ep(req):
            if req.method == 'POST':
                b = await req.json()
                qq_calls.append(('sleep', b))
                if 'on' in b:
                    qq_state['wake'] = bool(b['on'])
                if 'time' in b:
                    qq_state['wake_time'] = b['time']
            return web.json_response({'ok': True, 'wake': qq_state['wake'], 'wake_time': qq_state['wake_time']})
        async def qq_radio_ep(req):
            b = await req.json()
            qq_calls.append(('radio', b))
            qq_state['radio'] = b['mode']
            A.QQ_CFG_DIR.mkdir(parents=True, exist_ok=True)
            (A.QQ_CFG_DIR / 'config.json').write_text(json.dumps({'radio_audio_mode': b['mode']}), encoding='utf-8')
            return web.json_response({'ok': True, 'mode': b['mode']})
        qq_app.router.add_get('/api/music/sleep_session', qq_sleep_ep)
        qq_app.router.add_post('/api/music/sleep_session', qq_sleep_ep)
        qq_app.router.add_post('/api/music/quality', lambda _r: web.json_response({'ok': True}))
        qq_app.router.add_post('/api/music/radio/audio_mode', qq_radio_ep)
        qq_app.router.add_post('/api/music/qr/create', lambda _r: web.json_response({
            'ok': True, 'mode': 'qq', 'qrsig': 'sig123', 'qr_img': 'data:image/png;base64,AAAA',
        }))
        async def qq_qr_chk(req):
            b = await req.json()
            qq_calls.append(('qr_check', b))
            qq_state['logged_in'] = True
            return web.json_response({'ok': True, 'code': 0, 'message': '登录成功'})
        async def qq_out_ep(_r):
            qq_state['logged_in'] = False
            return web.json_response({'ok': True})
        qq_app.router.add_post('/api/music/qr/check', qq_qr_chk)
        qq_app.router.add_post('/api/music/logout', qq_out_ep)
        A.QQ_CFG_DIR = self.tmp / 'qqcfg'
        A.QQ_API = await self.fake(qq_app)

        r_qq_bad = await self.client.post('/admin/api/settings/extra', json={
            'action': 'qq_quality', 'quality_level': 'lossless', 'max_cache_mb': 200, 'wake': True, 'wake_time': '03:15',
        }, headers=self.hdr())
        self.assertEqual(r_qq_bad.status, 400)

        r_qq_ok = await self.client.post('/admin/api/settings/extra', json={
            'action': 'qq_quality', 'quality_level': 'lossless', 'radio_audio_mode': 'voice',
            'max_cache_mb': 250, 'wake': True, 'wake_time': '06:45',
        }, headers=self.hdr())
        self.assertEqual(r_qq_ok.status, 200, await r_qq_ok.text())
        jqq = (await r_qq_ok.json())['extra']['qqmusic']
        self.assertEqual(jqq['radio_audio_mode'], 'voice')
        self.assertEqual(jqq['wake_time'], '06:45')

        r_qrc = await self.client.post('/admin/api/settings/extra', json={'action': 'qq_qr_create', 'mode': 'qq'}, headers=self.hdr())
        self.assertEqual(r_qrc.status, 200)
        self.assertEqual((await r_qrc.json())['qrsig'], 'sig123')
        r_qrk = await self.client.post('/admin/api/settings/extra', json={'action': 'qq_qr_check', 'qrsig': 'sig123', 'mode': 'qq'}, headers=self.hdr())
        self.assertEqual(r_qrk.status, 200)
        jqrk = await r_qrk.json()
        self.assertTrue(jqrk['done'])
        self.assertTrue(jqrk['extra']['qqmusic']['logged_in'])
        r_qout = await self.client.post('/admin/api/settings/extra', json={'action': 'qq_logout'}, headers=self.hdr())
        self.assertEqual(r_qout.status, 200)
        self.assertFalse((await r_qout.json())['extra']['qqmusic']['logged_in'])

        cli_r75 = web.Application()
        async def cli_events_r75(req):
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
            await sr.prepare(req)
            payload = {
                'volume': 62, 'mic': False,
                'sleep': {'enabled': True, 'start': '22:00', 'end': '07:00', 'active': True},
                'reports': 2,
                'alarms': {'items': [], 'missed': [], 'ringing': {'id': 1, 'label': '起床'}},
            }
            await sr.write(('event: status\ndata: ' + json.dumps(payload) + '\n\n').encode('utf-8'))
            await sr.write_eof()
            return sr
        async def cli_act_r75(req):
            dv_calls.append(req.path)
            return web.json_response({'ok': True})
        cli_r75.router.add_get('/events', cli_events_r75)
        cli_r75.router.add_post('/api/alarms/snooze', cli_act_r75)
        cli_r75.router.add_post('/api/reports/play', cli_act_r75)
        A.CLIENT_API = await self.fake(cli_r75)

        jdv = (await (await self.client.get('/admin/api/settings')).json())['device']
        self.assertIs(jdv['mic_on'], False)
        self.assertTrue(jdv['sleep_active'])
        self.assertEqual(jdv['reports_count'], 2)
        self.assertEqual(jdv['hub_version']['checks'][0]['name'], 'ffmpeg')

        r_snz = await self.client.post('/admin/api/settings/device', json={'action': 'alarm_ctl', 'snooze': True}, headers=self.hdr())
        self.assertEqual(r_snz.status, 200)
        r_rep = await self.client.post('/admin/api/settings/device', json={'action': 'play_reports'}, headers=self.hdr())
        self.assertEqual(r_rep.status, 200)
        self.assertEqual(dv_calls, ['/api/alarms/snooze', '/api/reports/play'])

        # 5) Clash runtime toggles allowlist & strict security blocks
        for ok_patch in ({'ipv6': True}, {'tcp-concurrent': True}, {'unified-delay': True}, {'sniffing': False}):
            self.assertTrue(A.clash_body_ok('PATCH', 'configs', json.dumps(ok_patch).encode()), ok_patch)
        for bad_patch in ({'ipv6': 'true'}, {'allow-lan': True}, {'bind-address': '0.0.0.0'}, {'external-controller': '0.0.0.0:9090'}):
            self.assertFalse(A.clash_body_ok('PATCH', 'configs', json.dumps(bad_patch).encode()), bad_patch)

    async def test_r76_round7_grokbot_voice_tuning_study_focus_and_qq_bind(self):
        """R76 第七轮审查回归：呼名语音试录 schema 修复与 GrokBot_* 保真、Grok Bot PKCE/多机器人管理、
        EdgeTTS/火山/Sherpa双模型/豆包多语种/KWS路径微调、中新网RSS、立即检测+番茄专注、QQ号/Cookie绑定与零扬声器铃声试听。"""
        await self.login()

        # Prepare both local Sherpa model dirs + one Ogg ringtone file
        (self.tmp / 'xz-hub/models/sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03').mkdir(parents=True, exist_ok=True)
        (self.tmp / 'xz-hub/models/SenseVoiceSmall').mkdir(parents=True, exist_ok=True)
        (A.CLIENT_SOUNDS_DIR / 'alarm-coffee.ogg').write_bytes(b'OggS\x00\x02fake-coffee-ogg-stream')

        # 1) Fake xiaozhi-hub diag endpoint returning real Learner.status() + GrokBotAdmin.status() schemas
        hub_acts = []
        grok_state = {
            'account': {'logged_in': False, 'needs_login': False, 'expires_at': 0},
            'login': {'pending': False, 'url': ''},
            'entries': [],
            'bots': None,
            'bots_error': '',
        }
        hub_r76 = web.Application()

        async def hub_diag_get_r76(req):
            return web.json_response({
                'ok': True,
                'sessions': 0,
                'call_learning': {
                    'active': True,
                    'target': 'GrokBot_测试助手',
                    'samples': [{'text': '切换测试助手', 'alias': '测试助手'}],
                    'count': 3,
                    'seconds_left': 74,
                },
                'grokbot': dict(grok_state),
            })

        async def hub_diag_post_r76(req):
            body = await req.json()
            act = body.get('action')
            hub_acts.append(body)
            if act == 'learn_start':
                return web.json_response({
                    'ok': True,
                    'call_learning': {
                        'active': True,
                        'target': body.get('target'),
                        'samples': [{'text': '叫豆包来', 'alias': '豆包'}],
                        'count': 3,
                        'seconds_left': 88,
                    },
                })
            if act == 'grokbot_login':
                grok_state['login'] = {'pending': True, 'url': 'https://accounts.x.ai/authorize?client_id=test'}
                return web.json_response({
                    'ok': True,
                    'url': 'https://accounts.x.ai/authorize?client_id=test',
                    'note': '已生成 Grok 登录链接',
                    'grokbot': dict(grok_state),
                })
            if act == 'grokbot_refresh':
                grok_state.update({
                    'account': {'logged_in': True, 'needs_login': False, 'expires_at': 9999999999},
                    'login': {'pending': False, 'url': ''},
                    'bots': [
                        {'id': 'bot-1', 'name': '测试助手'},
                        {'id': 'bot-2', 'name': '英语外教'},
                    ],
                })
                return web.json_response({'ok': True, 'note': '已同步 2 个 Grok 机器人', 'grokbot': dict(grok_state)})
            if act == 'grokbot_save_bots':
                raw_bots = body.get('bots') or []
                grok_state['entries'] = [
                    {'entry': f"GrokBot_{b['name']}", 'bot_id': b['id'], 'bot_name': b['name']}
                    for b in raw_bots if isinstance(b, dict) and b.get('id')
                ]
                return web.json_response({'ok': True, 'note': 'Grok Bot 勾选已保存并生效', 'grokbot': dict(grok_state)})
            if act == 'grokbot_logout':
                grok_state.update({
                    'account': {'logged_in': False, 'needs_login': False, 'expires_at': 0},
                    'login': {'pending': False, 'url': ''},
                    'entries': [],
                    'bots': None,
                })
                return web.json_response({'ok': True, 'note': '已退出 Grok 账号', 'grokbot': dict(grok_state)})
            return web.json_response({'ok': True})

        async def hub_reload_r76(req):
            return web.json_response({'ok': True, 'pushed': 0})

        hub_r76.router.add_get('/kiosk/internal/diag', hub_diag_get_r76)
        hub_r76.router.add_post('/kiosk/internal/diag', hub_diag_post_r76)
        hub_r76.router.add_post('/kiosk/internal/reload-config', hub_reload_r76)
        hub_srv = TestServer(hub_r76)
        await hub_srv.start_server()
        self.fakes.append(hub_srv)
        A.HUB_API = f'http://127.0.0.1:{hub_srv.port}'

        # Seed YAML with GrokBot_测试助手 in LLM & call_names
        dot_y = self.tmp / 'xz-hub/data/.config.yaml'
        dot_y.write_text(
            f'server:\n  http_port: {hub_srv.port}\n'
            'LLM:\n  GrokBot_测试助手:\n    type: grok_bot\n    bot_id: bot-1\n    bot_name: 测试助手\n'
            'call_names:\n  enabled: true\n  models:\n    GrokBot_测试助手:\n      - 测试助手\n',
            encoding='utf-8',
        )

        # Verify GET /admin/api/settings normalizes call_names.learning (both samples & got, seconds_left & remaining) and preserves GrokBot_*
        st0 = await (await self.client.get('/admin/api/settings')).json()
        cl0 = st0['llm']['call_names']['learning']
        self.assertTrue(cl0['active'])
        self.assertEqual(cl0['target'], 'GrokBot_测试助手')
        self.assertEqual(cl0['samples'][0]['alias'], '测试助手')
        self.assertEqual(cl0['got'][0]['alias'], '测试助手')
        self.assertEqual(cl0['seconds_left'], 74)
        self.assertEqual(cl0['remaining'], 74)
        self.assertEqual(cl0['left'], 2)
        self.assertEqual(st0['llm']['call_names']['models']['GrokBot_测试助手'], ['测试助手'])

        # Verify call_learn_start accepts GrokBot_* target and normalizes response
        r_cls = await self.client.post('/admin/api/settings/llm', json={'action': 'call_learn_start', 'target': 'GrokBot_测试助手'}, headers=self.hdr())
        self.assertEqual(r_cls.status, 200, await r_cls.text())
        j_cls = await r_cls.json()
        self.assertEqual(j_cls['call_learning']['target'], 'GrokBot_测试助手')
        self.assertEqual(j_cls['call_learning']['samples'][0]['alias'], '豆包')
        self.assertEqual(j_cls['call_learning']['got'][0]['alias'], '豆包')
        self.assertEqual(j_cls['call_learning']['seconds_left'], 88)

        # Verify GrokBot login -> refresh -> save_bots -> logout
        r_gl = await self.client.post('/admin/api/settings/llm', json={'action': 'grokbot_login'}, headers=self.hdr())
        self.assertEqual(r_gl.status, 200)
        j_gl = await r_gl.json()
        self.assertIn('accounts.x.ai', j_gl['url'])
        self.assertTrue(j_gl['grokbot']['login']['pending'])

        r_gr = await self.client.post('/admin/api/settings/llm', json={'action': 'grokbot_refresh'}, headers=self.hdr())
        self.assertEqual(r_gr.status, 200)
        j_gr = await r_gr.json()
        self.assertTrue(j_gr['grokbot']['account']['logged_in'])
        self.assertEqual(len(j_gr['grokbot']['bots']), 2)

        r_gs = await self.client.post('/admin/api/settings/llm', json={
            'action': 'grokbot_save_bots',
            'bots': [{'id': 'bot-1', 'name': '测试助手'}],
        }, headers=self.hdr())
        self.assertEqual(r_gs.status, 200)
        j_gs = await r_gs.json()
        self.assertEqual(j_gs['grokbot']['entries'][0]['entry'], 'GrokBot_测试助手')

        r_go = await self.client.post('/admin/api/settings/llm', json={'action': 'grokbot_logout'}, headers=self.hdr())
        self.assertEqual(r_go.status, 200)
        self.assertFalse((await r_go.json())['grokbot']['account']['logged_in'])

        # Also verify saving LLM with chat_history.summary_llm = GrokBot_测试助手 and GrokBot_* in call_names preserves both
        r_llm = await self.client.post('/admin/api/settings/llm', json={
            'vendor': 'deepseek',
            'base_url': 'https://api.deepseek.com',
            'model': 'deepseek-chat',
            'api_key': 'sk-ds-r76-test-123456',
            'chat_history': {'enabled': True, 'summary': True, 'summary_llm': 'GrokBot_测试助手'},
            'call_names': {
                'enabled': True,
                'models': {'GrokBot_测试助手': ['测试助手', '小测'], 'DeepSeekLLM': ['深度求索']},
            },
        }, headers=self.hdr())
        self.assertEqual(r_llm.status, 200, await r_llm.text())
        j_llm = await r_llm.json()
        self.assertEqual(j_llm['llm']['chat_history']['summary_llm'], 'GrokBot_测试助手')
        self.assertEqual(j_llm['llm']['call_names']['models']['GrokBot_测试助手'], ['测试助手', '小测'])

        # 2) Voice fine-tuning: EdgeTTS rate/pitch, Huoshan speech_rate/loudness_rate, Sherpa SenseVoiceSmall + segment_pause_ms, Doubao multilingual, kws_paths
        shp_models0 = st0['voice']['asr']['sherpa']['models']
        self.assertEqual(len(shp_models0), 2)
        self.assertTrue(all(m['installed'] for m in shp_models0))

        r_vc = await self.client.post('/admin/api/settings/voice', json={
            'tts': {
                'selected': 'EdgeTTS',
                'edge': {'voice': 'zh-CN-XiaoxiaoNeural', 'rate': 15, 'pitch': -8},
                'huoshan': {'appid': 'hs-app-1', 'access_token': 'hs-tok-123456', 'speech_rate': 20, 'loudness_rate': 10},
            },
            'asr': {
                'selected': 'SherpaXASR',
                'sherpa': {'model_dir': 'models/SenseVoiceSmall', 'num_threads': 3, 'segment_pause_ms': 450},
                'doubao': {'auth_mode': 'api_key', 'api_key': 'db-ak-123456', 'enable_multilingual': True},
            },
            'wakeup': {
                'kws_threshold': 0.25,
                'kws_gain_db': 10,
                'follow_up_seconds': 10,
                'kws_paths': 12,
            },
        }, headers=self.hdr())
        self.assertEqual(r_vc.status, 200, await r_vc.text())
        j_vc = (await r_vc.json())['voice']
        self.assertEqual(j_vc['tts']['edge']['rate'], 15)
        self.assertEqual(j_vc['tts']['edge']['pitch'], -8)
        self.assertEqual(j_vc['tts']['huoshan']['speech_rate'], 20)
        self.assertEqual(j_vc['tts']['huoshan']['loudness_rate'], 10)
        self.assertEqual(j_vc['asr']['sherpa']['model_dir'], 'models/SenseVoiceSmall')
        self.assertEqual(j_vc['asr']['sherpa']['model_type'], 'sense_voice')
        self.assertEqual(j_vc['asr']['sherpa']['segment_pause_ms'], 450)
        self.assertTrue(j_vc['asr']['doubao']['enable_multilingual'])
        self.assertEqual(j_vc['wakeup']['kws_paths'], 12)

        # 3) APIs: ChinaNews category RSS URLs save & clear
        r_ap = await self.client.post('/admin/api/settings/apis', json={
            'city': '北京',
            'chinanews': {
                'default_rss_url': 'https://www.chinanews.com.cn/rss/scroll-news.xml',
                'finance_rss_url': 'https://www.chinanews.com.cn/rss/finance.xml',
            },
        }, headers=self.hdr())
        self.assertEqual(r_ap.status, 200, await r_ap.text())
        j_cn = (await r_ap.json())['apis']['chinanews']
        self.assertEqual(j_cn['default_rss_url'], 'https://www.chinanews.com.cn/rss/scroll-news.xml')
        self.assertEqual(j_cn['finance_rss_url'], 'https://www.chinanews.com.cn/rss/finance.xml')

        r_ap_clr = await self.client.post('/admin/api/settings/apis', json={
            'city': '北京',
            'chinanews': {'default_rss_url': '', 'finance_rss_url': ''},
        }, headers=self.hdr())
        self.assertEqual(r_ap_clr.status, 200)
        j_cn2 = (await r_ap_clr.json())['apis']['chinanews']
        self.assertEqual(j_cn2['default_rss_url'], '')
        self.assertEqual(j_cn2['finance_rss_url'], '')

        # 4) Study: check_now (speak=False) + focus_start / focus_stop + GET /api/study/focus probe
        study_calls = []
        focus_state = {'active': False, 'phase': 'idle', 'remaining': 0, 'minutes': 25, 'break_minutes': 5}
        st_app = web.Application()

        async def st_status(req):
            return web.json_response({'enabled': True, 'state': 'good'})

        async def st_focus_get(req):
            return web.json_response(focus_state)

        async def st_focus_post(req):
            b = await req.json()
            study_calls.append(('focus', b))
            if b.get('action') == 'start':
                focus_state.update({
                    'active': True,
                    'phase': 'focus',
                    'remaining': int(b.get('minutes', 25)) * 60,
                    'minutes': int(b.get('minutes', 25)),
                    'break_minutes': int(b.get('break_minutes', 5)),
                })
            else:
                focus_state.update({'active': False, 'phase': 'idle', 'remaining': 0})
            return web.json_response({'ok': True, **focus_state})

        async def st_check_now(req):
            b = await req.json()
            study_calls.append(('check_now', b))
            return web.json_response({'ok': True, 'status': 'good', 'summary': '坐姿端正'})

        st_app.router.add_get('/api/study/status', st_status)
        st_app.router.add_get('/api/study/focus', st_focus_get)
        st_app.router.add_post('/api/study/focus', st_focus_post)
        st_app.router.add_post('/api/study/check-now', st_check_now)
        A.STUDY_API = await self.fake(st_app)

        r_chk = await self.client.post('/admin/api/settings/study', json={'action': 'check_now', 'speak': False}, headers=self.hdr())
        self.assertEqual(r_chk.status, 200, await r_chk.text())
        j_chk = await r_chk.json()
        self.assertIn('坐姿端正', j_chk['note'])
        self.assertEqual(study_calls[-1], ('check_now', {'speak': False}))

        r_fst = await self.client.post('/admin/api/settings/study', json={'action': 'focus_start', 'minutes': 30, 'break_minutes': 5}, headers=self.hdr())
        self.assertEqual(r_fst.status, 200, await r_fst.text())
        j_fst = await r_fst.json()
        self.assertTrue(j_fst['study']['focus']['active'])
        self.assertEqual(j_fst['study']['focus']['minutes'], 30)

        r_fsp = await self.client.post('/admin/api/settings/study', json={'action': 'focus_stop'}, headers=self.hdr())
        self.assertEqual(r_fsp.status, 200)
        self.assertFalse((await r_fsp.json())['study']['focus']['active'])

        # 5) Extra: qq_bind forwards input to /api/music/bind and never leaks raw cookie/input in response
        qq_binds = []
        qq_app = web.Application()

        async def qq_bind_post(req):
            b = await req.json()
            qq_binds.append(b)
            return web.json_response({'ok': True, 'profile': {'nickname': '音乐VIP号', 'isVip': True}})

        async def qq_status_get(req):
            return web.json_response({
                'logged_in': bool(qq_binds),
                'user_profile': {'nickname': '音乐VIP号', 'isVip': True} if qq_binds else None,
            })

        qq_app.router.add_post('/api/music/bind', qq_bind_post)
        qq_app.router.add_get('/api/music/status', qq_status_get)
        A.QQ_API = await self.fake(qq_app)

        secret_cookie = 'uin=12345678; qm_keyst=SUPER_SECRET_QM_KEY_999'
        r_qb = await self.client.post('/admin/api/settings/extra', json={'action': 'qq_bind', 'input': secret_cookie}, headers=self.hdr())
        self.assertEqual(r_qb.status, 200, await r_qb.text())
        raw_qb_txt = await r_qb.text()
        self.assertNotIn('SUPER_SECRET_QM_KEY_999', raw_qb_txt)
        j_qb = json.loads(raw_qb_txt)
        self.assertIn('音乐VIP号', j_qb['note'])
        self.assertEqual(qq_binds, [{'input': secret_cookie}])
        self.assertTrue(j_qb['extra']['qqmusic']['logged_in'])

        # 6) Zero-speaker alarm ringtone preview: Ogg file ('coffee') & synthesized marimba WAV ('marimba')
        r_tc = await self.client.post('/admin/api/settings/test', json={'kind': 'alarm_tone_preview', 'tone': 'coffee'}, headers=self.hdr())
        self.assertEqual(r_tc.status, 200, await r_tc.text())
        j_tc = await r_tc.json()
        self.assertEqual(j_tc['mime'], 'audio/ogg')
        self.assertTrue(base64.b64decode(j_tc['audio_b64']).startswith(b'OggS'))

        r_tm = await self.client.post('/admin/api/settings/test', json={'kind': 'alarm_tone_preview', 'tone': 'marimba'}, headers=self.hdr())
        self.assertEqual(r_tm.status, 200, await r_tm.text())
        j_tm = await r_tm.json()
        self.assertEqual(j_tm['mime'], 'audio/wav')
        wav_raw = base64.b64decode(j_tm['audio_b64'])
        self.assertTrue(wav_raw.startswith(b'RIFF'))
        self.assertGreater(len(wav_raw), 40000)

    async def test_r77_round8_shutter_sleep_session_voice_tuning_and_clash_geo(self):
        """R77 第八轮审查回归：物理镜头遮挡盖+AirPlay投屏+对讲看护+桌面计时器+远程打断对话、
        番茄钟 break_minutes=0 保真、EdgeTTS软限幅volume("+0")+Sherpa热词权重+说完提示音+AI动态告别语、
        睡前助眠白噪音会话远程控制与 Clash 进程匹配模式/Geo 更新白名单。"""
        await self.login()

        # 1) HTML DOM & bug-fix markers check
        html = await (await self.client.get('/admin/')).text()
        for dom_id in (
            'ttsEdgeVolume', 'asrSherpaHwScore', 'sessStopNotifyEn', 'sessEndPromptAi',
            'studyShutterWarn', 'devShutterWarnNote', 'devDeskTimerBar', 'btnStopDeskTimer',
            'btnAbortVoice', 'qqSleepBox', 'qqSleepSoundSel', 'qqSleepVolInp',
            'btnStartQqSleep', 'btnStopQqSleep', 'updClashGeo', 'clashFindProc',
        ):
            self.assertIn(f'id="{dom_id}"', html, dom_id)
        self.assertIn('支持 10 家云端/兼容网关', html)
        self.assertIn('setCfgDirty(cfgGroupOf(clr), true)', html)
        self.assertIn('box.dataset.pwState', html)

        # 2) Fake intercom, home timer, study focus (break_minutes=0!), and client voice_state='speaking'
        ic_app = web.Application()
        async def ic_status(req):
            self.assertEqual(req.headers.get('X-Intercom-Msg'), '1')
            return web.json_response({
                'ok': True, 'in_call': True, 'viewers': 2, 'listeners': 1,
                'shutter_closed': True, 'hw_mic_muted': False, 'mirroring': True,
            })
        ic_app.router.add_get('/api/intercom/status', ic_status)
        A.INTERCOM_API = await self.fake(ic_app)

        home_calls = []
        hm_app = web.Application()
        desk_state = {'phase': 'running', 'mode': 'countdown', 'label': '泡茶', 'remaining': 180}
        async def hm_timer(req):
            if req.method == 'GET':
                return web.json_response({'ok': True, 'timer': desk_state if desk_state['phase'] != 'idle' else None})
            b = await req.json()
            home_calls.append((req.headers.get('Host'), b))
            if b.get('action') == 'stop':
                desk_state['phase'] = 'idle'
                return web.json_response({'ok': True})
            return web.json_response({'ok': False}, status=400)
        hm_app.router.add_get('/api/home/timer', hm_timer)
        hm_app.router.add_post('/api/home/timer', hm_timer)
        A.HOME_API = await self.fake(hm_app)

        st_app = web.Application()
        st_app.router.add_get('/api/study/focus', lambda _r: web.json_response({
            'active': True, 'phase': 'focus', 'remaining': 600, 'minutes': 25, 'break_minutes': 0,
        }))
        A.STUDY_API = await self.fake(st_app)

        cli_calls = []
        cli_app = web.Application()
        async def cli_events(req):
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
            await sr.prepare(req)
            payload = {'status': {'state': 'speaking', 'mic': True, 'volume': 55}}
            await sr.write(('event: snapshot\ndata: ' + json.dumps(payload) + '\n\n').encode('utf-8'))
            await sr.write_eof()
            return sr
        async def cli_abort(_req):
            cli_calls.append('abort')
            return web.json_response({'ok': True})
        cli_app.router.add_get('/events', cli_events)
        cli_app.router.add_post('/api/abort', cli_abort)
        A.CLIENT_API = await self.fake(cli_app)

        st0 = await (await self.client.get('/admin/api/settings')).json()
        dv0 = st0['device']
        self.assertIs(dv0['shutter_closed'], True)
        self.assertTrue(dv0['airplay_mirroring'])
        self.assertTrue(dv0['intercom_in_call'])
        self.assertEqual(dv0['intercom_viewers'], 2)
        self.assertEqual(dv0['voice_state'], 'speaking')
        self.assertEqual(dv0['desk_timer']['label'], '泡茶')
        self.assertEqual(st0['study']['focus']['break_minutes'], 0)

        r_ab = await self.client.post('/admin/api/settings/device', json={'action': 'abort_voice'}, headers=self.hdr())
        self.assertEqual(r_ab.status, 200)
        self.assertEqual(cli_calls, ['abort'])

        r_dt = await self.client.post('/admin/api/settings/device', json={'action': 'stop_desk_timer'}, headers=self.hdr())
        self.assertEqual(r_dt.status, 200)
        self.assertEqual(home_calls, [('127.0.0.1:8095', {'action': 'stop'})])
        self.assertIsNone((await r_dt.json())['device']['desk_timer'])

        # 3) Voice R77 fields: EdgeTTS volume=0 ("+0"), Sherpa hotwords_score + output_dir, stop_tts_notify, end_prompt_prompt + empty reply
        r_vc = await self.client.post('/admin/api/settings/voice', json={
            'tts': {
                'selected': 'EdgeTTS',
                'edge': {'voice': 'zh-CN-XiaoxiaoNeural', 'volume': 0, 'rate': -5, 'pitch': 5},
            },
            'asr': {
                'selected': 'SherpaXASR',
                'sherpa': {'num_threads': 2, 'segment_pause_ms': 300, 'hotwords_score': 2.4},
            },
            'session': {
                'end_prompt_enabled': True,
                'end_prompt_reply': '',
                'end_prompt_prompt': '用温柔简短的一句话和家人道晚安后休息。',
                'stop_tts_notify': True,
            },
        }, headers=self.hdr())
        self.assertEqual(r_vc.status, 200, await r_vc.text())
        j_vc = (await r_vc.json())['voice']
        self.assertEqual(j_vc['tts']['edge']['volume'], 0)
        self.assertEqual(j_vc['asr']['sherpa']['hotwords_score'], 2.4)
        self.assertTrue(j_vc['session']['stop_tts_notify'])
        self.assertEqual(j_vc['session']['end_prompt_reply'], '')
        self.assertEqual(j_vc['session']['end_prompt_prompt'], '用温柔简短的一句话和家人道晚安后休息。')

        raw_dot_yaml = (self.tmp / 'xz-hub/data/.config.yaml').read_text(encoding='utf-8')
        self.assertIn('+0', raw_dot_yaml)
        self.assertIn('tmp/', raw_dot_yaml)

        # 4) QQ Music bedtime sleep session remote control
        sl_calls = []
        sl_active = {'session': None, 'sleep_volume': 25}
        qq_app = web.Application()
        qq_app.router.add_get('/api/music/status', lambda _r: web.json_response({'logged_in': False}))
        async def qq_sl(req):
            if req.method == 'GET':
                return web.json_response({
                    'ok': True, 'session': sl_active['session'], 'sleep_volume': sl_active['sleep_volume'],
                    'wake': True, 'wake_time': '06:30',
                    'recipes': [{'id': 'deep_sleep', 'name': '深度入眠'}],
                    'presets': [{'id': 'rain', 'name': '窗外细雨'}],
                })
            b = await req.json()
            sl_calls.append(b)
            if b.get('action') == 'start':
                sl_active['sleep_volume'] = int(b.get('volume', 25))
                sl_active['session'] = {
                    'label': '混响配方 · 深度入眠',
                    'night': bool(b.get('night')),
                    'volume': sl_active['sleep_volume'],
                    'remaining': None if b.get('night') else 2700,
                }
            elif b.get('action') == 'stop':
                sl_active['session'] = None
            return web.json_response({'ok': True, 'message': '助眠已响应'})
        qq_app.router.add_get('/api/music/sleep_session', qq_sl)
        qq_app.router.add_post('/api/music/sleep_session', qq_sl)
        A.QQ_API = await self.fake(qq_app)

        r_ss = await self.client.post('/admin/api/settings/extra', json={
            'action': 'qq_sleep', 'sleep_action': 'start', 'sound': 'deep_sleep', 'duration': 'night', 'volume': 20,
        }, headers=self.hdr())
        self.assertEqual(r_ss.status, 200, await r_ss.text())
        j_ss = (await r_ss.json())['extra']['qqmusic']
        self.assertEqual(j_ss['sleep_volume'], 20)
        self.assertTrue(j_ss['sleep_session']['night'])
        self.assertEqual(sl_calls[-1], {'action': 'start', 'sound': 'deep_sleep', 'volume': 20, 'night': True})

        for s_act in ('extend', 'screen_off', 'stop'):
            r_sa = await self.client.post('/admin/api/settings/extra', json={'action': 'qq_sleep', 'sleep_action': s_act}, headers=self.hdr())
            self.assertEqual(r_sa.status, 200)
        self.assertIsNone((await r_sa.json())['extra']['qqmusic']['sleep_session'])

        # 5) Clash find-process-mode & upgrade/geo allowlist + security blocks
        for fp_ok in ('strict', 'always', 'off'):
            self.assertTrue(A.clash_body_ok('PATCH', 'configs', json.dumps({'find-process-mode': fp_ok}).encode()))
        for fp_bad in ('invalid', True, 123):
            self.assertFalse(A.clash_body_ok('PATCH', 'configs', json.dumps({'find-process-mode': fp_bad}).encode()))
        self.assertIsNotNone(A.CLASH_ALLOW['POST'].fullmatch('upgrade/geo'))
        self.assertIsNone(A.CLASH_ALLOW['POST'].fullmatch('upgrade'))
        self.assertFalse(A.clash_body_ok('PATCH', 'configs', json.dumps({'allow-lan': True}).encode()))

    async def test_r78_round9_clear_secrets_tutor_drafts_weather_rss_hw_qqmode(self):
        # 1) HTML & JS wiring checks for Round 9 (R78)
        html = (await (await self.client.get('/admin/')).text())
        for marker in (
            'id="btnTestVoiceWeather"', 'id="btnTestChinanews"', 'id="resTestChinanews"',
            'id="qqPlayModeSel"', 'id="sysHwStats"', 'id="hwDiskFree"', 'id="hwMemAvail"',
            'id="hwCpuTemp"', 'id="hwUptime"', 'id="tutStatsSub"', 'data-ndelay=',
            'function getEffectiveVendorCfg(', 'function testSingleNode(',
            'clear_metaso_key:', 'clear_amap_key:', 'clear_secret:', 'vendor_drafts:',
            "'btnPullHubTts'",
        ):
            self.assertIn(marker, html)

        # 2) Tutor stats + hardware sys_stats in GET /admin/api/settings
        (self.tmp / 'tutor-state/history.json').write_text(
            json.dumps([{'id': 'h1', 'question': '1+1'}, {'id': 'h2', 'question': '2+2'}, {'bad': 1}]),
            encoding='utf-8',
        )
        (self.tmp / 'tutor-state/mistake_book.json').write_text(
            json.dumps([{'id': 'm1', 'question': '3x=9'}]),
            encoding='utf-8',
        )
        st0 = await (await self.client.get('/admin/api/settings')).json()
        self.assertEqual(st0['llm']['tutor']['history_count'], 2)
        self.assertEqual(st0['llm']['tutor']['mistake_count'], 1)
        sst = st0['device']['sys_stats']
        self.assertGreater(sst['disk_total'], 0)
        self.assertGreater(sst['mem_total'], 0)
        self.assertGreaterEqual(sst['uptime_sec'], 0)

        # 3) Save secrets onto disk, then verify clear_*=True in test_settings NEVER falls back to old disk secrets!
        r_s1 = await self.client.post('/admin/api/settings/llm', json={
            'vendor': 'deepseek', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat', 'api_key': 'sk-saved-ds-123456',
        }, headers=self.hdr())
        self.assertEqual(r_s1.status, 200, await r_s1.text())
        r_s2 = await self.client.post('/admin/api/settings/apis', json={
            'qweather': {'api_host': 'abc.re.qweatherapi.com', 'api_key': 'qw-saved-123456', 'city': '北京'},
            'web_search': {'enabled': True, 'base_url': 'https://api.deepseek.com/anthropic/v1', 'model': 'deepseek-v4-flash',
                           'api_key': 'ws-saved-123', 'metaso_key': 'mk-saved-123', 'amap_key': 'am-saved-123'},
            'home_assistant': {'enabled': True, 'base_url': 'http://192.168.1.60:8123', 'api_key': 'ha-saved-123'},
            'ragflow': {'enabled': True, 'base_url': 'http://192.168.1.11:9380', 'api_key': 'rf-saved-123', 'dataset_ids': 'ds_1'},
            'vllm': {'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'model': 'glm-4.6v', 'api_key': 'vl-saved-123'},
        }, headers=self.hdr())
        self.assertEqual(r_s2.status, 200, await r_s2.text())
        await self.client.post('/admin/api/settings/study', json={
            'vision_url': 'http://192.168.1.2:8100/study/v1', 'secret': 'a' * 64,
        }, headers=self.hdr())
        await self.client.post('/admin/api/settings/voice', json={
            'tts': {
                'selected': 'CosyVoiceSiliconflow',
                'siliconflow': {'model': 'FunAudioLLM/CosyVoice2-0.5B', 'voice': 'FunAudioLLM/CosyVoice2-0.5B:alex', 'access_token': 'sf-saved-123'},
                'openai_tts': {'api_url': 'https://api.openai.com/v1/audio/speech', 'model': 'tts-1', 'voice': 'alloy', 'api_key': 'oa-saved-123'},
            },
            'asr': {
                'selected': 'DoubaoStreamASRV2',
                'doubao': {'auth_mode': 'api_key', 'api_key': 'db-saved-123', 'resource_id': 'volc.seedasr.sauc.duration'},
            },
        }, headers=self.hdr())

        for payload, exp_err in (
            ({'kind': 'llm', 'vendor': 'deepseek', 'api_key': '', 'clear_api_key': True}, 'API Key'),
            ({'kind': 'qweather', 'api_key': '', 'clear_api_key': True}, 'API Key'),
            ({'kind': 'web_search', 'api_key': '', 'clear_api_key': True}, 'API Key'),
            ({'kind': 'metaso', 'metaso_key': '', 'clear_metaso_key': True}, 'Metaso Key'),
            ({'kind': 'amap', 'amap_key': '', 'clear_amap_key': True}, 'Key'),
            ({'kind': 'hass', 'api_key': '', 'clear_api_key': True}, '令牌'),
            ({'kind': 'ragflow', 'api_key': '', 'clear_api_key': True}, 'API Key'),
            ({'kind': 'vllm', 'api_key': '', 'clear_api_key': True}, 'API Key'),
            ({'kind': 'study_vision', 'secret': '', 'clear_secret': True}, '64 位'),
            ({'kind': 'tts_preview', 'selected': 'CosyVoiceSiliconflow', 'access_token': '', 'clear_access_token': True}, 'API Key'),
            ({'kind': 'tts_preview', 'selected': 'OpenAITTS', 'api_key': '', 'clear_api_key': True}, 'API Key'),
            ({'kind': 'asr', 'selected': 'DoubaoStreamASRV2', 'auth_mode': 'api_key', 'api_key': '', 'clear_api_key': True}, 'API Key'),
        ):
            rt = await self.client.post('/admin/api/settings/test', json=payload, headers=self.hdr())
            self.assertEqual(rt.status, 400, f'Expected 400 for {payload}, got {rt.status}: {await rt.text()}')
            self.assertIn(exp_err, (await rt.json())['error'])

        # 4) SherpaXASR test_settings checks exact unsaved model_dir without silently falling back to another dir
        r_sh_miss = await self.client.post('/admin/api/settings/test', json={
            'kind': 'asr', 'selected': 'SherpaXASR', 'model_dir': 'models/SenseVoiceSmall',
        }, headers=self.hdr())
        self.assertEqual(r_sh_miss.status, 404)
        r_sh_trav = await self.client.post('/admin/api/settings/test', json={
            'kind': 'asr', 'selected': 'SherpaXASR', 'model_dir': '../outside',
        }, headers=self.hdr())
        self.assertEqual(r_sh_trav.status, 400)
        (self.tmp / 'xz-hub/models/SenseVoiceSmall').mkdir(parents=True)
        r_sh_ok = await self.client.post('/admin/api/settings/test', json={
            'kind': 'asr', 'selected': 'SherpaXASR', 'model_dir': 'models/SenseVoiceSmall',
        }, headers=self.hdr())
        self.assertEqual(r_sh_ok.status, 200)
        self.assertIn('SenseVoiceSmall', (await r_sh_ok.json())['message'])

        # 5) tutor_llm test_settings overlays unsaved vendor_drafts (OpenAI gateway not yet saved on disk!)
        seen_tutor = []
        ext_app = web.Application()
        async def fake_chat(req):
            b = await req.json()
            seen_tutor.append((req.headers.get('Authorization'), b.get('model')))
            return web.json_response({'choices': [{'message': {'content': 'OK'}}]})
        async def fake_om_geo(req):
            nm = req.query.get('name', '')
            if nm.endswith('市'):
                return web.json_response({'results': []})
            return web.json_response({'results': [{'name': nm, 'admin1': '北京市', 'latitude': 39.90, 'longitude': 116.41}]})
        async def fake_om_fc(_req):
            return web.json_response({'current': {'temperature_2m': 14.5, 'relative_humidity_2m': 38, 'weather_code': 1, 'wind_speed_10m': 9.2}})
        async def fake_rss(req):
            if req.query.get('xxe') == '1':
                return web.Response(text='<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "bad">]><rss/>', content_type='application/xml')
            return web.Response(
                text='<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
                     '<item><title>科技创新推动高质量发展</title></item>'
                     '<item><title>第二条要闻</title></item></channel></rss>',
                content_type='application/rss+xml',
            )
        ext_app.router.add_post('/v1/chat/completions', fake_chat)
        ext_app.router.add_get('/geo', fake_om_geo)
        ext_app.router.add_get('/forecast', fake_om_fc)
        ext_app.router.add_get('/rss.xml', fake_rss)
        ext_base = await self.fake(ext_app)
        ext_port = int(ext_base.rsplit(':', 1)[1])
        saved_prot = set(A._PROTECTED_LOOPBACK_PORTS)
        A._PROTECTED_LOOPBACK_PORTS = saved_prot - {ext_port}
        try:
            A.OPEN_METEO_GEO_URL = ext_base + '/geo'
            A.OPEN_METEO_FORECAST_URL = ext_base + '/forecast'
            A.CHINANEWS_DEFAULT_RSS = ext_base + '/rss.xml'

            r_tut = await self.client.post('/admin/api/settings/test', json={
                'kind': 'tutor_llm',
                'fast_model': 'gpt-6.1-terra',
                'deep_model': 'gpt-6.1-sol',
                'backup_model': 'gpt-6-luna',
                'vendor_drafts': {
                    'openai': {'base_url': ext_base + '/v1', 'api_key': 'sk-draft-oa-999'},
                    'deepseek': {'clear_api_key': True},
                },
            }, headers=self.hdr())
            self.assertEqual(r_tut.status, 200, await r_tut.text())
            self.assertEqual(len(seen_tutor), 3)
            self.assertTrue(all(auth == 'Bearer sk-draft-oa-999' for auth, _ in seen_tutor))

            # 6) voice_weather (Open-Meteo, including stripping trailing '市' fallback) & chinanews RSS + XXE guard
            r_vw = await self.client.post('/admin/api/settings/test', json={
                'kind': 'voice_weather', 'city': '北京市',
            }, headers=self.hdr())
            self.assertEqual(r_vw.status, 200, await r_vw.text())
            vw_msg = (await r_vw.json())['message']
            self.assertIn('北京（北京市）', vw_msg)
            self.assertIn('14.5°C', vw_msg)

            r_cn = await self.client.post('/admin/api/settings/test', json={'kind': 'chinanews'}, headers=self.hdr())
            self.assertEqual(r_cn.status, 200, await r_cn.text())
            self.assertIn('科技创新推动高质量发展', (await r_cn.json())['message'])

            r_xxe = await self.client.post('/admin/api/settings/test', json={
                'kind': 'chinanews', 'rss_url': ext_base + '/rss.xml?xxe=1',
            }, headers=self.hdr())
            self.assertEqual(r_xxe.status, 400)
        finally:
            A._PROTECTED_LOOPBACK_PORTS = saved_prot

        # 7) QQ Music default play_mode ('shuffle' / 'single' / 'loop') round-trip
        qq_ctl_calls = []
        pm_state = {'mode': 'loop'}
        qq_app = web.Application()
        qq_app.router.add_get('/api/music/status', lambda _r: web.json_response({
            'quality_level': 'lossless', 'max_cache_mb': 200, 'radio_audio_mode': 'hifi',
            'play_mode': pm_state['mode'], 'user_profile': {'nickname': '测试号', 'isVip': True},
        }))
        qq_app.router.add_post('/api/music/quality', lambda _r: web.json_response({'ok': True}))
        qq_app.router.add_post('/api/music/radio/audio_mode', lambda _r: web.json_response({'ok': True}))
        qq_app.router.add_post('/api/music/sleep_session', lambda _r: web.json_response({'ok': True}))
        async def qq_ctl(req):
            b = await req.json()
            qq_ctl_calls.append(b)
            pm_state['mode'] = b['action']
            return web.json_response({'ok': True, 'play_mode': pm_state['mode']})
        qq_app.router.add_post('/api/music/control', qq_ctl)
        A.QQ_API = await self.fake(qq_app)

        r_pm = await self.client.post('/admin/api/settings/extra', json={
            'action': 'qq_quality', 'quality_level': 'lossless', 'play_mode': 'shuffle', 'radio_audio_mode': 'hifi', 'max_cache_mb': 200,
        }, headers=self.hdr())
        self.assertEqual(r_pm.status, 200, await r_pm.text())
        self.assertEqual((await r_pm.json())['extra']['qqmusic']['play_mode'], 'shuffle')
        self.assertEqual(qq_ctl_calls, [{'action': 'shuffle', 'source': 'admin'}])

        r_pm_bad = await self.client.post('/admin/api/settings/extra', json={
            'action': 'qq_quality', 'quality_level': 'lossless', 'play_mode': 'bad_mode',
        }, headers=self.hdr())
        self.assertEqual(r_pm_bad.status, 400)

    async def test_r79_round10_kws_paths_hub_updater_doubao_study_screen_wake(self):
        """R79 第十轮设置页回归：已存在 config.json 时 kws_paths 单独保存不丢、TTS 发包间隔与 WS 保活应答、
        豆包单 Key / AppID 互斥清理、小智中枢 updater 全状态检查与一键升级、学习陪伴今日统计与倒计时、息屏点亮背光及清历史网关兜底。"""
        await self.login()

        # 1) HTML 标记完整性检查
        html = await (await self.client.get('/admin/')).text()
        for marker in ('id="ttsSendDelayMs"', 'id="asrDbKeyRow"', 'id="sessWsPingEn"',
                       'id="studyTodayNote"', 'id="btnWakeScreen"', 'id="btnInstallHubUpd"'):
            self.assertIn(marker, html)

        # 2) CLIENT_CFG 已存在时，单独修改 kws_paths 必须触发落盘并返回重启提示；同时验证 tts.send_delay_ms 与 session.websocket_ping
        A.CLIENT_CFG.parent.mkdir(parents=True, exist_ok=True)
        A.CLIENT_CFG.write_text(json.dumps({
            'wake_words': ['x_iǎo_m_ài_x_iǎo_m_ài@小麦小麦'],
            'kws_threshold': 0.25,
            'kws_score': 2.0,
            'kws_paths': 8,
            'kws_gain_db': 10,
            'follow_up_seconds': 10,
            'wake_verify': '',
        }, ensure_ascii=False), encoding='utf-8')

        r_wk = await self.client.post('/admin/api/settings/voice', json={
            'tts': {'selected': 'EdgeTTS', 'prebuffer_count': 25, 'send_delay_ms': 20, 'timeout': 15},
            'asr': {
                'selected': 'SherpaXASR',
                'doubao': {'auth_mode': 'api_key', 'api_key': 'db-single-key-1', 'resource_id': 'volc.seedasr.sauc.duration'},
            },
            'session': {'websocket_ping': True},
            'wakeup': {
                'wake_words': ['x_iǎo_m_ài_x_iǎo_m_ài@小麦小麦'],
                'kws_threshold': 0.25,
                'kws_score': 2.0,
                'kws_paths': 14,
                'kws_gain_db': 10,
                'follow_up_seconds': 10,
                'wake_verify': '',
            },
        }, headers=self.hdr())
        self.assertEqual(r_wk.status, 200, await r_wk.text())
        j_wk = await r_wk.json()
        self.assertIn('重启设备后生效', j_wk['note'])
        self.assertEqual(j_wk['voice']['wakeup']['kws_paths'], 14)
        self.assertEqual(json.loads(A.CLIENT_CFG.read_text(encoding='utf-8'))['kws_paths'], 14)
        self.assertEqual(j_wk['voice']['tts']['send_delay_ms'], 20)
        self.assertTrue(j_wk['voice']['session']['websocket_ping'])
        self.assertEqual(j_wk['voice']['asr']['doubao']['auth_mode'], 'api_key')
        self.assertTrue(j_wk['voice']['asr']['doubao']['key_set'])

        # 3) 豆包 ASR 在非激活状态下从单 API Key 切回传统 AppID + Token 时，必须自动清空 api_key 防止覆盖鉴权头
        r_db = await self.client.post('/admin/api/settings/voice', json={
            'tts': {'selected': 'EdgeTTS'},
            'asr': {
                'selected': 'SherpaXASR',
                'doubao': {
                    'auth_mode': 'appid',
                    'appid': '88899900',
                    'access_token': 'db-tok-xyz',
                    'resource_id': 'volc.seedasr.sauc.duration',
                },
            },
        }, headers=self.hdr())
        self.assertEqual(r_db.status, 200, await r_db.text())
        j_db = await r_db.json()
        self.assertEqual(j_db['voice']['asr']['doubao']['auth_mode'], 'appid')
        self.assertFalse(j_db['voice']['asr']['doubao']['key_set'])
        self.assertTrue(j_db['voice']['asr']['doubao']['token_set'])
        merged_after, _ = A.load_hub_configs()
        self.assertEqual(merged_after['ASR']['DoubaoStreamASRV2']['api_key'], '')

        # 4) 小智中枢 updater 真实状态返回（latest / available / dirty / error）、一键升级（200 / 409）与清历史网关兜底
        hub_chk_mode = {'mode': 'latest'}
        hub_upd_busy = {'busy': False}
        chat_clear_calls = []
        hub_app = web.Application()

        async def h_diag_get(_req):
            return web.json_response({
                'ok': True,
                'asr_recent': [],
                'call_learning': {'active': False},
                'hub_version': {'short': '9a1b2c3', 'commit': '9a1b2c3000', 'subject': 'init hub'},
                'hub_check': {'state': 'available', 'behind': 2, 'current': {'short': '9a1b2c3', 'commit': '9a1b2c3000'}, 'latest': {'short': 'fe45dc6', 'subject': 'speedup tts'}},
                'hub_update': {'running': False, 'step': 'idle', 'error': None},
                'hub_checks': [{'name': 'FFmpeg', 'ok': True, 'detail': 'ready'}],
                'active_sessions': 1,
            })

        async def h_diag_post(req):
            b = await req.json()
            act = b.get('action')
            if act == 'hub_update_check':
                m = hub_chk_mode['mode']
                if m == 'latest':
                    return web.json_response({'ok': True, 'hub_check': {'state': 'latest', 'message': '已是最新', 'behind': 0, 'current': {'short': '9a1b2c3'}}})
                if m == 'available':
                    return web.json_response({'ok': True, 'hub_check': {'state': 'available', 'behind': 3, 'current': {'short': '9a1b2c3'}, 'latest': {'short': 'fe45dc6', 'subject': 'fix audio'}}})
                if m == 'dirty':
                    return web.json_response({'ok': True, 'hub_check': {'state': 'dirty', 'message': '目录里有本地修改，不能自动更新', 'current': {'short': '9a1b2c3'}}})
                return web.json_response({'ok': True, 'hub_check': {'state': 'error', 'message': '连不上远端仓库'}})
            if act == 'hub_update_start':
                if hub_upd_busy['busy']:
                    return web.json_response({'ok': False, 'error': '当前有 1 路设备正在对话，等说完再试'}, status=409)
                return web.json_response({'ok': True, 'hub_update': {'running': True, 'step': 'pull'}})
            return web.json_response({'ok': True})

        async def h_chat_clear(req):
            b = await req.json()
            chat_clear_calls.append(b)
            return web.json_response({'ok': True, 'deleted': 5, 'agents': []})

        hub_app.router.add_get('/kiosk/internal/diag', h_diag_get)
        hub_app.router.add_post('/kiosk/internal/diag', h_diag_post)
        hub_app.router.add_post('/kiosk/internal/reload', lambda _r: web.json_response({'ok': True, 'results': {}}))
        hub_app.router.add_get('/kiosk/internal/memory', lambda _r: web.json_response({'ok': True, 'devices': []}))
        hub_app.router.add_post('/kiosk/chat/clear', h_chat_clear)
        A.HUB_API = await self.fake(hub_app)

        rc_lat = await self.client.post('/admin/api/settings/device', json={'action': 'hub_update_check'}, headers=self.hdr())
        self.assertEqual(rc_lat.status, 200)
        self.assertIn('已是最新提交（9a1b2c3）', (await rc_lat.json())['note'])

        hub_chk_mode['mode'] = 'available'
        rc_av = await self.client.post('/admin/api/settings/device', json={'action': 'hub_update_check'}, headers=self.hdr())
        self.assertEqual(rc_av.status, 200)
        av_json = await rc_av.json()
        self.assertIn('3 个新提交', av_json['note'])
        self.assertIn('fe45dc6', av_json['note'])
        self.assertEqual(av_json['device']['hub_version']['active_sessions'], 1)

        hub_chk_mode['mode'] = 'dirty'
        rc_dt = await self.client.post('/admin/api/settings/device', json={'action': 'hub_update_check'}, headers=self.hdr())
        self.assertEqual(rc_dt.status, 200)
        self.assertIn('本地修改', (await rc_dt.json())['note'])

        hub_chk_mode['mode'] = 'error'
        rc_er = await self.client.post('/admin/api/settings/device', json={'action': 'hub_update_check'}, headers=self.hdr())
        self.assertEqual(rc_er.status, 502)
        self.assertIn('连不上远端仓库', (await rc_er.json())['error'])

        hub_upd_busy['busy'] = True
        ru_409 = await self.client.post('/admin/api/settings/device', json={'action': 'hub_update_start'}, headers=self.hdr())
        self.assertEqual(ru_409.status, 409)
        hub_upd_busy['busy'] = False
        ru_200 = await self.client.post('/admin/api/settings/device', json={'action': 'hub_update_start'}, headers=self.hdr())
        self.assertEqual(ru_200.status, 200, await ru_200.text())

        # CLIENT_API 离线时 clear_history 自动走 HUB_API /kiosk/chat/clear 兜底
        r_clr_fb = await self.client.post('/admin/api/settings/device', json={
            'action': 'clear_history', 'memory_only': True, 'device': 'aa:bb:cc:dd:ee:02',
        }, headers=self.hdr())
        self.assertEqual(r_clr_fb.status, 200, await r_clr_fb.text())
        self.assertEqual(chat_clear_calls, [{'device': 'aa:bb:cc:dd:ee:02', 'memory_only': True}])

        # 5) 学习陪伴今日统计与自动关闭倒计时 + 屏幕息屏状态探测与静默点亮背光
        disp_state = {'screen_on': False, 'dimmed': True, 'level': 65, 'preview_calls': []}
        mon_app = web.Application()
        mon_app.router.add_get('/api/display', lambda _r: web.json_response({
            'auto': True, 'level': disp_state['level'], 'lux': 42.0, 'sensor_ok': True,
            'screen_on': disp_state['screen_on'], 'dimmed': disp_state['dimmed'],
        }))
        async def mon_preview(req):
            if req.headers.get('Origin') != A.MONITOR_API:
                return web.json_response({'error': 'bad origin'}, status=403)
            b = await req.json()
            disp_state['preview_calls'].append(b)
            disp_state['screen_on'] = True
            disp_state['dimmed'] = False
            disp_state['level'] = b['level']
            return web.json_response({'ok': True})
        mon_app.router.add_post('/api/display/preview', mon_preview)
        A.MONITOR_API = await self.fake(mon_app)

        now_ts = int(time.time())
        st_app = web.Application()
        st_app.router.add_get('/api/study', lambda _r: web.json_response({
            'enabled': True,
            'auto_off_at': now_ts + 5400,
            'last': {'ts': now_ts - 30, 'present': True, 'posture': 'slouch', 'issue': '趴桌太低', 'spoken': True},
            'today': {'checks': 12, 'present': 10, 'reminders': [{'ts': 1}, {'ts': 2}], 'focus_sessions': 2, 'focus_minutes': 50},
        }))
        st_app.router.add_get('/api/study/focus', lambda _r: web.json_response({'active': False, 'minutes': 25, 'break_minutes': 5}))
        A.STUDY_API = await self.fake(st_app)

        j_full = await (await self.client.get('/admin/api/settings')).json()
        self.assertTrue(j_full['study']['enabled'])
        self.assertEqual(j_full['study']['auto_off_at'], now_ts + 5400)
        self.assertEqual(j_full['study']['today']['checks'], 12)
        self.assertEqual(j_full['study']['today']['reminders_count'], 2)
        self.assertEqual(j_full['study']['today']['focus_minutes'], 50)
        self.assertEqual(j_full['study']['last']['issue'], '趴桌太低')
        self.assertIs(j_full['device']['screen_on'], False)
        self.assertIs(j_full['device']['dimmed'], True)

        r_wake = await self.client.post('/admin/api/settings/device', json={'action': 'wake_screen', 'level': 75}, headers=self.hdr())
        self.assertEqual(r_wake.status, 200, await r_wake.text())
        j_wake = await r_wake.json()
        self.assertEqual(disp_state['preview_calls'], [{'level': 75}])
        self.assertIs(j_wake['device']['screen_on'], True)


class Round111AdminAuthAndWanGateTests(Base):
    """2026-11-23 R111：后台初始账号密码 admin/admin、专属 18443 隧道继续免密、公网外网访问默认关闭且受后台按钮+账号密码双重门控。"""

    def _run_cli_set_pw(self, line: str) -> int:
        old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO(line)
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            return A.cli_set_password()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err

    async def test_initial_admin_credentials_username_change_and_4char_min(self):
        self.assertEqual(self._run_cli_set_pw('abc\n'), 2)      # 少于 4 位拒绝
        self.assertEqual(self._run_cli_set_pw('admin:admin\n'), 0)
        self.assertEqual(self.admin.auth.username(), 'admin')

        # 缺少账号或账号不对均不能登录
        r_no_user = await self.client.post('/admin/api/login', json={'password': 'admin'}, headers=self.hdr())
        self.assertEqual(r_no_user.status, 400)
        r_bad_user = await self.client.post('/admin/api/login', json={'username': 'root', 'password': 'admin'}, headers=self.hdr())
        self.assertEqual(r_bad_user.status, 403)
        r_ok = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'admin', 'remember': True}, headers=self.hdr())
        self.assertEqual(r_ok.status, 200, await r_ok.text())
        self.assertEqual((await r_ok.json())['username'], 'admin')

        # 支持仅修改管理账号（保留原密码），改后旧账号被拒绝
        r_chg = await self.client.post('/admin/api/password', json={'old': 'admin', 'username': 'alice', 'new': ''}, headers=self.hdr())
        self.assertEqual(r_chg.status, 200, await r_chg.text())
        self.assertEqual((await r_chg.json())['username'], 'alice')
        self.client.session.cookie_jar.clear()
        self.assertEqual((await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'admin'}, headers=self.hdr())).status, 403)
        self.assertEqual((await self.client.post('/admin/api/login', json={'username': 'alice', 'password': 'admin'}, headers=self.hdr())).status, 200)

        # CLI 空行清除密码并同步关闭外网开关
        self.assertEqual((await self.client.post('/admin/api/wan_access', json={'enabled': True}, headers=self.hdr())).status, 200)
        self.assertTrue(self.admin.wan_enabled())
        self.assertEqual(self._run_cli_set_pw('\n'), 0)
        self.assertFalse((A.STATE_DIR / 'auth.json').exists())
        self.assertFalse(self.admin.wan_enabled())

    async def test_whitelisted_18443_vpn_stays_passwordless(self):
        await self.login('admin', username='admin')
        self.client.session.cookie_jar.clear()

        us_vpn = {'X-TS-Peer': '127.0.0.1', 'X-TS-Orig-Host': '203.0.113.10:18443', 'X-Forwarded-Proto': 'https', 'X-Real-IP': '203.0.113.10'}
        alt_vpn = {'X-TS-Peer': '127.0.0.1', 'X-TS-Orig-Host': '198.51.100.20:18443', 'X-Forwarded-Proto': 'https', 'X-Real-IP': '198.51.100.21'}
        with mock.patch.object(A, '_VPN_TUNNEL_NETS', ('127.0.0.0/8', '::1/128', '203.0.113.10/32', '198.51.100.21/32')):
            for hdrs in (us_vpn, alt_vpn):
                rs = await self.client.get('/admin/api/session', headers=hdrs)
                self.assertEqual(rs.status, 200)
                js = await rs.json()
                self.assertTrue(js['logged_in'])
                self.assertTrue(js['password_on'])
                self.assertFalse(js['wan_enabled'])
                self.assertEqual(js['entry'], 'vps')
                self.assertEqual((await self.client.get('/admin/api/songs', headers=hdrs)).status, 200)

            # 非白名单 IP 或普通局域网不享受 18443 免密，必须凭 Cookie 登录
            bad_ip = {'X-TS-Peer': '127.0.0.1', 'X-TS-Orig-Host': '203.0.113.10:18443', 'X-Forwarded-Proto': 'https', 'X-Real-IP': '8.8.8.8'}
            self.assertEqual((await self.client.get('/admin/api/songs', headers=bad_ip)).status, 401)
        lan = {'X-TS-Peer': '192.168.1.23', 'X-TS-Orig-Host': '192.168.1.100:18091'}
        rj = await (await self.client.get('/admin/api/session', headers=lan)).json()
        self.assertFalse(rj['logged_in'])
        self.assertEqual(rj['entry'], 'lan')
        self.assertEqual((await self.client.get('/admin/api/songs', headers=lan)).status, 401)

    async def test_wan_toggle_and_wan_login_gate(self):
        # 未设密码时禁止开启外网访问
        self.assertEqual((await self.client.post('/admin/api/wan_access', json={'enabled': True}, headers=self.hdr())).status, 400)

        await self.login('admin', username='admin')
        wan_get = {'X-TS-Peer': '127.0.0.1', 'X-TS-WAN': '1', 'X-TS-Orig-Host': 'panel.example.com', 'X-Forwarded-Proto': 'https', 'X-Real-IP': '203.0.113.9'}
        wan_post = {**wan_get, 'Origin': 'https://panel.example.com', 'X-TS-Admin': '1'}

        # 默认关闭：即使内网已登录拿到 Cookie，公网入口仍一律 403
        self.assertEqual((await self.client.get('/admin/', headers=wan_get)).status, 403)
        self.assertEqual((await self.client.get('/admin/api/session', headers=wan_get)).status, 403)
        self.assertEqual((await self.client.get('/admin/api/songs', headers=wan_get)).status, 403)

        # 开启外网开关后：开启状态下禁止取消密码；公网可打开登录页但未登录前 API 仍 401
        ron = await self.client.post('/admin/api/wan_access', json={'enabled': True}, headers=self.hdr())
        self.assertEqual(ron.status, 200, await ron.text())
        self.assertTrue((await ron.json())['wan_enabled'])
        self.assertEqual((await self.client.post('/admin/api/password', json={'old': 'admin', 'remove': True}, headers=self.hdr())).status, 400)

        self.client.session.cookie_jar.clear()
        rs = await self.client.get('/admin/api/session', headers=wan_get)
        self.assertEqual(rs.status, 200)
        js = await rs.json()
        self.assertFalse(js['logged_in'])
        self.assertEqual(js['entry'], 'wan')
        self.assertTrue(js['wan_enabled'])
        self.assertEqual((await self.client.get('/admin/api/songs', headers=wan_get)).status, 401)

        # 公网使用 admin/admin 登录并拿回 ts_admin + ts_wan Cookie，可正常调 API；再次关闭外网开关立即收回 403
        rl = await self.client.post('/admin/api/login', json={'username': 'admin', 'password': 'admin', 'remember': True}, headers=wan_post)
        self.assertEqual(rl.status, 200, await rl.text())
        sc_all = rl.headers.getall('Set-Cookie', [])
        self.assertTrue(any('ts_wan=' in c and 'Secure' in c for c in sc_all), sc_all)
        from http.cookies import SimpleCookie
        jar_sc = SimpleCookie()
        for hdr_v in sc_all:
            jar_sc.load(hdr_v)
        tok = jar_sc['ts_admin'].value
        self.assertEqual((await self.client.get('/admin/api/songs', headers={**wan_get, 'Cookie': f'ts_admin={tok}'})).status, 200)

        vpn_post = {'X-TS-Peer': '127.0.0.1', 'X-TS-Orig-Host': '203.0.113.10:18443', 'X-Forwarded-Proto': 'https',
                    'X-Real-IP': '203.0.113.10', 'Origin': 'https://203.0.113.10:18443', 'X-TS-Admin': '1'}
        with mock.patch.object(A, '_VPN_TUNNEL_NETS', ('127.0.0.0/8', '::1/128', '203.0.113.10/32')):
            self.assertEqual((await self.client.post('/admin/api/wan_access', json={'enabled': False}, headers=vpn_post)).status, 200)
        self.assertEqual((await self.client.get('/admin/api/songs', headers={**wan_get, 'Cookie': f'ts_admin={tok}'})).status, 403)

    async def test_intercom_wan_guard_and_session_cookie(self):
        import textwrap
        I = load(INTERCOM_PY, 'ts_intercom_wan_test')
        ic_src = Path(INTERCOM_PY).read_text(encoding='utf-8')
        m = re.search(r'(    @web\.middleware\n    async def intercom_guard\(request, handler\):.*?\n        return await handler\(request\))', ic_src, re.S)
        self.assertIsNotNone(m)
        guard_src = textwrap.dedent(m.group(1))
        ns = dict(I.__dict__)
        ns['shutting_down'] = [False]
        exec(compile(guard_src, '<intercom_guard>', 'exec'), ns)
        intercom_guard = ns['intercom_guard']

        old_dir = I.ADMIN_STATE_DIR
        I.ADMIN_STATE_DIR = A.STATE_DIR
        I._WAN_CFG_CACHE = {'sig': 'unset', 'data': {}}
        I._WAN_SESS_CACHE = {'sig': 'unset', 'data': {}}
        try:
            async def _admin_ok(_r):
                return web.Response(text='admin-ok')
            async def _sess_ok(_r):
                return web.json_response({'ok': True})
            async def _tutor_ok(_r):
                return web.Response(text='tutor-ok')
            async def _msg_ok(_r):
                return web.Response(text='msg-ok')
            proxy_app = web.Application(middlewares=[intercom_guard])
            proxy_app.router.add_get('/admin/', _admin_ok)
            proxy_app.router.add_get('/admin/api/session', _sess_ok)
            proxy_app.router.add_get('/tutor/', _tutor_ok)
            proxy_app.router.add_get('/', _msg_ok)
            cli = TestClient(TestServer(proxy_app))
            await cli.start_server()
            try:
                wan_h = {'Host': 'panel.example.com', 'X-Forwarded-Proto': 'https', 'X-TS-WAN': '1'}
                # 未开外网：/admin/ 返回 403 HTML，/admin/api/* 返回 403 JSON，/tutor/?embed=1 与 /?embed=msg 返回 403
                r1 = await cli.get('/admin/', headers=wan_h)
                self.assertEqual(r1.status, 403)
                self.assertIn('外网访问未开启', await r1.text())
                r2 = await cli.get('/admin/api/session', headers=wan_h)
                self.assertEqual(r2.status, 403)
                self.assertFalse((await r2.json())['ok'])
                self.assertEqual((await cli.get('/tutor/?embed=1', headers=wan_h)).status, 403)

                # 开启外网但未登录：允许打开 /admin/ 登录页，禁止未授权打开 /tutor/?embed=1 与 /?embed=msg
                A.STATE_DIR.mkdir(parents=True, exist_ok=True)
                (A.STATE_DIR / 'wan_access.json').write_text(json.dumps({'enabled': True}), encoding='utf-8')
                self.assertEqual((await cli.get('/admin/', headers=wan_h)).status, 200)
                self.assertEqual((await cli.get('/tutor/?embed=1', headers=wan_h)).status, 403)
                self.assertEqual((await cli.get('/?embed=msg', headers=wan_h)).status, 403)

                # 登录生成真实 sessions.json 后持 ts_wan Cookie 可打开嵌入式页面
                await self.login('admin', username='admin')
                cookies = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/admin/'))
                tok = cookies['ts_admin'].value
                r_t = await cli.get('/tutor/?embed=1', headers={**wan_h, 'Cookie': f'ts_wan={tok}'})
                self.assertEqual(r_t.status, 200)
                r_m = await cli.get('/?embed=msg', headers={**wan_h, 'Cookie': f'ts_wan={tok}'})
                self.assertEqual(r_m.status, 200)

                # 验证 X-TS-WAN 防伪造头：局域网转发清空伪造头，直连非内网地址自动打标 X-TS-WAN=1
                spoof = I._admin_fwd_headers({'Host': '192.168.1.100:18091', 'X-TS-WAN': '1', 'X-Real-IP': '8.8.8.8'}, '192.168.1.23')
                self.assertNotIn('X-TS-WAN', spoof)
                ext = I._admin_fwd_headers({'Host': 'panel.example.com', 'X-Forwarded-Proto': 'https'}, '203.0.113.5')
                self.assertEqual(ext.get('X-TS-WAN'), '1')
            finally:
                await cli.close()
        finally:
            I.ADMIN_STATE_DIR = old_dir
            I._WAN_CFG_CACHE = {'sig': 'unset', 'data': {}}
            I._WAN_SESS_CACHE = {'sig': 'unset', 'data': {}}


if __name__ == '__main__':
    unittest.main(verbosity=1)
