#!/opt/thinksmart-addons/venv/bin/python
"""2026-11-12 evening / night (Claude): Product UX refinements after review.
- night 00:00-06:30 every day (music service), the night view flag
- 温柔叫醒 (night round): the user's time, not on days off (calendar), volume 20% -> 50%, a touch / the switch stops it,
  a sleep sound still on makes way
- the screen owner (idle-blanker): lowest level when lit in the night / on the night view; the wake-up brightens from
  the screen's own level up to 50% and goes back to the user's level
- message board: latest 20 kept (reminders still to come kept apart), voice under 1 s refused, 「没听清」, repeating
  reminders, recall within 2 minutes, delete from the phone, no screen wake while a sleep session is on
Never touches live state, plays nothing, wakes nothing: every path is a temporary one, services are faked.
    /opt/thinksmart-addons/venv/bin/python -B /opt/thinksmart-addons/test_r2_20261112.py
R2_ROOT=<dir> tests the copies in <dir> instead of the installed files."""
import ast
import asyncio
import datetime as dt
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.environ.get('R2_ROOT')
sys.path.insert(0, '/opt/thinksmart-addons/vendor')
sys.path.insert(1, '/usr/local/bin')


def src(fname, installed):
    return pathlib.Path(ROOT, fname) if ROOT else pathlib.Path(installed)


def load(name, path, drop_engine=False):
    t = ast.parse(path.read_text(encoding='utf-8'))
    t.body = [n for n in t.body if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                                        and getattr(n.test.left, 'id', '') == '__name__')]
    if drop_engine:
        t.body = [n for n in t.body if not (isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == 'engine' for x in n.targets))]
    m = types.ModuleType(name)
    m.__file__ = str(path)
    exec(compile(t, str(path), 'exec'), m.__dict__)
    return m


TMP = tempfile.TemporaryDirectory()
os.environ['STATE_DIRECTORY'] = os.path.join(TMP.name, 'state')
H = load('home_r2', src('thinksmart-home.py', '/usr/local/bin/thinksmart-home.py'))
Q = load('qq_r2', src('thinksmart-qqmusic.py', '/usr/local/bin/thinksmart-qqmusic.py'), drop_engine=True)
Q.SLEEP_STATE_PATH = pathlib.Path(TMP.name, 'sleep.json')
Q.BACKLIGHT_PATH = pathlib.Path(TMP.name, 'backlight')
Q.NIGHT_VIEW_PATH = pathlib.Path(TMP.name, 'night-view')
Q.SUNRISE_PATH = pathlib.Path(TMP.name, 'sunrise.json')
Q.CALL_FLAG_PATH = pathlib.Path(TMP.name, 'call')
Q.MIRROR_FLAG_PATH = pathlib.Path(TMP.name, 'mirror')
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

HD = {'Host': '127.0.0.1:8095'}
HI = dict(HD, **{'X-TS-Internal': '1'})


def box(name):
    return H.Inbox(pathlib.Path(TMP.name, name, 'messages.json'))


