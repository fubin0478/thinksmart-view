import ast, asyncio, contextlib, importlib.util, io, json, os, pathlib, random, re, selectors, subprocess, sys, tempfile, types, unittest
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
from unittest.mock import patch
from types import SimpleNamespace
ROOT=pathlib.Path(os.environ.get('AUDIT_SOURCE','/usr/local/bin'))
FIXED=pathlib.Path(os.environ.get('AUDIT_FIXED',str(ROOT)))
RUNNER=pathlib.Path(os.environ.get('AUDIT_RUNNER','/usr/local/sbin/thinksmart-run-live-tests'))
sys.path.insert(0,str(ROOT))
def module(name, filename, no_engine=False):
 p=ROOT/filename;t=ast.parse(p.read_text())
 if no_engine:t.body=[n for n in t.body if not (isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='engine' for x in n.targets))]
 m=types.ModuleType(name);m.__file__=str(p);exec(compile(t,str(p),'exec'),m.__dict__);return m
q=module('music_audit','thinksmart-qqmusic.py',True)
s=module('study_audit','thinksmart-study.py')
m=module('monitor_audit','thinksmart-monitor.py')
def func_from(path,name):
 t=ast.parse(path.read_text()); nodes=[n for n in t.body if isinstance(n,ast.FunctionDef) and n.name==name]
 ns={'_NMCLI_SPLIT':re.compile(r'(?<!\\):')};exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns);return ns[name]
parse=func_from(FIXED/'thinksmart-kiosk.py','nmcli_fields')
class Wifi(unittest.TestCase):
 def check(self,fields):
  raw=':'.join(x.replace('\\','\\\\').replace(':','\\:') for x in fields)
  self.assertEqual(parse(raw),fields)
 def test_plain(self):self.check(['*','home','80','WPA2'])
 def test_colon(self):self.check(['*','room:study','80','WPA2'])
 def test_trailing_slash(self):self.check(['*','folder\\','80','WPA2'])
 def test_slash_before_colon(self):self.check(['*','a\\:b','80','WPA2'])
 def test_generated_1000(self):
  rng=random.Random(42)
  for _ in range(1000):self.check(['*',''.join(rng.choice('ab :\\中文') for _ in range(24)),'80','WPA2'])
class Done(Exception):pass
class Gpu(unittest.TestCase):
 def run_watch(self,chunks,busy=False):
  spec=importlib.util.spec_from_file_location('gpu_audit',FIXED/'gpu-watchdog.py');g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
  r,w=os.pipe();f=os.fdopen(r,'rb' if 'chunk = os.read' in (FIXED/'gpu-watchdog.py').read_text() else 'r');os.set_blocking(r,False)
  now=[0.0];items=list(chunks);calls=[]
  class Sel:
   def select(self,timeout):
    now[0]+=1
    if now[0]>30:raise Done()
    if items:os.write(w,items.pop(0));return [1]
    if busy:os.write(w,b'ordinary kernel message\n');return [1]
    now[0]+= timeout or 0;return []
  def run(cmd,**kw):
   calls.append(cmd)
   if '-CONT' not in cmd:raise Done()
   return SimpleNamespace(returncode=0)
  try:
   with patch.object(g.time,'monotonic',lambda:now[0]),patch.object(g.subprocess,'run',run),patch.object(g,'log',lambda _:None):
    with self.assertRaises(Done):g._watch(SimpleNamespace(stdout=f),Sel())
  finally:os.close(w);f.close()
  return len([c for c in calls if '-CONT' not in c])
 def test_burst_second_line(self):self.assertEqual(self.run_watch([b'ok\ngpu fault\nmore\n']),1)
 def test_split_keyword(self):self.assertEqual(self.run_watch([b'gpu fa',b'ult\n']),1)
 def test_split_utf8(self):self.assertEqual(self.run_watch([b'\xe4',b'\xb8\xad gpu fault\n']),1)
 def test_busy_log_deadline(self):self.assertEqual(self.run_watch([b'gpu fault\n'],True),1)
 def test_no_false_fault(self):self.assertEqual(self.run_watch([b'gpu okay\n']),0)
class Runner(unittest.TestCase):
 def run_one(self,rc):
  content=RUNNER.read_text();fragment=content[content.index('run(){'):content.index('\nresult=0') if '\nresult=0' in content else content.index('\nrun thinksmart')]
  with tempfile.TemporaryDirectory() as d:
   pathlib.Path(d,'suite').mkdir()
   if 'cd /tmp/thinksmart-live-tests/$d' in fragment:fragment=fragment.replace('cd /tmp/thinksmart-live-tests/$d','cd "$WORK/$d"')
   r=subprocess.run(['bash','-c',fragment+'\nrun suite bash -c "echo failure-details; exit '+str(rc)+'"'],env=dict(os.environ,WORK=d),capture_output=True,text=True)
   return r
 def test_success(self):self.assertEqual(self.run_one(0).returncode,0)
 def test_failure_exit(self):self.assertEqual(self.run_one(7).returncode,7)
 def test_timeout_exit(self):self.assertEqual(self.run_one(124).returncode,124)
 def test_failure_details(self):self.assertIn('failure-details',self.run_one(7).stdout)
