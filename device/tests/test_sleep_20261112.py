#!/opt/thinksmart-addons/venv/bin/python
"""2026-11-12 (Claude) sleep sessions in thinksmart-qqmusic.py: timer variants, long fade, all-night mode, own volume and
its return, sound names. Never touches live state or plays anything: engines are built without __init__, playback and
volume calls are stubbed, the state file lives in a temporary directory.
    /opt/thinksmart-addons/venv/bin/python -B /opt/thinksmart-addons/test_sleep_20261112.py
SLEEP_ROOT=<dir> tests the copy in <dir> instead of the installed file."""
import ast
import json
import math
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest

ROOT = os.environ.get('SLEEP_ROOT')
SRC = pathlib.Path(ROOT, 'thinksmart-qqmusic.py') if ROOT else pathlib.Path('/usr/local/bin/thinksmart-qqmusic.py')
sys.path.insert(0, '/opt/thinksmart-addons/vendor')


def load():
    t = ast.parse(SRC.read_text(encoding='utf-8'))
    t.body = [n for n in t.body if not (isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == 'engine' for x in n.targets))]
    t.body = [n for n in t.body if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and getattr(n.test.left, 'id', '') == '__name__')]
    m = types.ModuleType('qq_sleep_test')
    m.__file__ = str(SRC)
    exec(compile(t, str(SRC), 'exec'), m.__dict__)
    return m


Q = load()
TMP = tempfile.TemporaryDirectory()
Q.SLEEP_STATE_PATH = pathlib.Path(TMP.name, 'sleep.json')
Q.BACKLIGHT_PATH = pathlib.Path(TMP.name, 'backlight')
Q.AMBIENT_DIR = pathlib.Path(TMP.name, 'ambient')


def engine(volume=75):
    e = object.__new__(Q.QQMusicEngine)
    e._lock = threading.Lock()
    e._aec_lock = threading.Lock()
    e.aec_clients = set()
    e.queue, e.index, e.playing, e.paused, e.elapsed_base = [], 0, False, False, 0.0
    e.play_started_mono, e._gen, e._fail_streak, e.play_mode = 0.0, 1, 0, 'loop'
    e.lyrics, e.lyrics_seq, e._just_started_mono, e.ducked, e.alarm_ringing = [], 1, None, False, False
    e.sleep_mode, e.sleep_deadline, e.sleep_total, e.sleep_tracks_left = None, 0.0, 0, 0
    e.sleep_fade, e.sleep_night, e.sleep_started_mono, e.sleep_ends_wall = e.SLEEP_FADE_SECONDS, False, 0.0, None
    e._sg_last, e.sleep_session, e._sleep_idle_since = (1.0, 0.0), None, None
    e._sleep_state = {}
    e._preview = None
    e.ambient_preset = None
    e.ambient_layer = None
    e.volume = volume
    e.calls = []

    def set_volume(vol=None, delta=None):
        e.calls.append(('volume', vol))
        e.volume = vol
        return vol

    def set_ambient(preset, volume=None, layer=None, layer_gain=None):
        e.calls.append(('ambient', preset, layer, layer_gain))
        e.ambient_preset = None if preset == 'off' else preset
        return {'ok': True, 'ambient': {'preset': preset}}

    def control(action, *a, **k):
        e.calls.append(('control', action))
        if action == 'pause':
            e.paused = True
        return {'ok': True}

    e.set_volume, e.set_ambient, e.control = set_volume, set_ambient, control
    e.play_tracks = lambda tracks, idx=0: e.calls.append(('tracks', len(tracks))) or {'ok': True}
    return e