def at(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


# ---------------------------------------------------------------------------------------------------- messages
class InboxR2Tests(unittest.TestCase):
    def test_keep_20_and_reminders_apart(self):
        b = box('k1')
        rem = b.add('带水杯', 'phone', '姐姐', '', time.time() + 3600)
        for i in range(25):
            b.recent_adds.clear()
            b.add(f'第{i}条', 'phone')
        texts = [m['text'] for m in b.items]
        self.assertIn('带水杯', texts)                     # never pushed out by newer messages
        self.assertEqual(len([m for m in b.items if not m.get('due')]), 20)
        self.assertEqual(texts[-1], '第24条')
        self.assertNotIn('第4条', texts)
        self.assertIn(rem['id'], [m['id'] for m in b.recent(5)])   # the screen list still shows it
        self.assertEqual(len(H.Inbox(b.path).items), 21)            # and so does a reload

    def test_parse_repeat_and_days(self):
        self.assertEqual(H.parse_repeat('每天'), ('daily', None))
        self.assertEqual(H.parse_repeat('workdays'), ('workdays', None))
        self.assertEqual(H.parse_repeat('weekly', '一三五'), ('weekly', [1, 3, 5]))
        self.assertEqual(H.parse_days('周一到周五'), [1, 2, 3, 4, 5])
        self.assertEqual(H.parse_days('周六日'), [6, 7])
        self.assertEqual(H.parse_days([1, 3, 9, 'x']), [1, 3])
        self.assertEqual(H.parse_repeat('once'), (None, None))
        self.assertEqual(H.repeat_label({'repeat': 'weekly', 'days': [1, 3]}), '每周一三')

    def test_repeating_reminders(self):
        b = box('r1')
        fri = at(2026, 11, 13, 7, 10)          # a Friday
        with mock.patch.object(H.time, 'time', return_value=fri - 3600):
            d = b.add('带水杯', 'phone', '姐姐', '', fri, 'workdays')
            w = b.add('带红领巾', 'phone', '妹妹', '', fri, 'weekly', [1])   # aligned to the Monday
        self.assertEqual(d['repeat'], 'workdays')
        self.assertEqual(dt.datetime.fromtimestamp(w['due']).isoweekday(), 1)
        self.assertEqual(w['days'], [1])
        fired = b.fire_due(fri + 5)
        self.assertEqual([m['text'] for m in fired], ['带水杯'])
        nxt = [m for m in b.pending() if m['text'] == '带水杯']
        self.assertEqual(len(nxt), 1)
        self.assertEqual(dt.datetime.fromtimestamp(nxt[0]['due']).strftime('%a %H:%M'), 'Mon 07:10')   # skips the weekend
        # a long time off: the next one is still in the future
        fired = b.fire_due(nxt[0]['due'] + 3 * 86400)
        later = [m for m in b.pending() if m['text'] == '带水杯'][0]
        self.assertGreater(later['due'], nxt[0]['due'] + 3 * 86400)
        # cancelling the one to come ends the series
        self.assertTrue(b.cancel_reminder(later['id']))
        self.assertFalse([m for m in b.pending() if m['text'] == '带水杯'])
        again = H.Inbox(b.path)
        self.assertEqual([m['repeat'] for m in again.pending()], ['weekly'])

    def test_remove_and_recall_rules(self):
        b = box('rm')
        m = b.add('发错了', 'screen')
        with self.assertRaises(ValueError):
            b.remove(m['id'], source='phone', max_age=120)
        b.items[-1]['at'] -= 300
        with self.assertRaises(ValueError):
            b.remove(m['id'], source='screen', max_age=120)
        self.assertEqual(b.remove(m['id'])['text'], '发错了')
        with self.assertRaises(LookupError):
            b.remove(m['id'])

    def test_no_speech_mark(self):
        b = box('ns')
        b.voice_dir.mkdir(parents=True, exist_ok=True)
        v = b.add_voice(b'\xff\xfb' + b'\0' * 300, 1.5, 'screen')
        self.assertTrue(b.set_transcript(v['id'], ''))
        self.assertTrue(b.recent(1)[0].get('no_speech'))
        self.assertTrue(H.Inbox(b.path).recent(1)[0].get('no_speech'))
        b.set_transcript(v['id'], '你好')
        self.assertNotIn('no_speech', b.recent(1)[0])

    def test_voice_under_one_second_refused(self):
        with self.assertRaises(ValueError) as cm:
            H._transcode_to_voice_mp3(b'\x10\x27' * 12000, True)    # 0.75 s of screen PCM
        self.assertIn('说话时间太短', str(cm.exception))
        import io, wave
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b'\x10\x27' * 12800)                       # 0.8 s from the phone
        with self.assertRaises(ValueError):
            H._transcode_to_voice_mp3(buf.getvalue(), False)


