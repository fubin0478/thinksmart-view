#!/opt/thinksmart-addons/venv/bin/python
"""Claude 2026-11-25：小智融合改版的回归测试。

付总定的：小麦没有单独的页面，叫醒只弹灵动胶囊和富媒体卡片；可调参数和对话都在设置里，闹钟只在主页「闹钟留言」里管；主页底栏一行七格，去掉「亮度」。
1. 8090 底座页：设置九个分类（含对话），参数控件都在；没有小麦页、没有「语音呈现」两种模式，也没有旧的睡眠时段、待汇报、在线更新。
2. home_overlay.js：一颗胶囊长成一张不透明的卡片；响铃不切页面；小提示（错过的闹钟、倒计时）。
3. 客户端 /api/settings、assistant.py；网关 kiosk_voice；主页 /api/home/config；音乐助眠音量。
4. /tmp 这类带粘滞位的公用目录不会被收成 0700（11-25 真发生过一次）。
不碰任何在跑的服务：只读文件、在进程里起临时的 aiohttp 测试服务器、在临时目录里改配置副本。
"""
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

CLIENT = pathlib.Path(os.environ.get('XZ_CLIENT', '/opt/thinksmart/client/current'))
CLIENT_PY = '/opt/thinksmart/client/current/.venv/bin/python'
HUB = pathlib.Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
HUB_PY = str(HUB / '.venv/bin/python')
HUB_VOICE = pathlib.Path(os.environ.get('XZ_HUB_VOICE', HUB / 'core/admin/kiosk_voice.py'))
HUB_ADMIN = pathlib.Path(os.environ.get('XZ_HUB_ADMIN', HUB / 'core/admin/admin_handler.py'))
OVERLAY = pathlib.Path(os.environ.get('OVERLAY_JS', '/usr/local/share/thinksmart-monitor/home_overlay.js'))
HOME_HTML = pathlib.Path(os.environ.get('HOME_HTML', '/usr/local/share/thinksmart-home/index.html'))
APPS = pathlib.Path(os.environ.get('HOME_APPS', '/usr/local/share/thinksmart-home/apps.json'))
KEYBOARD = pathlib.Path(os.environ.get('KEYBOARD_JS', '/usr/local/share/thinksmart-kiosk/keyboard.js'))
SVC = {'home': os.environ.get('HOME_PY', '/usr/local/bin/thinksmart-home.py'), 'qq': os.environ.get('QQ_PY', '/usr/local/bin/thinksmart-qqmusic.py'),
       'study': os.environ.get('STUDY_PY', '/usr/local/bin/thinksmart-study.py'), 'monitor': os.environ.get('MONITOR_PY', '/usr/local/bin/thinksmart-monitor.py')}
SECTIONS = ['display', 'sound', 'net', 'voice', 'chat', 'weather', 'music', 'study', 'sys']


def _run(py, code, cwd='/tmp', timeout=60, **env):
    e = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', **env}
    r = subprocess.run([py, '-B', '-c', code], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=e)
    return r.returncode, r.stdout, r.stderr


class BasePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        v2 = CLIENT / 'web/v2'
        cls.html = (v2 / 'index.html').read_text('utf-8')
        cls.js = (v2 / 'app.js').read_text('utf-8')
        cls.css = (v2 / 'app.css').read_text('utf-8')
        cls.set = (v2 / 'settings.js').read_text('utf-8')

    def test_no_page_of_its_own(self):
        for gone in ('class="stage"', 'id="orb"', 'id="welcome"', 'id="btnAbort"', 'voiceModeSeg', 'id="setHistory"', 'homeSlot'):
            self.assertNotIn(gone, self.html)
        for gone in ('DEMO', 'corona', 'renderSide', '__tsVoiceMode', 'setCorona'):
            self.assertNotIn(gone, self.js)
        # the base screen only shows when the home page can't be reached; it keeps the shortcuts
        self.assertIn('<div class="app" id="base">', self.html)
        for go in ('wake', '8091', '8092', '8096', 'settings'):
            self.assertIn(f'data-go="{go}"', self.html)

    def test_nine_sections_each_with_its_nav_button(self):
        self.assertEqual(re.findall(r'<button data-sec="(\w+)"', self.html), SECTIONS)
        self.assertEqual(re.findall(r'<section class="sec(?: on)?" data-sec="(\w+)"', self.html), SECTIONS)
        chat = self.html[self.html.index('<section class="sec" data-sec="chat">'):]
        chat = chat[:chat.index('</section>')]
        for i in ('modelPill', 'btnMic', 'messages', 'chatEmpty', 'headMeta'):
            self.assertIn(f'id="{i}"', chat)

    def test_every_id_the_scripts_use_exists(self):
        ids = set(re.findall(r'id="([\w-]+)"', self.html + self.js + self.set))
        used = set(re.findall(r"\$\('([\w-]+)'\)", self.js + self.set))
        used |= {x for t in re.findall(r"slider\('(\w+)', '(\w+)'", self.js) for x in t}
        self.assertEqual(used - ids, {'pending'})          # made by the script itself

    def test_parameters_are_on_the_page(self):
        for i in ('brightRange', 'brightModeSeg', 'brightPresets', 'themeSeg', 'orientSeg', 'offSwitch', 'offMinutes', 'volRange',
                  'airplayCard', 'airplaySwitch', 'btSwitch', 'setModel', 'clearBtn', 'clearMemBtn',
                  'wifiBtn', 'netWifiList', 'wxMainInp', 'wxExtraInp', 'adminQr', 'facts', 'powerBtn'):
            self.assertIn(f'id="{i}"', self.html)
        # 闹钟只在主页「闹钟留言」里管；多余入口与旧「睡眠」分类已移除
        for i in ('alarmVolRange', 'alarmList', 'tones', 'mkOpen', 'missedBox', 'sleepSwitch', 'reportPill',
                  'displaySleepLinkCard', 'alarmShortcutCard', 'netMonitorCard', 'sleepVolRange', 'sleepShortcutCard'):
            self.assertNotIn(f'id="{i}"', self.html)
        self.assertIn('window.__tsOpenHomeAlarms()', self.js)
        for k in ('wake_words', 'kws_threshold', 'kws_gain_db', 'kws_score', 'kws_paths', 'wake_verify', 'wake_sound', 'follow_up_seconds', 'asr_engine', 'silence_ms',
                  'vad_threshold', 'no_voice_close_sec', 'tts_engine', 'edge_voice', 'edge_rate', 'edge_pitch', 'edge_volume', 'openai_speed', 'history', 'max_messages',
                  'summary', 'call_names', 'quality_level', 'play_mode', 'radio_audio_mode', 'max_cache_mb', 'extra_cities', 'interval',
                  'burst_frames', 'burst_gap', 'cooldown', 'min_gap', 'auto_off_hours'):
            self.assertIn(f"'{k}'" if f"'{k}'" in self.set else k, self.set, k)
        for a in ('restart_client', 'restart_hub', 'reboot'):
            self.assertIn(f'data-act="{a}"', self.html)
        # dangerous ones need a second tap
        for fn in ("'确认重启'", "'确认关机'", "'确认清除'", '确认忘记'):
            self.assertIn(fn, self.js + self.set)

    def test_style_rules(self):
        css = self.css
        self.assertEqual(re.findall(r'\d+\.\d+px', css), [])
        self.assertLessEqual(set(re.findall(r'font-weight:\s*(\w+)', css)), {'400', '700'})
        sizes = [int(v) for v in re.findall(r'font-size:\s*(\d+)px', css)]
        self.assertGreaterEqual(min(sizes), 15)
        self.assertNotIn('backdrop-filter', css)
        for s in ('html.ts-light {', 'html.ts-light body {', 'html.ts-light .sheet {', '.panel > * { flex-shrink: 0; }', '/* ---- native rich media cards',
                  '.panel > .sec[data-sec="chat"]'):
            self.assertIn(s, css)
        self.assertIn('touch-action: none', css)


class CapsuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = OVERLAY.read_text('utf-8')

    def test_capsule_card_and_notices(self):
        self.assertIn('window.__tsOpenHomeAlarms = () => {', self.js)
        self.assertIn('routed', self.js)                 # 「倒计时 5 分钟」转到主页计时器，卡片跟着它
        for gone in ('sleep_mode', 'reports', 'ensureBrightnessDom', 'tsBrightnessPanel'):
            self.assertNotIn(gone, self.js)
        for s in ("voicePill.id = 'tsVoicePill'", "vcEl.id = 'tsVoiceCard'", "noticeEl.id = 'tsNotice'", 'id="tsVpWave"', 'id="tsVcRich"', 'id="tsVcChips"',
                  'function detectRichSpec(', 'function buildRichHtml(', 'function syncNotice(', 'window.__tsDecorateBotMsg', 'window.__tsHasCard',
                  "addEventListener('ts-settings-section', () => { syncVoiceCard(); });", '--isl-y'):
            self.assertIn(s, self.js)
        for k in ('music', 'weather', 'schedule', 'timer', 'countdown', 'message', 'server', 'tutor', 'volume', 'brightness', 'news'):
            self.assertIn(f"kind: '{k}'", self.js)
            self.assertIn(f"k === '{k}'", self.js)

    def test_no_page_switching(self):
        for gone in ("setView(", "view = 'xiaozhi'", 'xzAuto', '__tsShowXiaozhi', 'tsHomeBtn', 'ts-xz-paused', 'ts-still', 'autoReturn'):
            self.assertNotIn(gone, self.js)
        ring = self.js[self.js.index("if (document.body.classList.contains('ringing')) {\n      closeBrightness(false);"):][:260]
        self.assertNotIn('closeSub', ring)               # an alarm rings over whatever is open

    def test_a_card_needs_a_tool_or_a_weather_question(self):
        fn = self.js[self.js.index('function detectRichSpec('):]
        fn = fn[:fn.index('\n  }\n') + 4]
        self.assertIn('if (tool && tool.error) return null;', fn)
        self.assertNotIn('combined', fn)                 # no guessing from the words of the answer
        self.assertEqual(fn.count('.test(q'), 1)

    def test_opaque_surfaces_and_class_names(self):
        css = self.js[self.js.index('2026-11-25 Claude：灵动胶囊'):self.js.index('  `;\n  document.head.appendChild(style);')]
        self.assertNotIn('backdrop', css)
        self.assertEqual(re.findall(r'\d+\.\d+px', css), [])
        self.assertLessEqual(set(re.findall(r'font-weight:(\w+)', css)), {'400', '700'})
        self.assertGreaterEqual(min(int(v) for v in re.findall(r'font-size:(\d+)px', css)), 15)
        self.assertNotIn('class="rk main"', self.js)     # .main used to be the old page's layout class

    def test_narrow_cards_in_the_portrait_conversation(self):
        # 竖版「设置 → 对话」里卡片只有 474 宽：名字、标题不许被按钮挤没；倒计时也走字
        for x in ('html.ts-portrait .msg-rich .rc .nodes{grid-template-columns:repeat(2,1fr)}', 'html.ts-portrait .msg-rich .rc.sch .ra{flex:1 0 100%}',
                  'html.ts-portrait .msg-rich .rc .ra .tag{display:none}', 'html.ts-portrait .msg-rich .rc .aqs{display:inline}',
                  '<div class="rc sch">', '<div class="rc mus">', 'if (!document.hidden && chatOpen()) tickCards(xzBox)', 'setInterval(() => tickCards(vcEl), 1000)'):
            self.assertIn(x, self.js)

    def test_base_page_shows_when_home_is_down(self):
        for s in ('function baseNote()', 'if (bootCycles === 1)', "el.textContent = bootCycles > 1 ? '主页暂时打不开"):
            self.assertIn(s, self.js)

    def test_keyboard_follows_the_theme(self):
        k = KEYBOARD.read_text('utf-8')
        self.assertIn('html.ts-light .tskb, html[data-theme="light"] .tskb {', k)
        self.assertNotIn('linear-gradient', k)


class HomeDockTests(unittest.TestCase):
    def test_dock_is_one_row_of_seven(self):
        d = json.loads(APPS.read_text('utf-8'))
        self.assertEqual(len(d['apps']), 9)
        by = {a['id']: a for a in d['apps']}
        self.assertTrue(by['brightness'].get('hidden'))
        self.assertFalse(by['settings'].get('hidden', False))
        shown = [a['id'] for a in sorted(d['apps'], key=lambda a: a.get('order', 0)) if not a.get('hidden')]
        self.assertGreater(shown.index('settings'), shown.index('tutor'))
        h = HOME_HTML.read_text('utf-8')
        self.assertEqual(shown, ['xiaozhi', 'music', 'focus', 'messages', 'server', 'tutor', 'settings'])
        self.assertIn("main.length === 6 || main.length === 7", h)
        self.assertIn('#dock.dock-row { grid-template-columns: none; grid-auto-flow: column;', h)
        self.assertIn('<body class="dock-one">', h)

    def test_alarm_board_fits_and_stays_quiet(self):
        h = HOME_HTML.read_text('utf-8')
        self.assertIn('.al-grid { grid-template-columns: minmax(0, 1fr) 468px; }', h)
        self.assertIn("toggleCls(dockTiles.get('messages'), 'live', unr > 0);", h)
        self.assertNotIn('或在右侧自由调时间', h)
        for i in ('alHMinus', 'alHPlus', 'alMMinus', 'alMPlus'):
            self.assertRegex(h, rf'id="{i}" aria-label="[^"]+"><svg')


