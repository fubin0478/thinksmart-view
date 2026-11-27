#!/opt/thinksmart-addons/venv/bin/python
"""Claude 2026-11-24 round-6 (second deep check) regression tests.

1. 「工作日」重复提醒按国家放假安排：法定假日跳过，调休上班的周末照响，长假后能找到下一个工作日。
2. 音乐：U 盘拔掉后当前曲目标 missing；列表里一首能放的都没有时「继续播放」直接说明白，不改播放状态；
   列表里还有能放的歌时照常继续。主页状态把 missing 带给页面。
3. 两个页面在 missing 时写「U盘已拔出」。
No service is touched: the modules are loaded from files, engines are built without __init__, playback is stubbed.
"""
import ast
import datetime as dt
import importlib.util
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

E = os.environ.get
HOME_PY = E('HOME_PY', '/usr/local/bin/thinksmart-home.py')
QQ_PY = E('QQ_PY', '/usr/local/bin/thinksmart-qqmusic.py')
HOME_HTML = pathlib.Path(E('HOME_HTML', '/usr/local/share/thinksmart-home/index.html'))
QQ_HTML = pathlib.Path(E('QQ_HTML', '/usr/local/share/thinksmart-qqmusic/index.html'))


def _load_home():
    spec = importlib.util.spec_from_file_location('ts_home_r6', HOME_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_qq_no_main(path):
    src = pathlib.Path(path).read_text('utf-8')
    tree = ast.parse(src)
    tree.body = [
        n for n in tree.body
        if not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'engine' for t in n.targets))
        and not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and getattr(n.test.left, 'id', '') == '__name__')
    ]
    mod = types.ModuleType('ts_qq_r6_test')
    mod.__file__ = path
    exec(compile(tree, path, 'exec'), mod.__dict__)
    return mod


home = _load_home()
qq = _load_qq_no_main(QQ_PY)


def _ts(y, m, d, hh=7, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=home.LOCAL_TZ).timestamp()


def _day(ts):
    return dt.datetime.fromtimestamp(ts, home.LOCAL_TZ).strftime('%Y-%m-%d %H:%M')


class WorkdayReminderTests(unittest.TestCase):
    def setUp(self):
        if home.HolidayUtil is None:
            self.skipTest('lunar_python not available')

    def test_holiday_and_makeup_days(self):
        self.assertFalse(home._is_workday(dt.date(2027, 1, 1)))      # 元旦（周五）放假
        self.assertTrue(home._is_workday(dt.date(2027, 1, 31)))      # 周日，春节调休上班
        self.assertFalse(home._is_workday(dt.date(2027, 2, 8)))      # 春节假期中的周一
        self.assertTrue(home._is_workday(dt.date(2027, 10, 9)))      # 周六，国庆调休上班
        self.assertFalse(home._is_workday(dt.date(2027, 10, 1)))
        self.assertTrue(home._is_workday(dt.date(2026, 11, 24)))     # 普通周二
        self.assertFalse(home._is_workday(dt.date(2026, 11, 28)))    # 普通周六

    def test_next_workday_skips_holidays(self):
        self.assertEqual(_day(home.remind_next(_ts(2026, 12, 31), 'workdays')), '2027-01-04 07:00')
        self.assertEqual(_day(home.remind_next(_ts(2027, 1, 29), 'workdays')), '2027-01-31 07:00')
        # 春节 2/5–2/13 放假 9 天，下一次是 2/14（周日调休上班）：原来只往后找 7 天会找不到，提醒就断了
        self.assertEqual(_day(home.remind_next(_ts(2027, 2, 4, 6, 50), 'workdays')), '2027-02-14 06:50')
        self.assertEqual(_day(home.remind_next(_ts(2027, 2, 14), 'workdays')), '2027-02-15 07:00')
        self.assertEqual(_day(home.remind_next(_ts(2026, 11, 27), 'workdays')), '2026-11-30 07:00')
        self.assertEqual(_day(home.remind_next(_ts(2027, 9, 30), 'workdays')), '2027-10-08 07:00')

    def test_daily_and_weekly_unchanged(self):
        self.assertEqual(_day(home.remind_next(_ts(2027, 2, 4), 'daily')), '2027-02-05 07:00')
        self.assertEqual(_day(home.remind_next(_ts(2027, 2, 4), 'weekly', [1])), '2027-02-08 07:00')
        self.assertEqual(_day(home.remind_next(_ts(2027, 2, 4), 'weekly', [4])), '2027-02-11 07:00')
        self.assertIsNone(home.remind_next(_ts(2027, 2, 4), 'weekly', []))

    def test_repeat_hits(self):
        d = dt.datetime(2027, 1, 1, 7, 0, tzinfo=home.LOCAL_TZ)
        self.assertFalse(home._repeat_hits(d, 'workdays', None))
        self.assertTrue(home._repeat_hits(d, 'daily', None))
        self.assertTrue(home._repeat_hits(d, 'weekly', [5]))