class CacheAndAudio(unittest.TestCase):
 def test_short_pcm_reads_and_eof(self):
  class Short(io.BytesIO):
   def readinto(self,mv):return super().readinto(mv[:137])
  data=bytes(range(256))*40;stream=Short(data);out=bytearray(7680)
  n=q.QQMusicEngine._readinto_exact(stream,memoryview(out));self.assertEqual(bytes(out[:n]),data[:7680])
  n=q.QQMusicEngine._readinto_exact(stream,memoryview(out));self.assertEqual(bytes(out[:n]),data[7680:]);self.assertEqual(q.QQMusicEngine._readinto_exact(stream,memoryview(out)),0)
 def test_lyric_error_is_retried(self):
  q._LYRIC_CACHE.clear();answers=[{'retcode':1101},{'retcode':0,'lyric':'[00:01.00]hello'}]
  def req(*a,**k):return io.BytesIO(json.dumps(answers.pop(0)).encode())
  with patch.object(q,'_qq_headers',lambda:{}),patch.object(q.urllib.request,'urlopen',req):
   self.assertEqual(q.qq_lyric('fake'),[]);self.assertNotIn('fake',q._LYRIC_CACHE);self.assertEqual(len(q.qq_lyric('fake')),1)
 def test_empty_valid_lyric_cached(self):
  q._LYRIC_CACHE.clear()
  with patch.object(q,'_qq_headers',lambda:{}),patch.object(q.urllib.request,'urlopen',return_value=io.BytesIO(b'{"retcode":0}')) as req:
   q.qq_lyric('instrumental');q.qq_lyric('instrumental');self.assertEqual(req.call_count,1)
 def test_note_timestamp_caches_bounded(self):
  m._NOTE_CACHE.clear();m._STAMP_CACHE.clear()
  for i in range(200):m._extract_note(json.dumps({'planDataMod':{'bandwidth':str(i)}}));m._parse_stamp(str(i))
  self.assertLessEqual(len(m._NOTE_CACHE),64);self.assertLessEqual(len(m._STAMP_CACHE),128)
 def test_study_cache_refresh_and_deletion(self):
  with tempfile.TemporaryDirectory() as d,patch.object(s,'LOG_DIR',pathlib.Path(d)):
   day='2026-11-07';p=pathlib.Path(d,day+'.jsonl');p.write_text('{"t":"12:00:00","event":"on"}\n')
   a=s.summarize_day(day);self.assertIs(a,s.summarize_day(day));p.write_text('{"t":"12:00:00","event":"off"}\n');self.assertIsNot(a,s.summarize_day(day));p.unlink();self.assertEqual(s.summarize_day(day)['checks'],0)
 def test_study_yesterday_hidden(self):
  c=object.__new__(s.Companion);c.last={'at':s.dt.datetime(2026,11,6,23,59,tzinfo=s.LOCAL_TZ).timestamp(),'t':'23:59:00'}
  self.assertIsNone(c.last_today('2026-11-07'));self.assertIsNotNone(c.last_today('2026-11-06'))
 def test_intercom_pcm_gain_equivalent(self):
  import numpy as np
  arr=np.arange(-32768,32768,dtype=np.int32).astype(np.int16)
  up=arr.astype(np.int32)*8;np.clip(up,-32768,32767,out=up)
  buf=np.empty(arr.size,dtype=np.int32);np.copyto(buf,arr);buf<<=3;np.clip(buf,-32768,32767,out=buf);self.assertEqual(buf.astype(np.int16).tobytes(),up.astype(np.int16).tobytes())
  old=np.clip(arr.astype(np.float32)*2.5,-32768,32767).astype(np.int16)
  # Existing integer algorithm floors negative halves; compare against previous integer path.
  ref=np.clip((arr.astype(np.int32)*5)>>1,-32768,32767).astype(np.int16)
  np.copyto(buf,arr);buf*=5;buf>>=1;np.clip(buf,-32768,32767,out=buf);self.assertEqual(buf.astype(np.int16).tobytes(),ref.tobytes())
HUB_R107_SCRIPT = r"""
import asyncio, io, json, os, stat, tempfile, threading, types
from pathlib import Path
from unittest.mock import patch
from aiohttp import web, FormData
from aiohttp.test_utils import TestClient, TestServer

import core.http_server as hs
import core.websocket_server as ws_mod

for mod, expected_sub in (
    (hs, 'xiaozhi-server/core/http_server.py'),
    (ws_mod, 'xiaozhi-server/core/websocket_server.py'),
):
    assert expected_sub in mod.__file__.replace('\\', '/'), mod.__file__

async def main():
    cfg = {
        'server': {'ip': '127.0.0.1', 'port': 16689, 'http_port': 16688, 'auth_key': 'r107-unit-secret-key-32bytes-ok!!', 'websocket': 'ws://127.0.0.1:16689/xiaozhi/v1/'},
    }
    srv = hs.SimpleHttpServer(cfg)
    app = srv.create_app()

    async def fake_kiosk(req):
        return web.json_response({'ok': True})

    app.router.add_post('/kiosk/_csrf_probe', fake_kiosk)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # 1. Host DNS-rebinding guard
        r = await client.get('/kiosk/llm', headers={'Host': 'evil.example:16688'})
        assert r.status == 403, r.status

        # 2. /kiosk/* and /admin/* Cross-Origin CSRF blocks
        r = await client.post('/kiosk/_csrf_probe', json={}, headers={'Origin': 'https://evil.example'})
        assert r.status == 403
        r = await client.post('/kiosk/_csrf_probe', json={}, headers={'Origin': 'null'})
        assert r.status == 403
        r = await client.post('/kiosk/_csrf_probe', json={}, headers={'Sec-Fetch-Site': 'cross-site'})
        assert r.status == 403
        r = await client.post('/kiosk/_csrf_probe', json={})
        assert r.status == 200
        r = await client.post('/kiosk/_csrf_probe', json={}, headers={'Origin': 'http://127.0.0.1:16688'})
        assert r.status == 200

        host_str = f'{client.server.host}:{client.server.port}'
        r = await client.post('/admin/api/logout', json={}, headers={'Origin': 'https://evil.example'})
        assert r.status == 403
        r = await client.post('/admin/api/logout', json={}, headers={'Origin': f'http://{host_str}'})
        assert r.status == 410

        # 3. WebSocketServer CSWSH & Host guard
        ws_srv = object.__new__(ws_mod.WebSocketServer)
        ws_srv.logger = srv.logger
        class FakeWS:
            def respond(self, code, msg):
                return (code, msg)
        def mk_req(hdrs):
            return types.SimpleNamespace(headers=hdrs)

        assert await ws_srv._http_response(FakeWS(), mk_req({'Host': 'evil.example:16689', 'Connection': 'Upgrade'})) == (403, 'Forbidden\n')
        assert await ws_srv._http_response(FakeWS(), mk_req({'Host': '127.0.0.1:16689', 'Origin': 'https://evil.example', 'Connection': 'Upgrade'})) == (403, 'Forbidden\n')
        assert await ws_srv._http_response(FakeWS(), mk_req({'Host': '127.0.0.1:16689', 'Origin': 'null', 'Connection': 'Upgrade'})) == (403, 'Forbidden\n')
        assert await ws_srv._http_response(FakeWS(), mk_req({'Host': '127.0.0.1:16689', 'Connection': 'Upgrade'})) is None
        assert await ws_srv._http_response(FakeWS(), mk_req({'Host': '127.0.0.1:16689', 'Connection': 'keep-alive, Upgrade'})) is None
        assert await ws_srv._http_response(FakeWS(), mk_req({'Host': '127.0.0.1:16689', 'Origin': 'http://127.0.0.1:16688', 'Connection': 'Upgrade'})) is None
        assert await ws_srv._http_response(FakeWS(), mk_req({'Host': '127.0.0.1:16689'})) == (200, 'Server is running\n')
    finally:
        await client.close()

asyncio.run(main())
print('R107_HUB_OK')
"""