class PortraitStripTests(unittest.TestCase):
    """付总定的：竖版留言板和日历收矮，日历下面、音乐上面加一条「接下来」；横版不变。"""

    def test_portrait_only(self):
        h = HOME_HTML.read_text('utf-8')
        i = h.index('body.dock-one .wrap { grid-template-rows: 404px 352px 84px 228px minmax(0, 1fr); }')
        block = h[h.rindex('@media (orientation: portrait) {', 0, i):i + 3200]
        for s in ('body.dock-one #dock { grid-row: 5; }', 'body.dock-one #musicCard { grid-row: 4; }', 'body.dock-one #upNext { grid-column: 1 / -1; grid-row: 3; display: grid;', 'body.dock-one .cal-week {', 'body.dock-one #side .ring'):
            self.assertIn(s, block)
        self.assertIn('\n.up { display: none; }\n', h)                  # landscape: not shown
        self.assertLess(h.index('id="side"'), h.index('id="upNext"'))
        self.assertLess(h.index('id="upNext"'), h.index('<nav class="dock"'))
        for s in ('function upItems()', 'function renderUpNext()', "openMessages(go)", 'openCountdownPage()', 'upComing = Array.isArray(t.coming) ? t.coming : [];',
                  "x.kind !== 'jq-next' || !up.some(",                  # a solar term already in the strip is not written twice
                  '<span class="mw">${esc(metaTxt)}</span><span class="mn">${esc(metaShort)}</span>', '\n.s-meta .mn { display: none; }\n'):   # the portrait heading fits
            self.assertIn(s, h)

    def test_coming_days_with_the_real_calendar(self):
        code = r'''
import sys, os, datetime as dt, contextlib
sys.path.insert(0, '/opt/thinksmart-addons/vendor')
from lunar_python import Solar
from lunar_python.util import HolidayUtil
src = open(os.environ['HOME_PY'], encoding='utf-8').read()
fn = src[src.index('HOL_SHORT = {'):src.index('def today_info():')]
exec(fn)
def look(*a): return {(x['kind'], x['name']): x for x in coming_days(dt.date(*a))}
d = look(2026, 11, 25)
assert d[('rest', '周末')]['days'] == 3, d
assert d[('jq', '大雪')]['days'] == 12 and d[('jq', '冬至')]['days'] == 27, d
off = [x for x in d.values() if x['kind'] == 'off']
assert off and off[0]['name'] == '元旦' and off[0]['days'] == 37 and off[0]['len'] >= 3, off
assert [x['days'] for x in coming_days(dt.date(2026, 11, 25))] == sorted(x['days'] for x in coming_days(dt.date(2026, 11, 25)))
assert ('rest', '周末') not in look(2026, 11, 28)            # already resting
for i in range(0, 400, 7):                                   # never raises, always something
    assert coming_days(dt.date(2026, 1, 1) + dt.timedelta(days=i))
print('COMING_OK')
'''
        rc, out, err = _run('/opt/thinksmart-addons/venv/bin/python', code, HOME_PY=SVC['home'])
        self.assertEqual(rc, 0, err + out)
        self.assertIn('COMING_OK', out)