class HttpR2Tests(unittest.TestCase):
    def test_recall_delete_repeat_and_sleep_quiet(self):
        async def go():
            b = box('h1')
            woke, said, music = [], [], {'sleep_session': None}

            async def fake_post(session, url, body, timeout=5.0, headers=None):
                said.append((url, body))
                return 200, {'ok': True, 'url': url}

            async def fake_get(session, url, timeout=2.0, max_bytes=262144, etag_box=None):
                return {'playing': False, 'sleep_session': music['sleep_session']} if '/status' in url else None
            with mock.patch.object(H, '_post_json', fake_post), mock.patch.object(H, '_get_json', fake_get), \
                    mock.patch.object(H, 'send_preview', lambda body: woke.append(body)):
                client = TestClient(TestServer(H.make_app(b)))
                await client.start_server()
                try:
                    # a phone message lights the screen ... but not while a sleep session is on
                    r = await client.post('/api/home/messages', json={'text': '晚上好'}, headers=HI)
                    self.assertEqual(r.status, 200)
                    self.assertEqual(woke, [{'clear': True}])
                    music['sleep_session'] = {'label': '雨夜壁炉'}
                    await client.post('/api/home/messages', json={'text': '睡了吗'}, headers=HI)
                    self.assertEqual(len(woke), 1)
                    music['sleep_session'] = None
                    # a repeating reminder from the phone
                    due = time.time() + 600
                    r = await client.post('/api/home/messages', json={'text': '吃药', 'due': due, 'repeat': 'daily'}, headers=HI)
                    self.assertEqual((await r.json())['message']['repeat'], 'daily')
                    self.assertEqual((await client.post('/api/home/messages', json={'text': 'x', 'due': due, 'repeat': 5}, headers=HI)).status, 400)
                    # 小麦: weekly on Mondays
                    r = await client.post('/api/home/messages/tool', json={'action': 'remind', 'text': '带红领巾', 'time': '07:00',
                                                                         'repeat': 'weekly', 'weekdays': '一'}, headers=HI)
                    self.assertIn('每周一 7:00', (await r.json())['text'])
                    r = await client.post('/api/home/messages/tool', json={'action': 'reminders'}, headers=HI)
                    self.assertIn('每天', (await r.json())['text'])
                    # recall: the screen's own reply within 2 minutes, from the screen
                    rep_m = (await (await client.post('/api/home/messages/reply', json={'text': '收到啦'}, headers=HD)).json())['message']
                    self.assertEqual((await client.post('/api/home/messages/recall', json={'id': rep_m['id']}, headers=HD)).status, 200)
                    self.assertNotIn(rep_m['id'], [m['id'] for m in b.items])
                    # a phone message only from the phone (the relay's internal header), and not a reminder
                    ph = [m for m in b.items if m['text'] == '睡了吗'][0]
                    self.assertEqual((await client.post('/api/home/messages/recall', json={'id': ph['id']}, headers=HD)).status, 403)
                    self.assertEqual((await client.post('/api/home/messages/recall', json={'id': ph['id']}, headers=HI)).status, 200)
                    remd = [m for m in b.items if m['text'] == '吃药'][0]
                    self.assertEqual((await client.post('/api/home/messages/recall', json={'id': remd['id']}, headers=HI)).status, 400)
                    old = [m for m in b.items if m['text'] == '晚上好'][0]
                    old['at'] -= 600
                    r = await client.post('/api/home/messages/recall', json={'id': old['id']}, headers=HI)
                    self.assertIn('2 分钟', (await r.json())['error'])
                    # delete: only the phone, any message
                    self.assertEqual((await client.post('/api/home/messages/delete', json={'id': old['id']}, headers=HD)).status, 403)
                    self.assertEqual((await client.post('/api/home/messages/delete', json={'id': old['id']}, headers=HI)).status, 200)
                    self.assertEqual((await client.post('/api/home/messages/delete', json={'id': old['id']}, headers=HI)).status, 404)
                    # the sleep page's switch and night view go to the music service
                    r = await client.post('/api/home/sleepmode', json={'action': 'night_view', 'on': True}, headers=HD)
                    self.assertEqual(r.status, 200)
                    self.assertEqual(said[-1][1], {'action': 'night_view', 'on': True})
                    await client.post('/api/home/sleepmode', json={'action': 'wake', 'on': False}, headers=HD)
                    self.assertEqual(said[-1][1], {'action': 'wake', 'on': False})
                    self.assertEqual((await client.post('/api/home/sleepmode', json={'action': 'wake', 'on': 'yes'}, headers=HD)).status, 400)
                finally:
                    await client.close()
        asyncio.run(go())

    def test_reminder_quiet_in_sleep_mode(self):
        async def go(sleeping):
            b = box(f'h2{int(sleeping)}')
            woke, said = [], []

            async def fake_post(session, url, body, timeout=5.0, headers=None):
                said.append(body)
                return 200, {'ok': True}

            async def fake_get(session, url, timeout=2.0, max_bytes=262144, etag_box=None):
                return {'sleep_session': {'label': '海浪'} if sleeping else None}
            b.add('关窗', 'phone', '爸爸', '', time.time() + 60)
            b.items[-1]['due'] = time.time() - 1          # its time has come
            with mock.patch.object(H, '_post_json', fake_post), mock.patch.object(H, '_get_json', fake_get), \
                    mock.patch.object(H, 'send_preview', lambda body: woke.append(body)):
                app = H.make_app(b)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    await app[H.FIRE_REMINDERS]()
                finally:
                    await client.close()
            return woke, said, b
        woke, said, b = asyncio.run(go(True))
        self.assertEqual((woke, said), ([], []))          # sleep mode: no light, not read out
        self.assertEqual([m['text'] for m in b.unread()], ['关窗'])   # waits on the screen
        woke, said, _ = asyncio.run(go(False))
        self.assertEqual(woke, [{'clear': True}])
        self.assertIn('关窗', said[0]['text'])