class TimeHelpers(unittest.TestCase):
    def test_seconds_until(self):
        base = time.mktime((2026, 11, 12, 22, 0, 0, 0, 0, -1))
        self.assertAlmostEqual(Q._seconds_until('23:30', base), 5400, delta=1)
        self.assertAlmostEqual(Q._seconds_until('7:00', base), 9 * 3600, delta=1)       # tomorrow morning
        self.assertAlmostEqual(Q._seconds_until('7点', base), 9 * 3600, delta=1)
        self.assertAlmostEqual(Q._seconds_until('22:00', base), 24 * 3600, delta=1)     # now -> tomorrow
        for bad in ('25:00', 'abc', '', None, '12:61'):
            self.assertIsNone(Q._seconds_until(bad, base))

    def test_night_and_restore_time(self):
        # Product spec 11-12 evening: the night is 00:00-06:30 every day (was 20:00-07:00)
        day = time.mktime((2026, 11, 12, 14, 0, 0, 0, 0, -1))
        evening = time.mktime((2026, 11, 12, 23, 10, 0, 0, 0, -1))
        night = time.mktime((2026, 11, 13, 1, 0, 0, 0, 0, -1))
        early = time.mktime((2026, 11, 13, 6, 29, 0, 0, 0, -1))
        morning = time.mktime((2026, 11, 13, 6, 30, 0, 0, 0, -1))
        self.assertFalse(Q._is_sleep_night(day))
        self.assertFalse(Q._is_sleep_night(evening))
        self.assertTrue(Q._is_sleep_night(night))
        self.assertTrue(Q._is_sleep_night(early))
        self.assertFalse(Q._is_sleep_night(morning))
        self.assertTrue(Q._is_sleep_evening(evening))
        self.assertFalse(Q._is_sleep_evening(day))
        self.assertAlmostEqual(Q._next_sleep_restore_at(day), day + 3, delta=0.5)
        self.assertAlmostEqual(Q._next_sleep_restore_at(evening), evening + 3, delta=0.5)   # before midnight: right away
        self.assertEqual(time.localtime(Q._next_sleep_restore_at(night))[2:5], (13, 6, 30))
        self.assertEqual(time.localtime(Q._next_sleep_restore_at(early))[2:5], (13, 6, 30))


class TimerTests(unittest.TestCase):
    def test_default_timer_unchanged(self):
        e = engine()
        r = e.set_sleep(minutes=30)
        self.assertTrue(r['ok'])
        self.assertEqual((r['sleep']['mode'], r['sleep']['fade'], r['sleep']['night']), ('time', 20, False))
        e.sleep_deadline = time.monotonic() + 10
        self.assertAlmostEqual(e._sleep_gain_locked(), 0.25, delta=0.03)

    def test_long_fade_is_even_in_loudness_and_capped(self):
        e = engine()
        e.set_sleep(minutes=45, fade=600)
        self.assertEqual(e.sleep_fade, 600)
        self.assertEqual(e._sleep_gain_locked(), 1.0)
        e.sleep_deadline = time.monotonic() + 300          # halfway through the fade: about -22 dB
        self.assertAlmostEqual(20 * math.log10(e._sleep_gain_locked()), -22.5, delta=0.5)
        e.sleep_deadline = time.monotonic() + 30
        self.assertLess(20 * math.log10(e._sleep_gain_locked()), -40)
        e2 = engine()
        e2.set_sleep(minutes=15, fade=600)                  # at most a third of the timer
        self.assertAlmostEqual(e2.sleep_fade, 300, delta=0.1)

    def test_gain_never_jumps_up(self):
        e = engine()
        e.set_sleep(minutes=45, fade=600)
        e.sleep_deadline = time.monotonic() + 30
        low = e._sleep_gain_locked()
        e.sleep_deadline = time.monotonic() + 3600           # a longer timer while fading
        first = e._sleep_gain_locked()
        self.assertLess(first, low * 2.01)                  # at most +6 dB in the first second
        e._sg_last = (e._sg_last[0], time.monotonic() - 1.0)
        self.assertLess(e._sleep_gain_locked(), first * 2.01)
        for _ in range(12):
            e._sg_last = (e._sg_last[0], time.monotonic() - 1.0)
            g = e._sleep_gain_locked()
        self.assertAlmostEqual(g, 1.0, places=3)

    def test_until_and_all_night(self):
        e = engine()
        hhmm = time.strftime('%H:%M', time.localtime(time.time() + 1800 + 30))
        r = e.set_sleep(until=hhmm, fade=600)
        self.assertTrue(r['ok'], r)
        self.assertIn(r['sleep']['total'], (30, 31))
        self.assertIn(hhmm, r['message'])
        self.assertFalse(e.set_sleep(until='9:99')['ok'])
        n = engine()
        r = n.set_sleep(night=True)
        self.assertTrue(r['ok'])
        self.assertTrue(r['sleep']['night'])
        self.assertEqual(n.sleep_fade, 180)
        left = n.sleep_deadline - time.monotonic()
        want = Q._seconds_until('%02d:%02d' % Q.SLEEP_MORNING)   # 06:30
        self.assertAlmostEqual(left, want if want <= 12 * 3600 else 3 * 3600, delta=2)
        n.sleep_started_mono = time.monotonic() - Q.SLEEP_NIGHT_SETTLE - Q.SLEEP_NIGHT_RAMP - 5
        self.assertAlmostEqual(20 * math.log10(n._sleep_gain_locked()), Q.SLEEP_NIGHT_DB, delta=0.3)
        n.sleep_started_mono = time.monotonic() - 60
        n._sg_last = (1.0, time.monotonic())
        self.assertEqual(n._sleep_gain_locked(), 1.0)


