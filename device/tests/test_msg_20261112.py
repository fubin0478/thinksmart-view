#!/opt/thinksmart-addons/venv/bin/python
"""2026-11-12 (Claude) message board, round 2: who (署名), quick replies, 60 s voice + transcripts, timed reminders,
小麦's tool endpoint, and the intercom relay passing the new fields. Never touches live state: every inbox lives in a
temporary directory, speech and the recognizer are faked, no service is called.
    /opt/thinksmart-addons/venv/bin/python -B /opt/thinksmart-addons/test_msg_20261112.py
MSG_ROOT=<dir> tests the copies in <dir> (thinksmart-home.py, thinksmart-intercom.py) instead of the installed files."""
import array
import ast
import asyncio
import datetime as dt
import io
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import types
import unittest
import wave
from unittest import mock

ROOT = os.environ.get('MSG_ROOT')
sys.path.insert(0, '/opt/thinksmart-addons/vendor')
sys.path.insert(1, '/usr/local/bin')


def load(name, fname, installed):
    p = pathlib.Path(ROOT, fname) if ROOT else pathlib.Path(installed)
    t = ast.parse(p.read_text(encoding='utf-8'))
    t.body = [n for n in t.body if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                                        and getattr(n.test.left, 'id', '') == '__name__')]
    m = types.ModuleType(name)
    m.__file__ = str(p)
    exec(compile(t, str(p), 'exec'), m.__dict__)
    return m


TMP = tempfile.TemporaryDirectory()
os.environ['STATE_DIRECTORY'] = os.path.join(TMP.name, 'state')
H = load('home_msg', 'thinksmart-home.py', '/usr/local/bin/thinksmart-home.py')
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

HD = {'Host': '127.0.0.1:8095'}
HI = dict(HD, **{'X-TS-Internal': '1'})


def box(name):
    return H.Inbox(pathlib.Path(TMP.name, name, 'messages.json'))


def wav_bytes(seconds, freq=440.0):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        a = array.array('h', (int(8000 * math.sin(2 * math.pi * freq * i / 16000)) for i in range(int(16000 * seconds))))
        w.writeframes(a.tobytes())
    return buf.getvalue()


class InboxTests(unittest.TestCase):
    def test_who_tag_and_reload(self):
        b = box('i1')
        m = b.add('我到家了', 'screen', '姐姐', 'home')
        self.assertEqual((m['who'], m['tag'], m['due']), ('姐姐', 'home', None))
        m2 = b.add('晚上见', 'phone', '外人')
        self.assertEqual(m2['who'], '')
        again = H.Inbox(b.path).recent(5)
        self.assertEqual([x['who'] for x in again], ['', '姐姐'])
        self.assertEqual(again[1]['tag'], 'home')

    def test_reminders(self):
        b = box('i2')
        with self.assertRaises(ValueError):
            b.add('太早了', 'screen', '姐姐', '', time.time() + 5)
        r = b.add('带作业', 'screen', '姐姐', '', time.time() + 60)
        self.assertEqual((r['tag'], r['fired']), ('remind', False))
        self.assertEqual(b.unread(), [])                              # not shown before its time
        self.assertEqual([x['id'] for x in b.pending()], [r['id']])
        self.assertEqual(b.fire_due(time.time()), [])
        fired = b.fire_due(time.time() + 61)
        self.assertEqual([x['id'] for x in fired], [r['id']])
        self.assertEqual([x['id'] for x in b.unread()], [r['id']])    # now on the screen until 收到
        self.assertEqual(b.pending(), [])
        r2 = b.add('取消我', 'phone', '妈妈', '', time.time() + 3600)
        self.assertTrue(b.cancel_reminder(r2['id']))
        self.assertFalse(b.cancel_reminder(r['id']))                  # a fired one is not cancelled
        self.assertEqual(H.Inbox(b.path).unread()[0]['id'], r['id'])  # survives a restart

    def test_quick_replies(self):
        b = box('i3')
        self.assertEqual(b.quick, list(H.QUICK_DEFAULT))
        self.assertEqual(b.set_quick(['  好的 ', '好的', '这一条实在是太长了太长了太长了', '马上回来']), ['好的', '马上回来'])
        self.assertEqual(H.Inbox(b.path).quick, ['好的', '马上回来'])
        with self.assertRaises(ValueError):
            b.set_quick([])

    def test_transcript_and_voice_who(self):
        b = box('i4')
        v = b.add_voice(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 128, 45.0, 'phone', '爸爸')
        self.assertEqual((v['who'], v['duration']), ('爸爸', 45.0))
        self.assertTrue(b.set_transcript(v['id'], '  晚上 七点  回家 '))
        self.assertEqual(H.Inbox(b.path).recent(1)[0]['transcript'], '晚上 七点 回家')