# ---------------------------------------------------------------------------------------------- music service
def engine(volume=40):
    e = object.__new__(Q.QQMusicEngine)
    e._lock = threading.Lock()
    e.queue, e.index, e.playing, e.paused = [], 0, False, False
    e.alarm_ringing, e.ducked = False, False
    e.sleep_mode, e.sleep_deadline, e.sleep_total, e.sleep_tracks_left = None, 0.0, 0, 0
    e.sleep_fade, e.sleep_night, e.sleep_started_mono, e.sleep_ends_wall = e.SLEEP_FADE_SECONDS, False, 0.0, None
    e._sg_last, e.sleep_session, e._sleep_idle_since = (1.0, 0.0), None, None
    e._sleep_state, e._preview, e._wake = {}, None, None
    e.ambient_preset, e.ambient_layer, e.volume = None, None, volume
    e.calls = []

    def set_ambient(preset, volume=None, layer=None, layer_gain=None):
        e.calls.append(('ambient', preset, layer))
        e.ambient_preset = None if preset == 'off' else preset
        return {'ok': True}

    def set_volume(vol=None, delta=None):   # never the real one: it would change the device's volume
        e.calls.append(('volume', vol))
        e.volume = vol
        return vol

    def sleep_session_stop():
        e.calls.append(('stop',))
        if isinstance(e.sleep_session, dict):
            e.sleep_session['stopping'] = True
        return {'ok': True}
    e.set_ambient, e.set_volume, e.sleep_session_stop = set_ambient, set_volume, sleep_session_stop
    return e


def started(e, t):
    with mock.patch.object(Q.time, 'time', return_value=t), mock.patch.object(Q, '_write_sleep_state', lambda d: None):
        e.sleep_wake_tick()