class SessionTests(unittest.TestCase):
    def setUp(self):
        if Q.SLEEP_STATE_PATH.exists():
            Q.SLEEP_STATE_PATH.unlink()

    def test_start_sets_volume_before_sound_and_remembers(self):
        e = engine(volume=75)
        r = e.sleep_session_start({'name': '海浪'}, 45, None, False, None, None)
        self.assertTrue(r['ok'], r)
        self.assertEqual(e.calls[0], ('volume', 25))
        self.assertEqual(e.calls[1][:2], ('ambient', 'waves'))
        self.assertEqual(r['session']['label'], '海岸慢浪')
        self.assertEqual((e.sleep_mode, e.sleep_fade), ('time', 600))
        st = json.loads(Q.SLEEP_STATE_PATH.read_text())
        self.assertEqual(st['restore'], {'day_volume': 75, 'sleep_volume': 25, 'at': None})
        self.assertEqual(st['last']['sound']['preset'], 'waves')
        self.assertEqual(oct(Q.SLEEP_STATE_PATH.stat().st_mode & 0o777), '0o600')
        # the last choice is used when nothing is named
        e2 = engine(volume=60)
        e2._sleep_state = json.loads(Q.SLEEP_STATE_PATH.read_text())
        r2 = e2.sleep_session_start('last', None, None, False, None, None)
        self.assertTrue(r2['ok'])
        self.assertEqual(e2.calls[1][:2], ('ambient', 'waves'))
        self.assertEqual(e2._sleep_state['restore']['day_volume'], 75)   # a pending day volume is kept

    def test_stop_fades_then_ends_and_day_volume_returns(self):
        e = engine(volume=70)
        e.sleep_session_start({'name': '褐噪音'}, 30, None, False, None, 20)
        self.assertEqual(e.ambient_preset, 'brown_noise')
        e.sleep_session_stop()
        self.assertEqual(e.sleep_mode, 'time')
        self.assertLess(e.sleep_deadline - time.monotonic(), 3)
        e.sleep_deadline = time.monotonic() - 0.1
        self.assertTrue(e.sleep_tick())
        self.assertIsNone(e.ambient_preset)
        self.assertIsNone(e.sleep_session)
        r = e._sleep_state['restore']
        self.assertIsNotNone(r['at'])
        r['at'] = time.time() - 1
        e.sleep_housekeeping()
        self.assertEqual(e.volume, 70)
        self.assertIsNone(e._sleep_state['restore'])

    def test_volume_changed_during_the_session_still_returns(self):
        e = engine(volume=80)
        e.sleep_session_start({'name': '雨声'}, 30, None, False, None, 30)
        e.volume = 20                                   # "小声" from the bed: the morning restore follows it
        e.sleep_housekeeping()
        self.assertEqual(e._sleep_state['restore']['sleep_volume'], 20)
        e.ambient_preset = None
        e._sleep_idle_since = time.monotonic() - 9      # the sound was stopped another way
        e.sleep_housekeeping()
        self.assertIsNone(e.sleep_session)
        e._sleep_state['restore']['at'] = time.time() - 1
        e.sleep_housekeeping()
        self.assertEqual(e.volume, 80)

    def test_changed_after_the_session_is_left_alone(self):
        e = engine(volume=80)
        e.sleep_session_start({'name': '雨声'}, 30, None, False, None, 30)
        e.ambient_preset = None
        e._sleep_idle_since = time.monotonic() - 9
        e.sleep_housekeeping()
        self.assertIsNone(e.sleep_session)
        e.volume = 40                                   # someone turned it up in the night, after the session
        e._sleep_state['restore']['at'] = time.time() - 1
        e.sleep_housekeeping()
        self.assertEqual(e.volume, 40)
        self.assertIsNone(e._sleep_state['restore'])

    def test_no_race_with_a_session_being_started(self):
        e = engine(volume=75)
        e._sleep_state = {'restore': {'day_volume': 75, 'sleep_volume': 25, 'at': None}}
        e.sleep_housekeeping()                          # first call after a start: a lost session gets its time
        self.assertIsNotNone(e._sleep_state['restore']['at'])
        e._sleep_state = {'restore': {'day_volume': 75, 'sleep_volume': 25, 'at': None}}   # a session being started
        e.sleep_housekeeping()                          # later calls leave it to the session
        self.assertIsNone(e._sleep_state['restore']['at'])
        e.sleep_session_start({'name': '雨声'}, 30, None, False, None, 25)
        e.volume = 35                                   # 大声 twice in the night view
        e.sleep_housekeeping()
        e.sleep_session_stop()
        e.sleep_deadline = time.monotonic() - 0.1
        e.sleep_tick()
        e._sleep_state['restore']['at'] = time.time() - 1
        e.sleep_housekeeping()
        self.assertEqual(e.volume, 75)

    def test_extend_and_playlist_and_errors(self):
        e = engine()
        e.sleep_session_start({'name': '海浪'}, 30, None, False, None, None)
        before = e.sleep_deadline
        r = e.sleep_session_extend(30)
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(e.sleep_deadline - before, 1800, delta=2)
        self.assertFalse(engine().sleep_session_start({'name': '不存在的声音'}, 30, None, False, None, None)['ok'])
        self.assertFalse(engine().sleep_session_start({'kind': 'current'}, 30, None, False, None, None)['ok'])
        p = engine()
        Q.qq_playlist_tracks = lambda pid, limit=160: [{'mid': f'm{i}', 'title': str(i)} for i in range(10)]
        Q._qq_playable_tracks = lambda tr: tr[:4]
        r = p.sleep_session_start({'name': '钢琴'}, 30, None, False, None, None)
        self.assertTrue(r['ok'], r)
        self.assertIn(('tracks', 4), p.calls)
        self.assertEqual(r['session']['kind'], 'playlist')
        Q._qq_playable_tracks = lambda tr: []
        self.assertFalse(engine().sleep_session_start({'name': '摇篮曲'}, 30, None, False, None, None)['ok'])

    def test_preview(self):
        e = engine(volume=70)
        e._sleep_state = {'sleep_volume': 25}
        self.assertFalse(e.sleep_preview('助眠钢琴')['ok'])            # playlists are not previewed
        r = e.sleep_preview('雨夜壁炉', None, 30)
        self.assertTrue(r['ok'], r)
        self.assertEqual((e.volume, e.ambient_preset), (25, 'rain'))
        self.assertEqual(r['preview']['label'], '雨夜壁炉')
        self.assertIsNone(e.sleep_status())                         # no sleep timer shown for a preview
        self.assertIsNone(e._sleep_state.get('restore'))           # the day-volume restore is not touched
        r2 = e.sleep_preview('褐噪细雨', 30, 30)                    # switching while previewing keeps the first volume to return to
        self.assertTrue(r2['ok'])
        self.assertEqual(e._preview['prev_volume'], 70)
        e.sleep_preview_stop()
        self.assertLess(e.sleep_deadline - time.monotonic(), 1.5)
        e.sleep_deadline = time.monotonic() - 0.1
        e.sleep_tick()
        self.assertIsNone(e.ambient_preset)
        self.assertIsNone(e._preview)
        self.assertEqual(e.volume, 70)

    def test_start_during_preview_keeps_the_day_volume(self):
        e = engine(volume=80)
        e._sleep_state = {'sleep_volume': 25}
        e.sleep_preview('深度入眠', 25, 30)
        self.assertEqual(e.volume, 25)
        r = e.sleep_session_start('深度入眠', 30, None, False, None, 25)
        self.assertTrue(r['ok'], r)
        self.assertIsNone(e._preview)
        self.assertEqual(e._sleep_state['restore']['day_volume'], 80)
        self.assertEqual(e.sleep_fade, 600)
        busy = engine()
        busy.sleep_session_start('雨声', 30, None, False, None, 25)
        self.assertFalse(busy.sleep_preview('海浪', None, 30)['ok'])  # not during a session

    def test_screen_off(self):
        e = engine()
        e.sleep_session_start({'name': '海浪'}, 30, None, False, None, None)
        self.assertTrue(e.sleep_screen_off()['ok'])
        self.assertEqual(Q.BACKLIGHT_PATH.read_text().strip(), '10')

    def test_audit_polish_default_cancel_voice_and_alarm(self):
        # 1. First-run 'last' with empty state picks paired '雨夜壁炉'
        e0 = engine(volume=70)
        r0 = e0.sleep_session_start('last', None, None, False, None, None)
        self.assertTrue(r0['ok'], r0)
        self.assertEqual(e0.calls[1], ('ambient', 'rain', 'fireplace', 0.4))
        self.assertEqual(r0['session']['label'], '雨夜壁炉')

        # 2. sleep_session_stop hides public session immediately during the 2.5s fade
        e0.sleep_session_stop()
        self.assertIsNone(e0._sleep_session_public_locked())
        self.assertTrue(e0.sleep_session.get('stopping'))

        # 3. set_sleep(cancel=True) cleanly ends an active sleep_session and schedules restore
        ec = engine(volume=72)
        ec.sleep_session_start('雨声', 30, None, False, None, 25)
        self.assertIsNotNone(ec.sleep_session)
        rc = ec.set_sleep(cancel=True)
        self.assertTrue(rc['ok'])
        self.assertIsNone(ec.sleep_session)
        self.assertIsNotNone(ec._sleep_state['restore']['at'])

        # 4. Bedtime voice stop phrases in play_query (active session vs ordinary ambient)
        ev = engine(volume=68)
        ev.sleep_session_start('雨夜壁炉', 30, None, False, None, 25)
        rv1 = ev.play_query('不睡了')
        self.assertTrue(rv1['ok'] and rv1.get('mode_only'))
        self.assertIsNone(ev._sleep_session_public_locked())

        ev2 = engine(volume=68)
        ev2.sleep_session_start('雨声', 30, None, False, None, 25)
        rv2 = ev2.play_query('关闭雨声')
        self.assertEqual(rv2.get('source'), '睡眠模式')
        self.assertIsNone(ev2._sleep_session_public_locked())

        ev3 = engine(volume=68)
        ev3.ambient_preset = 'rain'
        rv3 = ev3.play_query('关闭雨声')
        self.assertEqual(rv3.get('source'), '自然环境音')
        self.assertIsNone(ev3.ambient_preset)

        # 5. Wake-up alarm stops bedtime sound immediately, defers volume restore until ringing ends
        ea = engine(volume=76)
        ea.sleep_session_start('海浪', 60, None, False, None, 25)
        ea.alarm_ringing = True
        ea.sleep_housekeeping()
        self.assertIsNone(ea.ambient_preset)
        self.assertIsNone(ea.sleep_session)
        self.assertEqual(ea.volume, 25)  # untouched while XiaoZhi alarm holds speaker volume
        self.assertIsNotNone(ea._sleep_state['restore']['at'])
        ea.alarm_ringing = False
        ea.sleep_housekeeping()
        self.assertEqual(ea.volume, 76)
        self.assertIsNone(ea._sleep_state['restore'])