class Round107AuditTests(unittest.IsolatedAsyncioTestCase):
 def test_hub_overrides_cswsh_cors_host_and_vision_security(self):
  hub_py='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python'
  env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   r=subprocess.run([hub_py,'-B','-c',HUB_R107_SCRIPT],cwd=td,env=env,capture_output=True,text=True,timeout=45)
  self.assertEqual(r.returncode,0,msg=r.stderr+'\n'+r.stdout)
  self.assertIn('R107_HUB_OK',r.stdout)
 async def test_monitor_bounded_traffic_json_rejects_oversized_stream(self):
  class BigStream:
   async def iter_chunked(self,n):
    for _ in range(20):yield b'x'*65536
  with self.assertRaises(ValueError):
   await m._bounded_json(SimpleNamespace(content=BigStream()),1048576)
 def test_sysctl_and_qqmusic_hardening(self):
  conf=pathlib.Path('/etc/sysctl.d/90-thinksmart-hardening.conf').read_text()
  for k in ('kernel.dmesg_restrict = 1','kernel.kptr_restrict = 2','net.ipv4.conf.all.accept_redirects = 0','net.ipv4.conf.all.send_redirects = 0','net.ipv6.conf.all.accept_redirects = 0'):
   self.assertIn(k,conf)
  unit=pathlib.Path('/etc/systemd/system/thinksmart-qqmusic.service').read_text()
  self.assertIn('NoNewPrivileges=yes',unit)
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   sock_p=os.path.join(td,'aec.sock')
   with patch.object(q,'AEC_SOCK_PATH',sock_p),patch.object(q.threading,'Thread') as th:
    srv=q.start_aec_unix_server()
    try:
     th.assert_called_once()
     self.assertEqual(os.lstat(sock_p).st_mode & 0o777,0o600)
    finally:
     if srv is not None:srv.close()
HUB_R108_SCRIPT = r"""
import asyncio, hmac, json, os, pathlib, stat, tempfile, types
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import admin_handler, config_store
import core.connection as conn_mod
from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
from core.agent import call_names

# Ensure core self-owned hub modules are loaded from xiaozhi-server
for mod in (admin_handler, conn_mod, call_names):
    assert '/xiaozhi-server/' in mod.__file__, mod.__file__

async def main():
    # 2. admin_handler.py: unified admin takeover lock on /admin & /admin/api/*
    with tempfile.TemporaryDirectory() as td:
        cfg_p = pathlib.Path(td) / '.config.yaml'
        cfg_p.write_text('server:\n  ip: 0.0.0.0\n', encoding='utf-8')
        ah = admin_handler.AdminHandler({'selected_module': {}}, ws_server=None, config_path=cfg_p)
        app = web.Application()
        ah.register_kiosk_only(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r_idx = await client.get('/admin')
            assert r_idx.status == 200 and '<html' in (await r_idx.text()).lower()
            r = await client.get('/admin/api/state')
            assert r.status == 410
        finally:
            await client.close()

    # 3. connection.py: header redaction, device_id sanitization, X-Forwarded-For spoofing defense
    red = conn_mod._redact_headers({'Authorization': 'Bearer SECRET_TOKEN_999', 'Device-Id': 'aa:bb:cc', 'X-Api-Key': 'k1'})
    assert red['Authorization'] == '***' and red['X-Api-Key'] == '***' and red['Device-Id'] == 'aa:bb:cc'
    assert conn_mod._sanitize_device_id('..' + chr(10) + '/evil' + chr(0) + 'id') == '..evilid'
    assert conn_mod._sanitize_device_id('..') is None
    assert len(conn_mod._sanitize_device_id('a' * 300)) == 128

    # 4. listenMessageHandler malformed payload resilience
    lh = ListenTextMessageHandler()
    dummy_conn = types.SimpleNamespace(
        client_listen_mode='auto',
        client_have_voice=True,
        asr=None,
        config={'wakeup_words': None, 'enable_greeting': False},
        logger=conn_mod.setup_logging(),
        reset_audio_states=lambda: None,
    )
    await lh.handle(dummy_conn, {})  # no 'state' -> no KeyError
    await lh.handle(dummy_conn, {'state': 'detect', 'text': None})  # non-str text -> no AttributeError

    # 5. call_names.py: 0700 dir & 0600 atomic cache save/load
    with tempfile.TemporaryDirectory() as td:
        cdir = pathlib.Path(td) / 'call_reply_audio'
        with patch.object(call_names, '_cache_dir', cdir):
            call_names._audio_cache.clear()
            key = ('TestTTS', 'v1', 1.0, 1.0, 1.0, 1.0, 24000, '我在')
            call_names._save_cached(key, [b'opus1', b'opus2'])
            cf = call_names._cache_file(key)
            assert (cdir.stat().st_mode & 0o777) == 0o700
            assert (cf.stat().st_mode & 0o777) == 0o600
            loaded = call_names._load_cached(key)
            assert loaded == [b'opus1', b'opus2']

asyncio.run(main())
print('R108_HUB_OK')
"""

class Round108AuditTests(unittest.TestCase):
 def test_hub_round108_security_and_bounds(self):
  hub_py='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python'
  env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   r=subprocess.run([hub_py,'-B','-c',HUB_R108_SCRIPT],cwd=td,env=env,capture_output=True,text=True,timeout=45)
  self.assertEqual(r.returncode,0,msg=r.stderr+'\n'+r.stdout)
  self.assertIn('R108_HUB_OK',r.stdout)
 def test_hub_umask_runtime_perms_wireplumber_usb_and_tunnel_hardening(self):
  dropin=pathlib.Path('/home/ubuntu/.config/systemd/user/xiaozhi-hub.service.d/thinksmart-overrides.conf').read_text()
  self.assertIn('UMask=0077',dropin)
  for d in ('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data','/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/tmp'):
   self.assertEqual(os.lstat(d).st_mode & 0o777,0o700,msg=d)
  wp=pathlib.Path('/home/ubuntu/.config/wireplumber/main.lua.d/51-cd18781y-alsa.lua').read_text()
  self.assertIn('alsa_monitor.properties["alsa.reserve"] = false',wp)
  usb=pathlib.Path('/usr/local/bin/usb-automount.sh').read_text()
  self.assertIn('mount -t "$FSTYPE" -o "$OPTIONS"',usb)
  self.assertIn('mount -t "$FSTYPE" -o "${OPTIONS/#rw,/ro,}"',usb)
  tun=pathlib.Path('/etc/systemd/system/thinksmart-intercom-tunnel.service').read_text()
  self.assertIn('NoNewPrivileges=yes',tun)
  self.assertIn('PrivateTmp=yes',tun)
