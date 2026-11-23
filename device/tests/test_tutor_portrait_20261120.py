#!/opt/thinksmart-addons/venv/bin/python
"""Claude 2026-11-20: AI 讲题 portrait redesign + gravity-sensor auto rotation.
- GET /api/tutor/orient returns the BMA253 raw x/y/z (found by file, not by a fixed iio:device number), sensor:false without one
- index.html keeps Antigravity's R58 symbols and has the auto-rotation, both portrait turns, finger scrolling, 点读, 划线 and settings
TUTOR_PY / TUTOR_SHARE may point at staged copies."""
import importlib.util
import os
import pathlib
import re
import sys
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

TUTOR_PY = os.environ.get('TUTOR_PY', '/usr/local/bin/thinksmart-tutor.py')
SHARE = pathlib.Path(os.environ.get('TUTOR_SHARE', '/usr/local/share/thinksmart-tutor'))


def _load():
    spec = importlib.util.spec_from_file_location('ts_tutor_portrait_test', TUTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['ts_tutor_portrait_test'] = mod
    spec.loader.exec_module(mod)
    return mod


tutor = _load()


class TestOrientEndpoint(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._td.name)
        self._saved = {k: getattr(tutor, k) for k in ('STATE_DIR', 'SNAP_DIR', 'HIST_PATH', 'PREFS_PATH', 'HUB_CFG_PATH')}
        tutor.STATE_DIR = root / 'state'
        tutor.SNAP_DIR = tutor.STATE_DIR / 'snaps'
        tutor.HIST_PATH = tutor.STATE_DIR / 'history.json'
        tutor.PREFS_PATH = tutor.STATE_DIR / 'prefs.json'
        tutor.HUB_CFG_PATH = root / 'no-hub.yaml'
        self.iio = root / 'iio'
        self.dev = self.iio / 'iio:device3'
        self.dev.mkdir(parents=True)
        (self.iio / 'iio:device0').mkdir()                     # an ADC without accelerometer files comes first
        (self.dev / 'name').write_text('bma253\n')
        self._write(3, -497, 165)
        self._old_env = os.environ.get('TUTOR_IIO_DIR')
        os.environ['TUTOR_IIO_DIR'] = str(self.iio)
        tutor._ACCEL_CACHE.update(dir=None, checked=0.0)
        self.eng = tutor.TutorEngine()
        return tutor.make_app(self.eng)

    def _write(self, x, y, z):
        for a, v in (('x', x), ('y', y), ('z', z)):
            (self.dev / f'in_accel_{a}_raw').write_text(f'{v}\n')

    async def asyncTearDown(self):
        await super().asyncTearDown()
        for k, v in self._saved.items():
            setattr(tutor, k, v)
        if self._old_env is None:
            os.environ.pop('TUTOR_IIO_DIR', None)
        else:
            os.environ['TUTOR_IIO_DIR'] = self._old_env
        tutor._ACCEL_CACHE.update(dir=None, checked=0.0)
        self._td.cleanup()

    async def test_orient_reads_raw_values_and_reports_missing_sensor(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}
        r = await self.client.get('/api/tutor/orient', headers=H)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get('Cache-Control'), 'no-store')
        j = await r.json()
        self.assertEqual((j['sensor'], j['x'], j['y'], j['z']), (True, 3, -497, 165))
        self._write(500, 4, 160)                                 # laid back to landscape
        j = await (await self.client.get('/api/tutor/orient', headers=H)).json()
        self.assertEqual((j['x'], j['y']), (500, 4))
        for f in self.dev.glob('in_accel_*'):                   # sensor gone (driver unloaded)
            f.unlink()
        j = await (await self.client.get('/api/tutor/orient', headers=H)).json()
        self.assertEqual(j, {'ok': True, 'sensor': False})
        r = await self.client.get('/api/tutor/orient', headers={'Host': 'evil.example:80'})
        self.assertEqual(r.status, 403)                          # the service's own host guard still applies


class TestPortraitPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (SHARE / 'index.html').read_text('utf-8')
        cls.css = cls.html[cls.html.index('<style>'):cls.html.index('</style>')]

    def test_r58_symbols_kept(self):
        for sym in ('portrait-ui', 'toggleTutorOrientation', 'getLocalMetrics', 'eventToLocalPx'):
            self.assertIn(sym, self.html)

    def test_auto_rotation_both_turns(self):
        h = self.html
        for sym in ("localStorage.getItem('ts.tutorOrientMode')", "fetch('/api/tutor/orient'", 'function classifyAccel(',
                    'function applyOrient(', 'function wantedOrient(', "classList.toggle('rot-270'"):
            self.assertIn(sym, h)
        self.assertIn('transform: translateX(100vw) rotate(90deg);', self.css)
        self.assertIn('html.portrait-ui.rot-270 .page { transform: translateY(100vh) rotate(-90deg); }', self.css)
        self.assertIn('{ x: m.rect.bottom - e.clientY, y: e.clientX - m.rect.left }', h)      # rot-270 touch mapping
        self.assertIn('{ x: e.clientY - m.rect.top, y: m.rect.right - e.clientX }', h)        # rot-90 touch mapping
        self.assertIn('if (Math.max(ax, ay) < 220) return', h)                                 # lying flat: keep
        self.assertIn('if (orientCandN >= 2)', h)                                              # debounce

    def test_portrait_interactions(self):
        h = self.html
        for sym in ('function pageDelta(', 'function findScroller(', "document.addEventListener('touchmove'",
                    'window.readQuestionAloud', 'window.readSubtitleAloud', 'function ddSpeak(', '/api/tutor/tts_hint?text=',
                    'window.clearQuestionMarks', 'document.caretRangeFromPoint', 'mark.qhl', 'window.openTutorSettings',
                    'id="tutorSetSheet"', 'id="btnTutorSet"', 'id="btnQRead"', 'id="btnQErase"', 'quiz-turn'):
            self.assertIn(sym, h)

    def test_round2_ease_of_use(self):
        h = self.html
        for sym in ('function detectCamBlocks(', 'window.selectCamBlock', 'function mergeAnswerLines(', 'id="camCandLayer"',
                    'window.goCamNextQuestion', "goCamNextQuestion()\">${ico('frame')}拍下一题", 'function addErrorCard(',
                    'function syncDockStates(', 'function ddResumeLesson(', 'function addMoreHint(', "addMoreHint('notesList'",
                    '#viewPhone:has(#phoneStage:not(.live)) .ph-btn-row', '框这题'):
            self.assertIn(sym, h)
        self.assertIn('const fromCamLesson = ', h)                      # camera lessons end with 「拍下一题」
        self.assertIn('detectWorksheetQuestionBlocks(cv, [])', h)       # same detector as the phone page

    def test_typography_rules(self):
        self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', self.css)) <= {'400', '700'})
        self.assertEqual(re.findall(r'\d+\.\d+px', self.css), [])
        block = self.css[self.css.index('2026-11-20 Claude 竖版重新设计'):]
        sizes = [int(v) for v in re.findall(r'font-size:\s*(\d+)px', block)]
        self.assertTrue(sizes and min(sizes) >= 14, f'portrait text under 14px: {sorted(sizes)[:5]}')


if __name__ == '__main__':
    unittest.main()