class MusicR2Tests(unittest.TestCase):
    def setUp(self):
        for p in (Q.SUNRISE_PATH, Q.NIGHT_VIEW_PATH, Q.SLEEP_STATE_PATH):
            p.unlink(missing_ok=True)

    def test_night_window_and_all_night_end(self):
        self.assertEqual((Q.SLEEP_MORNING, Q.SLEEP_DAY_END), ((6, 30), (0, 0)))
        self.assertAlmostEqual(Q._seconds_until('06:30', at(2026, 11, 12, 21, 0)), 9.5 * 3600, delta=1)
        info = engine().sleep_session_info()
        self.assertEqual((info['morning'], info['night_from'], info['wake'], info['wake_time']), ('06:30', '00:00', True, '06:30'))
        self.assertEqual((info['wake_volume'], info['wake_screen_max']), ([20, 50], 50))

    def test_wake_on_a_workday(self):
        e = engine(volume=45)
        e._sleep_state = {'last_started': int(at(2026, 11, 12, 21, 30))}
        t0 = at(2026, 11, 13, 6, 30) + 5                       # a Friday
        started(e, t0)
        self.assertEqual(e.calls, [('volume', 20), ('ambient', 'forest', 'off')])
        sr = json.loads(Q.SUNRISE_PATH.read_text())
        self.assertEqual((sr['start'], sr['ramp'], sr['max']), (int(t0), 600, 50))
        self.assertEqual((e._wake['day_vol'], e._sleep_state['wake_done']), (45, '2026-11-13'))
        e._wake['start'] = time.monotonic() - 300              # half way: 35%
        e.sleep_wake_tick()
        self.assertEqual(e.calls[-1], ('volume', 35))
        e._wake['start'] = time.monotonic() - 700              # 50% and no further
        e.sleep_wake_tick()
        n = len(e.calls)
        e.sleep_wake_tick()
        self.assertEqual((e.volume, len(e.calls)), (50, n))
        e._wake['start'] = time.monotonic()                    # a soft start, then steady, a fade at the end
        e._sg_last = (0.003, time.monotonic())
        self.assertLess(e._sleep_gain_locked(), 0.05)
        e._wake['start'] = time.monotonic() - 100
        e._sg_last = (1.0, time.monotonic())
        self.assertAlmostEqual(e._sleep_gain_locked(), 1.0, delta=0.01)
        e._wake['end'] = time.monotonic() + 6
        self.assertLess(e._sleep_gain_locked(), 0.02)
        e.playing = True                                        # never on music
        e._sg_last = (1.0, time.monotonic())
        self.assertAlmostEqual(e._sleep_gain_locked(), 1.0, delta=0.01)
        e.playing = False
        e._wake['end'] = time.monotonic() - 1                   # its end: the sound off, the volume from before
        e.sleep_wake_tick()
        self.assertIsNone(e._wake)
        self.assertEqual(e.calls[-2:], [('ambient', 'off', None), ('volume', 45)])
        e.calls.clear()
        started(e, t0 + 60)                                      # once a morning
        self.assertEqual(e.calls, [])

    def test_someone_sets_a_volume(self):
        e = engine(volume=45)
        e._sleep_state = {'last_started': int(at(2026, 11, 12, 21, 30))}
        started(e, at(2026, 11, 13, 6, 30) + 5)
        e.volume = 60                                            # by hand during the wake-up: theirs from now on
        e._wake['start'] = time.monotonic() - 300
        e.sleep_wake_tick()
        e._wake['end'] = time.monotonic() - 1
        e.sleep_wake_tick()
        self.assertEqual((e.calls[-1], e.volume), (('ambient', 'off', None), 60))

    def test_a_touch_or_the_switch_stops_it(self):
        e = engine(volume=45)
        e._sleep_state = {'last_started': int(at(2026, 11, 12, 21, 30))}
        started(e, at(2026, 11, 13, 6, 30) + 5)
        d = json.loads(Q.SUNRISE_PATH.read_text())
        d['stopped'] = int(time.time())                          # what the screen owner writes after a touch
        Q.SUNRISE_PATH.write_text(json.dumps(d))
        e.sleep_wake_tick()
        self.assertEqual((e._wake['stopping'], e._wake['fade']), ('a touch on the screen', 2.0))
        self.assertLessEqual(e._wake['end'], time.monotonic() + 2.01)
        e._wake['end'] = time.monotonic() - 0.1
        e.sleep_wake_tick()
        self.assertIsNone(e._wake)
        self.assertEqual(e.calls[-2:], [('ambient', 'off', None), ('volume', 45)])
        s2 = engine(volume=45)
        s2._sleep_state = {'last_started': int(at(2026, 11, 12, 21, 30))}
        started(s2, at(2026, 11, 13, 6, 30) + 5)
        with mock.patch.object(Q, '_write_sleep_state', lambda d: None):
            self.assertTrue(s2.sleep_set_wake(False)['ok'])
        self.assertEqual(s2._wake['stopping'], 'the switch')
        self.assertIn('stopped', json.loads(Q.SUNRISE_PATH.read_text()))   # the screen goes back too

    def test_days_off_by_the_calendar(self):
        self.assertEqual(Q._wake_day_info(2026, 11, 14), (True, '周末'))
        off, why = Q._wake_day_info(2026, 10, 1)
        self.assertTrue(off and '国庆' in why)
        off, why = Q._wake_day_info(2026, 10, 10)                 # a Saturday, 调休上班
        self.assertTrue(not off and '调休' in why)
        sat = engine()
        sat._sleep_state = {'last_started': int(at(2026, 11, 13, 21, 30))}
        started(sat, at(2026, 11, 14, 6, 30) + 5)
        self.assertEqual((sat.calls, sat._sleep_state['wake_done']), ([], '2026-11-14'))
        hol = engine()
        hol._sleep_state = {'last_started': int(at(2026, 9, 30, 21, 30))}
        started(hol, at(2026, 10, 1, 6, 30) + 5)
        self.assertEqual(hol.calls, [])
        work = engine()
        work._sleep_state = {'last_started': int(at(2026, 10, 9, 21, 30))}
        started(work, at(2026, 10, 10, 6, 30) + 5)
        self.assertEqual(work.calls[0], ('volume', 20))
        nxt = Q._wake_next({}, at(2026, 11, 13, 20, 0))           # Friday evening: tomorrow is a Saturday
        self.assertEqual((nxt['when'], nxt['off'], nxt['why'], nxt['weekday']), ('明天', True, '周末', '周六'))

    def test_the_time_is_the_users_and_a_sleep_sound_makes_way(self):
        e = engine(volume=30)
        with mock.patch.object(Q, '_write_sleep_state', lambda d: None):
            self.assertFalse(e.sleep_set_wake(None, '04:00')['ok'])
            self.assertFalse(e.sleep_set_wake(None, '10:05')['ok'])
            self.assertFalse(e.sleep_set_wake()['ok'])
            r = e.sleep_set_wake(None, '07:15')
        self.assertEqual((r['ok'], r['wake_time'], r['wake_next']['time']), (True, '07:15', '7:15'))
        e._sleep_state['last_started'] = int(at(2026, 11, 12, 21, 30))
        started(e, at(2026, 11, 13, 6, 30) + 5)                  # not yet
        self.assertEqual(e.calls, [])
        e.sleep_session = {'label': '雨夜壁炉'}
        started(e, at(2026, 11, 13, 7, 15) + 5)                  # the sleep sound is still on: it makes way
        started(e, at(2026, 11, 13, 7, 15) + 7)
        self.assertEqual(e.calls, [('stop',)])
        e.sleep_session = None
        e._sleep_state['restore'] = {'day_volume': 42, 'sleep_volume': 25, 'at': at(2026, 11, 13, 7, 15) + 9}
        started(e, at(2026, 11, 13, 7, 15) + 9)
        self.assertEqual((e._wake['day_vol'], e._sleep_state['restore']), (42, None))
        self.assertEqual(e.sleep_session_info()['wake_time'], '07:15')

    def test_wake_skips(self):
        t = at(2026, 11, 13, 6, 31)
        with mock.patch.object(Q.time, 'time', return_value=t), mock.patch.object(Q, '_write_sleep_state', lambda d: None):
            off = engine()
            off._sleep_state = {'last_started': int(at(2026, 11, 12, 21, 30)), 'wake': False}
            off.sleep_wake_tick()
            nap = engine()
            nap._sleep_state = {'last_started': int(at(2026, 11, 12, 14, 0))}   # a nap yesterday afternoon
            nap.sleep_wake_tick()
            busy = engine()
            busy._sleep_state = {'last_started': int(at(2026, 11, 12, 22, 0))}
            busy.playing = True
            busy.sleep_wake_tick()
            late = engine()
            late._sleep_state = {'last_started': int(at(2026, 11, 12, 22, 0))}
            with mock.patch.object(Q.time, 'time', return_value=at(2026, 11, 13, 7, 0)):
                late.sleep_wake_tick()
        self.assertEqual(off.calls + nap.calls + busy.calls + late.calls, [])
        self.assertEqual(busy._sleep_state.get('wake_done'), '2026-11-13')   # skipped for this morning
        self.assertFalse(Q.SUNRISE_PATH.exists())

    def test_night_view_flag_and_switch(self):
        e = engine()
        with mock.patch.object(Q, '_write_sleep_state', lambda d: None):
            self.assertTrue(e.sleep_night_view(True)['ok'])
            self.assertGreater(json.loads(Q.NIGHT_VIEW_PATH.read_text())['until'], time.time() + 11 * 3600)
            self.assertTrue(e.sleep_night_view(False)['ok'])
            self.assertFalse(Q.NIGHT_VIEW_PATH.exists())
            self.assertTrue(e.sleep_set_wake(False)['ok'])
            self.assertFalse(e.sleep_set_wake('x')['ok'])
            self.assertIs(e._sleep_state['wake'], False)
        e.sleep_night_view(True)                                 # a forgotten flag goes in the day
        with mock.patch.object(Q.time, 'time', return_value=at(2026, 11, 13, 9, 0)):
            e.sleep_wake_tick()
        self.assertFalse(Q.NIGHT_VIEW_PATH.exists())
        with mock.patch.object(Q.time, 'time', return_value=at(2026, 11, 12, 22, 0)):   # screen-off from 20:00
            self.assertTrue(e.sleep_screen_off()['ok'])
        with mock.patch.object(Q.time, 'time', return_value=at(2026, 11, 12, 15, 0)):
            self.assertFalse(e.sleep_screen_off()['ok'])