class TimeTests(unittest.TestCase):
    def test_remind_due(self):
        now = dt.datetime(2026, 11, 12, 20, 0).timestamp()
        f = lambda **k: H.remind_due(now=now, **k)
        self.assertEqual(dt.datetime.fromtimestamp(f(time_s='21:30')).strftime('%d %H:%M'), '12 21:30')
        self.assertEqual(dt.datetime.fromtimestamp(f(time_s='7:00')).strftime('%d %H:%M'), '13 07:00')     # passed today -> tomorrow
        self.assertEqual(dt.datetime.fromtimestamp(f(day='tomorrow', time_s='07:00')).strftime('%d %H:%M'), '13 07:00')
        self.assertEqual(dt.datetime.fromtimestamp(f(day='后天', time_s='8点')).strftime('%d %H:%M'), '14 08:00')
        self.assertEqual(dt.datetime.fromtimestamp(f(at='2026-11-13 06:45')).strftime('%d %H:%M'), '13 06:45')
        self.assertAlmostEqual(f(in_minutes=30), now + 1800)
        self.assertIsNone(f(day='today', time_s='19:00'))              # an explicit today that has passed
        self.assertIsNone(f(time_s='25:00'))
        self.assertIsNone(f(at='2026-12-30 08:00'))                    # more than 8 days ahead
        self.assertIsNone(f(in_minutes=0))

    def test_asr_endpoint(self):
        p = pathlib.Path(TMP.name, 'hub.yaml')
        p.write_text('ASR:\n  MacASR:\n    # comment\n    type: openai\n    base_url: http://gw.example:8080/tok/v1/audio/transcriptions\n'
                     '    model_name: Qwen3-ASR-1.7B  # note\n    language: auto\n    prompt: "@default"\n  Other:\n    base_url: x\n', encoding='utf-8')
        self.assertEqual(H.asr_endpoint(p), {'url': 'http://gw.example:8080/tok/v1/audio/transcriptions', 'model': 'Qwen3-ASR-1.7B',
                                             'language': 'auto', 'prompt': '@default', 'key': ''})
        p.write_text('MacASR:\n  base_url: ftp://nope\n', encoding='utf-8')
        self.assertIsNone(H.asr_endpoint(p))

    def test_pieces(self):
        pcs = H._wav_pieces(wav_bytes(45))
        self.assertEqual(len(pcs), 3)
        self.assertTrue(all(len(x) <= 20 * 32000 + 44 for x in pcs))
        self.assertEqual(len(H._wav_pieces(wav_bytes(12))), 1)

    def test_sixty_second_voice(self):
        mp3, dur = H._transcode_to_voice_mp3(wav_bytes(45), False)
        self.assertAlmostEqual(dur, 45.0, delta=0.6)
        self.assertLess(len(mp3), H.MAX_VOICE_MP3)
        mp3b, durb = H._transcode_to_voice_mp3(wav_bytes(59.5), False)
        self.assertAlmostEqual(durb, 59.5, delta=0.6)
        with self.assertRaises(ValueError):            # over 60 s is more than the phone ever sends
            H._transcode_to_voice_mp3(wav_bytes(70), False)