class ClientTests(unittest.TestCase):
    def test_assistant_and_settings_api(self):
        code = r'''
import asyncio, os, sys, tempfile
sys.path.insert(0, os.environ['XZ']); sys.path.append('/usr/local/bin')
os.environ['HOME'] = tempfile.mkdtemp(dir='/tmp')
from thinksmart_client import assistant, config, settings_api
assert [w['on'] for w in assistant.view(config.DEFAULTS)['wake_words']] == [True, False, False]
cfg = {**config.DEFAULTS, 'wake_words': ['x iǎo m ài x iǎo m ài @小麦小麦'], 'kws_threshold': 0.16}
v = assistant.view(cfg)
assert [w['on'] for w in v['wake_words']] == [False, True, False] and v['kws_threshold'] == .16 and v['kws_paths'] == 8
assert assistant.normalize(cfg, {'kws_threshold': .2, 'follow_up_seconds': 10}) == {'kws_threshold': .2}
for bad in ({'kws_threshold': .9}, {'wake_words': []}, {'wake_words': ['x @别的']}, {'kws_gain_db': True}, {'foo': 1}, {}, {'wake_verify': 'hub'}, {'wake_sound': 'bang'}):
    try:
        assistant.normalize(cfg, bad)
    except ValueError:
        continue
    raise SystemExit(('accepted', bad))
f = settings_api.device_facts()
assert f['disk'] and f['mem'] and f['version']
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
class Fake:
    cfg = dict(cfg, hub_http='http://127.0.0.1:1')
    state = 'idle'; ring = None
    class keys: mic_on = True
    def health(self): return {'audio': True, 'hub': True}
    async def set_assistant(self, ch):
        self.cfg.update(assistant.normalize(self.cfg, ch)); return assistant.view(self.cfg)
async def main():
    app = web.Application(); fc = Fake()
    settings_api.register(app, fc, lambda r: (r.headers.get('Origin') or 'http://127.0.0.1:8090').startswith('http://127.0.0.1'))
    async with TestClient(TestServer(app)) as c:
        j = await (await c.get('/api/settings?part=assistant')).json(); assert j['ok'] and j['voice'] is None, j
        r = await c.post('/api/settings', json={'part': 'assistant', 'values': {'kws_threshold': .22}}); j = await r.json(); assert r.status == 200 and j['assistant']['kws_threshold'] == .22, j
        r = await c.post('/api/settings', json={'part': 'assistant', 'values': {'kws_threshold': 3}}); assert r.status == 400
        r = await c.post('/api/settings', json={'part': 'assistant', 'values': {'asr_engine': 'SherpaXASR'}}); assert r.status == 502   # the gateway isn't there
        r = await c.post('/api/settings', json={'part': 'x', 'values': {'a': 1}}); assert r.status == 404
        r = await c.post('/api/settings', json={'part': 'assistant', 'values': {'kws_threshold': .2}}, headers={'Origin': 'http://evil.example'}); assert r.status == 403
        r = await c.post('/api/settings', json={'part': 'system', 'values': {'action': 'nope'}}); assert r.status == 400
        fc.state = 'listening'; r = await c.post('/api/settings', json={'part': 'system', 'values': {'action': 'reboot'}}); assert r.status == 409; fc.state = 'idle'
asyncio.run(main())
print('CLIENT_OK')
'''
        rc, out, err = _run(CLIENT_PY, code, XZ=str(CLIENT))
        self.assertEqual(rc, 0, err + out)
        self.assertIn('CLIENT_OK', out)

    def test_wake_model_gain_and_listening_window(self):
        kws = (CLIENT / 'thinksmart_client/kws.py').read_text('utf-8')
        app = (CLIENT / 'thinksmart_client/app.py').read_text('utf-8')
        self.assertIn('def set_gain(', kws)
        for s in ('async def set_assistant(', 'WAKE_LISTEN', 'assistant_lock', 'async def reboot('):
            self.assertIn(s, app)


class HubTests(unittest.TestCase):
    def test_kiosk_voice_whitelist_on_a_copy(self):
        code = r'''
import importlib.util, json, os, pathlib, shutil, sys, tempfile
sys.path.insert(0, '.')
spec = importlib.util.spec_from_file_location('kiosk_voice_t', os.environ['KV']); kv = importlib.util.module_from_spec(spec); spec.loader.exec_module(kv)
from core.admin import config_store
d = kv.describe(config_store.load_merged())
txt = json.dumps(d, ensure_ascii=False).lower()
for bad in ('http', 'api_key', 'token', 'secret'): assert bad not in txt, bad
td = pathlib.Path(tempfile.mkdtemp(dir='/tmp')); cp = td / '.config.yaml'
try:
    src = pathlib.Path('data/.config.yaml').read_text(encoding='utf-8'); cp.write_text(src, encoding='utf-8'); os.chmod(cp, 0o600)
    mg = config_store.load_merged(custom_path=cp)
    r = kv.save({'silence_ms': 900, 'edge_voice': 'zh-CN-YunxiNeural', 'edge_volume': 20, 'max_messages': 12, 'call_names': False, 'openai_speed': 1.2}, mg, cp)
    d2 = kv.describe(config_store.load_merged(custom_path=cp))
    assert d2['vad']['silence_ms'] == 900 and d2['tts']['edge']['voice'] == 'zh-CN-YunxiNeural' and d2['tts']['openai']['speed'] == 1.2 and d2['history']['max_messages'] == 12 and d2['call_names'] is False
    assert r['tts'] and set(r['sections']) == {'TTS', 'VAD', 'call_names'}, r
    assert kv.save({'silence_ms': 900}, config_store.load_merged(custom_path=cp), cp)['changed'] == []
    assert (cp.stat().st_mode & 0o777) == 0o600
    for bad in ({'silence_ms': 50}, {'edge_voice': 'x'}, {'tts_engine': 'Nope'}, {'asr_engine': 'FunASR'}, {'openai_speed': 9}, {'greeting': True}, {'prompt': 'x'}, {}, {'edge_rate': 99}, {'edge_rate': 1.5}):
        try:
            kv.save(bad, mg, cp)
        except ValueError:
            continue
        raise SystemExit(('accepted', bad))
finally:
    shutil.rmtree(td)
print('HUB_OK')
'''
        rc, out, err = _run(HUB_PY, code, cwd=str(HUB), KV=str(HUB_VOICE))
        self.assertEqual(rc, 0, err + out)
        self.assertIn('HUB_OK', out)

    def test_only_this_machine(self):
        s = HUB_ADMIN.read_text('utf-8')
        block = s[s.index('async def handle_kiosk_internal_voice('):s.index('async def handle_kiosk_internal_memory(')]
        self.assertEqual(block.count('X-TS-Internal-Reload'), 2)
        self.assertEqual(block.count('_from_this_machine(request)'), 2)
        self.assertEqual(block.count('"Origin" in request.headers'), 2)