# ------------------------------------------------------------------------------------------------ the blanker
def blanker(hint='{"brightness":100,"idle":1800}', mode='{"auto": true, "anchor": 100}', stored='3459'):
    spec = importlib.util.spec_from_file_location('idle_r2', src('idle-blanker.py', '/usr/local/bin/idle-blanker.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    d = pathlib.Path(tempfile.mkdtemp(dir=TMP.name))
    m.HINT_PATH = d / 'hint'
    m.HINT_PATH.write_text(hint)
    m.MODE_PATH = d / 'mode'
    m.MODE_PATH.write_text(mode)
    m.MIRROR_FLAG_PATH, m.INTERCOM_FLAG_PATH = d / 'mirror', d / 'call'
    m.USER_BRIGHTNESS_PATH = d / 'ub'
    m.USER_BRIGHTNESS_PATH.write_text(stored + '\n')
    m.NIGHT_VIEW_PATH, m.SUNRISE_PATH = d / 'night-view', d / 'sunrise.json'
    m.NIGHT_TEST = True
    m.read_als = lambda: None
    return m


class Done(Exception):
    pass


def drive(m, script, start_bl, wall):
    """Run the loop: script(m, bl) -> True for an input event, or (True, False) for a brightness-channel message.
    Returns the brightness writes."""
    bl = [start_bl]
    writes = []
    ticks = [0]
    clock = [1000.0]

    def drain(*a):
        ticks[0] += 1
        clock[0] += 1.0
        if ticks[0] > len(script):
            raise Done()
        r = script[ticks[0] - 1](m, bl)
        if isinstance(r, tuple):
            drain.dev_event = r[1]
            return r[0]
        drain.__dict__.pop('dev_event', None)
        return r

    def setb(v):
        writes.append(v)
        bl[0] = v
        return True
    with mock.patch.object(m, 'open_input_devices', return_value=[]), mock.patch.object(m, 'drain', drain), \
            mock.patch.object(m, 'get_brightness', side_effect=lambda: bl[0]), mock.patch.object(m, 'set_brightness', side_effect=setb), \
            mock.patch.object(m, 'set_blank_render_paused'), mock.patch.object(m.signal, 'signal'), \
            mock.patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), \
            mock.patch.object(m.time, 'time', side_effect=lambda: wall[0] + clock[0] - 1000.0):
        try:
            m._run()
        except Done:
            pass
    return writes


IDLE = lambda m, bl: False
TOUCH = lambda m, bl: True
POKE = lambda m, bl: (True, False)      # the brightness channel (e.g. a new phone message), not a touch


def sunrise_file(m, start):
    m.SUNRISE_PATH.write_text(json.dumps({'start': start, 'ramp': 600, 'hold': 360, 'max': 50}))


class BlankerR2Tests(unittest.TestCase):
    def test_lit_in_the_night_is_lowest(self):
        m = blanker()
        self.assertTrue(m.in_night(at(2026, 11, 13, 1, 0)))
        self.assertFalse(m.in_night(at(2026, 11, 13, 6, 30)))
        night = drive(m, [IDLE, TOUCH, IDLE], 10, [at(2026, 11, 13, 1, 0)])
        self.assertEqual(night[0], m.NIGHT_LEVEL)
        day = drive(blanker(), [IDLE, TOUCH, IDLE], 10, [at(2026, 11, 13, 9, 0)])
        self.assertEqual(day[0], 3459)

    def test_night_view_keeps_it_dim(self):
        m = blanker()

        def up(m_, bl):
            m_.NIGHT_VIEW_PATH.write_text(json.dumps({'until': time.time() + 3600 * 24 * 400}))
            return False

        def gone(m_, bl):
            m_.NIGHT_VIEW_PATH.unlink()
            return False
        w = drive(m, [IDLE, up, IDLE, IDLE, gone, IDLE, IDLE], 3459, [at(2026, 11, 12, 21, 30)])
        self.assertEqual(w, [m.NIGHT_LEVEL, 3459])          # dim while it is up, back when it is gone
        m2 = blanker()                                       # blanked by the night view, then a touch: lowest
        m2.NIGHT_VIEW_PATH.write_text(json.dumps({'until': time.time() + 3600 * 24 * 400}))
        w2 = drive(m2, [IDLE, TOUCH, IDLE], 10, [at(2026, 11, 12, 21, 30)])
        self.assertEqual(w2[0], m2.NIGHT_LEVEL)

    def test_sunrise_from_its_own_level_up_to_half(self):
        m = blanker(stored='1600')
        wall = [at(2026, 11, 13, 6, 30)]
        sunrise_file(m, wall[0])

        def later(m_, bl):
            wall[0] += 299
            return False
        w = drive(m, [IDLE, IDLE, later, IDLE, later, IDLE, IDLE], 10, wall)
        self.assertEqual(w[0], 1600)                         # where the screen would wake
        self.assertEqual(max(w), m.manual_brightness(50))    # 50% and no more
        self.assertEqual(w, sorted(w))

    def test_sunrise_on_a_lit_screen_starts_where_it_is(self):
        m = blanker()
        wall = [at(2026, 11, 13, 6, 30)]

        def rise(m_, bl):
            sunrise_file(m_, wall[0])
            return False

        def later(m_, bl):
            wall[0] += 200
            return False
        w = drive(m, [IDLE, rise, IDLE, later, IDLE], 2000, wall)
        self.assertTrue(w and min(w) >= 2000 and w == sorted(w), w)   # never darker first

    def test_by_hand_level_comes_back_and_a_touch_stops_the_birds(self):
        m = blanker(hint='{"brightness":0,"idle":0}', mode='{"auto": false}')   # 0% by hand, never off (11-12)
        wall = [at(2026, 11, 13, 6, 30)]
        sunrise_file(m, wall[0])

        def later(m_, bl):
            wall[0] += 400
            return False
        w = drive(m, [IDLE, IDLE, later, IDLE, TOUCH] + [IDLE] * 6, 10, wall)
        self.assertEqual(w[0], 1400)                         # the user's own level
        self.assertGreater(max(w), 2000)
        self.assertEqual(w[-1], 1400)                         # back to it after the touch
        self.assertIn('stopped', json.loads(m.SUNRISE_PATH.read_text()))   # so the birds stop too

    def test_not_a_touch_and_stopped_by_the_music_service(self):
        m = blanker(stored='1600')
        wall = [at(2026, 11, 13, 6, 30)]
        sunrise_file(m, wall[0])
        seen = {}

        def later(m_, bl):
            wall[0] += 100
            return False

        def stop(m_, bl):
            d = json.loads(m_.SUNRISE_PATH.read_text())
            seen['before'] = 'stopped' in d                  # the channel message did not stop it
            d['stopped'] = int(wall[0])
            m_.SUNRISE_PATH.write_text(json.dumps(d))
            return False
        w = drive(m, [IDLE, IDLE, later, POKE, IDLE, stop, IDLE, IDLE, later, IDLE], 10, wall)
        self.assertFalse(seen['before'])
        self.assertTrue(w == sorted(w))                       # automatic: no jump back, the light sensor takes over


if __name__ == '__main__':
    unittest.main(verbosity=2)