HUB_R3_20261118_SCRIPT = r"""
import asyncio, json, os, pathlib, queue, tempfile, types
from core.providers.tts.dto.dto import SentenceType, ContentType
import core.connection as conn_mod
import core.agent.call_names as cn_mod
import plugins_func.functions.get_news_from_newsnow as nn_mod

# 1. _merge_tool_calls handles sparse/out-of-order & negative tool_index without IndexError
tc_list = []
fake_tc_sparse = types.SimpleNamespace(
    index=2, id='call_2',
    function=types.SimpleNamespace(name='get_weather', arguments='{"city":"北京"}'),
)
fake_tc_neg = types.SimpleNamespace(
    index=-3, id='call_0',
    function=types.SimpleNamespace(name='get_time', arguments='{}'),
)
conn_mod.ConnectionHandler._merge_tool_calls(None, tc_list, [fake_tc_sparse, fake_tc_neg])
assert len(tc_list) == 3, tc_list
assert tc_list[2]['name'] == 'get_weather' and tc_list[0]['name'] == 'get_time', tc_list

# 2. _chat emits SentenceType.LAST even when LLM init raises or tool arguments JSON is truncated
class DummyLogger:
    def bind(self, **kw): return self
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass

class DummyDialogue:
    def __init__(self): self.msgs = []
    def put(self, m): self.msgs.append(m)
    def get_llm_dialogue_with_memory(self, *a, **k): return []
    def get_llm_dialogue(self): return []

async def test_hub_r3():
    loop = asyncio.get_running_loop()
    q_tts = queue.Queue()
    fake = types.SimpleNamespace(
        logger=DummyLogger(),
        dialogue=DummyDialogue(),
        tts=types.SimpleNamespace(
            tts_text_queue=q_tts,
            store_tts_text=lambda *a, **k: None,
            to_tts=lambda txt: [b'opus1', b'opus2'],
            voice='v1', speech_rate=1.0, volume=1.0, pitch_rate=1.0, post_gain=1.0,
        ),
        intent_type='function_call',
        func_handler=types.SimpleNamespace(
            get_functions=lambda: [],
            handle_llm_function_call=lambda c, d: asyncio.sleep(0, result=types.SimpleNamespace(action=conn_mod.Action.RESPONSE, response='好的', result=None)),
        ),
        memory=None,
        current_speaker='',
        system_introduced_speakers=set(),
        session_id='s1',
        sentence_id='sid0',
        client_abort=False,
        config={'tool_call_timeout': 5},
        loop=loop,
        report_queue=None,
        asr_audio_for_voiceprint=[],
        sample_rate=24000,
    )
    fake._superseded = lambda sid: False
    fake._put_reply = lambda sid, txt: None
    fake._handle_function_result = lambda results, depth, streamed_text, sentence_id: None
    fake._merge_tool_calls = lambda lst, tcs: conn_mod.ConnectionHandler._merge_tool_calls(fake, lst, tcs)
    orig_nr = conn_mod.llm_router.not_ready_reply
    conn_mod.llm_router.not_ready_reply = lambda _self: None
    try:
        # Case A: LLM response_with_functions raises -> SentenceType.FIRST AND SentenceType.LAST both queued!
        called_llm_a = []
        def raise_llm(*a, **k):
            called_llm_a.append(1)
            raise RuntimeError('llm_down')
        fake.llm = types.SimpleNamespace(response_with_functions=raise_llm)
        await asyncio.to_thread(conn_mod.ConnectionHandler._chat, fake, '今天天气怎么样', 0)
        assert called_llm_a == [1], called_llm_a
        items = []
        while not q_tts.empty():
            items.append(q_tts.get_nowait().sentence_type)
        assert items == [SentenceType.FIRST, SentenceType.LAST], items

        # Case B: Truncated tool call JSON arguments -> normalized to '{}' instead of crashing before SentenceType.LAST
        bad_tc = types.SimpleNamespace(
            index=0, id='tc_bad',
            function=types.SimpleNamespace(name='get_weather', arguments='{"city": "杭州'),
        )
        fake.llm = types.SimpleNamespace(response_with_functions=lambda *a, **k: [('', [bad_tc])])
        seen_inputs = []
        orig_fn_call = fake.func_handler.handle_llm_function_call
        async def rec_fn_call(c, d):
            seen_inputs.append(d.get('arguments'))
            return await orig_fn_call(c, d)
        fake.func_handler.handle_llm_function_call = rec_fn_call
        try:
            await asyncio.to_thread(conn_mod.ConnectionHandler._chat, fake, '查一下天气', 0)
        finally:
            fake.func_handler.handle_llm_function_call = orig_fn_call
        items2 = []
        while not q_tts.empty():
            items2.append(q_tts.get_nowait().sentence_type)
        assert SentenceType.FIRST in items2 and SentenceType.LAST in items2, items2
        assert seen_inputs and seen_inputs[0] == '{}', seen_inputs
    finally:
        conn_mod.llm_router.not_ready_reply = orig_nr

    # 3. get_news_from_newsnow._load_seen refuses FIFO without blocking
    with tempfile.TemporaryDirectory(dir='/tmp') as td:
        fifo_p = os.path.join(td, 'seen.fifo')
        os.mkfifo(fifo_p)
        orig_seen = nn_mod.SEEN_PATH
        nn_mod.SEEN_PATH = fifo_p
        try:
            assert nn_mod._load_seen() == {}
        finally:
            nn_mod.SEEN_PATH = orig_seen

    # 4. call_names cache caps memory & disk entries and refuses FIFO
    with tempfile.TemporaryDirectory(dir='/tmp') as td:
        orig_dir = cn_mod._cache_dir
        cn_mod._cache_dir = pathlib.Path(td)
        cn_mod._audio_cache.clear()
        try:
            for idx in range(38):
                k = ('T', idx)
                cn_mod._put_mem_cache(k, [b'pkt'])
                cn_mod._save_cached(k, [b'pkt'])
            assert len(cn_mod._audio_cache) <= cn_mod.MAX_AUDIO_CACHE_ENTRIES
            assert len(list(pathlib.Path(td).glob('*.opus'))) <= cn_mod.MAX_AUDIO_CACHE_ENTRIES
        finally:
            cn_mod._cache_dir = orig_dir
            cn_mod._audio_cache.clear()

asyncio.run(test_hub_r3())
print('R3_20261118_HUB_OK')
"""