class HttpTests(unittest.TestCase):
    def test_flow(self):
        async def go():
            b = box('h1')
            said = []

            async def fake_post(session, url, body, timeout=5.0, headers=None):
                said.append((url, body))
                return 200, {'ok': True}
            asr_app = web.Application(client_max_size=4 * 1024 * 1024)
            asr_seen = []

            async def fake_asr(request):
                form = await request.post()
                asr_seen.append((form['model'], len(form['file'].file.read())))
                return web.json_response({'text': f'第{len(asr_seen)}段。'})
            asr_app.router.add_post('/tok/v1/audio/transcriptions', fake_asr)
            asr_srv = TestServer(asr_app)
            await asr_srv.start_server()
            asr_cfg = {'url': f'http://127.0.0.1:{asr_srv.port}/tok/v1/audio/transcriptions', 'model': 'm1', 'language': 'auto', 'prompt': '', 'key': ''}
            with mock.patch.object(H, '_post_json', fake_post), mock.patch.object(H, 'send_preview', None), \
                    mock.patch.object(H, 'asr_endpoint', lambda path=None: asr_cfg):
                client = TestClient(TestServer(H.make_app(b)))
                await client.start_server()
                try:
                    r = await client.post('/api/home/messages', json={'text': '记得喝水', 'who': '妈妈'}, headers=HI)
                    self.assertEqual((r.status, (await r.json())['message']['who']), (200, '妈妈'))
                    j = await (await client.get('/api/home/messages?limit=5', headers=HD)).json()
                    self.assertEqual(j['family'], ['爸爸', '妈妈', '姐姐', '妹妹'])
                    self.assertEqual(j['quick'][0], '👌 收到啦')
                    # quick replies only from the relay (internal)
                    self.assertEqual((await client.post('/api/home/messages/quick', json={'quick': ['好']}, headers=HD)).status, 403)
                    r = await client.post('/api/home/messages/quick', json={'quick': ['好的', '马上来']}, headers=HI)
                    self.assertEqual((await r.json())['quick'], ['好的', '马上来'])
                    # screen reply with who + 到家了
                    r = await client.post('/api/home/messages/reply', json={'text': '我到家了', 'who': '姐姐', 'tag': 'home'}, headers=HD)
                    m = (await r.json())['message']
                    self.assertEqual((m['who'], m['tag'], m['source']), ('姐姐', 'home', 'screen'))
                    # phone reminder: not on the screen before its time, then read out once
                    due = time.time() + 30
                    r = await client.post('/api/home/messages', json={'text': '带作业', 'who': '姐姐', 'due': due}, headers=HI)
                    rem = (await r.json())['message']
                    self.assertEqual(rem['tag'], 'remind')
                    ib = await (await client.get('/api/home/inbox', headers=HD)).json()
                    self.assertEqual([x['text'] for x in ib['unread']], ['记得喝水'])
                    self.assertEqual((await client.post('/api/home/messages', json={'text': 'x', 'due': True}, headers=HI)).status, 400)
                    # 小麦's tool
                    t = lambda body: client.post('/api/home/messages/tool', json=body, headers=HI)
                    self.assertEqual((await t({'action': 'read'})).status, 200)
                    self.assertEqual((await client.post('/api/home/messages/tool', json={'action': 'read'}, headers=HD)).status, 403)
                    j = await (await t({'action': 'remind', 'text': '关火', 'in_minutes': 20})).json()
                    self.assertTrue(j['ok'], j)
                    j = await (await t({'action': 'reminders'})).json()
                    self.assertIn('带作业', j['text'])
                    self.assertIn('关火', j['text'])
                    j = await (await t({'action': 'cancel', 'keyword': '关火'})).json()
                    self.assertTrue(j['ok'])
                    j = await (await t({'action': 'remind', 'text': 'x', 'time': '99:00'})).json()
                    self.assertFalse(j['ok'])
                    j = await (await t({'action': 'send', 'text': '我放学到家了', 'from': '妹妹'})).json()
                    self.assertTrue(j['ok'])
                    self.assertEqual(b.recent(1)[0]['who'], '妹妹')
                    # (the 「读留言」 above already marked 记得喝水 read, silently)
                    self.assertEqual(b.unread(), [])
                    self.assertEqual([x for x in said if 'voice/say' in x[0]], [])
                    # the reminder comes up: unread on the screen, read out once
                    with mock.patch.object(time, 'time', lambda: due + 1):
                        pass
                    for m in b.items:
                        if m['id'] == rem['id']:
                            m['due'] = time.time() - 1
                    app_tasks = [x for x in b.fire_due()]
                    self.assertEqual([x['id'] for x in app_tasks], [rem['id']])
                    ib = await (await client.get('/api/home/inbox', headers=HD)).json()
                    self.assertEqual([x['id'] for x in ib['unread']], [rem['id']])
                    r = await client.post('/api/home/inbox/ack', json={'id': rem['id']}, headers=HD)
                    self.assertEqual(r.status, 200)
                    self.assertEqual([x for x in said if 'voice/say' in x[0]], [])   # no second read-out on 收到
                    # phone voice, 45 s, with who -> transcript from the recognizer
                    r = await client.post('/api/home/messages/voice?who=%E7%88%B8%E7%88%B8', data=wav_bytes(45),
                                          headers=dict(HI, **{'Content-Type': 'audio/wav'}))
                    vm = (await r.json())['message']
                    self.assertEqual((vm['who'], round(vm['duration'])), ('爸爸', 45))
                    for _ in range(60):
                        await asyncio.sleep(0.25)
                        if b.recent(1)[0].get('transcript'):
                            break
                    self.assertEqual(b.recent(1)[0]['transcript'], '第1段，第2段，第3段。')   # 45 s in 20 s pieces, joined with ，
                    self.assertEqual(asr_seen[0][0], 'm1')
                    self.assertTrue(all(n < 700 * 1024 for _, n in asr_seen))
                    # ack of a phone text message reads it out with the sender's name
                    r = await client.post('/api/home/messages', json={'text': '早点睡', 'who': '爸爸'}, headers=HI)
                    mid = (await r.json())['message']['id']
                    await client.post('/api/home/inbox/ack', json={'id': mid}, headers=HD)
                    self.assertEqual([x[1]['text'] for x in said if 'voice/say' in x[0]], ['爸爸留言：早点睡'])
                    # cancel from the screen
                    r = await client.post('/api/home/messages', json={'text': '明天交表', 'who': '妹妹', 'due': time.time() + 600}, headers=HI)
                    cid = (await r.json())['message']['id']
                    self.assertEqual((await client.post('/api/home/messages/cancel', json={'id': cid}, headers=HD)).status, 200)
                    self.assertEqual((await client.post('/api/home/messages/cancel', json={'id': cid}, headers=HD)).status, 404)
                finally:
                    await client.close()
                    await asr_srv.close()
        asyncio.run(go())


