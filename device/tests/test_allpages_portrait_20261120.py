#!/opt/thinksmart-addons/venv/bin/python
"""Claude 2026-11-20: every page except 小智 turns to portrait with the gravity sensor ().
- thinksmart-home.py GET /api/home/orient: raw BMA253 x/y/z for the overlay, sensor:false without one, host guard
- home_overlay.js: polls it, turns the home / music / server frames, the brightness panel, the message card and the
  voice pill; leaves the AI 讲题 frame to its own rotation and tells it the orientation; 跟随机器 / 竖版 / 横版
- home, music and server pages: a portrait layer and the drag-scroll shim (taps keep working: only drags ≥10px blocked)
- brightness panel: 屏幕方向 row; AI 讲题: follows the overlay, touchmove guard only for real drags
- round 2: cover + fade while turning, faster but steadier sensing, no turn while a finger is down, 小麦 in portrait =
  status pill + text card (no sideways XiaoZhi page), XiaoZhi's alarm / toast / HUD upright, lock hint, sliders
  draggable in portrait, server page fits all eight cards, weather / timer polish
HOME_PY / HOME_HTML / QQ_HTML / MON_HTML / DISPLAY_HTML / OVERLAY_JS / TUTOR_HTML may point at staged copies."""
import importlib.util
import os
import pathlib
import re
import sys
import tempfile
import unittest

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')
from aiohttp.test_utils import AioHTTPTestCase

E = os.environ.get
HOME_PY = E('HOME_PY', '/usr/local/bin/thinksmart-home.py')
HOME_HTML = pathlib.Path(E('HOME_HTML', '/usr/local/share/thinksmart-home/index.html'))
QQ_HTML = pathlib.Path(E('QQ_HTML', '/usr/local/share/thinksmart-qqmusic/index.html'))
MON_HTML = pathlib.Path(E('MON_HTML', '/usr/local/share/thinksmart-monitor/index.html'))
DISPLAY_HTML = pathlib.Path(E('DISPLAY_HTML', '/usr/local/share/thinksmart-monitor/display.html'))
OVERLAY_JS = pathlib.Path(E('OVERLAY_JS', '/usr/local/share/thinksmart-monitor/home_overlay.js'))
TUTOR_HTML = pathlib.Path(E('TUTOR_HTML', '/usr/local/share/thinksmart-tutor/index.html'))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


home = _load('ts_home_portrait_test', HOME_PY)
H = {'Host': '127.0.0.1:8095'}


class TestOrientEndpoint(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._td.name)
        self.iio = root / 'iio'
        (self.iio / 'iio:device0').mkdir(parents=True)          # an ADC without accelerometer files comes first
        self.dev = self.iio / 'iio:device2'
        self.dev.mkdir()
        self._write(2, -493, 166)
        self._old = os.environ.get('HOME_IIO_DIR')
        os.environ['HOME_IIO_DIR'] = str(self.iio)
        home._ACCEL_CACHE.update(dir=None, checked=0.0)
        return home.make_app(home.Inbox(root / 'messages.json'), home.CountdownStore(root / 'countdowns.json'))

    def _write(self, x, y, z):
        for a, v in (('x', x), ('y', y), ('z', z)):
            (self.dev / f'in_accel_{a}_raw').write_text(f'{v}\n')

    async def asyncTearDown(self):
        await super().asyncTearDown()
        if self._old is None:
            os.environ.pop('HOME_IIO_DIR', None)
        else:
            os.environ['HOME_IIO_DIR'] = self._old
        home._ACCEL_CACHE.update(dir=None, checked=0.0)
        self._td.cleanup()

    async def test_raw_values_cors_missing_sensor_and_host_guard(self):
        r = await self.client.get('/api/home/orient', headers={**H, 'Origin': 'http://127.0.0.1:8090'})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get('Access-Control-Allow-Origin'), 'http://127.0.0.1:8090')   # the overlay lives on 8090
        self.assertEqual(r.headers.get('Cache-Control'), 'no-store')
        self.assertEqual(await r.json(), {'ok': True, 'sensor': True, 'x': 2, 'y': -493, 'z': 166})
        self._write(497, -3, 150)                                                                     # laid back to landscape
        j = await (await self.client.get('/api/home/orient', headers=H)).json()
        self.assertEqual((j['x'], j['y']), (497, -3))
        for f in self.dev.glob('in_accel_*'):
            f.unlink()
        j = await (await self.client.get('/api/home/orient', headers=H)).json()
        self.assertEqual(j, {'ok': True, 'sensor': False})
        r = await self.client.get('/api/home/orient', headers={'Host': 'evil.example:80'})
        self.assertEqual(r.status, 403)
        r = await self.client.get('/api/home/orient', headers={**H, 'Origin': 'http://evil.example'})
        self.assertEqual(r.status, 403)


def _layer(text, marker):
    i = text.index(marker)
    return text[i:]


class TestOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = OVERLAY_JS.read_text('utf-8')

    def test_rotation_css_and_tutor_left_alone(self):
        js = self.js
        for sym in ('html.ts-portrait #tsHomeLayer iframe', 'html.ts-portrait #tsSubLayer>iframe:not([data-port="8096"])',
                    'transform:translateX(100vw) rotate(90deg)', 'html.ts-portrait.ts-rot270 #tsHomeLayer iframe',
                    'transform:translateY(100vh) rotate(-90deg)', 'html.ts-portrait #tsBrightnessPanel.open{transform:translate3d(-50%,-50%,0) rotate(90deg)}',
                    '@keyframes tsMsgDropP', 'html.ts-portrait #tsVoicePill.show', 'f.dataset.port = u.port;'):
            self.assertIn(sym, js)
        self.assertEqual(js.count('f.dataset.port = u.port;'), 2)                  # opened and prewarmed sub frames

    def test_sensor_logic_modes_and_messages(self):
        js = self.js
        for sym in ("fetch(HOME + '/api/home/orient'", 'function orClassify(', "if (Math.max(ax, ay) < 220 || Math.abs(ax - ay) < 120) return '';",
                    "if (ay > ax) return y < 0 ? 'p90' : 'p270';", 'if (orCandN >= 2)', "OR_MODE_KEY = 'ts.orientMode'",
                    "root.classList.toggle('ts-portrait', o !== 'l')", "root.classList.toggle('ts-rot270', o === 'p270')",
                    "postMessage({ ts: 'orient', cur: orCur, mode: orMode }", "d.thinksmart === 'setOrient'", "d.cmd === 'orientMode'",
                    "if (v.port === '8096')"):
            self.assertIn(sym, js)
        i = js.index('2026-11-20 Claude：机身重力感应')
        self.assertLess(js.index("if (!isXZ) return;"), i)                         # only the top frame (8090) reads the sensor

    def test_round2_turning_feel(self):
        js = self.js
        for sym in ("orCoverEl.id = 'tsRotCover'", '#tsRotCover.on{opacity:1;visibility:visible;transition:none}',
                    'nativeRaf(() => nativeRaf(() => {', 'if (g < 400 || g > 640) return', '} else next = 150;',
                    'Math.abs(x - orLast.x) + Math.abs(y - orLast.y) + Math.abs(z - orLast.z) > 160',
                    "if (d.ts === 'touch')", 'function orTouchOn(', 'if (orTouching()) orPending = true; else orGo();',
                    "orHintEl.id = 'tsOrHint'", '#tsOrHint[data-phys="p90"]', "orHintEl.onclick = () => { orHintEl.classList.remove('show'); orSetMode('auto'); };"):
            self.assertIn(sym, js)
        self.assertIn(':not(#powerOff):not(#ring):not(#tsRotCover):not(#tsVoiceCard):not(#tsOrHint):not(.tskb-dock)', js)
        self.assertIn('html.ts-sub-opaque #tsHomeLayer', js)

    def test_round2_xiaomai_in_portrait(self):
        js = self.js
        for sym in ("const xzAuto = () => !root.classList.contains('ts-portrait');", "fetch('/api/wake', { method: 'POST' })",
                    "} else if (!xzAuto() && !document.body.classList.contains('ringing')) {\n        wakeXzPortrait();",
                    'if (!xzAuto()) return;   // 竖屏：小麦的闹钟卡片在当前页面上面转正显示，不切页面',
                    "if (!subOpen && !sheetOpen() && view === 'home' && homeOk && xzAuto()) {", "if (isBusy() && xzAuto()) { autoReturn = true; view = 'xiaozhi'; }",
                    "vcEl.id = 'tsVoiceCard'", "xzBox.querySelectorAll('.msg:not(.pending)')", 'vcSince', 'function orSyncVoiceView(',
                    'html.ts-portrait #ring,html.ts-portrait #powerOff{', 'html.ts-xz-covered #ring{z-index:9700}', 'html.ts-portrait #hud.on{',
                    'html.ts-portrait #tsVoicePill{top:50%;left:100%;width:max-content;white-space:nowrap;'):
            self.assertIn(sym, js)


class TestPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages = {'home': HOME_HTML.read_text('utf-8'), 'qq': QQ_HTML.read_text('utf-8'), 'mon': MON_HTML.read_text('utf-8')}

    def test_portrait_layers_and_shim(self):
        for name, h in self.pages.items():
            self.assertIn('@media (orientation: portrait)', h, name)
            self.assertIn('2026-11-20 Claude 竖版', h, name)
            for sym in ('window.__tsPortraitShim', "matchMedia('(orientation: portrait)')", 'function findScroller(',
                        "addEventListener('touchmove'", '>= 10) e.preventDefault();', 'clickBlockUntil = performance.now() + 380',
                        "window.parent.postMessage({ ts: 'touch', on }, PO)", 'function setRange(', "r.dispatchEvent(new Event('change', { bubbles: true }));"):
                self.assertIn(sym, h, name)

    def test_home_portrait_order(self):
        css = _layer(self.pages['home'], '2026-11-20 Claude 竖版')
        for sym in ('.top, .mid { display: contents; }', '#side { grid-column: 1; grid-row: 2;', '#calCard { grid-column: 2; grid-row: 2; }',
                    '#dock .qk { grid-row: 2; grid-column: span 2;', '.mp-grid { grid-template-columns: minmax(0, 1fr);',
                    '.wp-col { display: contents; }', '.cp-body { grid-template-columns: minmax(0, 1fr);', '.tp-run { flex-direction: column;',
                    '.modal.cd-edit, .modal.cd-edit.show { left: 16px;', '.modal.amb-lab { width: calc(100% - 32px);',
                    '#qs-brightness::after { content: "、方向"; }', '.tp-amb .ta-btns { grid-row: 6; }',
                    '.tr-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));', '.wp-head-r { display: contents; }',
                    '.wp-grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); grid-template-rows: 240px 218px 362px minmax(250px, 1fr);'):
            self.assertIn(sym, css)

    def test_music_and_server_keep_their_content(self):
        qq = _layer(self.pages['qq'], '2026-11-20 Claude 竖版')
        self.assertIn('body { grid-template-columns: 94px minmax(0, 1fr);', qq)
        self.assertIn('body:has(#viewSearch.active) .np .lyrics-stage { display: none; }', qq)
        mon = _layer(self.pages['mon'], '2026-11-20 Claude 竖版')
        self.assertIn('.network-strip .carriers { order: 6; display: flex;', mon)   # same data as landscape (phones hide it)
        self.assertIn('.grid { flex: 1; min-height: 0; overflow-y: auto;', mon)
        self.assertIn('grid-auto-rows: minmax(268px, 1fr)', mon)                   # all eight servers on one portrait screen
        self.assertIn('.netstat { display: grid; grid-template-columns: auto minmax(0, 1fr);', mon)
        self.assertIn('.modal, .isp-modal, .fleet-modal { width: 100%; max-height: calc(100% - 28px);', mon)
        self.assertIn('.fleet-modal .fl-links { order: 5; flex: 0 0 100%;', mon)
        for name, h in self.pages.items():
            self.assertIn('function findScroller(t, axis)', h, name)
            self.assertIn('const alt = findScroller(p.startEl, wantAxis);', h, name)
            self.assertIn('flushScroll(p);', h, name)
        self.assertIn('let resizeProgRaf = 0;', self.pages['qq'])

    def test_typography_in_new_layers(self):
        for name, h in self.pages.items():
            i = h.index('2026-11-20 Claude 竖版')
            css = h[i:h.index('</style>', i)]
            self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', css)) <= {'400', '700'}, name)
            self.assertEqual(re.findall(r'\d+\.\d+px', css), [], name)
            sizes = [int(v) for v in re.findall(r'font-size:\s*(\d+)px', css)]
            self.assertTrue(not sizes or min(sizes) >= 15, f'{name}: {sorted(sizes)[:4]}')


class TestDisplayAndTutor(unittest.TestCase):
    def test_display_orientation_row(self):
        h = DISPLAY_HTML.read_text('utf-8')
        for sym in ('<span class="th-lbl">屏幕方向</span>', 'data-orient-btn="auto"', 'data-orient-btn="portrait"', 'data-orient-btn="landscape"',
                    "parent.postMessage({thinksmart:'setOrient',mode:m}", "e.data?.ts==='orient'"):
            self.assertIn(sym, h)
        self.assertLess(h.index('屏幕方向'), h.index('<p id="note">'))
        self.assertIn("slider.addEventListener('touchmove'", h)
        self.assertIn('tellParentTouch(true)', h)

    def test_tutor_follows_overlay_and_keeps_taps(self):
        h = TUTOR_HTML.read_text('utf-8')
        for sym in ('let orientFromParent = false', "if (d.ts !== 'orient') return;", 'if (orientFromParent) return;',
                    "postMessage({ ts: 'sub', cmd: 'orientMode', mode: m }", 'if (orientFromParent) askParentOrient(v);',
                    'if (pScroll && pScroll.active) { e.preventDefault(); return; }', 'tellTouch(true)'):
            self.assertIn(sym, h)
        self.assertNotIn("document.addEventListener('touchmove', e => { if (isPortraitUi() && e.cancelable) e.preventDefault(); }", h)
        for sym in ('const activePtrs = new Map();', 'function findScroller(t, axis)', 'let qMarkRaf = 0;', 'let camDetectTimer = 0;',
                    'm.maxScroll = el.scrollHeight - el.clientHeight;', 'html.portrait-ui .tskb-dock', 'html.portrait-ui.rot-270 .tskb-dock',
                    'function ensureScratchBase(', 'function syncTutorBgTimers('):
            self.assertIn(sym, h)


if __name__ == '__main__':
    unittest.main()