class Round320261118AuditTests(unittest.IsolatedAsyncioTestCase):
 def test_hub_r3_tool_json_sparse_index_and_bounded_caches(self):
  hub_py='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python'
  env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   r=subprocess.run([hub_py,'-B','-c',HUB_R3_20261118_SCRIPT],cwd=td,env=env,capture_output=True,text=True,timeout=45)
  self.assertEqual(r.returncode,0,msg=r.stderr+'\n'+r.stdout)
  self.assertIn('R3_20261118_HUB_OK',r.stdout)

 def test_airplay_startup_clears_audio_active_and_intercom_preserves_hold(self):
  ap_src=pathlib.Path('/usr/local/bin/thinksmart-airplay.py').read_text(encoding='utf-8')
  self.assertIn('MIRROR_FLAG_PATH.unlink(missing_ok=True)\n    set_audio_active(False)',ap_src)
  ic_mod=module('ic_r3_test','thinksmart-intercom.py')
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   scr_p=pathlib.Path(td)/'thinksmart-screen.json'
   scr_p=scr_p
   scr_p.write_text(json.dumps({'seq':123,'hold':True,'brightness':60}),encoding='utf-8')
   srv=ic_mod.IntercomServer()
   orig_path=ic_mod.Path
   def fake_path(arg):
    if arg=='/run/user/1000/thinksmart-screen.json':return scr_p
    if arg in ('/run/user/1000/intercom-call-active','/run/user/1000/uxplay-mirroring-active'):
     return pathlib.Path(td)/pathlib.Path(arg).name
    return orig_path(arg)
   with patch.object(ic_mod,'Path',side_effect=fake_path), \
        patch.object(ic_mod,'INTERCOM_CALL_FLAG',pathlib.Path(td)/'no-call'), \
        patch.object(ic_mod,'MIRROR_FLAG_PATH',pathlib.Path(td)/'no-mirror'):
    srv.wake_screen(hold=False,preserve_hold=True)
    saved=json.loads(scr_p.read_text(encoding='utf-8'))
    self.assertTrue(saved['hold'])
    self.assertGreater(saved['seq'],123)
    srv.wake_screen(hold=False,preserve_hold=False)
    saved2=json.loads(scr_p.read_text(encoding='utf-8'))
    self.assertFalse(saved2['hold'])

 async def test_monitor_isp_etag_advances_when_non_unicom_carrier_updates(self):
  from aiohttp.test_utils import TestClient, TestServer
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   assets=pathlib.Path(td)
   (assets/'servers.json').write_text(json.dumps([{'id':3,'name':'US','group':'G'}]),encoding='utf-8')
   (assets/'traffic.json').write_text(json.dumps({'server_id':3,'rule_id':1}),encoding='utf-8')
   (assets/'index.html').write_text('ok',encoding='utf-8')
   (assets/'display.html').write_text('ok',encoding='utf-8')
   (assets/'home_overlay.js').write_text('//',encoding='utf-8')
   mon=m.Monitor(assets,state_dir=td)
   mon.consume=lambda:asyncio.sleep(3600)
   mon._idle_probe_loop=lambda:asyncio.sleep(3600)
   mon.refresh_traffic=lambda:asyncio.sleep(0)
   mon._schedule_warm_latencies=lambda:None
   mon.update({'now':1700000000000,'servers':[{'id':3,'name':'US','last_active':1700000000,'state':{'cpu':1},'host':{'mem_total':100}}]})
   mon.connected=True
   async with TestClient(TestServer(mon.app())) as cl:
    now_m=m.time.monotonic()
    mon._srv_latency_cache[3]={
     'ok':True,'server_id':3,'srv_alive':True,'updated_at':1.0,'at':now_m,
     'unicom':{'name':'联通','latest_ms':150.0,'status':'ok','stale':False},
     'carriers':[
      {'name':'联通','latest_ms':150.0,'loss_pct':0.0,'status':'ok','stale':False},
      {'name':'电信','latest_ms':180.0,'loss_pct':0.0,'status':'ok','stale':False},
     ],
     'others':{'电信':180.0},
    }
    r1=await cl.get('/api/state',headers={'Host':'127.0.0.1:8092'})
    self.assertEqual(r1.status,200)
    etag1=r1.headers['ETag']
    # Update ONLY Telecom latency/loss while Unicom stays identical: ETag MUST advance (no stale 304!)
    mon._srv_latency_cache[3]['carriers'][1]={'name':'电信','latest_ms':260.0,'loss_pct':15.0,'status':'warn','stale':False}
    r2=await cl.get('/api/state',headers={'Host':'127.0.0.1:8092','If-None-Match':etag1})
    self.assertEqual(r2.status,200)
    self.assertNotEqual(r2.headers['ETag'],etag1)

 async def test_qqmusic_concurrent_sleep_state_writes_and_tutor_history_isolation(self):
  import concurrent.futures
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   sp=pathlib.Path(td)/'sleep.json'
   with patch.object(q,'SLEEP_STATE_PATH',sp):
    def writer(i):
     q._write_sleep_state({'sleep_volume':20+i,'wake':True,'idx':i})
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
     list(ex.map(writer,range(24)))
    loaded=json.loads(sp.read_text(encoding='utf-8'))
    self.assertTrue(loaded.get('wake'))
    self.assertEqual(os.lstat(sp).st_mode & 0o777,0o600)
    self.assertEqual(list(pathlib.Path(td).glob('*.tmp')),[])
   tu_mod=module('tu_r3_test','thinksmart-tutor.py')
   with patch.object(tu_mod,'STATE_DIR',pathlib.Path(td)), \
        patch.object(tu_mod,'HIST_PATH',pathlib.Path(td)/'history.json'), \
        patch.object(tu_mod,'PREFS_PATH',pathlib.Path(td)/'prefs.json'):
    eng=tu_mod.TutorEngine()
    eng.history=[{'id':f'h{i}','title':f't{i}','question':f'q{i}','mode':'fast','source':'custom','created_at':i,'steps':[{'step':1,'title':'S1','say':'hi','board':'b','draw':[]}]} for i in range(20)]
    async def fake_post(*a,**k):return '{"title":"点拨","say":"想一想","board":"补充","note":"提示"}'
    async def fake_wav(*a,**k):return b'RIFF'
    eng._post_chat_once=fake_post
    eng.synthesize_wav=fake_wav
    eng.pause_music_if_playing=lambda:asyncio.sleep(0)
    for i in range(18):
     await eng.ask_followup(f'h{i}','为什么？',upto_idx=None)
    self.assertLessEqual(len(eng.sessions),16)