class RelayTests(unittest.TestCase):
    def test_relay_passes_new_fields(self):
        try:
            I = load('intercom_msg', 'thinksmart-intercom.py', '/usr/local/bin/thinksmart-intercom.py')
        except Exception as exc:  # pragma: no cover
            self.skipTest(f'intercom not loadable here: {exc}')
        if not hasattr(I, 'handle_message_quick'):
            self.skipTest('relay without the 11-12 additions')

        async def go():
            import aiohttp
            seen = []

            async def fake_home(request):
                body = await request.read()
                seen.append((request.method, request.path, request.query_string, body[:200]))
                if request.path == '/api/home/messages' and request.method == 'GET':
                    return web.json_response({'messages': [{'id': 'abcdefabcdef', 'text': 'hi', 'at': 1, 'read_at': None, 'source': 'phone',
                                                            'who': '妈妈', 'tag': 'remind', 'due': 5, 'fired': False, 'transcript': ''}],
                                              'quick': ['好的'], 'family': ['爸爸', '妈妈']})
                return web.json_response({'ok': True, 'message': {'id': 'abcdefabcdef', 'text': 'hi', 'at': 1, 'read_at': None}})
            up = web.Application(client_max_size=4 * 1024 * 1024)
            up.router.add_route('*', '/{tail:.*}', fake_home)
            upstream = TestServer(up)
            await upstream.start_server()
            app = web.Application()
            app['study_session'] = aiohttp.ClientSession()
            app.router.add_get('/api/messages', I.handle_messages)
            app.router.add_post('/api/message', I.handle_messages)
            app.router.add_post('/api/message/voice', I.handle_message_voice)
            app.router.add_post('/api/message/quick', I.handle_message_quick)
            app.router.add_post('/api/message/cancel', I.handle_message_cancel)
            client = TestClient(TestServer(app))
            await client.start_server()
            mh = {'X-Intercom-Msg': '1'}
            try:
                with mock.patch.object(I, 'HOME_API', f'http://127.0.0.1:{upstream.port}'):
                    j = await (await client.get('/api/messages', headers=mh)).json()
                    self.assertEqual(j['messages'][0]['who'], '妈妈')
                    self.assertEqual(j['messages'][0]['due'], 5)
                    self.assertEqual((j['quick'], j['family']), (['好的'], ['爸爸', '妈妈']))
                    await client.post('/api/message', json={'text': '明早交表', 'who': '妈妈', 'due': 2000000000}, headers=mh)
                    self.assertEqual(json.loads(seen[-1][3]), {'text': '明早交表', 'who': '妈妈', 'due': 2000000000})
                    rv = await client.post('/api/message/voice?who=%E5%A6%88%E5%A6%88', data=wav_bytes(40), headers=dict(mh, **{'Content-Type': 'audio/wav'}))
                    self.assertEqual(rv.status, 200, await rv.text())
                    self.assertIn('who=', seen[-1][2])
                    r = await client.post('/api/message/quick', json={'quick': ['好的', '马上']}, headers=mh)
                    self.assertEqual(r.status, 200)
                    r = await client.post('/api/message/cancel', json={'id': 'abcdefabcdef'}, headers=mh)
                    self.assertEqual(r.status, 200)
                    self.assertEqual((await client.post('/api/message/quick', json={'quick': ['x']})).status, 403)
            finally:
                await client.close()
                await app['study_session'].close()
                await upstream.close()
        asyncio.run(go())