class HomeMusicPublicTests(unittest.TestCase):
    def test_missing_passed_through(self):
        st = {'playing': True, 'paused': True, 'track': {'title': 'a', 'artist': 'U盘', 'missing': True}}
        self.assertIs(home._music_public(st)['track']['missing'], True)
        st['track'].pop('missing')
        self.assertIs(home._music_public(st)['track']['missing'], False)
        st['track']['missing'] = 'yes'
        self.assertIs(home._music_public(st)['track']['missing'], False)


def _usb(n):
    return {'title': f'u{n}', 'artist': 'U盘', 'url': f'/mnt/usb/{n:02d}. song.flac', 'is_local': True, 'duration': 200}


def _remote(n):
    return {'title': f'q{n}', 'artist': 'x', 'mid': f'mid{n}', 'url': None, 'is_local': False, 'duration': 200}


class QQTrackGoneTests(unittest.TestCase):
    def test_usb_mounted_matches_proc(self):
        with open('/proc/self/mounts', encoding='utf-8') as f:
            want = any(line.split(' ')[1] == '/mnt/usb' for line in f)
        self.assertEqual(qq._usb_mounted(), want)

    def test_local_track_gone(self):
        self.assertFalse(qq._local_track_gone(None))
        self.assertFalse(qq._local_track_gone(_remote(1)))
        self.assertTrue(qq._local_track_gone(_usb(1), usb_on=False))
        self.assertFalse(qq._local_track_gone(_usb(1), usb_on=True))
        self.assertTrue(qq._local_track_gone({'is_local': True, 'url': '/home/ubuntu/Music/usb/a.flac'}, usb_on=False))
        with tempfile.NamedTemporaryFile(suffix='.flac') as f:
            self.assertFalse(qq._local_track_gone({'is_local': True, 'url': f.name}))
        self.assertTrue(qq._local_track_gone({'is_local': True, 'url': '/home/ubuntu/Music/__r6_no_such__.flac'}))
        self.assertFalse(qq._local_track_gone({'is_local': True, 'url': ''}))


class QQResumeTests(unittest.TestCase):
    def setUp(self):
        self._orig_mounted = qq._usb_mounted
        qq._usb_mounted = lambda: False
        e = qq.QQMusicEngine.__new__(qq.QQMusicEngine)
        e._lock = threading.RLock()
        e.queue, e.index = [_usb(1), _usb(2)], 0
        e.ducked = False
        e.playing, e.paused, e.just_started = True, True, False
        e._proc, e._gen, e._starting_gen, e._fail_streak = None, 0, -1, 0
        e.elapsed_base, e.play_started_mono = 2.5, 0.0
        e.ambient_preset = e.ambient_layer = None
        e.sleep_session, e.sleep_mode, e._preview = None, None, None
        e.started = []
        e._clear_duck_except_home_locked = lambda: None
        e._stop_proc_locked = lambda: False
        e._start_current_track = lambda sec, gen: e.started.append((sec, gen))
        e._send_aec_flush = lambda: None
        e._sleep_session_end = lambda why: None
        e._pause_bluetooth_if_streaming = lambda: None
        e.save_session = lambda *a, **k: None
        self.e = e

    def tearDown(self):
        qq._usb_mounted = self._orig_mounted

    def test_all_usb_tracks_gone(self):
        r = self.e.control('resume')
        self.assertFalse(r['ok'])
        self.assertIn('U盘已拔出', r['error'])
        self.assertEqual(r['message'], r['error'])
        self.assertTrue(self.e.paused)
        self.assertEqual(self.e.started, [])

    def test_voice_word_play_same(self):
        self.assertFalse(self.e.control('继续')['ok'])

    def test_something_still_playable(self):
        self.e.queue = [_usb(1), _remote(2)]
        r = self.e.control('resume')
        self.assertTrue(r['ok'])
        self.assertFalse(self.e.paused)
        for _ in range(50):
            if self.e.started:
                break
            threading.Event().wait(0.02)
        self.assertEqual(len(self.e.started), 1)

    def test_usb_back_in(self):
        qq._usb_mounted = lambda: True
        r = self.e.control('resume')
        self.assertTrue(r['ok'])

    def test_lite_status_flags_missing(self):
        e = self.e
        e.alarm_ringing = False
        e._ambient_public_locked = lambda: None
        e.open_ui_seq, e.study_duck_until, e.boot_id = 0, 0.0, 1
        e.volume, e.play_mode, e.lyrics_seq = 50, 'loop', 1
        e._sleep_public_locked = e._sleep_session_public_locked = e._preview_public_locked = lambda: None
        st = e.status(lite=True)
        self.assertIs(st['track']['missing'], True)
        e.queue = [_remote(1)]
        self.assertIs(e.status(lite=True)['track']['missing'], False)


class PageTests(unittest.TestCase):
    def test_home_card(self):
        s = HOME_HTML.read_text('utf-8')
        self.assertIn("t && t.missing ? 'U盘已拔出'", s)

    def test_music_page(self):
        s = QQ_HTML.read_text('utf-8')
        self.assertIn("st.track.missing ? 'U盘已拔出 · 插回 U 盘或换一首'", s)


if __name__ == '__main__':
    unittest.main(verbosity=1)