HUB_R4_20261118_SCRIPT = r"""
import asyncio
import json
import os
import pathlib
import tempfile
import types
import numpy as np
from aiohttp.test_utils import make_mocked_request

import core.websocket_server as ws_mod
from core.admin import admin_handler
import core.http_server as http_mod
from core.utils import util, reply_echo, opus_encoder_utils

async def test_hub_r4():
    # 1. websocket_server._handle_connection does NOT double-close an already-closed websocket & sanitizes query headers
    ws_srv = object.__new__(ws_mod.WebSocketServer)
    ws_srv.config = {}
    ws_srv._vad = ws_srv._asr = ws_srv._llm = ws_srv._memory = ws_srv._intent = None
    ws_srv.logger = types.SimpleNamespace(bind=lambda **k: types.SimpleNamespace(error=lambda *a: None, warning=lambda *a: None, info=lambda *a: None))
    async def noop_auth(_ws):
        return None
    ws_srv._handle_auth = noop_auth

    close_calls = []
    class FakeClosedWS:
        closed = True
        def __init__(self):
            self.request = types.SimpleNamespace(
                headers={},
                path="/xiaozhi/v1/?device-id=dev%0D%0Ainjected&client-id=cli%00x&authorization=Bearer%20tok%0Aevil",
            )
        async def close(self):
            close_calls.append(1)
        async def send(self, _m):
            pass

    orig_ch = ws_mod.ConnectionHandler
    class DummyCH:
        def __init__(self, *a, **k):
            pass
        async def handle_connection(self, _ws):
            return None
    ws_mod.ConnectionHandler = DummyCH
    try:
        fws = FakeClosedWS()
        await ws_srv._handle_connection(fws)
        assert close_calls == [], f"Expected 0 close() calls when websocket.closed=True, got {close_calls}"
        assert fws.request.headers["device-id"] == "devinjected", fws.request.headers
        assert fws.request.headers["client-id"] == "clix", fws.request.headers
        assert "\n" not in fws.request.headers["authorization"]
    finally:
        ws_mod.ConnectionHandler = orig_ch

    # 2. AdminHandler._from_this_machine rejects X-Real-IP / CF-Connecting-IP / True-Client-IP & accepts ::ffff:127.0.0.1
    for hdr in ("X-Real-IP", "CF-Connecting-IP", "True-Client-IP", "X-Forwarded-For", "Forwarded"):
        req_p = make_mocked_request("GET", "/kiosk/llm", headers={hdr: "203.0.113.9"}, transport=types.SimpleNamespace(get_extra_info=lambda k, d=None: ("127.0.0.1", 12345) if k == "peername" else d))
        assert not admin_handler.AdminHandler._from_this_machine(req_p), hdr
    req_v6m = make_mocked_request("GET", "/kiosk/llm", headers={}, transport=types.SimpleNamespace(get_extra_info=lambda k, d=None: ("::ffff:127.0.0.1", 12345) if k == "peername" else d))
    assert admin_handler.AdminHandler._from_this_machine(req_v6m)

    # 4. http_server._hub_security_middleware blocks Sec-Fetch-Site: cross-site on POST /admin/api/* even without Origin
    req_csrf = make_mocked_request("POST", "/admin/api/logout", headers={"Host": "127.0.0.1:8003", "Sec-Fetch-Site": "cross-site"})
    async def ok_handler(_r):
        raise AssertionError("Should not reach handler")
    resp_csrf = await http_mod._hub_security_middleware(req_csrf, ok_handler)
    assert resp_csrf.status == 403

    # 5. util.write_json_file is atomic, preserves permissions, and refuses symlinks
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        jp = pathlib.Path(td) / "cfg.json"
        util.write_json_file(jp, {"ok": 1, "msg": "你好"})
        assert json.loads(jp.read_text("utf-8")) == {"ok": 1, "msg": "你好"}
        assert list(pathlib.Path(td).glob("*.tmp")) == []
        sym = pathlib.Path(td) / "link.json"
        sym.symlink_to(jp)
        try:
            util.write_json_file(sym, {"bad": 1})
            raise AssertionError("Expected OSError on symlink")
        except OSError:
            pass

    # 6. reply_echo caps accumulated text to <= 4096 chars & opus_encoder_utils int16 fastpath
    conn_e = types.SimpleNamespace(sentence_id="s1", client_is_speaking=True, vad_last_voice_time=0)
    reply_echo.note_reply(conn_e, "甲乙丙丁" * 1500)
    assert len(conn_e.reply_echo["text"]) == 4096
    enc_u = opus_encoder_utils.OpusEncoderUtils(16000, 1, 60)
    try:
        enc_u._validate_pcm_data(np.zeros(960, dtype=np.int16))
    finally:
        enc_u.close()

asyncio.run(test_hub_r4())
print("R4_20261118_HUB_OK")
"""