class NameTests(unittest.TestCase):
    def test_names(self):
        c = Q._clean_sleep_sound
        self.assertEqual(c('雨声')['preset'], 'rain')
        self.assertEqual(c('褐噪音')['preset'], 'brown_noise')
        self.assertEqual(c('白噪音')['preset'], 'white_noise')
        self.assertEqual(c('粉红噪音')['preset'], 'pink_noise')
        self.assertEqual(c('深度入眠'), {'kind': 'ambient', 'preset': 'waves', 'layer': 'singing_bowl', 'layer_gain': 0.35})  # the pair, same sound as the recipe
        self.assertEqual(c('deep_sleep')['preset'], 'deep_sleep')
        self.assertEqual(c('钢琴'), {'kind': 'playlist', 'id': Q.SLEEP_PLAYLISTS[0]['id']})
        self.assertEqual(c('摇篮曲')['id'], Q.SLEEP_PLAYLISTS[2]['id'])
        self.assertEqual(c({'kind': 'ambient', 'preset': 'waves', 'layer': 'singing_bowl', 'layer_gain': 0.35})['layer'], 'singing_bowl')
        self.assertIsNone(c('随便什么'))
        self.assertIsNone(c({'kind': 'playlist', 'id': '123'}))
        for p in ('white_noise', 'pink_noise', 'brown_noise'):
            self.assertIn(p, Q.AMBIENT_PRESETS)

    def test_combos(self):
        mains = set()
        for m, l, g, n in Q.SLEEP_COMBOS:
            self.assertIn(m, Q.AMBIENT_PRESETS)
            self.assertIn(l, Q.AMBIENT_PRESETS)
            self.assertNotEqual(m, l)
            self.assertTrue(0.2 <= g <= 0.5)
            self.assertNotIn(m, mains)                  # one matching layer per sound
            mains.add(m)
            self.assertEqual(Q._clean_sleep_sound(n), {'kind': 'ambient', 'preset': m, 'layer': l, 'layer_gain': g})
            self.assertEqual(Q._sleep_sound_label({'kind': 'ambient', 'preset': m, 'layer': l, 'layer_gain': 0.6}), n)
        self.assertEqual(Q._sleep_sound_label({'kind': 'ambient', 'preset': 'rain', 'layer': 'night', 'layer_gain': 0.3}), '窗外细雨 + 夏夜虫鸣')
        e = engine()
        r = e.sleep_session_start('雨夜壁炉', 30, None, False, None, None)
        self.assertTrue(r['ok'], r)
        self.assertEqual(e.calls[1], ('ambient', 'rain', 'fireplace', 0.4))
        self.assertEqual(r['session']['label'], '雨夜壁炉')
        self.assertIn('combos', e.sleep_session_info())

    def test_flac_first(self):
        Q.AMBIENT_DIR.mkdir(exist_ok=True)
        (Q.AMBIENT_DIR / 'rain.m4a').write_bytes(b'0' * 20000)
        self.assertEqual(Q._ambient_file_for('rain').suffix, '.m4a')
        (Q.AMBIENT_DIR / 'rain.flac').write_bytes(b'0' * 20000)
        self.assertEqual(Q._ambient_file_for('rain').suffix, '.flac')
        self.assertIsNone(Q._ambient_file_for('pink_noise'))


if __name__ == '__main__':
    unittest.main(verbosity=1)