class ServiceTests(unittest.TestCase):
    def test_sticky_public_dirs_are_left_alone(self):
        for name, path in SVC.items():
            s = pathlib.Path(path).read_text('utf-8')
            self.assertTrue('S_ISVTX' in s or '0o1000' in s, name)
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            pub = pathlib.Path(td) / 'pub'
            pub.mkdir()
            os.chmod(pub, 0o1777)
            src = pathlib.Path(SVC['home']).read_text('utf-8')
            fn = src[src.index('def _harden_private_dir(d):'):]
            fn = fn[:fn.index('\n\n\n')]
            ns = {}
            exec('import contextlib, os, stat\n' + fn, ns)   # noqa: S102 - the function itself, on a temp dir
            ns['_harden_private_dir'](pub)
            self.assertEqual(stat.S_IMODE(os.lstat(pub).st_mode), 0o1777)
            priv = pathlib.Path(td) / 'priv'
            priv.mkdir()
            os.chmod(priv, 0o755)
            ns['_harden_private_dir'](priv)
            self.assertEqual(stat.S_IMODE(os.lstat(priv).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat('/tmp').st_mode) & 0o1777, 0o1777)

    def test_study_numbers_can_be_read(self):
        s = pathlib.Path(SVC['study']).read_text('utf-8')
        self.assertIn("app.router.add_get('/api/study/config', config_get)", s)
        fn = s[s.index('    async def config_get(request):'):s.index('    async def config(request):')]
        for k in ('interval', 'cooldown', 'min_gap', 'burst_frames', 'burst_gap', 'auto_off_hours'):
            self.assertIn(f"'{k}'", fn)
        for secret in ('vision_url', 'tts_url', 'token'):
            self.assertNotIn(secret, fn)
        api = (CLIENT / 'thinksmart_client/settings_api.py').read_text('utf-8')
        self.assertIn("'/api/study/config', timeout=2.5", api)

    def test_home_config_and_sleep_volume(self):
        h = pathlib.Path(SVC['home']).read_text('utf-8')
        for s in ("add_get('/api/home/config', get_home_config)", "add_post('/api/home/config', post_home_config)", "add_post('/api/home/alarms', alarms_api)"):
            self.assertIn(s, h)
        q = pathlib.Path(SVC['qq']).read_text('utf-8')
        fn = q[q.index('    def sleep_set_volume(self, volume)'):]
        fn = fn[:fn.index('\n    def ', 10)]
        ns = {}
        exec('import threading\nstate = []\ndef _write_sleep_state(s): state.append(dict(s))\nclass E:\n    _lock = threading.Lock()\n    _sleep_state = {}\n'
             '    def _snap_sleep_state_locked(self): return dict(self._sleep_state)\n' + fn, ns)   # noqa: S102
        e = ns['E']()
        self.assertEqual(e.sleep_set_volume(25), {'ok': True, 'sleep_volume': 25})
        self.assertEqual(ns['state'][-1]['sleep_volume'], 25)
        for bad in (4, 61, True, None, '30', float('nan')):
            self.assertFalse(e.sleep_set_volume(bad)['ok'], bad)


if __name__ == '__main__':
    unittest.main(verbosity=1)