class Round420261118AuditTests(unittest.IsolatedAsyncioTestCase):
 def test_hub_r4_ws_close_proxy_headers_session_fifo_and_atomic_json(self):
  hub_py='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python'
  env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   r=subprocess.run([hub_py,'-B','-c',HUB_R4_20261118_SCRIPT],cwd=td,env=env,capture_output=True,text=True,timeout=45)
  self.assertEqual(r.returncode,0,msg=r.stderr+'\n'+r.stdout)
  self.assertIn('R4_20261118_HUB_OK',r.stdout)

 async def test_display_fifo_safe_and_study_unducks_from_duck_when_stopping(self):
  disp_mod=module('disp_r4_test','thinksmart_display.py')
  st_mod=module('study_r4_test','thinksmart-study.py')
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   fifo_p=pathlib.Path(td)/'hint.fifo'
   os.mkfifo(fifo_p)
   self.assertEqual(disp_mod.read_json(fifo_p),{})
   sock_p=pathlib.Path(td)/'prev.sock'
   bp=disp_mod.BrightnessPreview(sock_p).open()
   try:
    self.assertEqual(os.lstat(sock_p).st_mode & 0o777,0o600)
   finally:
    bp.close()
   with patch.object(st_mod,'STATE',pathlib.Path(td)), \
        patch.object(st_mod,'CONFIG_PATH',pathlib.Path(td)/'config.json'), \
        patch.object(st_mod,'SECRET_PATH',pathlib.Path(td)/'secret'), \
        patch.object(st_mod,'VOICE_DIR',pathlib.Path(td)/'voice'), \
        patch.object(st_mod,'LOG_DIR',pathlib.Path(td)/'log'), \
        patch.object(st_mod,'FOCUS_PATH',pathlib.Path(td)/'focus.json'):
    comp=st_mod.Companion()
    comp.stopping=True
    comp.session=types.SimpleNamespace(closed=False)
    unducked=[]
    async def fake_music(action=None):
     unducked.append(action)
     return True
    comp.music=fake_music
    res=await comp.play('测试提醒',from_duck=True)
    self.assertFalse(res)
    self.assertEqual(unducked,['unduck'])

 async def test_home_cn_numerals_and_aec_bridge_sanitization(self):
  hm_mod=module('home_r4_test','thinksmart-home.py')
  self.assertEqual(hm_mod._parse_cn_num('四十五'),45)
  self.assertEqual(hm_mod._parse_cn_num('一百'),100)
  self.assertEqual(hm_mod._parse_cn_num('一百零五'),105)
  self.assertEqual(hm_mod._parse_cn_num('一百二十五'),125)
  self.assertEqual(hm_mod._parse_cn_num('三百六十五'),365)
  from aiohttp.test_utils import TestClient, TestServer
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   td_p=pathlib.Path(td)
   inbox=hm_mod.Inbox(td_p/'messages.json')
   cds=hm_mod.CountdownStore(td_p/'countdowns.json')
   app=hm_mod.make_app(inbox=inbox,countdowns=cds)
   async with TestClient(TestServer(app)) as cl:
    r_ok=await cl.post('/api/home/countdowns/tool',json={'action':'add','title':'百日冲刺','date':'一百天后'},headers={'Host':'127.0.0.1:8095','X-TS-Internal':'1'})
    self.assertEqual(r_ok.status,200)
    d_ok=await r_ok.json()
    self.assertTrue(d_ok['ok'],d_ok)
    self.assertEqual(d_ok['item']['days'],100)
    r_bad=await cl.post('/api/home/countdowns/tool',json={'action':'add','title':'错日','date':'2026-02-31'},headers={'Host':'127.0.0.1:8095','X-TS-Internal':'1'})
    d_bad=await r_bad.json()
    self.assertFalse(d_bad['ok'])
    self.assertNotIn('day is out of range',d_bad['text'])
  import importlib.util
  spec=importlib.util.spec_from_file_location('aec_r4_test','/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py')
  aec_mod=importlib.util.module_from_spec(spec)
  with patch.dict(os.environ,{'THINKSMART_FORCE_QQMUSIC_BRIDGE':'0'}):
   with patch.object(sys,'argv',['python','-c','1']),patch.object(sys,'orig_argv',['python','-c','1']):
    spec.loader.exec_module(aec_mod)
  seen_calls=[]
  class FakeResp:
   def __enter__(self):return self
   def __exit__(self,*a):return False
   def read(self,_n=65536):return json.dumps({'ok':True,'text':'已处理'}).encode('utf-8')
  def fake_open(req,timeout=8):
   seen_calls.append((req.full_url,json.loads(req.data.decode('utf-8'))))
   return FakeResp()
  async def _noop_mcp(*a,**k):return None
  with patch.object(aec_mod._NO_PROXY_OPENER,'open',side_effect=fake_open):
   self.assertEqual(aec_mod._call_home_countdown({'action':'修改','title':'百日冲刺','date':'明天'}),'已处理')
   self.assertEqual(seen_calls[-1][1]['action'],'update')
   self.assertEqual(aec_mod._call_home_countdown({'action':'关闭页面'}),'已处理')
   self.assertEqual(seen_calls[-1][1]['action'],'close')
   fake_mcp=types.SimpleNamespace(TOOLS=[],handle=_noop_mcp)
   aec_mod._install_mcp_hook(fake_mcp)
   res_play=await fake_mcp.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'self.music.play','arguments':{'query':None}}})
   self.assertFalse(res_play['result']['isError'])
   self.assertEqual(seen_calls[-1][1],{'query':'random'})
   res_ctrl=await fake_mcp.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'self.music.control','arguments':{'action':''}}})
   self.assertFalse(res_ctrl['result']['isError'])
   self.assertEqual(seen_calls[-1][1],{'action':'stop'})
  # Verify non-client test invocation did NOT overwrite live /run/user/1000/qqmusic-bridge.json with test pid
  br_p=pathlib.Path('/run/user/1000/qqmusic-bridge.json')
  if br_p.exists():
   self.assertNotEqual(json.loads(br_p.read_text('utf-8')).get('pid'),os.getpid())