class BridgeTests(unittest.TestCase):
    def test_voice_tools(self):
        p = pathlib.Path(ROOT, 'sitecustomize.py') if ROOT else pathlib.Path('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py')
        src = p.read_text(encoding='utf-8')
        if 'self.home.reminder' not in src:
            self.skipTest('bridge without the 11-12 tools')
        b = load('bridge_msg', p.name if ROOT else 'sitecustomize.py', str(p))
        names = [t['name'] for t in b.MUSIC_TOOLS]
        self.assertIn('self.home.message', names)
        self.assertIn('self.home.reminder', names)
        self.assertIn('self.home.reminder', b._MY_TOOLS)
        sent = []
        b._post_home_tool = lambda body: sent.append(body) or 'ok'
        b._call_home_message({'action': 'send', 'text': '我到家了', 'from': '姐姐'})
        b._call_home_message({'action': 'send', 'text': 'x', 'from': '路人'})
        b._call_home_message({'action': 'read'})
        b._call_home_reminder({'action': 'add', 'text': '带作业', 'who': '姐姐', 'day': 'tomorrow', 'time': '07:00'})
        b._call_home_reminder({'action': 'add', 'text': '关火', 'in_minutes': 30})
        b._call_home_reminder({'action': 'list'})
        b._call_home_reminder({'action': 'cancel', 'keyword': '关火'})
        self.assertEqual(sent, [
            {'action': 'send', 'text': '我到家了', 'from': '姐姐'},
            {'action': 'send', 'text': 'x', 'from': ''},
            {'action': 'read'},
            {'action': 'remind', 'text': '带作业', 'who': '姐姐', 'day': 'tomorrow', 'time': '07:00'},
            {'action': 'remind', 'text': '关火', 'who': '', 'in_minutes': 30},
            {'action': 'reminders'},
            {'action': 'cancel', 'keyword': '关火'},
        ])
        with self.assertRaises(ValueError):
            b._call_home_message({'action': 'send', 'text': ' '})


if __name__ == '__main__':
    unittest.main(verbosity=1)