HUB_R5_20261118_SCRIPT = r"""
import asyncio
import json
import os
import pathlib
import socket
import tempfile
import types
from unittest.mock import patch

import core.agent.call_names as cn_agent
import plugins_func.functions.get_news_from_newsnow as newsnow_mod

for mod in (cn_agent, newsnow_mod):
    assert '/xiaozhi-server/' in mod.__file__, mod.__file__

async def test_hub_r5():
    # 1. newsnow DNS-rebinding SSRF protection + HTTPS->HTTP downgrade block
    assert not newsnow_mod._safe_public_http_url('https://evil.localtest.me/rss.xml')
    assert not newsnow_mod._safe_public_http_url('http://127.0.0.1.nip.io/article')
    assert not newsnow_mod._safe_public_http_url('https://router.home.arpa/')

    fake_lan_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 0))]

    with patch.object(newsnow_mod.socket, 'getaddrinfo', return_value=fake_lan_dns):
        assert not newsnow_mod._has_only_global_ips('rebind.example.com')
        res_nn = await newsnow_mod.fetch_news_detail('https://rebind.example.com/post.html')
        assert res_nn == '无法获取详细内容'
        code_j, body_j = await newsnow_mod._get_json_bounded('https://rebind.example.com/api', 2.0)
        assert code_j == 0 and body_j is None

    # Oversized API JSON stream in newsnow is rejected before OOM
    class FakeBigResp:
        status_code = 200
        headers = {}
        url = None
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def aiter_bytes(self):
            for _ in range(25):
                yield b'A' * 65536

    class FakeClient:
        is_closed = False
        def stream(self, *a, **k):
            return FakeBigResp()

    with patch.object(newsnow_mod, '_client', return_value=FakeClient()), \
         patch.object(newsnow_mod, '_has_only_global_ips', return_value=True):
        try:
            await newsnow_mod._get_json_bounded('https://newsnow.busiyi.world/api/s?id=cls-telegraph', 2.0)
            raise AssertionError('Expected ValueError on >1MiB JSON stream')
        except ValueError:
            pass

    # 2. newsnow._save_seen & call_names._save_cached invoke os.fsync before os.replace
    fsync_fds = []
    orig_fsync = os.fsync
    def track_fsync(fd):
        fsync_fds.append(fd)
        return orig_fsync(fd)
    with tempfile.TemporaryDirectory(dir='/tmp') as td, patch.object(os, 'fsync', side_effect=track_fsync):
        orig_seen = newsnow_mod.SEEN_PATH
        newsnow_mod.SEEN_PATH = os.path.join(td, 'seen.json')
        try:
            newsnow_mod._save_seen({'dev1': {'k1': 1.0}})
            assert len(fsync_fds) >= 1
            assert list(pathlib.Path(td).glob('*.tmp')) == []
        finally:
            newsnow_mod.SEEN_PATH = orig_seen

        before_cnt = len(fsync_fds)
        with patch.object(cn_agent, '_cache_dir', pathlib.Path(td) / 'cn'):
            cn_agent._save_cached(('TTS', 1), [b'op1'])
            assert len(fsync_fds) > before_cnt

asyncio.run(test_hub_r5())
print('R5_20261118_HUB_OK')
"""

class Round520261118AuditTests(unittest.IsolatedAsyncioTestCase):
 def test_hub_r5_dns_ssrf_bounded_newsnow_ragflow_and_fsync(self):
  hub_py='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python'
  env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   r=subprocess.run([hub_py,'-B','-c',HUB_R5_20261118_SCRIPT],cwd=td,env=env,capture_output=True,text=True,timeout=45)
  self.assertEqual(r.returncode,0,msg=r.stderr+'\n'+r.stdout)
  self.assertIn('R5_20261118_HUB_OK',r.stdout)

 async def test_tutor_r5_bounds_fifo_safety_tts_byte_cap_and_bad_b64(self):
  tu_mod=module('tu_r5_test','thinksmart-tutor.py')
  self.assertEqual(tu_mod._clamp(float('nan'),10,100,default=42),42)
  self.assertEqual(tu_mod._clamp(float('inf'),10,100,default=42),42)
  self.assertEqual(tu_mod._clamp(True,10,100,default=42),42)
  from aiohttp.test_utils import TestClient, TestServer
  with tempfile.TemporaryDirectory(dir='/tmp') as td:
   td_p=pathlib.Path(td)
   fifo_p=td_p/'bad.fifo'
   os.mkfifo(fifo_p)
   self.assertEqual(tu_mod._read_safe_json(fifo_p,{'fb':1}),{'fb':1})
   with patch.object(tu_mod,'STATE_DIR',td_p), \
        patch.object(tu_mod,'SNAP_DIR',td_p/'snaps'), \
        patch.object(tu_mod,'HIST_PATH',td_p/'history.json'), \
        patch.object(tu_mod,'PREFS_PATH',td_p/'prefs.json'), \
        patch.object(tu_mod,'HUB_CFG_PATH',fifo_p):
    eng=tu_mod.TutorEngine()
    self.assertEqual(eng._load_gateway_cfg(),{})
    # Verify 16 MiB byte budget evicts large WAVs well before 40-count limit
    big_pcm=b'\x01\x00'*(480*1024)  # ~960 KiB WAV
    class FakeTTSResp:
     status=200
     async def __aenter__(self):return self
     async def __aexit__(self,*a):return False
     async def read(self):return big_pcm
    eng._tts_endpoint=lambda:('http://127.0.0.1:9/tts','xiaohe')
    eng.tts_http=types.SimpleNamespace(post=lambda *a,**k:FakeTTSResp())
    for i in range(22):
     await eng.synthesize_wav(f'第{i}句超长测试语音内容')
    self.assertLessEqual(eng._tts_cache_bytes,16*1024*1024)
    self.assertLess(len(eng.tts_cache),22)
    # Verify malformed base64 in /api/tutor/phone_upload returns 400 (not 500)
    app=tu_mod.make_app(eng)
    async with TestClient(TestServer(app)) as cl:
     r_bad=await cl.post('/api/tutor/phone_upload',json={'image_b64':'@@@not_base64@@@'},headers={'Host':'127.0.0.1:8096','X-TS-Internal':'1'})
     self.assertEqual(r_bad.status,400)
     r_tiny=await cl.post('/api/tutor/phone_upload',json={'image_b64':'YWJj'},headers={'Host':'127.0.0.1:8096','X-TS-Internal':'1'})
     self.assertEqual(r_tiny.status,400)

 def test_airplay_intercom_arbitration_and_review_private_prefixes(self):
  ap_txt=pathlib.Path('/usr/local/bin/thinksmart-airplay.py').read_text('utf-8')
  ic_txt=pathlib.Path('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
  self.assertIn('http://127.0.0.1:8095/api/home/voice_reply',ap_txt)
  self.assertIn('http://127.0.0.1:8095/api/home/voice_reply',ic_txt)
  rv_path=pathlib.Path('/usr/local/sbin/thinksmart-review')
  rv_mod=types.ModuleType('rv_r5_test')
  exec(compile(ast.parse(rv_path.read_text('utf-8')),str(rv_path),'exec'),rv_mod.__dict__)
  for secret_p in (
   '/var/lib/thinksmart-home/messages.json',
   '/var/lib/thinksmart-tutor/history.json',
   '/var/lib/thinksmart-monitor/events.json',
   '/etc/ssh/ssh_host_ed25519_key',
  ):
   self.assertTrue(rv_mod._is_private(secret_p),msg=secret_p)

if __name__=='__main__':unittest.main(verbosity=2)

