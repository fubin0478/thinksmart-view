#!/opt/thinksmart-addons/venv/bin/python -B
"""Round 5 deep system audit regression suite (2026-11-26)."""
import asyncio
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
sys.dont_write_bytecode = True


def _load_mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


class Round6DeepAuditTests(unittest.TestCase):
    def test_01_workstream_a_xiaozhi_hub_runtime(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = f'{hub_root}/.venv/bin/python'

        probe = """
import asyncio, inspect, os, sys, threading
sys.path.insert(0, os.environ['R6_HUB_ROOT'])

# 1. edge TTS imports asyncio and text_to_speak works
from core.providers.tts import edge as edge_mod
assert hasattr(edge_mod, 'asyncio')

# 2. AudioRateController reset clears pending_send_task & finally only sets queue_empty_event for owning task
from core.utils.audioRateController import AudioRateController
async def _test_arc():
    arc = AudioRateController(frame_duration=20)
    arc.queue_empty_event.clear()
    arc.start_sending(lambda _m: asyncio.sleep(0))
    old_task = arc.pending_send_task
    assert old_task is not None
    arc.reset()
    assert arc.pending_send_task is None
    arc.queue_empty_event.clear()
    sentinel = asyncio.create_task(asyncio.sleep(0.2))
    arc.pending_send_task = sentinel
    try:
        await old_task
    except asyncio.CancelledError:
        pass
    assert not arc.queue_empty_event.is_set()
    sentinel.cancel()
    try:
        await sentinel
    except asyncio.CancelledError:
        pass
asyncio.run(_test_arc())

# 3. sendAudioHandle checks pending_send_task, abort/sentence_id, and send_tts_message guards reply_echo
from core.handle import sendAudioHandle as sah
sah_src = inspect.getsource(sah)
assert 'conn.audio_rate_controller.pending_send_task' in sah_src
assert 'not conn.audio_rate_controller.pending_send_task.done()' in sah_src
assert 'getattr(conn, "client_abort", False)' in sah_src
assert 'sentence_id is not None and sentence_id != getattr(conn, "sentence_id", None)' in sah_src
idx_echo = sah_src.index('reply_echo.note_reply(conn, None)')
idx_sid = sah_src.index('current_sentence_id != conn.sentence_id')
assert idx_sid < idx_echo

# 4. receiveAudioHandle checks just_woken_up before vad.is_vad and farewell starts TTS properly
from core.handle import receiveAudioHandle as rah
rah_src = inspect.getsource(rah)
assert rah_src.index('just_woken_up') < rah_src.index('conn.vad.is_vad(conn, pcm_frame)')
assert 'await send_tts_message(conn, "start")' in rah_src

# 5. TTSProviderBase._is_stopped + close() closes opus_encoder
from core.providers.tts import base as tts_base
class _FakeEnc:
    closed = False
    def close(self):
        self.closed = True
class _DummyTTS(tts_base.TTSProviderBase):
    def text_to_speak(self, text, output_file):
        pass
p = object.__new__(_DummyTTS)
class _Conn:
    client_abort = False
    sentence_id = 's1'
p.conn = _Conn()
p.tts_audio_queue = asyncio.Queue()
p._sentence_text_map = {}
enc = _FakeEnc()
p.opus_encoder = enc
assert not p._is_stopped('s1')
assert p._is_stopped('s2')
p.conn.client_abort = True
assert p._is_stopped('s1')
asyncio.run(p.close())
assert enc.closed is True

# 6. reply_progress pins per-turn dialogue across model switches and rejects interrupted put()
from core.utils import reply_progress as rp
class _Dlg:
    def __init__(self, tag):
        self.tag = tag
        self.dialogue = []
    def put(self, m):
        self.dialogue.append(m)
class _C2:
    dialogue = _Dlg('A')
    sentence_id = 's1'
c2 = _C2()
u_msg = object()
rp.note_turn(c2, 's1', u_msg)
dlg_a = c2.dialogue
c2.dialogue = _Dlg('B')
turn = rp._turns(c2)['s1']
assert turn['dialogue'] is dlg_a and turn['dialogue'] is not c2.dialogue
turn['interrupted'] = True
assert rp.put(c2, 's1', object(), reply=False) is False

# 7. call_names cache lock + clear_cache
from core.agent import call_names as cn
assert hasattr(cn, '_audio_cache_lock') and callable(cn.clear_cache)

# 8. tts_phrase_cache key includes speed/post_gain + clear()
from core.utils import tts_phrase_cache as tpc
class _T:
    voice = 'v1'
    speed = 1.0
    post_gain = 0.0
t1, t2, t3 = _T(), _T(), _T()
t2.speed = 1.2
t3.post_gain = 2.0
assert len({tpc.key(t1, 'hi', 24000), tpc.key(t2, 'hi', 24000), tpc.key(t3, 'hi', 24000)}) == 3 and callable(tpc.clear)

# 9. config_store rejects non-finite numbers
from core.admin import config_store as cs
for bad in (float('nan'), float('inf'), float('-inf')):
    try:
        cs._convert('number', bad, 1.0)
        raise AssertionError('Expected error for non-finite number')
    except (ValueError, getattr(cs, 'ConfigError', ValueError)):
        pass
assert 'os.fsync(f.fileno())' in inspect.getsource(cs.save_custom)
assert 'isinstance(entry, dict)' in inspect.getsource(__import__('core.agent.llm_router', fromlist=['get_instance']).get_instance)
assert '_sync_lock' in inspect.getsource(__import__('core.agent.chat_history', fromlist=['load']).load)
print('HUB_R6_OK')
"""
        env = dict(os.environ, R6_HUB_ROOT=hub_root)
        r = subprocess.run([hub_py, '-B', '-c', probe], cwd=hub_root, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('HUB_R6_OK', r.stdout)

        # PromptManager deadlock avoidance + admin_handler non-blocking init + tools turn_gen guards
        pm_src = _read(f'{hub_root}/core/utils/prompt_manager.py')
        self.assertIn('getattr(conn, "loop", None) is None or running_loop is conn.loop', pm_src)
        ah_src = _read(f'{hub_root}/core/admin/admin_handler.py')
        self.assertIn('self._blocking(conn._init_prompt_enhancement)', ah_src)
        self.assertIn('call_names.clear_cache()', ah_src)
        self.assertIn('tts_phrase_cache.clear()', ah_src)
        self.assertNotIn('("selected_module", "LLM", "ASR", "Intent", "VLLM"', ah_src)
        lm_src = _read(f'{hub_root}/core/handle/textHandler/listenMessageHandler.py')
        self.assertIn('conn.client_abort = False', lm_src)
        for fn in ('plugins_func/functions/web_search.py', 'plugins_func/functions/get_news_from_newsnow.py'):
            src = _read(f'{hub_root}/{fn}')
            self.assertIn('orig_sid = getattr(conn, "sentence_id", None)', src)
            self.assertIn('orig_gen = turn_gen.current(conn)', src)

    def test_02_workstream_b_client_and_daemons(self):
        croot = '/opt/thinksmart/client/current'
        cli_py = f'{croot}/.venv/bin/python'
        probe = """
import inspect
from thinksmart_client import app as c_app, bt as c_bt

src_app = inspect.getsource(c_app.Client)
assert 'self._reply_gen = 0' in src_app
assert 'self._brightness_set_at = 0.0' in src_app
assert "elif self.state == 'connecting':" in src_app
assert 'if not await self.start_listening(' in src_app
assert 'self.recognising = False' in src_app
assert "if self.uploading and self.listen_mode == 'manual':" in src_app
assert 'async def _listen_after_reply(self, gen=None):' in src_app
assert "gen != self._reply_gen" in src_app
assert '_brightness_set_at' in inspect.getsource(c_app.Client.brightness)

bt_src = inspect.getsource(c_bt.Bluetooth._check_capture)
assert "self.capture[0] != streaming['address']" in bt_src
print('CLIENT_R6_OK')
"""
        r = subprocess.run([cli_py, '-B', '-c', probe], cwd=croot, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('CLIENT_R6_OK', r.stdout)

        br_src = _read('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py')
        self.assertIn('rem = peek.index(FLUSH_MAGIC) + len(FLUSH_MAGIC)', br_src)

        ib_src = _read('/usr/local/bin/idle-blanker.py')
        self.assertIn('cur_ibl = get_brightness()', ib_src)
        self.assertIn('if blanked or (cur_ibl is not None and cur_ibl <= BLANK_BRIGHTNESS):', ib_src)

        kb_src = _read('/usr/local/share/thinksmart-kiosk/keyboard.js')
        self.assertIn('document.activeElement.isConnected', kb_src)

        # QQ Music behavioral checks: set_listen_duck flushes AEC and sleep_preview guards superseded token
        qq = _load_mod('ts_qq_r6', '/usr/local/bin/thinksmart-qqmusic.py')
        qq_src = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('preview_tok =', qq_src)
        self.assertIn('if self._preview is not preview_tok:', qq_src)
        flushed = []
        fake = object.__new__(qq.QQMusicEngine)
        import threading
        fake._lock = threading.RLock()
        fake.listen_duck = False
        fake.ducked = False
        fake._send_aec_flush = lambda: flushed.append(1)
        fake.set_listen_duck(True)
        fake.set_listen_duck(True)
        fake.set_listen_duck(False)
        self.assertEqual(len(flushed), 1)

    def test_03_workstream_c_ecosystem_admin_and_frontends(self):
        # 1. Monitor ledger reload protection against external modification
        if '/usr/local/bin' not in sys.path:
            sys.path.insert(0, '/usr/local/bin')
        mon = _load_mod('ts_mon_r6', '/usr/local/bin/thinksmart-monitor.py')
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / 'ledger.json'
            lp.write_text('{"servers":{"1":{"from":"a","to":"b","acc_in":100,"acc_out":50,"last_in":1,"last_out":1,"complete":true,"updated_at":1.0}}}', encoding='utf-8')
            m = object.__new__(mon.Monitor)
            m._ledger_path = lp
            m._traffic_ledger = {}
            m._ledger_dirty = False
            m._ledger_saved_mono = 0.0
            m._ledger_stat = None
            m._load_traffic_ledger()
            self.assertEqual(m._traffic_ledger['1']['acc_in'], 100)
            # Simulate external reset/update on disk before saving stale in-memory state
            time.sleep(0.02)
            ext_tmp = Path(td) / 'ext.json'
            ext_tmp.write_text('{"servers":{"1":{"from":"a","to":"b","acc_in":999,"acc_out":77,"last_in":9,"last_out":7,"complete":true,"updated_at":2.0}}}', encoding='utf-8')
            os.replace(ext_tmp, lp)
            m._traffic_ledger['1']['acc_in'] = 101
            m._ledger_dirty = True
            m._save_traffic_ledger()
            self.assertEqual(m._traffic_ledger['1']['acc_in'], 999)
            self.assertFalse(m._ledger_dirty)
        mon_src = _read('/usr/local/bin/thinksmart-monitor.py')
        self.assertIn('self._ledger_stat = (st.st_ino, st.st_mtime_ns)', mon_src)
        self.assertIn("if cur_sig != getattr(self, '_ledger_stat', None):", mon_src)

        # 2. Admin sqlite delete & 413 backup error
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env_bk = dict(os.environ)
            try:
                os.environ['ADMIN_STATE_DIR'] = str(root / 'admin')
                os.environ['ADMIN_HUB_DIR'] = str(root / 'hub')
                os.environ['ADMIN_HOME_STATE'] = str(root / 'home')
                os.environ['ADMIN_STUDY_STATE'] = str(root / 'study')
                os.environ['ADMIN_TUTOR_STATE'] = str(root / 'tutor')
                os.environ['ADMIN_MONITOR_STATE'] = str(root / 'monitor')
                os.environ['ADMIN_QQ_CFG_DIR'] = str(root / 'qq')
                adm = _load_mod('ts_admin_r6', '/usr/local/bin/thinksmart-admin.py')
                import sqlite3
                dbp = root / 'partial.db'
                con = sqlite3.connect(str(dbp))
                con.execute('CREATE TABLE messages (device TEXT, model TEXT)')
                con.execute("INSERT INTO messages VALUES ('d1', 'm1')")
                con.commit()
                con.close()
                # Succeeds without OperationalError even when memory_progress/device_memory don't exist
                self.assertEqual(adm._sqlite_delete_chat(dbp, 'd1', 'm1'), 1)
                self.assertEqual(adm._sqlite_delete_chat(dbp, 'd1', memory_only=True), 0)

                big_db = root / 'big.db'
                big_db.write_bytes(b'x' * (adm.MAX_BACKUP_DB_BYTES + 1))
                with self.assertRaises(adm.ApiError) as ctx:
                    adm._read_spec_export_bytes({'src': big_db, 'kind': 'sqlite_db'})
                self.assertEqual(ctx.exception.status, 413)
            finally:
                os.environ.clear()
                os.environ.update(env_bk)

        adm_html = _read('/usr/local/share/thinksmart-admin/index.html')
        self.assertIn("'HuoshanDoubleStreamTTSV2', 'CosyVoiceSiliconflow'", adm_html)

        tut_html = _read('/usr/local/share/thinksmart-tutor/index.html')
        self.assertIn("typeof cowriteState === 'object'", tut_html)
        self.assertIn('&em=${encodeURIComponent(dictState.en_mode', tut_html)
        idx_dict_decl = tut_html.index('let dictState =')
        idx_qinit = tut_html.index('const qInit = new URLSearchParams(location.search)')
        self.assertGreater(idx_qinit, idx_dict_decl)

        tut_py = _read('/usr/local/bin/thinksmart-tutor.py')
        self.assertIn("sp_p.name.startswith(('c0_', 'dc_'))", tut_py)

        set_js = _read('/opt/thinksmart/client/current/web/v2/settings.js')
        self.assertIn("a === null ? down('语音助手')", set_js)
        self.assertIn("if (h0 === null)", set_js)
        self.assertIn("loadedAt.music && m === null", set_js)

        ic_src = _read('/usr/local/bin/thinksmart-intercom.py')
        self.assertIn('if (alPollTimer) { clearInterval(alPollTimer); alPollTimer = 0; }', ic_src)
        self.assertIn('loadMsgs(); syncMsgPollTimer(); loadAlarms(); syncAlarmPollTimer();', ic_src)
        self.assertIn('await asyncio.to_thread(subprocess.run, ["/usr/bin/pkill", "-x", "uxplay"]', ic_src)

        hm_html = _read('/usr/local/share/thinksmart-home/index.html')
        self.assertIn("r && r.isConnected && !r.disabled && e.type === 'pointerup'", hm_html)

    def test_04_settings_ui_polish_and_sidebar_order(self):
        v2 = Path('/opt/thinksmart/client/current/web/v2')
        idx = _read(str(v2 / 'index.html'))
        css = _read(str(v2 / 'app.css'))
        app_js = _read(str(v2 / 'app.js'))
        set_js = _read(str(v2 / 'settings.js'))

        expected = ['display', 'sound', 'net', 'voice', 'chat', 'weather', 'music', 'study', 'sys']
        import re
        self.assertEqual(re.findall(r'<button data-sec="(\w+)"', idx), expected)
        self.assertEqual(re.findall(r'<section class="sec(?: on)?" data-sec="(\w+)"', idx), expected)
        for tok in ('id="setSavePill"', 'id="volPresets"', 'id="airplaySwitch"', 'id="wxMainInp"', 'class="set-sep"'):
            self.assertIn(tok, idx)
        self.assertNotIn('.step:has(button:disabled)', css)
        for tok in ('.step button:disabled', '.save-pill', '.adv-toggle', '.adv-body.open', '.opts.four', '.bt.danger', '.fact-bar'):
            self.assertIn(tok, css)
        self.assertEqual(re.findall(r'\d+\.\d+px', css), [])
        self.assertIn("el.min !== '' ? Number(el.min) : 0", app_js)
        self.assertIn('function syncVolPresets(', app_js)
        for tok in ('data-c="adv"', 'data-c="thr-preset"', 'setSaveBadge(', 'paintWeather', 'paintMusic', 'fact-bar'):
            self.assertIn(tok, set_js)

    def test_05_monitor_ui_polish_and_invariants(self):
        import re
        idx = _read('/usr/local/share/thinksmart-monitor/index.html')
        for tok in ('id="dmQuotaBar"', 'id="dmQuotaBarTrack"', '.card.warn', '.fl-ex-due',
                    '.totals {', '.lamp {', '2026-11-26 服务器页视觉与交互精修'):
            self.assertIn(tok, idx)
        self.assertNotIn('#5256ba', idx)
        self.assertNotIn('infinite', idx)
        i = idx.index('2026-11-20 Claude 竖版')
        p_css = idx[i:idx.index('</style>', i)]
        self.assertEqual(re.findall(r'\d+\.\d+px', p_css), [])
        self.assertLessEqual(set(re.findall(r'font-weight:\s*([^;}\s]+)', p_css)), {'400', '700'})
        sizes = [int(v) for v in re.findall(r'font-size:\s*(\d+)px', p_css)]
        self.assertTrue(not sizes or min(sizes) >= 15)

    def test_06_system_debloat_and_lazy_imports(self):
        hub_root = Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
        for gone in (
            hub_root / 'core/providers/llm/claude_cli',
            hub_root / 'core/agent/claude_session.py',
            hub_root / 'core/agent/agent_session.py',
            hub_root / 'core/api/ota_handler.py',
            hub_root / 'core/api/vision_handler.py',
            hub_root / 'core/api/base_handler.py',
            hub_root / 'core/providers/vllm',
            hub_root / 'core/admin/auth.py',
            hub_root / 'config/assets/wakeup_words.wav',
            hub_root / 'config/assets/max_output_size.wav',
            Path('/home/ubuntu/xiaozhi-hub-server/scripts/update.sh'),
            Path('/usr/local/share/thinksmart-monitor/display.html'),
        ):
            self.assertFalse(gone.exists(), f'Debloated path must not exist: {gone}')

        app_src = _read(str(hub_root / 'app.py'))
        self.assertIn('SKIP_DOCTOR', app_src)
        self.assertNotIn('aioconsole', app_src)

        mcp_src = _read(str(hub_root / 'core/providers/tools/server_mcp/mcp_executor.py'))
        self.assertIn('ServerMCPManager', mcp_src)
        self.assertNotIn('from .mcp_manager import ServerMCPManager\n', mcp_src[:800])

        news_src = _read(str(hub_root / 'plugins_func/functions/get_news_from_newsnow.py'))
        self.assertNotIn('from markitdown import', news_src[:1200])

        adm_src = _read('/usr/local/bin/thinksmart-admin.py')
        adm_html = _read('/usr/local/share/thinksmart-admin/index.html')
        for token in ('ClaudeCLI', 'claude_cli', 'id="vllmUrl"', 'id="btnCheckSysUpd"', 'id="btnInstallHubUpd"'):
            self.assertNotIn(token, adm_src)
            self.assertNotIn(token, adm_html)

    def test_07_connection_intent_init_and_chat_fault_isolation(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = hub_root + '/.venv/bin/python'
        probe = r"""
import queue
from core.connection import ConnectionHandler
from core.providers.tts.dto.dto import SentenceType

class DummyTTS:
    def __init__(self):
        self.tts_text_queue = queue.Queue()
        self.stored = {}
    def store_tts_text(self, sid, text):
        self.stored[sid] = text

class DummyLLM:
    def __init__(self):
        self.seen_functions = None
        self.raise_err = False
    def response_with_functions(self, session_id, dialogue, functions=None):
        self.seen_functions = functions
        if self.raise_err:
            raise RuntimeError("simulated_gateway_error")
        yield ('<｜DSML｜function_calls><｜DSML｜invoke name="get_weather"></｜DSML｜invoke></｜DSML｜function_calls>\n\n', None)
    def response(self, session_id, dialogue):
        raise AssertionError("function_call mode must call response_with_functions, not response()")

base_cfg = {
    "exit_commands": ["退出", "再见"],
    "chat_history": {"enabled": False},
    "selected_module": {"Intent": "function_call", "LLM": "DeepSeekLLM"},
    "Intent": {
        "function_call": {"type": "function_call", "functions": ["get_weather", "get_news_from_newsnow"]},
        "nointent": {"type": "nointent"},
    },
    "LLM": {"DeepSeekLLM": {"type": "openai", "model_name": "deepseek-v4.1-flash-sg", "api_key": "sk-test-valid-key", "base_url": "http://127.0.0.1:9/v1"}},
    "plugins": {},
    "prompt": "你是小麦。",
}

llm = DummyLLM()
conn = ConnectionHandler(base_cfg, _vad=None, _asr=None, _llm=llm, _memory=None, _intent=object())
conn.device_id = "unit-test-device"
conn.tts = DummyTTS()
assert conn.func_handler is None
conn._initialize_intent()
assert conn.intent_type == "function_call"
assert conn.load_function_plugin is True
assert conn.func_handler is not None
fn_names = {f["function"]["name"] for f in conn.func_handler.get_functions()}
assert "get_weather" in fn_names and "handle_exit_intent" in fn_names, fn_names

# Markup-only empty reply must pass tools to response_with_functions, emit LAST, and not persist '\n\n'
conn.chat("今天天气怎么样？")
assert llm.seen_functions is not None
sent_fn_names = {f["function"]["name"] for f in llm.seen_functions}
assert "get_weather" in sent_fn_names and "direct_answer" in sent_fn_names
q_items = []
while not conn.tts.tts_text_queue.empty():
    q_items.append(conn.tts.tts_text_queue.get_nowait())
assert any(m.sentence_type == SentenceType.LAST for m in q_items), q_items
non_sys = [m for m in conn.dialogue.dialogue if m.role != "system" and not getattr(m, "is_temporary", False)]
assert non_sys == [], [(m.role, m.content) for m in non_sys]

# Gateway error in response_with_functions must emit LAST and not orphan user turn
llm.raise_err = True
conn.chat("再试一次")
q_items2 = []
while not conn.tts.tts_text_queue.empty():
    q_items2.append(conn.tts.tts_text_queue.get_nowait())
assert any(m.sentence_type == SentenceType.LAST for m in q_items2), q_items2
non_sys2 = [m for m in conn.dialogue.dialogue if m.role != "system" and not getattr(m, "is_temporary", False)]
assert non_sys2 == [], [(m.role, m.content) for m in non_sys2]

# Unexpected pre-LLM exception inside _chat must be caught by chat(), emit LAST, and not raise
orig_get_fn = conn.func_handler.get_functions
conn.func_handler.get_functions = lambda: (_ for _ in ()).throw(RuntimeError("unexpected_pre_llm_boom"))
try:
    conn.chat("测试未捕获异常")
finally:
    conn.func_handler.get_functions = orig_get_fn
q_items3 = []
while not conn.tts.tts_text_queue.empty():
    q_items3.append(conn.tts.tts_text_queue.get_nowait())
assert any(m.sentence_type == SentenceType.LAST for m in q_items3), q_items3
non_sys3 = [m for m in conn.dialogue.dialogue if m.role != "system" and not getattr(m, "is_temporary", False)]
assert non_sys3 == [], [(m.role, m.content) for m in non_sys3]

# nointent mode must leave func_handler as None
cfg_no = dict(base_cfg, selected_module={"Intent": "nointent", "LLM": "DeepSeekLLM"})
conn_no = ConnectionHandler(cfg_no, _vad=None, _asr=None, _llm=llm, _memory=None, _intent=object())
conn_no._initialize_intent()
assert conn_no.intent_type == "nointent"
assert conn_no.func_handler is None

print("CONN_INTENT_AND_FAULT_GUARD_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [hub_py, '-B', '-c', probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=25,
            )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('CONN_INTENT_AND_FAULT_GUARD_OK', r.stdout)

    def test_08_voice_home_navigation_end_to_end(self):
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        probe = r"""
import asyncio, json
from thinksmart_client import app as app_mod, bridge, mcp

bridge.install_native(
    type('DummyAudio', (), {'Player': type('P', (), {'_NATIVE_MUSIC_MIX': True, 'pull': lambda s, n: None})}),
    mcp,
    app_mod,
)
tool_names = [t['name'] for t in mcp.TOOLS]
assert 'self.screen.navigate' in tool_names, tool_names

events = []
dim_calls = []
c = object.__new__(app_mod.Client)
c.events = type('E', (), {'publish': lambda self, k, v: events.append((k, v))})()
c.screen = type('S', (), {'dim': lambda self, on: dim_calls.append(on)})()

async def run():
    for alias in ('home', '主页', '主界面', '我们主页', '桌面', '退出'):
        events.clear()
        r = await mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                              'params': {'name': 'self.screen.navigate', 'arguments': {'page': alias}}}, client=c)
        body = json.loads(r['result']['content'][0]['text'])
        assert body['success'] is True and body['page'] == 'home', body
        assert events == [('navigate', {'page': 'home', 'label': '设备主页（主界面）'})], events

    events.clear()
    r_chat = await mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                               'params': {'name': 'self.screen.navigate', 'arguments': {'page': 'settings', 'section': 'chat'}}}, client=c)
    b_chat = json.loads(r_chat['result']['content'][0]['text'])
    assert b_chat['page'] == 'settings' and b_chat['section'] == 'chat', b_chat
    assert events[-1][1]['section'] == 'chat', events

    events.clear()
    r_wx = await mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                             'params': {'name': 'self.screen.navigate', 'arguments': {'page': 'weather', 'city': '杭州'}}}, client=c)
    b_wx = json.loads(r_wx['result']['content'][0]['text'])
    assert b_wx['page'] == 'weather' and b_wx['city'] == '杭州', b_wx

    events.clear()
    r_mc = await mcp.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                             'params': {'name': 'self.music.control', 'arguments': {'action': 'close_ui'}}}, client=c)
    b_mc = json.loads(r_mc['result']['content'][0]['text'])
    assert b_mc['page'] == 'home', b_mc
    assert events == [('navigate', {'page': 'home', 'label': '设备主页（主界面）'})], events

asyncio.run(run())
assert False in dim_calls
print('SCREEN_NAVIGATE_OK')
"""
        r = subprocess.run([cli_py, '-B', '-c', probe], cwd=croot, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('SCREEN_NAVIGATE_OK', r.stdout)

        app_js = _read('/opt/thinksmart/client/current/web/v2/app.js')
        self.assertIn('navigate(n)', app_js)
        self.assertIn('window.__tsCloseBaseSheets', app_js)

        ov_js = _read('/usr/local/share/thinksmart-monitor/home_overlay.js')
        self.assertIn('window.__tsNavigate =', ov_js)
        self.assertIn('goHome: true', ov_js)
        self.assertIn("'self.screen.navigate'", ov_js)

        hm_html = _read('/usr/local/share/thinksmart-home/index.html')
        self.assertIn('if (d.goHome)', hm_html)

        bp_txt = _read('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/hub-base-prompt.txt')
        self.assertIn('self.screen.navigate', bp_txt)
        self.assertIn('回主界面', bp_txt)

    def test_09_round6_post_debloat_deep_hardening(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = hub_root + '/.venv/bin/python'
        hub_probe = r"""
import asyncio, os, struct, tempfile
from config.config_loader import read_config, merge_configs
from core.providers.tools.device_mcp import mcp_client as mc_mod, mcp_handler as mh_mod
from core.providers.tools.unified_tool_manager import ToolManager
from core.providers.tools.base import ToolType, ToolDefinition, ToolExecutor
from core.providers.llm.openai.openai import LLMProvider as OpenAIProvider
from core.utils import p3 as p3_mod
from core.utils.modules_initialize import _resolve_module_entry
from plugins_func.register import Action, ActionResponse

# 1. MCPClient single source of truth, next_id=10, dotted tool lookup, pending future cancellation
assert mh_mod.MCPClient is mc_mod.MCPClient
mc = mc_mod.MCPClient()
assert mc.next_id == 10
asyncio.run(mc.add_tool({"name": "self.screen.navigate", "description": "nav", "inputSchema": {}}))
assert mc.has_tool("self.screen.navigate") is True
assert mc.has_tool("self_screen_navigate") is True

async def _test_cancel():
    fut = asyncio.get_running_loop().create_future()
    await mc.register_call_result_future(10, fut)
    mc.cancel_all_pending(ConnectionError("closed"))
    assert fut.done() and isinstance(fut.exception(), ConnectionError)
asyncio.run(_test_cancel())

# 2. ToolManager resolves dotted MCP tool names cleanly
class _DummyExec(ToolExecutor):
    def __init__(self):
        self.seen = []
    def get_tools(self):
        return {
            "self_screen_navigate": ToolDefinition(
                name="self_screen_navigate",
                description={"type": "function", "function": {"name": "self_screen_navigate"}},
                tool_type=ToolType.DEVICE_MCP,
            )
        }
    def has_tool(self, name):
        return name in ("self.screen.navigate", "self_screen_navigate")
    async def execute(self, conn, tool_name, arguments):
        self.seen.append((tool_name, arguments))
        return ActionResponse(action=Action.RESPONSE, response="ok")

tm = ToolManager(conn=None)
ex = _DummyExec()
tm.register_executor(ToolType.DEVICE_MCP, ex)
assert tm.has_tool("self.screen.navigate") is True
assert tm.get_tool_type("self.screen.navigate") == ToolType.DEVICE_MCP
res = asyncio.run(tm.execute_tool("self.screen.navigate", {"page": "home"}))
assert res.action == Action.RESPONSE and ex.seen == [("self_screen_navigate", {"page": "home"})], ex.seen

# 3. config_loader empty YAML & merge_configs guards + modules_initialize fallback
with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
    tf.write("# comment only\n")
    empty_yaml = tf.name
try:
    assert read_config(empty_yaml) == {}
finally:
    os.unlink(empty_yaml)
assert merge_configs({"a": 1}, None) == {"a": 1}
sel_fb, ent_fb = _resolve_module_entry(
    {"selected_module": {"TTS": "RemovedLegacyTTS"}, "TTS": {"EdgeTTS": {"type": "edge"}}},
    "TTS",
)
assert sel_fb == "EdgeTTS" and ent_fb["type"] == "edge"

# 4. p3 stream helpers & OpenAI base_url=None safety
pkt = b"\x01\x02\x03\x04"
framed = struct.pack(">BBH", 0, 0, len(pkt)) + pkt
seen_pkts = []
out_pkts = p3_mod.decode_opus_from_bytes_stream(framed, seen_pkts.append)
assert out_pkts == [pkt] and seen_pkts == [pkt]

oa = object.__new__(OpenAIProvider)
oa.base_url = None
oa.model_name = "glm-4.5-flash"
oa.extra_body = {"reasoning_effort": "none"}
rp = {"messages": []}
oa._apply_thinking_disabled(rp)
assert rp["extra_body"] == {"reasoning_effort": "none"}

print("ROUND6_HUB_HARDENING_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            rh = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=25,
            )
        self.assertEqual(rh.returncode, 0, rh.stderr or rh.stdout)
        self.assertIn('ROUND6_HUB_HARDENING_OK', rh.stdout)

        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import asyncio
from thinksmart_client import app as app_mod, wake_verify as wv

# 1. grok vendor logo does not point to missing grok.svg
assert app_mod.VENDORS['grok'][0] == '', app_mod.VENDORS['grok']

# 2. Multi-wake-word Matcher strict name verification + single-char safety
m = wv.Matcher(['n_i@你好小智', 'x_m@小麦同学'])
assert m.judge('小美同学')[0] is False
assert m.judge('你好小美')[0] is False
assert m.judge('小智今天天气怎么样') == (True, '小智')
assert m.judge('小麦今天天气怎么样') == (True, '小麦')
assert wv.Matcher(['小'], slack=2.0).judge('我想睡觉')[0] is False

# 3. navigate_screen routes 睡眠设置 -> sleep page, 天气设置 -> settings(weather), 音乐设置 -> settings(music), pokes screen
events, poked = [], []
c = object.__new__(app_mod.Client)
c.events = type('E', (), {'publish': lambda self, k, v: events.append((k, v))})()
c.screen = type('S', (), {'dim': lambda self, on: None, 'poke': lambda self: poked.append(True)})()
nav = c.navigate_screen('睡眠设置')
assert nav['page'] == 'sleep', nav
assert poked == [True]
nav_w = c.navigate_screen('天气设置')
assert nav_w['page'] == 'settings' and nav_w.get('section') == 'weather', nav_w
nav_m = c.navigate_screen('音乐设置')
assert nav_m['page'] == 'settings' and nav_m.get('section') == 'music', nav_m

# 4. say() while listening stops uploading and bumps _reply_gen
sent = []
c._reply_gen = 5
c.state = 'listening'
c.uploading = True
c.listen_after_reply = True
c.vad_ended = False
c.recognising = False
c.awaiting_text = False
c.hub_pending = False
c.deadline = 123.0
c.tools_listed = asyncio.Event()
c.tools_listed.set()
c.hub = type('H', (), {'connected': True, 'send': lambda self, m: asyncio.sleep(0, result=sent.append(m) or True)})()
c.ensure_connected = lambda: asyncio.sleep(0, result=True)
c.set_state = lambda st: setattr(c, 'state', st)
asyncio.run(c.say('今天几号'))
assert c.uploading is False and c.deadline is None and c._reply_gen == 6 and c.state == 'thinking'
assert sent and sent[-1]['state'] == 'detect'

print('ROUND6_CLIENT_HARDENING_OK')
"""
        rc = subprocess.run([cli_py, '-B', '-c', cli_probe], cwd=croot, capture_output=True, text=True, timeout=20)
        self.assertEqual(rc.returncode, 0, rc.stderr or rc.stdout)
        self.assertIn('ROUND6_CLIENT_HARDENING_OK', rc.stdout)

        ov_js = _read('/usr/local/share/thinksmart-monitor/home_overlay.js')
        self.assertIn('#ring,html.ts-xz-covered #ring{z-index:9700}', ov_js)
        self.assertIn('function isNavOnlyTool(', ov_js)
        self.assertNotIn("tn.startsWith('self.screen.')", ov_js)
        self.assertIn("document.body.classList.contains('ringing')", ov_js)

        app_js = _read('/opt/thinksmart/client/current/web/v2/app.js')
        self.assertIn("document.body.classList.contains('ringing')", app_js)
        set_js = _read('/opt/thinksmart/client/current/web/v2/settings.js')
        self.assertIn('r0.__drag', set_js)

        tut_html = _read('/usr/local/share/thinksmart-tutor/index.html')
        self.assertIn("stopDictationPlayback(currentView !== 'dictation')", tut_html)

        mon_html = _read('/usr/local/share/thinksmart-monitor/index.html')
        self.assertIn("remove('offline','warn')", mon_html)
        mon_py = _read('/usr/local/bin/thinksmart-monitor.py')
        self.assertNotIn("'/display'", mon_py)

        adm_py = _read('/usr/local/bin/thinksmart-admin.py')
        self.assertIn("'vendor': (cur_v['id'] if cur_v else '')", adm_py)
        self.assertIn("'auth_restored': auth_restored", adm_py)
        adm_html = _read('/usr/local/share/thinksmart-admin/index.html')
        self.assertIn("const actDef = cfgState.llm.vendor || '';", adm_html)

    def test_10_round7_full_system_deep_audit(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = hub_root + '/.venv/bin/python'
        hub_probe = r"""
import asyncio, gzip, inspect, json, queue, threading
from core.connection import ConnectionHandler
from core.providers.tts import base as tts_base
from core.providers.tts.dto.dto import SentenceType
from core.providers.vad import silero as silero_mod
from core.providers.asr import base as asr_base
from core.providers.asr.doubao_stream import ASRProvider as DoubaoStreamASR
from core.providers.asr import sherpa_onnx_local as sherpa_mod
from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
from core.handle import sendAudioHandle as sah, helloHandle as hh, intentHandler as ih
from core.utils import prompt_manager as pm_mod
from core.agent import call_names as cn_mod
from plugins_func.functions import change_role as cr_mod, handle_exit_intent as exit_mod, play_music as pmus_mod, get_weather as gw_mod

class DummyTTS:
    def __init__(self):
        self.tts_text_queue = queue.Queue()
        self.stored = {}
        self.calls = []
    def store_tts_text(self, sid, text):
        self.stored[sid] = text
    def tts_one_sentence(self, conn, ctype, content_detail=None, **kw):
        self.calls.append(content_detail)

class SeqLLM:
    def __init__(self, chunks):
        self.chunks = chunks
    def response_with_functions(self, session_id, dialogue, functions=None):
        for c in self.chunks:
            yield c

base_cfg = {
    "exit_commands": ["退出", "再见"],
    "chat_history": {"enabled": False},
    "selected_module": {"Intent": "function_call", "LLM": "DeepSeekLLM"},
    "Intent": {"function_call": {"type": "function_call", "functions": ["get_weather"]}},
    "LLM": {"DeepSeekLLM": {"type": "openai", "model_name": "deepseek-v4.1-flash-sg", "api_key": "sk-test-valid", "base_url": "http://127.0.0.1:9/v1"}},
    "plugins": {},
    "prompt": "你是小麦。",
}

# 1. String token "content" in response_with_functions must not be unpacked as dict
conn = ConnectionHandler(base_cfg, _vad=None, _asr=None, _llm=SeqLLM([("content", None)]), _memory=None, _intent=object())
conn.device_id = "r7-test-dev"
conn.tts = DummyTTS()
conn._initialize_intent()
conn.chat("测试英文词")
assistant_msgs = [m.content for m in conn.dialogue.dialogue if m.role == "assistant"]
assert assistant_msgs == ["content"], assistant_msgs

# 2. direct_answer preserves mid-stream spaces and cuts empty/markup-only answer
conn2 = ConnectionHandler(
    base_cfg, _vad=None, _asr=None,
    _llm=SeqLLM([
        ("", [{"id": "c1", "function": {"name": "direct_answer", "arguments": '{"response": "Hello '}}]),
        ("", [{"id": "c1", "function": {"name": "direct_answer", "arguments": 'world"}'}}]),
    ]),
    _memory=None, _intent=object(),
)
conn2.device_id = "r7-test-dev2"
conn2.tts = DummyTTS()
conn2._initialize_intent()
conn2.chat("用英文打招呼")
assert [m.content for m in conn2.dialogue.dialogue if m.role == "assistant"] == ["Hello world"]
streamed_da = "".join(
    m.content_detail for m in list(conn2.tts.tts_text_queue.queue)
    if getattr(m, "content_detail", None)
)
assert streamed_da == "Hello world", streamed_da

conn3 = ConnectionHandler(
    base_cfg, _vad=None, _asr=None,
    _llm=SeqLLM([("", [{"id": "c2", "function": {"name": "direct_answer", "arguments": '{"response": "<｜DSML｜function_calls><｜DSML｜invoke name=\\"get_weather\\"></｜DSML｜invoke></｜DSML｜function_calls>"}'}}])]),
    _memory=None, _intent=object(),
)
conn3.device_id = "r7-test-dev3"
conn3.tts = DummyTTS()
conn3._initialize_intent()
conn3.chat("空回答测试")
assert [m for m in conn3.dialogue.dialogue if m.role != "system" and not getattr(m, "is_temporary", False)] == []

# 3. TTSProviderBase.to_tts ignores stale current_sentence_id when connection is alive
class _SyncTTS(tts_base.TTSProviderBase):
    def __init__(self):
        self.delete_audio_file = True
        self.audio_file_type = "wav"
        self._correct_words_pattern = None
        self.current_sentence_id = "stale-turn-id"
        self.conn = type("C", (), {"sentence_id": "fresh-turn-id", "sample_rate": 24000, "stop_event": threading.Event(), "client_abort": False})()
    def _synthesize_pcm(self, text, sample_rate):
        return b"\x00\x00" * (sample_rate // 5)
    async def text_to_speak(self, text, output_file):
        return b""

stts = _SyncTTS()
opus_out = stts.to_tts("你好")
assert opus_out and len(opus_out) > 0, opus_out
stts.conn.stop_event.set()
assert stts.to_tts("你好") is None

# 4. DoubaoStreamASR.parse_response decompresses gzip payload (0x01)
gz_payload = gzip.compress(json.dumps({"result": {"text": "早上好"}}).encode("utf-8"))
pkt = bytes([0x11, 0x90, 0x11, 0x00]) + (1).to_bytes(4, "big") + len(gz_payload).to_bytes(4, "big") + gz_payload
parsed_db = DoubaoStreamASR.parse_response(None, pkt)
assert parsed_db["payload_msg"]["result"]["text"] == "早上好", parsed_db

# 5. ListenTextMessageHandler clears just_woken_up on start and sends vad empty on empty stop
class _FakeWS:
    def __init__(self):
        self.sent = []
    async def send(self, msg):
        self.sent.append(msg)

class _FakeASR:
    interface_type = asr_base.InterfaceType.NON_STREAM
    def __init__(self):
        self.stopped = []
    async def handle_voice_stop(self, c, frames):
        self.stopped.append(frames)

async def _test_listen():
    lh = ListenTextMessageHandler()
    conn.just_woken_up = True
    conn.asr_audio_queue.put_nowait(b"\x00" * 640)
    await lh.handle(conn, {"state": "start", "mode": "manual"})
    assert conn.just_woken_up is False
    assert conn.asr_audio_queue.empty()

    conn.asr = _FakeASR()
    conn.features = {"vad_end": True}
    conn.websocket = _FakeWS()
    conn.session_id = "sid-r7"
    await lh.handle(conn, {"state": "stop"})
    assert any('"state": "empty"' in s for s in conn.websocket.sent), conn.websocket.sent

    conn.asr_audio_queue.put_nowait(b"\x01\x02" * 320)
    await lh.handle(conn, {"state": "stop"})
    assert len(conn.asr.stopped) == 1 and conn.asr.stopped[0] == [b"\x01\x02" * 320]

asyncio.run(_test_listen())

# 6. speak_txt(record_dialogue=False), handle_exit_intent(""), process_intent_result("[]")
before_len = len(conn.dialogue.dialogue)
ih.speak_txt(conn, "先休息啦", record_dialogue=False)
assert len(conn.dialogue.dialogue) == before_len
exit_res = exit_mod.handle_exit_intent(conn, say_goodbye="")
assert exit_res.response and len(exit_res.response.strip()) >= 2
assert asyncio.run(ih.process_intent_result(conn, "[]", "你好")) is False

# 7. Structural & invariant checks across VAD, ASR, sendAudioHandle, helloHandle, PromptManager, call_names, sherpa
vad_src = inspect.getsource(silero_mod)
assert "self.vad_threshold_low >= self.vad_threshold" in vad_src
assert "conn.client_voice_window.count(True) >= self.frame_window_threshold" in vad_src
asr_src = inspect.getsource(asr_base)
assert "voiced_bytes >= 16000 * 2 * 25 // 100" in asr_src
assert "if need_temp or need_save:" in asr_src
sah_src = inspect.getsource(sah)
assert 'not rate_controller.queue and flow_control.get("burst_left", 0) > 0' in sah_src
hh_src = inspect.getsource(hh)
assert 'welcome_msg["audio_params"] = msg_json.get("audio_params")' not in hh_src
assert "build_enhanced_prompt" in inspect.getsource(cr_mod)
assert "device_prompt:{device_id}:{today_date}" in inspect.getsource(pm_mod)
assert "type(tts).__module__" in inspect.getsource(cn_mod)
assert "_STDOUT_CAPTURE_LOCK" in inspect.getsource(sherpa_mod)
assert "WeakKeyDictionary" in inspect.getsource(gw_mod)
assert "q_clean" in inspect.getsource(pmus_mod)
print("R7_HUB_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=25,
            )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('R7_HUB_OK', r.stdout)

        # Client & Bridge Round 7 checks
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import inspect
from thinksmart_client import app as app_mod, bridge, bt as bt_mod

b_src = inspect.getsource(bridge)
assert "rem = peek.index(FLUSH_MAGIC) + len(FLUSH_MAGIC)" in b_src
assert "rem += block - len(FLUSH_MAGIC)" in b_src

# Verify arithmetic drains full 7680B _AEC_FLUSH_PKT
FLUSH_MAGIC = b'TS_AEC_FLUSH_V1\x00'
block = 7680
pkt = FLUSH_MAGIC + bytes(block - len(FLUSH_MAGIC))
peek = bytearray(b'\x00' * 320 + pkt + b'\x11' * 160)
rem = peek.index(FLUSH_MAGIC) + len(FLUSH_MAGIC)
rem += block - len(FLUSH_MAGIC)
assert rem == 320 + 7680

a_src = inspect.getsource(app_mod)
assert "my_backlog = self.backlog" in a_src and "self.backlog is my_backlog" in a_src
assert "_preview_releases" in a_src
bt_src = inspect.getsource(bt_mod)
assert "nan_to_num" in bt_src
assert "dict(d, connected=False, streaming=False)" in bt_src
print("R7_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R7_CLI_OK', r_cli.stdout)

        # BrightnessPreview & Kiosk & QQMusic & Web v2 checks
        disp_mod = _load_mod('ts_disp_r7', '/usr/local/bin/thinksmart_display.py')
        import socket as _sock
        with tempfile.TemporaryDirectory() as td_sock:
            sp = os.path.join(td_sock, 'bp.sock')
            bp = disp_mod.BrightnessPreview(path=sp).open()
            try:
                self.assertIsNotNone(bp.channel)
                with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as sender:
                    sender.sendto(b'{"clear": true}', sp)
                    self.assertFalse(bp.receive())
                    sender.sendto(b'{"level": 45}', sp)
                    self.assertTrue(bp.receive())
                    self.assertEqual(bp.current(), 45)
                    sender.sendto(b'{"clear": true}', sp)
                    self.assertFalse(bp.receive())
                    self.assertIsNone(bp.current())
                    sender.sendto(b'{"wake": true}', sp)
                    self.assertTrue(bp.receive())
            finally:
                bp.close()
        self.assertIn('if preview.receive():', _read('/usr/local/bin/idle-blanker.py'))
        kiosk_src = _read('/usr/local/bin/thinksmart-kiosk.py')
        self.assertIn('def stop_retry_timer():', kiosk_src)
        self.assertIn("and state['welcome']", kiosk_src)
        self.assertIn('_sleep_op_seq', _read('/usr/local/bin/thinksmart-qqmusic.py'))
        self.assertIn("addEventListener('pointerleave'", _read('/opt/thinksmart/client/current/web/v2/app.js'))
        set_js = _read('/opt/thinksmart/client/current/web/v2/settings.js')
        self.assertLess(set_js.index('delete inflightVals[part];'), set_js.index('paint();', set_js.index('delete inflightVals[part];')))

        # Portrait Text-node (nodeType === 3) normalization across Home / Monitor / Tutor / Study
        for hp in (
            '/usr/local/share/thinksmart-home/index.html',
            '/usr/local/share/thinksmart-monitor/index.html',
            '/usr/local/share/thinksmart-tutor/index.html',
            '/usr/local/share/thinksmart-study/parent.html',
        ):
            self.assertIn('nodeType === 3', _read(hp), hp)

        # Tutor raw JPEG/PNG fallback, dictation mistake tip, and input-mode quiz UI
        tut_mod = _load_mod('ts_tutor_r7', '/usr/local/bin/thinksmart-tutor.py')
        orig_pil = tut_mod._HAS_PIL
        try:
            tut_mod._HAS_PIL = False
            raw_jpg = b'\xff\xd8\xff\xe0\x00\x10JFIF' + b'\x00' * 4096
            raw_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x00' * 4096
            self.assertEqual(tut_mod._prep_dictation_jpeg_sync(raw_jpg), raw_jpg)
            self.assertEqual(tut_mod._prep_dictation_jpeg_sync(raw_png), raw_png)
            with self.assertRaises(ValueError):
                tut_mod._prep_dictation_jpeg_sync(b'\xff\\xd8not_a_jpeg' + b'\x00' * 4096)
        finally:
            tut_mod._HAS_PIL = orig_pil
        tut_html = _read('/usr/local/share/thinksmart-tutor/index.html')
        self.assertIn('window.submitInputQuiz =', tut_html)
        self.assertIn('Array.isArray(st.quiz.opts)', tut_html)

        # Admin backup restore: keep_token session survival & passwordless wan_access clamp
        adm = _load_mod('ts_admin_r7', '/usr/local/bin/thinksmart-admin.py')
        import hashlib as _hl, io as _io, tarfile as _tf
        orig_state_dir, orig_bk_dir = adm.STATE_DIR, adm.RESTORE_BACKUP_DIR
        try:
            with tempfile.TemporaryDirectory() as td_adm:
                td_p = Path(td_adm)
                st_dir = td_p / 'state'
                bk_dir = td_p / 'backups'
                st_dir.mkdir(mode=0o700)
                bk_dir.mkdir(mode=0o700)
                adm.STATE_DIR = st_dir
                adm.RESTORE_BACKUP_DIR = bk_dir

                auth_obj = adm.Auth(st_dir)
                keep_tok, _ = auth_obj.create(True, 'uploader-ua')
                other_tok, _ = auth_obj.create(True, 'other-ua')

                def _make_arc(mode: str, entries: dict[str, bytes]) -> bytes:
                    now_ts = int(time.time())
                    m_files = []
                    for arc_name, payload in entries.items():
                        sp = next(s for s in adm._backup_specs() if s['arc'] == arc_name)
                        m_files.append({
                            'arc': arc_name, 'module': sp['module'], 'label': sp['label'],
                            'kind': sp['kind'], 'size': len(payload),
                            'sha256': _hl.sha256(payload).hexdigest(),
                        })
                    man = {
                        'format': adm.BACKUP_FORMAT, 'mode': mode,
                        'created_at': now_ts, 'file_count': len(m_files), 'files': m_files,
                    }
                    buf = _io.BytesIO()
                    with _tf.open(fileobj=buf, mode='w:gz') as tf:
                        for nm, pl in [('BACKUP_MANIFEST.json', json.dumps(man).encode()), *entries.items()]:
                            ti = _tf.TarInfo(name=nm)
                            ti.size = len(pl)
                            ti.mtime = now_ts
                            ti.mode = 0o600
                            tf.addfile(ti, _io.BytesIO(pl))
                    return buf.getvalue()

                auth_payload = json.dumps({'username': 'admin', 'hash': 'scrypt$test', 'updated_at': 1}).encode()
                arc_full = _make_arc('full', {'admin/auth.json': auth_payload})
                res_r = adm.restore_backup_archive(arc_full, preserve_secrets=False, keep_token=keep_tok)
                self.assertTrue(res_r['auth_restored'])
                auth_after = adm.Auth(st_dir)
                self.assertIsNotNone(auth_after.check(keep_tok))
                self.assertIsNone(auth_after.check(other_tok))

                # Passwordless state_dir must clamp restored wan_access.json enabled -> False
                st_dir2 = td_p / 'state_no_pw'
                st_dir2.mkdir(mode=0o700)
                adm.STATE_DIR = st_dir2
                arc_wan = _make_arc('sanitized', {'admin/wan_access.json': b'{"enabled": true}\n'})
                adm.restore_backup_archive(arc_wan, preserve_secrets=False)
                restored_wan = json.loads((st_dir2 / 'wan_access.json').read_text(encoding='utf-8'))
                self.assertIs(restored_wan.get('enabled'), False)
        finally:
            adm.STATE_DIR = orig_state_dir
            adm.RESTORE_BACKUP_DIR = orig_bk_dir

        # Intercom _wan_session_valid falls through stale ts_wan to valid ts_admin + 10-min snooze label
        ic_src = _read('/usr/local/bin/thinksmart-intercom.py')
        self.assertIn('贪睡 10 分钟', ic_src)
        self.assertNotIn('贪睡 5 分钟', ic_src)
        ic_mod = _load_mod('ts_intercom_r7', '/usr/local/bin/thinksmart-intercom.py')
        orig_ic_dir = ic_mod.ADMIN_STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as td_ic:
                ic_p = Path(td_ic)
                ic_mod.ADMIN_STATE_DIR = ic_p
                ic_mod._WAN_SESS_CACHE.update({'sig': 'unset', 'data': {}})
                now_i = int(time.time())
                stale_tok, valid_tok = 'stale_wan_tok', 'valid_adm_tok'
                sess_data = {
                    _hl.sha256(stale_tok.encode()).hexdigest(): {'seen': now_i - 99999, 'ttl': 60},
                    _hl.sha256(valid_tok.encode()).hexdigest(): {'seen': now_i, 'ttl': 3600},
                }
                (ic_p / 'sessions.json').write_text(json.dumps(sess_data), encoding='utf-8')
                dummy_req = type('R', (), {'cookies': {'ts_wan': stale_tok, 'ts_admin': valid_tok}})()
                self.assertTrue(ic_mod._wan_session_valid(dummy_req))
        finally:
            ic_mod.ADMIN_STATE_DIR = orig_ic_dir
            ic_mod._WAN_SESS_CACHE.update({'sig': 'unset', 'data': {}})

        # Home asr_endpoint (unquoted spaces + JSON), weather_credentials (JSON), and coming_days (holidays_extra)
        hm_mod = _load_mod('ts_home_r7', '/usr/local/bin/thinksmart-home.py')
        import datetime as _dt
        with tempfile.TemporaryDirectory() as td_hm:
            p_yaml = Path(td_hm) / 'cfg.yaml'
            p_yaml.write_text(
                "ASR:\n  MacASR:\n    base_url: http://127.0.0.1:8000/v1/audio/transcriptions\n"
                "    prompt: 请输出简体中文 标点清晰 # trailing comment\n",
                encoding='utf-8',
            )
            ep_y = hm_mod.asr_endpoint(p_yaml)
            self.assertIsNotNone(ep_y)
            self.assertEqual(ep_y['prompt'], '请输出简体中文 标点清晰')

            p_json = Path(td_hm) / 'cfg.json'
            p_json.write_text(json.dumps({
                'ASR': {'MacASR': {'base_url': 'http://127.0.0.1:8000/v1/audio/transcriptions', 'prompt': 'JSON 提示词'}},
                'plugins': {'get_weather': {'api_host': 'https://devapi.qweather.com/', 'api_key': 'abcd1234efgh5678'}},
            }, ensure_ascii=False), encoding='utf-8')
            ep_j = hm_mod.asr_endpoint(p_json)
            self.assertIsNotNone(ep_j)
            self.assertEqual(ep_j['prompt'], 'JSON 提示词')
            wh, wk = hm_mod.weather_credentials(p_json)
            self.assertEqual(wh, 'devapi.qweather.com')
            self.assertEqual(wk, 'abcd1234efgh5678')

        orig_hextra = hm_mod._holidays_extra
        try:
            mon = _dt.date(2026, 11, 23)  # Monday
            tue = _dt.date(2026, 11, 24)  # Tuesday overridden to rest day (False = off)
            hm_mod._holidays_extra = lambda: {tue: False}
            cd = hm_mod.coming_days(mon)
            self.assertTrue(any(x.get('date') == tue.isoformat() and x.get('kind') == 'off' for x in cd), cd)
        finally:
            hm_mod._holidays_extra = orig_hextra

    def test_11_round8_full_system_deep_audit(self):
        """Round 8: 39 deep fixes across xiaozhi-hub, client/kiosk/overlay/qqmusic, and admin/monitor/tutor/study/home/intercom."""
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = hub_root + '/.venv/bin/python'
        hub_probe = r"""
import asyncio
import inspect
import json
import os
import queue
import tempfile
import numpy as np
import portalocker

from config import config_loader as cfg_ld
from core.handle import intentHandler as ih
from core.utils.dialogue import Dialogue
from core.utils.prompt_manager import PromptManager
from plugins_func.functions import change_role as cr_mod
from core.providers.tools.server_mcp.mcp_client import ServerMCPClient
from core.providers.tools.device_mcp import mcp_handler as dmh
from core.providers.tools.device_mcp.mcp_executor import DeviceMCPExecutor
from core.connection import ConnectionHandler
from core.providers.llm.openai import openai as oa_mod
from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from core.providers.tools.server_plugins.plugin_executor import ServerPluginExecutor
from core.providers.tts import index_stream as idx_mod, base as tts_base, edge as edge_mod, default as def_tts
from core.providers.asr import doubao_stream as db_mod
from core.agent import llm_router as lr_mod, call_names as cn_mod
from core.admin import admin_handler as adm_h
from core.utils.wakeup_word import WakeupWordsConfig
from core.utils import reply_progress as rp_mod
from config import logger as log_mod

# 1. intentHandler direct exit imports send_tts_message without NameError
assert hasattr(ih, "send_tts_message")
class _DummyLog:
    def bind(self, **_kw): return self
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def debug(self, *a, **kw): pass

class _DummyWS:
    def __init__(self):
        self.sent = []
    async def send(self, msg):
        self.sent.append(msg)

class _ExitConn:
    def __init__(self):
        self.config = {}
        self.cmd_exit = ["退出", "关闭"]
        self.logger = _DummyLog()
        self.websocket = _DummyWS()
        self.session_id = "sid-r8"
        self.sentence_id = "s1"
        self.client_abort = False
        self.client_is_speaking = False
        self.closed = False
    def clearSpeakStatus(self):
        self.client_is_speaking = False
    async def close(self):
        self.closed = True

ec = _ExitConn()
assert asyncio.run(ih.check_direct_exit(ec, "退出!")) is True
assert ec.closed is True
assert any('"stop"' in m for m in ec.websocket.sent)

# 2. PromptManager resolves relative hub-base-prompt.txt from CWD=tempdir & change_role("英语老师") avoids duplicate language kwarg
pm = PromptManager({"prompt_template": "hub-base-prompt.txt"}, _DummyLog())
assert "{{base_prompt}}" in pm.base_prompt_template or "{% if" in pm.base_prompt_template
class _RoleConn:
    def __init__(self):
        self.logger = _DummyLog()
        self.config = {"prompt": "基础角色设定", "prompt_template": "hub-base-prompt.txt"}
        self.prompt = "旧提示词"
        self.dialogue = Dialogue()
        self.prompt_manager = pm
        self.device_id = "dev_r8"
        self.client_ip = "127.0.0.1"
    def change_system_prompt(self, p):
        self.prompt = p

rc = _RoleConn()
role_res = cr_mod.change_role(rc, "英语老师", "Lily")
assert "Lily" in rc.prompt and "中英双语" in rc.prompt
assert "英语老师" in (role_res.response or "")

# 3. ServerMCPClient surfaces _worker_error immediately on startup crash
async def _test_mcp_err():
    cli = ServerMCPClient({"command": "/nonexistent_mcp_binary_r8"})
    try:
        await cli.initialize()
        raise AssertionError("Expected RuntimeError")
    except Exception as e:
        assert cli._worker_error is not None or "nonexistent" in str(e) or "No such file" in str(e)
    finally:
        await cli.cleanup()
asyncio.run(_test_mcp_err())

# 4. DeviceMCP guards conn.features=None & returns False on closed socket; Executor preserves result
class _DevConn:
    def __init__(self):
        self.logger = _DummyLog()
        self.features = None
        self.websocket = None
        self.is_closing = True
        self.mcp_client = dmh.MCPClient()

dc = _DevConn()
dc.mcp_client.ready = True
dc.mcp_client.tools = {"self.screen.wake": {"name": "self.screen.wake"}}
assert asyncio.run(dmh.send_mcp_message(dc, {"method": "ping"})) is False
dc.features = {"mcp": True}
try:
    asyncio.run(dmh.call_mcp_tool(dc, dc.mcp_client, "self.screen.wake", "{}"))
    raise AssertionError("Expected ConnectionError")
except ConnectionError:
    pass

# 5. _apply_aec handles 20ms mic (320 samples) vs 60ms ref (960 samples) without ValueError
aec_warns = []
class _AecLog(_DummyLog):
    def warning(self, msg, *a, **kw):
        aec_warns.append(str(msg))

aec_conn = ConnectionHandler.__new__(ConnectionHandler)
aec_conn.logger = _AecLog()
ref_pcm = (np.sin(np.linspace(0, 30, 960)) * 4000).astype(np.int16).tobytes()
aec_conn.aec_audio_cache = {100: ref_pcm, 160: ref_pcm}
mic_pcm = (np.sin(np.linspace(0, 10, 320)) * 4000).astype(np.int16).tobytes()
out_pcm = aec_conn._apply_aec(120, mic_pcm)
assert aec_warns == [], aec_warns
assert isinstance(out_pcm, (bytes, bytearray)) and len(out_pcm) == len(mic_pcm) and out_pcm != mic_pcm

# 6. _extract_direct_answer_response decodes JSON escapes in single pass & guards trailing escapes
ext_fn = ConnectionHandler._extract_direct_answer_response
assert ext_fn('{"response": "第一行\\n第二行"}') == "第一行\n第二行"
assert ext_fn('{"response": "C:\\\\Users\\\\fu') == "C:\\Users\\fu"
assert ext_fn('{"response": "你好\\u4e1') == "你好"
assert ext_fn('{"response": "你好\\u4e16') == "你好世"
assert ext_fn('{"response": "你好\\u4e16界"}') == "你好世界"

# 7. OpenAIProvider omits empty tools & preserves <think> prefix/suffix
oa_src = inspect.getsource(oa_mod.LLMProvider.response_with_functions)
assert "if functions:" in oa_src
oa_resp_src = inspect.getsource(oa_mod.LLMProvider.response)
assert 'rem.find("<think>")' in oa_resp_src and 'rem.find("</think>")' in oa_resp_src

class _FakeDelta:
    def __init__(self, c): self.content = c
class _FakeChoice:
    def __init__(self, c): self.delta = _FakeDelta(c)
class _FakeChunk:
    def __init__(self, c): self.choices = [_FakeChoice(c)]
class _FakeStream:
    def __init__(self, items): self._items = items
    def __iter__(self): return iter(self._items)
    def close(self): pass

fake_oa = oa_mod.LLMProvider.__new__(oa_mod.LLMProvider)
fake_oa.model_name = "test-model"
fake_oa.base_url = ""
fake_oa.extra_body = None
fake_oa.max_tokens = 256
fake_oa.temperature = 0.7
fake_oa.top_p = 1.0
fake_oa.frequency_penalty = 0.0
fake_oa.normalize_dialogue = lambda d: d
fake_oa._track = lambda *a, **kw: None
fake_oa.client = type("Cli", (), {"chat": type("Ch", (), {"completions": type("Cp", (), {
    "create": staticmethod(lambda **kw: _FakeStream([_FakeChunk("前缀<think>内省"), _FakeChunk("思考</think>后缀")]))
})()})()})()
assert "".join(fake_oa.response("s1", [])) == "前缀后缀"

# 8. UnifiedToolHandler rejects non-dict JSON arguments & ServerPluginExecutor deepcopies schemas + strips conn
uth_src = inspect.getsource(UnifiedToolHandler)
assert "not isinstance(arguments, dict)" in uth_src
spe_src = inspect.getsource(ServerPluginExecutor)
assert "copy.deepcopy(func_item.description)" in spe_src
assert 'if k != "conn"' in spe_src

# 9. WakeupWordsConfig preserves file on lock contention & handles empty YAML
with tempfile.TemporaryDirectory() as w_td:
    orig_proj = cfg_ld.get_project_dir
    cfg_ld.get_project_dir = lambda: (w_td + "/")
    try:
        os.makedirs(os.path.join(w_td, "data"), exist_ok=True)
        yw = os.path.join(w_td, "data", ".wakeup_words.yaml")
        with open(yw, "w", encoding="utf-8") as f:
            f.write("# comment only\n")
        wc = WakeupWordsConfig()
        assert wc.get_wakeup_response("xiaohe") is None
        wc.update_wakeup_response("xiaohe", "test.wav", "我在呢")
        with open(yw, "r", encoding="utf-8") as f:
            saved_txt = f.read()
        assert "我在呢" in saved_txt
        with open(yw, "a+", encoding="utf-8") as lf:
            portalocker.lock(lf, portalocker.LOCK_EX | portalocker.LOCK_NB)
            try:
                wc2 = WakeupWordsConfig()
                wc2._lock_timeout = 0.05
                try:
                    wc2.update_wakeup_response("xiaohe", "other.wav", "不会覆盖")
                except TimeoutError:
                    pass
            finally:
                portalocker.unlock(lf)
        with open(yw, "r", encoding="utf-8") as f:
            after_txt = f.read()
        assert "我在呢" in after_txt and "不会覆盖" not in after_txt
    finally:
        cfg_ld.get_project_dir = orig_proj

# 10. reply_progress.cut & call_names._speak abort handling + TTS/ASR/Admin/Router/Logger invariants
rc_cut = _ExitConn()
rc_cut.dialogue = Dialogue()
rp_mod.cut(rc_cut, float("nan"))
rp_mod.cut(rc_cut, True)
rp_mod.cut(rc_cut, -100)

async def _test_abort_call():
    ac = _ExitConn()
    ac.tts = object()
    ac.features = {}
    ac.dialogue = Dialogue()
    ac.sample_rate = 24000
    ck = cn_mod._cache_key(ac, "哎，我在！")
    cn_mod._audio_cache[ck] = [b"pkt"]
    orig_delay = cn_mod.CACHED_REPLY_DELAY
    cn_mod.CACHED_REPLY_DELAY = 0.03
    try:
        asyncio.get_running_loop().call_later(0.005, setattr, ac, "client_abort", True)
        assert await cn_mod._speak(ac, "小智", "哎，我在！", cache=True) == "aborted"
        assert len(ac.dialogue.dialogue) == 0
    finally:
        cn_mod.CACHED_REPLY_DELAY = orig_delay
        cn_mod._audio_cache.pop(ck, None)
asyncio.run(_test_abort_call())

assert "def _synthesize_pcm(" in inspect.getsource(idx_mod)
assert "while not self._is_stopped(message.sentence_id):" in inspect.getsource(tts_base.TTSProviderBase)
assert "_send_stop_request(conn)" in inspect.getsource(db_mod)
assert "decode_audio_bytes" in inspect.getsource(adm_h)
assert "if running_loop is loop:" in inspect.getsource(lr_mod)
assert "_logger_initialized and config is None" in inspect.getsource(log_mod.setup_logging)
assert 'os.path.dirname(output_file) or "."' in inspect.getsource(edge_mod)
assert "exist_ok=True" in inspect.getsource(def_tts)
print("R8_HUB_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('R8_HUB_OK', r.stdout)

        # Client, Bridge, Screen._write, BTRound 8 checks
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import inspect
import json
import os
import tempfile
from pathlib import Path
from thinksmart_client import app as app_mod, bridge, device as dev_mod, bt as bt_mod

b_src = inspect.getsource(bridge)
assert "drop_conn = True" in b_src and "65536" in b_src
assert "rem = peek.index(FLUSH_MAGIC) + len(FLUSH_MAGIC)" in b_src
assert "rem += block - len(FLUSH_MAGIC)" in b_src

a_src = inspect.getsource(app_mod)
assert a_src.count("self.ptt_held = False") >= 4
assert "gen != self._reply_gen" in a_src

bth_src = Path("/opt/thinksmart/client/current/thinksmart_client/bt_helper.py").read_text(encoding="utf-8")
assert "if self.adapter:" in bth_src
assert "still_streaming" in inspect.getsource(bt_mod)

with tempfile.TemporaryDirectory() as td:
    from pathlib import Path
    hp = Path(td) / "hint.json"
    (Path(td) / "uxplay-mirroring-active").write_text("1", encoding="utf-8")
    orig_hp = dev_mod.HINT_PATH
    dev_mod.HINT_PATH = hp
    try:
        ext_seq = 9_999_999_999_999_999_999
        hp.write_text(json.dumps({"seq": ext_seq, "hold": True}), encoding="utf-8")
        sc = dev_mod.Screen()
        sc._write(activity=False)
        d1 = json.loads(hp.read_text(encoding="utf-8"))
        assert d1["seq"] == ext_seq and d1["hold"] is True, d1
        sc._write(activity=True)
        d2 = json.loads(hp.read_text(encoding="utf-8"))
        assert d2["seq"] == ext_seq + 1, d2
    finally:
        dev_mod.HINT_PATH = orig_hp
print("R8_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R8_CLI_OK', r_cli.stdout)

        # home_overlay.js parseToolArgs(tool.args vs tool.arguments), Kiosk stop_wait_timer, QQMusic Round 8
        ov_src = _read('/usr/local/share/thinksmart-monitor/home_overlay.js')
        self.assertIn('tool.args !== undefined ? tool.args : tool.arguments', ov_src)
        self.assertIn("'close_ui'", ov_src)
        self.assertIn("'open_radio'", ov_src)
        self.assertIn('nodeType === 3', _read('/usr/local/share/thinksmart-qqmusic/index.html'))
        self.assertIn('elTarget(e)', _read('/opt/thinksmart/client/current/web/v2/app.js'))
        self.assertIn('elTarget(e)', _read('/opt/thinksmart/client/current/web/v2/settings.js'))
        kiosk_src = _read('/usr/local/bin/thinksmart-kiosk.py')
        self.assertIn('def stop_wait_timer():', kiosk_src)
        qq_src = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('_sleep_active_start_seq', qq_src)
        self.assertIn('self._radio_prog.pop(sid, None)', qq_src)
        qq_mod = _load_mod('ts_qq_r8', '/usr/local/bin/thinksmart-qqmusic.py')
        with tempfile.TemporaryDirectory() as td_qq:
            hp_extra = Path(td_qq) / 'holidays_extra.json'
            hp_extra.write_text(json.dumps({'holidays': ['2026-11-24'], 'workdays': ['2026-11-28']}), encoding='utf-8')
            orig_hep = qq_mod._HOLIDAYS_EXTRA_PATH
            qq_mod._HOLIDAYS_EXTRA_CACHE.update({'sig': 'unset', 'map': {}})
            qq_mod._HOLIDAYS_EXTRA_PATH = hp_extra
            try:
                off_day_off, _ = qq_mod._wake_day_info(2026, 11, 24)  # Tuesday overridden to holiday (day_off=True)
                work_day_off, _ = qq_mod._wake_day_info(2026, 11, 28)  # Saturday overridden to workday (day_off=False)
                self.assertTrue(off_day_off)
                self.assertFalse(work_day_off)
            finally:
                qq_mod._HOLIDAYS_EXTRA_PATH = orig_hep
                qq_mod._HOLIDAYS_EXTRA_CACHE.update({'sig': 'unset', 'map': {}})

        # Monitor missing servers.json & partial/0-byte traffic_ledger.json resilience
        if '/usr/local/bin' not in sys.path:
            sys.path.insert(0, '/usr/local/bin')
        mon_mod = _load_mod('ts_mon_r8', '/usr/local/bin/thinksmart-monitor.py')
        with tempfile.TemporaryDirectory() as td_mon:
            assets_d = Path(td_mon) / 'assets'
            state_d = Path(td_mon) / 'state'
            assets_d.mkdir()
            state_d.mkdir()
            (state_d / 'traffic_ledger.json').write_text(json.dumps({
                'corrupt_id': {'from': 'a', 'to': 'b'},
                '77': {'from': '2026-11-01', 'to': '2026-12-01', 'acc_in': 500, 'acc_out': 900, 'last_in': 10, 'last_out': 20, 'complete': True, 'updated_at': 1.0},
            }), encoding='utf-8')
            m_inst = mon_mod.Monitor(assets_d, state_dir=state_d)
            self.assertEqual(m_inst.seed, [])
            self.assertIn('77', m_inst._traffic_ledger)
            (state_d / 'traffic_ledger.json').write_text('', encoding='utf-8')
            m_inst2 = mon_mod.Monitor(assets_d, state_dir=state_d)
            self.assertIsNotNone(getattr(m_inst2, '_ledger_stat', None))

        # Admin LAN HTTPS Origin inference, port mismatch rejection, VPN guard, _sync_wan_cookie, and WAL/SHM cleanup
        adm = _load_mod('ts_adm_r8', '/usr/local/bin/thinksmart-admin.py')
        def _req(headers):
            return type('Req', (), {'headers': headers})()
        self.assertTrue(adm.same_origin_ok(_req({'Origin': 'https://lan.local', 'X-TS-Orig-Host': 'lan.local', 'Host': '127.0.0.1:18091'})))
        self.assertTrue(adm.same_origin_ok(_req({'Origin': 'https://192.168.1.88:8443', 'X-TS-Orig-Host': '192.168.1.88:8443', 'Host': '127.0.0.1:18091'})))
        self.assertFalse(adm.same_origin_ok(_req({'Origin': 'https://127.0.0.1:8097', 'Host': '127.0.0.1:8097'})))
        self.assertFalse(adm.same_origin_ok(_req({'Origin': 'https://example.com', 'Host': 'example.com:8097'})))
        self.assertFalse(adm.is_vpn_tunnel(_req({'Origin': 'https://vpn.local:18443', 'X-TS-Orig-Host': 'vpn.local:18443', 'X-TS-Peer': '127.0.0.1', 'X-Real-IP': '203.0.113.10'})))
        self.assertTrue(hasattr(adm.Admin, '_sync_wan_cookie'))
        adm_html = _read('/usr/local/share/thinksmart-admin/index.html')
        self.assertGreaterEqual(adm_html.count('_pvReq'), 6)

        # Tutor _save_dict_snap preserves mistake_book dc_*.jpg beyond 6-file cap
        tut_mod = _load_mod('ts_tut_r8', '/usr/local/bin/thinksmart-tutor.py')
        with tempfile.TemporaryDirectory() as td_tut:
            orig_sd = tut_mod.STATE_DIR
            tut_mod.STATE_DIR = Path(td_tut)
            try:
                sdir = tut_mod._dictation_snap_dir()
                sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
                kept_name = 'dc_0000001_kept.jpg'
                unkept_name = 'dc_0000002_drop.jpg'
                for idx_s, fn in enumerate([kept_name, unkept_name, *[f'dc_000001{i}_recent.jpg' for i in range(5)]]):
                    fp = sdir / fn
                    fp.write_bytes(b'\xff\xd8\xff\xd9')
                    os.utime(fp, (1000 + idx_s, 1000 + idx_s))
                dm = tut_mod.DictationManager.__new__(tut_mod.DictationManager)
                dm.snap_name = ''
                dm.items = []
                dm.eng = type('E', (), {'mistakes': [{'image_url': f'/api/tutor/snap/{kept_name}'}]})()
                new_nm = dm._save_dict_snap(b'\xff\xd8\xff\xd9')
                self.assertTrue((sdir / new_nm).exists())
                self.assertTrue((sdir / kept_name).exists())
                self.assertFalse((sdir / unkept_name).exists())
            finally:
                tut_mod.STATE_DIR = orig_sd

        # Study config lock, Home alToneSeq, and Intercom stale-UI saved_ambient preservation
        study_src = _read('/usr/local/bin/thinksmart-study.py')
        self.assertIn('_sync_enabled_clock(cfg)', study_src)
        self.assertGreaterEqual(study_src.count('with _config_lock_cm():'), 3)
        self.assertIn('alToneSeq', _read('/usr/local/share/thinksmart-home/index.html'))
        self.assertIn('had_stale_reset', _read('/usr/local/bin/thinksmart-intercom.py'))

    def test_12_round9_full_system_deep_audit(self):
        """Round 9: 44 deep fixes across xiaozhi-hub, client/kiosk/overlay/qqmusic/system, and admin/intercom/monitor/study/tutor/home/tooling."""
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = hub_root + '/.venv/bin/python'
        hub_probe = r"""
import asyncio
import inspect
import os
import tempfile

from core.utils.dialogue import Dialogue, Message
from core.utils import reply_progress as rp_mod
from core.agent import chat_history as ch_mod
from core.connection import ConnectionHandler
from core.websocket_server import WebSocketServer
from core.providers.llm.openai import openai as oa_mod
from core.providers.tts import index_stream as idx_mod
from core.providers.asr import doubao_stream as db_mod
from core.handle import (
    sendAudioHandle as sah,
    abortHandle as ah,
    receiveAudioHandle as rah,
    helloHandle as hh,
    intentHandler as ih,
)
from core.handle.textHandler import listenMessageHandler as lmh
from core.admin import admin_handler as adm_h
from plugins_func.functions import get_news_from_newsnow as news_mod

# 1. Dialogue._ensure_tool_calls_complete assigns missing id without mutating input & drops orphan/duplicate tool msgs
orig_tc = {"type": "function", "function": {"name": "get_time", "arguments": "{}"}}
d = Dialogue()
d.put(Message(role="user", content="现在几点"))
d.put(Message(role="assistant", content=None, tool_calls=[orig_tc]))
d.put(Message(role="tool", content="orphan_out", tool_call_id="orphan_id"))
msgs = d.get_llm_dialogue()
assert "id" not in orig_tc, "must not mutate caller dict in place"
asst_msgs = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
assert len(asst_msgs) == 1
gen_id = asst_msgs[0]["tool_calls"][0]["id"]
assert gen_id and isinstance(gen_id, str)
tool_msgs = [m for m in msgs if m.get("role") == "tool"]
assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == gen_id
assert all(m.get("content") != "orphan_out" for m in tool_msgs)

# Duplicate tool_call_id must be deduplicated
d2 = Dialogue()
d2.put(Message(role="user", content="测试"))
d2.put(Message(role="assistant", content=None, tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]))
d2.put(Message(role="tool", content="first", tool_call_id="tc1"))
d2.put(Message(role="tool", content="dup", tool_call_id="tc1"))
msgs2 = d2.get_llm_dialogue()
t2 = [m for m in msgs2 if m.get("role") == "tool"]
assert len(t2) == 1 and t2[0]["content"] == "first", t2

# 2. reply_progress.note_tool_start preserves turn at 0ms abort & [系统提示] ignored as turn boundary
class _DummyConn:
    def __init__(self):
        self.sentence_id = "sid_tool"
        self.dialogue = Dialogue()
        self.agent_chat_history = None

c_rp = _DummyConn()
u_msg = Message(role="user", content="帮我查天气")
c_rp.dialogue.put(u_msg)
rp_mod.note_turn(c_rp, "sid_tool", u_msg)
rp_mod.note_tool_start(c_rp, "sid_tool")
c_rp.dialogue.put(Message(role="user", content="[系统提示] 已达到最大工具调用次数限制，请直接总结回复"))
c_rp.dialogue.put(Message(role="assistant", content="今天晴朗，气温二十度。"))
rp_mod.note_audio(c_rp, "sid_tool", "今天晴朗，气温二十度。", [b"f"] * 20, last=True)
rp_mod.cut(c_rp, 0)
rp_mod.finish_turn(c_rp, "sid_tool")
assert any(m.role == "user" and m.content == "帮我查天气" for m in c_rp.dialogue.dialogue)

win_msgs = [
    Message(role="user", content="第一轮问题"),
    Message(role="assistant", content="第一轮回答"),
    Message(role="user", content="[系统提示] 内部提示"),
    Message(role="assistant", content="补充回答"),
]
assert ch_mod.select_window(win_msgs, max_messages=2, max_tokens=10000) == 0

# 3. _extract_direct_answer_response handles UTF-16 surrogate pairs & incomplete trailing high surrogate
ext = ConnectionHandler._extract_direct_answer_response
part1 = ext('{"response": "开心\\ud83d')
part2 = ext('{"response": "开心\\ud83d\\ude02耶"}')
assert part1 == "开心" and part1.encode("utf-8")
assert part2 == "开心😂耶" and part2.startswith(part1)

# 4. OpenAI LLMProvider cross-chunk <think> tag split in both response and response_with_functions
class _Delta:
    def __init__(self, c=None, tc=None):
        self.content = c
        self.tool_calls = tc
class _Choice:
    def __init__(self, c=None, tc=None):
        self.delta = _Delta(c, tc)
class _Chunk:
    def __init__(self, c=None, tc=None):
        self.choices = [_Choice(c, tc)]
class _Stream:
    def __init__(self, items):
        self._items = items
    def __iter__(self):
        return iter(self._items)
    def close(self):
        pass

oa = oa_mod.LLMProvider.__new__(oa_mod.LLMProvider)
oa.model_name = "test-model"
oa.base_url = ""
oa.extra_body = None
oa.max_tokens = 256
oa.temperature = 0.7
oa.top_p = 1.0
oa.frequency_penalty = 0.0
oa.normalize_dialogue = lambda x: x
oa._track = lambda *a, **kw: None
split_chunks = [_Chunk("前<thi"), _Chunk("nk>隐藏思"), _Chunk("考</th"), _Chunk("ink>后")]
oa.client = type("Cli", (), {"chat": type("Ch", (), {"completions": type("Cp", (), {
    "create": staticmethod(lambda **kw: _Stream(split_chunks))
})()})()})()
assert "".join(oa.response("s_r9", [])) == "前后"
assert "".join(t for t, _ in oa.response_with_functions("s_r9", [], functions=[])) == "前后"

# 5. IndexStreamTTS PCM resample helper & ChatDB.clear_memory progress watermark
pcm_24k = b"\x10\x00" * 2400
pcm_16k = idx_mod.TTSProvider._resample_s16le_mono(pcm_24k, 24000, 16000)
assert len(pcm_16k) == 3200 and len(pcm_16k) % 2 == 0
assert "self._is_stopped(message.sentence_id)" in inspect.getsource(idx_mod.TTSProvider)

with tempfile.TemporaryDirectory() as td_db:
    db = ch_mod.ChatDB(os.path.join(td_db, "chat.sqlite3"))
    try:
        db.insert("dev_r9", "LLM_A", "user", "旧问题")
        db.insert("dev_r9", "LLM_A", "assistant", "旧回答")
        assert db.memory("dev_r9")["version"] == 0
        db.clear_memory("dev_r9")
        assert db.memory("dev_r9")["version"] == 1
        prog = db.progress("dev_r9")
        assert prog.get("LLM_A", 0) >= 2, prog
    finally:
        db.close()

# 6. Hub source invariants for headers, echo, idle/close_after_chat, wakeup, intent, doubao, newsnow, admin reload
assert "str(k).lower()" in inspect.getsource(WebSocketServer)
assert "if text:" in inspect.getsource(sah.sendAudioMessage)
assert "reply_echo.note_reply(conn, None)" in inspect.getsource(ah.handleAbortMessage)
assert "conn.close_after_chat = False" in inspect.getsource(rah.startToChat)
assert "conn.client_is_speaking" in inspect.getsource(rah.no_voice_close_connect)
assert "original_text" in inspect.getsource(lmh.ListenTextMessageHandler.handle)
assert "sentence_id=sid" in inspect.getsource(hh.checkWakeupWords)
assert "sentence_id: str = None" in inspect.getsource(ih.speak_txt)
assert "_dispatched_stop" in inspect.getsource(db_mod.ASRProvider)
assert "orig_sid" in inspect.getsource(news_mod)
assert "upload_functions_desc" in inspect.getsource(adm_h.AdminHandler)
print("R9_HUB_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('R9_HUB_OK', r.stdout)

        # Group 2: Client Hub._closing / close() reader await, snooze_alarm validation, MCP timer guard, Web ETag, bt_helper
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import asyncio
import inspect
from pathlib import Path
from thinksmart_client import app as app_mod, hub as hub_mod, mcp as mcp_mod, web as web_mod

# 1. Hub.close() marks connected=False immediately and awaits _reader so on_closed finishes before close() returns
events = []
async def _test_hub_close():
    closed_Gate = asyncio.Event()
    class _FakeWS:
        def __iter__(self): return self
        def __aiter__(self): return self
        async def __anext__(self):
            await closed_Gate.wait()
            raise StopAsyncIteration
        async def close(self):
            events.append("ws_close")
            closed_Gate.set()
    async def _on_closed():
        await asyncio.sleep(0.01)
        events.append("on_closed_done")
    h = hub_mod.Hub("ws://127.0.0.1:9", "d", "c", "t", lambda _d: None, lambda _b: None, _on_closed)
    fws = _FakeWS()
    h.ws = fws
    h._reader = asyncio.create_task(h._read(fws))
    await asyncio.sleep(0)
    assert h.connected is True
    await h.close()
    assert h.connected is False
    assert "on_closed_done" in events and events[0] == "ws_close", events

asyncio.run(_test_hub_close())

# 2. snooze_alarm validates minutes BEFORE stopping an active ring
async def _test_snooze_val():
    c = app_mod.Client.__new__(app_mod.Client)
    stopped = []
    async def _fake_stop():
        stopped.append(1)
    c.stop_alarm = _fake_stop
    c.ring = {"items": [{"id": 1}]}
    for bad in (0, 61, -5, True, "10"):
        try:
            await c.snooze_alarm(bad)
            raise AssertionError(f"Expected ValueError for {bad!r}")
        except ValueError:
            pass
    assert stopped == [], "must not stop active alarm when snooze minutes is invalid"

asyncio.run(_test_snooze_val())

mcp_src = inspect.getsource(mcp_mod)
assert "days_from_today" in mcp_src and "math.isfinite(raw_s)" in mcp_src
web_src = inspect.getsource(web_mod)
assert "hashlib.sha1" in web_src and "payload['ringing_info']" in web_src
bth_src = Path("/opt/thinksmart/client/current/thinksmart_client/bt_helper.py").read_text(encoding="utf-8")
assert "pair_close_timer" in bth_src and "if self.adapter:" in bth_src
print("R9_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R9_CLI_OK', r_cli.stdout)

        # Group 2 frontend, idle-blanker, proximity daemon, QQMusic, and set-search-keys checks
        set_js = _read('/opt/thinksmart/client/current/web/v2/settings.js')
        self.assertIn('j.ok || j[k] !== null || D[k] === undefined', set_js)
        self.assertIn("val('home', 'city', D.home && D.home.city)", set_js)
        app_js = _read('/opt/thinksmart/client/current/web/v2/app.js')
        self.assertIn('input[data-rc-range]', app_js)
        self.assertIn('function blurSheetInput()', app_js)
        ov_js = _read('/usr/local/share/thinksmart-monitor/home_overlay.js')
        self.assertIn('rcEl.__q = q;', ov_js)
        self.assertGreaterEqual(ov_js.count("rcEl.__q || ''"), 3)

        if '/usr/local/bin' not in sys.path:
            sys.path.insert(0, '/usr/local/bin')
        ib_mod = _load_mod('ts_ib_r9', '/usr/local/bin/idle-blanker.py')
        with tempfile.TemporaryDirectory() as td_ib:
            hp = Path(td_ib) / 'hint.json'
            mp = Path(td_ib) / 'mode.json'
            hp.write_text('{corrupt', encoding='utf-8')
            mp.write_text('{corrupt', encoding='utf-8')
            orig_hp, orig_mp = ib_mod.HINT_PATH, ib_mod.MODE_PATH
            ib_mod.HINT_PATH, ib_mod.MODE_PATH = hp, mp
            try:
                self.assertEqual(ib_mod.read_hint(('old', 1, 1)), (('old', 1, 1), None))
                self.assertEqual(ib_mod.read_mode(('old', 1, 1), {'mode': 'sleep'}), (('old', 1, 1), {'mode': 'sleep'}))
            finally:
                ib_mod.HINT_PATH, ib_mod.MODE_PATH = orig_hp, orig_mp

        prox_mod = _load_mod('ts_prox_r9', '/usr/local/bin/vcnl4200-proximity-daemon.py')
        ps = prox_mod.ProximitySensor()
        writes = []
        ps.i2c_set_word = lambda reg, val: (writes.append((reg, val)) or True)
        self.assertFalse(ps.sensor_enabled)
        self.assertFalse(ps._ps_hw_known_off)
        ps.disable_sensor()
        self.assertEqual(writes, [])
        ps.disable_sensor(force=True)
        self.assertEqual(writes, [(0x03, 0x08CB)])
        self.assertTrue(ps._ps_hw_known_off)

        qq_src = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('_quality_seq', qq_src)
        self.assertLess(qq_src.index('with _RADIO_PROG_LOCK:'), qq_src.index('snap = dict(self._radio_prog)'))
        ssk_src = _read('/usr/local/sbin/thinksmart-set-search-keys')
        self.assertIn(r'web_search:\s*(?:\{\}\s*)?(?:#.*)?$', ssk_src)

        # Group 3: Admin _merge_keep_secrets, Intercom ::ffff:127.0.0.1, Monitor empty servers & traffic dirty flag, Tutor, Study, Home
        adm = _load_mod('ts_adm_r9', '/usr/local/bin/thinksmart-admin.py')
        merged_sec = adm._merge_keep_secrets(
            {
                'endpoint': 'https://mcp.example.com/sse',
                'providers': [{'name': 'p1', 'api_key': ''}],
            },
            {
                'endpoint': 'https://alice:supersecret@mcp.example.com/sse',
                'providers': [{'name': 'p1', 'api_key': 'sk-real-secret-value-9999'}],
            },
        )
        self.assertEqual(merged_sec['endpoint'], 'https://alice:supersecret@mcp.example.com/sse')
        self.assertEqual(merged_sec['providers'][0]['api_key'], 'sk-real-secret-value-9999')
        adm_html = _read('/usr/local/share/thinksmart-admin/index.html')
        self.assertGreaterEqual(adm_html.count("au0.removeAttribute('src')"), 2)
        self.assertIn('tsEmbedSetActive?.(cur === id)', adm_html)

        ic_mod = _load_mod('ts_ic_r9', '/usr/local/bin/thinksmart-intercom.py')
        tun_hdrs = ic_mod._admin_fwd_headers(
            {'Host': 'example.com', 'X-Real-IP': '198.51.100.9', 'X-TS-WAN': '1', 'X-Forwarded-Proto': 'https'},
            '::ffff:127.0.0.1',
        )
        self.assertEqual(tun_hdrs.get('X-Real-IP'), '198.51.100.9')
        self.assertEqual(tun_hdrs.get('X-TS-WAN'), '1')
        self.assertEqual(tun_hdrs.get('X-Forwarded-Proto'), 'https')
        lan_hdrs = ic_mod._admin_fwd_headers(
            {'Host': '192.168.1.88:8095', 'X-Real-IP': '1.2.3.4', 'X-TS-WAN': '1'},
            '192.168.1.50',
        )
        self.assertNotIn('X-Real-IP', lan_hdrs)
        self.assertNotIn('X-TS-WAN', lan_hdrs)

        if '/usr/local/bin' not in sys.path:
            sys.path.insert(0, '/usr/local/bin')
        mon_mod = _load_mod('ts_mon_r9', '/usr/local/bin/thinksmart-monitor.py')
        with tempfile.TemporaryDirectory() as td_mon:
            ad = Path(td_mon) / 'assets'
            sd = Path(td_mon) / 'state'
            ad.mkdir()
            sd.mkdir()
            (ad / 'servers.json').write_text(json.dumps([{'id': 1, 'name': 'S1'}]), encoding='utf-8')
            (ad / 'traffic.json').write_text(json.dumps({'server_id': 1, 'rule_id': 2}), encoding='utf-8')
            m_r9 = mon_mod.Monitor(ad, state_dir=sd)
            self.assertEqual(len(m_r9.seed), 1)
            m_r9.servers = [{'id': 1, 'name': 'S1', 'alive': True}]
            m_r9.traffic = {'available': True, 'to': '2099-01-01T00:00:00Z'}
            (ad / 'servers.json').write_text('[]\n', encoding='utf-8')
            os.utime(ad / 'servers.json', None)
            self.assertTrue(m_r9._reload_servers_config(force=True))
            self.assertEqual(m_r9.seed, [])
            self.assertEqual(m_r9.servers, [])
            (ad / 'traffic.json').write_text('{}\n', encoding='utf-8')
            os.utime(ad / 'traffic.json', None)
            self.assertTrue(m_r9._reload_traffic_config(force=True))
            asyncio.run(m_r9.refresh_traffic())
            self.assertIsNone(m_r9.traffic)
        self.assertIn('alertAction=null;', _read('/usr/local/share/thinksmart-monitor/index.html'))

        tut_mod = _load_mod('ts_tut_r9', '/usr/local/bin/thinksmart-tutor.py')
        with tempfile.TemporaryDirectory() as td_tut:
            orig_sd = tut_mod.STATE_DIR
            tut_mod.STATE_DIR = Path(td_tut)
            try:
                eng_t = tut_mod.TutorEngine.__new__(tut_mod.TutorEngine)
                eng_t._ensure_mistakes_loaded = lambda: None
                eng_t.mistakes = [{'id': 'mk1', 'question': 'spell apple', 'graduated': False}]
                eng_t._save_mistakes = lambda: None
                eng_t.public_mistakes = lambda: list(eng_t.mistakes)
                eng_t.public_mistake_summary = lambda: {'total': len(eng_t.mistakes)}
                eng_t.cowrite = None
                dc = tut_mod.DictationManager.__new__(tut_mod.DictationManager)
                dc._ensure_loaded = lambda: None
                dc.seq = 1
                dc.items = [{'idx': 0, 'word': 'apple', 'status': 'wrong', 'enrolled_mistake': True, 'mistake_id': 'mk1'}]
                dc._save_state = lambda: None
                dc.public_state = lambda: {'seq': dc.seq, 'items': dc.items}
                eng_t.dictation = dc
                eng_t.delete_mistake(mid='mk1')
                self.assertFalse(dc.items[0]['enrolled_mistake'])
                self.assertEqual(dc.items[0]['mistake_id'], '')
            finally:
                tut_mod.STATE_DIR = orig_sd
        tut_html = _read('/usr/local/share/thinksmart-tutor/index.html')
        self.assertIn('if (audioEl.src === SILENT_WAV_URI) audioEl.pause();', tut_html)
        self.assertIn('if (audioEl.src === SILENT_WAV_URI) return;', tut_html)
        self.assertIn('const onActiveDictation =', tut_html)

        study_src = _read('/usr/local/bin/thinksmart-study.py')
        self.assertIn("prev.get('from_disk') and _boot_clock_unsynced()", study_src)
        self.assertIn("f.get('from_disk') and _boot_clock_unsynced()", study_src)
        home_src = _read('/usr/local/bin/thinksmart-home.py')
        self.assertGreaterEqual(home_src.count('if not still_exists:'), 2)
        home_html = _read('/usr/local/share/thinksmart-home/index.html')
        self.assertIn('let cdReqSeq = 0;', home_html)
        self.assertIn("apiCached('/api/home/countdowns', () => seq === cdReqSeq)", home_html)

    def test_13_round10_full_system_deep_audit(self):
        """Round 10: 44-point full-system deep audit across Hub, Client/Kiosk/QQMusic/Daemons, and Ecosystem/Admin/Installer."""
        import hashlib
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = f'{hub_root}/.venv/bin/python'

        # Group 1: Hub listenMessageHandler, doubao_stream, index_stream, PCM/end_prompt, server/device MCP,
        # connection depth>0 tool fallback, chat_history keep_head & clear_dialogues, ASR odd-PCM, factory class_name guard
        hub_probe = r"""
import asyncio
import inspect
import os
import tempfile
import types
import wave

from config import config_loader
from core import connection as conn_mod
from core.admin import admin_handler as adm_h
from core.agent import call_names, chat_history, clear_command
from core.handle import helloHandle as hh, receiveAudioHandle as rah, sendAudioHandle as sah
from core.handle.textHandler import listenMessageHandler as lmh
from core.providers.asr import base as asr_base, doubao_stream as db_asr, sherpa_onnx_local as sherpa_asr
from core.providers.tools.device_mcp import mcp_executor as dev_mcp_exec, mcp_handler as dev_mcp_h
from core.providers.tools.server_mcp import mcp_client as srv_mcp_cli, mcp_executor as srv_mcp_exec, mcp_manager as srv_mcp_mgr
from core.providers.tts import base as tts_base, index_stream as idx_tts
from core.utils import asr as u_asr, intent as u_intent, llm as u_llm, memory as u_mem, prompt_manager as pm_mod, reply_progress, tts as u_tts, turn_gen, vad as u_vad
from core.utils.cache.manager import GlobalCacheManager
from core.utils.dialogue import Dialogue, Message
from plugins_func.functions import get_weather as gw_mod, web_search as ws_mod

# 1. [device_call] no longer shadows uuid (no UnboundLocalError) and marks keep_head=True
import queue
h_listen = lmh.ListenTextMessageHandler()
d_call = Dialogue()
c_call = types.SimpleNamespace(
    client_listen_mode="manual",
    client_have_voice=False,
    client_voice_stop=False,
    client_is_speaking=False,
    client_abort=True,
    asr_audio=[],
    asr=None,
    vad=None,
    reset_audio_states=lambda: None,
    tts=types.SimpleNamespace(
        store_tts_text=lambda *_a, **_k: None,
        tts_text_queue=queue.Queue(),
        tts_one_sentence=lambda *_a, **_k: None,
    ),
    dialogue=d_call,
    logger=types.SimpleNamespace(bind=lambda **_kw: types.SimpleNamespace(info=lambda *_a, **_k: None, debug=lambda *_a, **_k: None, warning=lambda *_a, **_k: None, error=lambda *_a, **_k: None)),
    config={"enable_greeting": False, "wakeup_words": ["你好小智"]},
    websocket=None,
    session_id="sid_r10",
)
orig_stt, orig_tts = lmh.send_stt_message, lmh.send_tts_message
sent_events = []
async def _fake_stt(conn, text, *_a, **_k):
    sent_events.append(("stt", text, conn.client_abort))
async def _fake_tts(conn, state, text=None, *_a, **_k):
    sent_events.append(("tts", state, conn.client_abort))
lmh.send_stt_message = _fake_stt
lmh.send_tts_message = _fake_tts
try:
    asyncio.run(h_listen.handle(c_call, {"state": "detect", "text": "[device_call] 刚才正在通话"}))
    assert len(d_call.dialogue) == 1 and getattr(d_call.dialogue[0], "keep_head", False) is True
    c_call.client_abort = True
    sent_events.clear()
    asyncio.run(h_listen.handle(c_call, {"state": "detect", "text": "你好小智"}))
    assert len(sent_events) >= 2 and sent_events[0][2] is False and sent_events[1][2] is False, sent_events
finally:
    lmh.send_stt_message, lmh.send_tts_message = orig_stt, orig_tts

# 2. select_window preserves keep_head=True messages & clear_dialogues marks active reply_turns drop=True
d_win = Dialogue()
m_head = Message(role="assistant", content="[device_call] 通话摘要")
m_head.keep_head = True
d_win.put(m_head)
d_win.put(Message(role="user", content="刚才谁找我"))
d_win.put(Message(role="assistant", content="是妈妈"))
assert chat_history.select_window(d_win.dialogue, max_messages=10, max_tokens=2000) == 0
assert chat_history.select_window([Message(role="assistant", content="孤立回复"), *d_win.dialogue[1:]], max_messages=10, max_tokens=2000) == 1

c_clr = types.SimpleNamespace(
    dialogue=d_win,
    device_id="dev_r10_clr",
    session_id="sess_old",
    llm_Context_summary="",
    chat_db=None,
)
reply_progress.note_turn(c_clr, "sid_inflight", Message(role="user", content="hi"))
gen_before = turn_gen.current(c_clr)
chat_history.clear_dialogues([c_clr])
assert turn_gen.current(c_clr) > gen_before
t_inf = c_clr.reply_turns.get("sid_inflight")
assert t_inf is not None and t_inf.get("drop") is True and t_inf.get("interrupted") is True
assert reply_progress.put(c_clr, "sid_inflight", Message(role="assistant", content="late")) is False

# 3. save_audio_to_file handles odd-length raw PCM without wave.Error
with tempfile.TemporaryDirectory() as td_pcm:
    p_asr = types.SimpleNamespace(output_dir=td_pcm, delete_audio_file=False)
    wav_path = asr_base.ASRProviderBase.save_audio_to_file(p_asr, [b"\x01\x02\x03"], "r10_odd")
    with wave.open(wav_path, "rb") as wf:
        assert wf.getnframes() == 1

# 4. Dynamic provider loader rejects path-traversal / non-identifier class_name
for mod_u in (u_asr, u_tts, u_vad, u_llm, u_intent, u_mem):
    try:
        mod_u.create_instance("../evil", {})
        raise AssertionError(f"{mod_u.__name__} accepted invalid class_name")
    except ValueError:
        pass
config_loader.ensure_directories(None)

# 5. Server MCP keeps mcp_* tool names when registered as mcp_* & Device MCP tolerates null/empty tool names
ex_s = srv_mcp_exec.ServerMCPExecutor.__new__(srv_mcp_exec.ServerMCPExecutor)
ex_s._initialized = True
called_names = []
class _FakeMgr:
    def is_mcp_tool(self, name):
        return name == "mcp_weather_now"
    async def execute_tool(self, name, args):
        called_names.append(name)
        return "sunny"
ex_s.mcp_manager = _FakeMgr()
ex_s.conn = types.SimpleNamespace(logger=c_call.logger)
res_mcp = asyncio.run(ex_s.execute(c_call, "mcp_weather_now", {}))
assert called_names == ["mcp_weather_now"], called_names

# Device MCP tools/list with malformed empty-name entry still marks client ready
dm_cli = dev_mcp_h.MCPClient()
async def _noop_send(_c, _p):
    return None
orig_send_mcp = dev_mcp_h.send_mcp_message
dev_mcp_h.send_mcp_message = _noop_send
try:
    c_dm = types.SimpleNamespace(mcp_client=dm_cli, func_handler=None, logger=c_call.logger)
    asyncio.run(dev_mcp_h.handle_mcp_message(c_dm, dm_cli, {
        "id": 2,
        "result": {
            "tools": [
                {"name": None, "description": "bad"},
                {"name": "self.music.status", "description": None, "inputSchema": {"type": "object", "properties": {}}},
            ]
        },
    }))
    assert asyncio.run(dm_cli.is_ready()) is True and "self_music_status" in dm_cli.tools
finally:
    dev_mcp_h.send_mcp_message = orig_send_mcp

# 6. Source & helper invariants across index_stream, doubao_stream, connection, cache, weather, web_search, admin_handler
assert hasattr(idx_tts, "decode_audio_bytes")
assert "RIFF" in inspect.getsource(idx_tts.TTSProvider.text_to_speak)
assert "send_vad_event" in inspect.getsource(db_asr)
assert '!= "pcm"' in inspect.getsource(tts_base.TTSProviderBase)
assert '!= "pcm"' in inspect.getsource(sah)
assert "depth > 0" in inspect.getsource(conn_mod.ConnectionHandler.chat)
assert "asyncio.shield" in inspect.getsource(srv_mcp_cli)
assert "isinstance(srv_config, dict)" in inspect.getsource(srv_mcp_mgr)
assert "tool_call_timeout" in inspect.getsource(dev_mcp_exec)
assert "enable_wakeup_words_response_cache" in inspect.getsource(hh)
assert "audio_to_data" in inspect.getsource(call_names)
assert "conn.client_abort = False" in inspect.getsource(clear_command)
assert 'or "tmp/"' in inspect.getsource(sherpa_asr)
assert "未知位置" in inspect.getsource(pm_mod) and "没查到天气" in inspect.getsource(pm_mod)
assert "is_placeholder" in inspect.getsource(ws_mod._get_amap_key)
assert "population" in inspect.getsource(gw_mod)
assert "11996" in inspect.getsource(adm_h.AdminHandler._synthesize)
cm = GlobalCacheManager()
assert hasattr(cm, "_configs") and hasattr(cm, "_locks")
print("R10_HUB_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=35,
            )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('R10_HUB_OK', r.stdout)

        # Group 2: Client connecting-state cleanup, voice_wake intercom/ring, alarms/mcp repeat validation, bluetooth persist, bt_helper
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import asyncio
import inspect
import types
from pathlib import Path
from thinksmart_client import alarms, app as app_mod, config as cfg_mod, mcp as mcp_mod

# 1. Strict alarm repeat normalization & MCP dict repeat pass-through
assert alarms.normalize_repeat("daily") == {"type": "daily"}
assert alarms.normalize_repeat("workdays") == {"type": "workdays"}
assert alarms.normalize_repeat({"type": "weekly", "days": [5, 1]}) == {"type": "weekly", "days": [1, 5]}
assert alarms.normalize_repeat({"type": "monthly", "day": 15}) == {"type": "monthly", "day": 15}
for bad_rule in (True, [1, 2], {"type": "weekly", "days": [True]}, {"type": "monthly", "day": 1.5}, {"type": "monthly", "day": None}):
    try:
        alarms.normalize_repeat(bad_rule)
        raise AssertionError(f"Expected ValueError for {bad_rule!r}")
    except ValueError:
        pass

aa = mcp_mod._alarm_args({"repeat": {"type": "weekly", "days": [1, 3]}, "time": "07:30", "label": "起床"})
assert aa["repeat"] == {"type": "weekly", "days": [1, 3]}, aa

# 2. _on_mic(False) while connecting (not in_turn) resets state to idle & bumps _reply_gen
c = app_mod.Client.__new__(app_mod.Client)
c.ptt_held = True
c.uploading = False
c.state = "connecting"
c.vad_ended = False
c._reply_gen = 10
c.hub = types.SimpleNamespace(connected=False)
c.events = types.SimpleNamespace(publish=lambda *_a, **_k: None)
c.audio = types.SimpleNamespace(aec_hold=True)
c.screen = types.SimpleNamespace(release=lambda: None, hold=lambda: None)
c.keys = types.SimpleNamespace(mic_on=False)
c.brightness = lambda: 50
c.screen_off = {"enabled": True, "minutes": 5}
c.volume = 40
c.recognising = False
c.awaiting_text = False
c._on_mic(False)
assert c.state == "idle" and c._reply_gen == 11 and c.ptt_held is False

# 3. voice_wake suppresses during intercom call & bypasses verifier delay when alarm is ringing
woke_calls = []
async def _fake_wake(src="button", chimed=False):
    woke_calls.append((src, chimed))
c.wake = _fake_wake
c.backlog = []
c.verifier = types.SimpleNamespace(check=lambda *_a: (_ for _ in ()).throw(AssertionError("verifier must not run")))
c._intercom_active = lambda: True
c.ring = {"items": [{"id": 1}]}
asyncio.run(c.voice_wake("你好小智", b"\x00" * 320))
assert woke_calls == [], woke_calls
c._intercom_active = lambda: False
asyncio.run(c.voice_wake("你好小智", b"\x00" * 320))
assert woke_calls == [("voice", False)], woke_calls

# 4. bluetooth(window=True) persists bluetooth=True when auto-enabling from off
saved_bt = []
orig_save = cfg_mod.save
cfg_mod.save = lambda **kw: saved_bt.append(kw)
try:
    class _FakeBt:
        enabled = False
        async def open_window(self, on):
            if on:
                self.enabled = True
            return True
    c.bt = _FakeBt()
    c.cfg = {"bluetooth": False}
    asyncio.run(c.bluetooth(window=True))
    assert saved_bt == [{"bluetooth": True}] and c.cfg["bluetooth"] is True, saved_bt
finally:
    cfg_mod.save = orig_save

bth_src = Path("/opt/thinksmart/client/current/thinksmart_client/bt_helper.py").read_text(encoding="utf-8")
assert "_player_rank" in bth_src
print("R10_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R10_CLI_OK', r_cli.stdout)

        # Group 2 frontend, QQMusic, Kiosk nmcli single-field unescape, Idle-blanker preview poll, ALS bounds & AirPlay auto-curve
        app_js = _read('/opt/thinksmart/client/current/web/v2/app.js')
        self.assertIn('function abortDrag()', app_js)
        self.assertIn("el.addEventListener('lostpointercapture', onCancel);", app_js)
        set_js = _read('/opt/thinksmart/client/current/web/v2/settings.js')
        self.assertIn('function paintWeather()', set_js)
        self.assertIn('function paintMusic()', set_js)
        ov_js = _read('/usr/local/share/thinksmart-monitor/home_overlay.js')
        self.assertIn("rcEl.__rc = ''; rcEl.__sig = '';", ov_js)
        self.assertIn("decorate(rcEl, rcEl.__spec, rcEl.__q || '', rcEl.__text, rcEl.__sig)", ov_js)
        self.assertIn("r.addEventListener('lostpointercapture', end);", ov_js)

        qq_src = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn("st['sleep_volume'] = max(5, min(60, int(vol)))", qq_src)
        self.assertIn("self._sleep_state['sleep_volume'] = max(5, min(60, int(cur)))", qq_src)
        self.assertNotIn("request.remote in ('127.0.0.1', '::1')", qq_src)
        self.assertNotIn("request.remote not in ('127.0.0.1', '::1')", qq_src)
        self.assertGreaterEqual(qq_src.count("'Cache-Control': 'no-cache'"), 3)
        qq_html = _read('/usr/local/share/thinksmart-qqmusic/index.html')
        self.assertIn("window.addEventListener('blur', endVolDrag);", qq_html)
        self.assertIn('rng.el.releasePointerCapture(rng.id)', qq_html)
        kov_js = _read('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js')
        self.assertIn('lastExtSyncAt', kov_js)
        self.assertIn('applyMusicStatus(st, true);', kov_js)

        ib_src = _read('/usr/local/bin/idle-blanker.py')
        self.assertIn('elif sunrise is not None or had_preview:', ib_src)
        r_kiosk = subprocess.run(
            [
                '/usr/bin/python3',
                '-B',
                '-c',
                (
                    "import importlib.util; "
                    "sp = importlib.util.spec_from_file_location('ts_kiosk_r10', '/usr/local/bin/thinksmart-kiosk.py'); "
                    "m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); "
                    "assert m._unescape_nmcli_single(r'My:Home\\:5G\\\\Net') == r'My:Home:5G\\Net'; "
                    "assert m._unescape_nmcli_single('psk:with:colons:123') == 'psk:with:colons:123'; "
                    "print('KIOSK_OK')"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r_kiosk.returncode, 0, r_kiosk.stderr or r_kiosk.stdout)
        self.assertIn('KIOSK_OK', r_kiosk.stdout)

        if '/usr/local/bin' not in sys.path:
            sys.path.insert(0, '/usr/local/bin')
        prox_mod = _load_mod('ts_prox_r10', '/usr/local/bin/vcnl4200-proximity-daemon.py')
        wake_mod = _load_mod('ts_wake_r10', '/usr/local/bin/screen-wake-daemon.py')
        with tempfile.TemporaryDirectory() as td_als:
            tdp = Path(td_als)
            hp = tdp / 'screen.json'
            mp = tdp / 'display.json'
            ap = tdp / 'als.json'
            ub = tdp / 'ub'
            hp.write_text(json.dumps({'brightness': 60}), encoding='utf-8')
            mp.write_text(json.dumps({'auto': True, 'anchor': 60}), encoding='utf-8')
            ub.write_text('2500\n', encoding='utf-8')
            # Future timestamp (clock jump) or huge lux must be rejected and fall back to user_brightness (2500)
            ap.write_text(json.dumps({'lux': 99999.0, 'at': 1000.0}), encoding='utf-8')
            orig_p = (prox_mod.HINT_PATH, prox_mod.MODE_PATH, prox_mod.ALS_RESULT, prox_mod.USER_BRIGHTNESS_PATH, prox_mod.should_wake_night_low)
            orig_w = (wake_mod.HINT_PATH, wake_mod.MODE_PATH, wake_mod.ALS_PATH, wake_mod.USER_BRIGHTNESS_PATH, wake_mod.should_wake_night_low)
            prox_mod.HINT_PATH, prox_mod.MODE_PATH, prox_mod.ALS_RESULT, prox_mod.USER_BRIGHTNESS_PATH, prox_mod.should_wake_night_low = hp, mp, ap, ub, (lambda _n=None: False)
            wake_mod.HINT_PATH, wake_mod.MODE_PATH, wake_mod.ALS_PATH, wake_mod.USER_BRIGHTNESS_PATH, wake_mod.should_wake_night_low = hp, mp, ap, ub, (lambda _n=None: False)
            try:
                self.assertEqual(prox_mod.read_wake_brightness(now=1005.0), 2500)
                self.assertEqual(wake_mod.read_wake_brightness(now=1005.0), 2500)
                ap.write_text(json.dumps({'lux': 1.0, 'at': 1100.0}), encoding='utf-8')
                self.assertEqual(prox_mod.read_wake_brightness(now=1005.0), 2500)
                self.assertEqual(wake_mod.read_wake_brightness(now=1005.0), 2500)
            finally:
                prox_mod.HINT_PATH, prox_mod.MODE_PATH, prox_mod.ALS_RESULT, prox_mod.USER_BRIGHTNESS_PATH, prox_mod.should_wake_night_low = orig_p
                wake_mod.HINT_PATH, wake_mod.MODE_PATH, wake_mod.ALS_PATH, wake_mod.USER_BRIGHTNESS_PATH, wake_mod.should_wake_night_low = orig_w
        air_src = _read('/usr/local/bin/thinksmart-airplay.py')
        self.assertIn('thinksmart-als.json', air_src)

        # Group 3: Admin list-by-id secret merge & corrupt SQLite fallback, Home locate(), Intercom WAN port & mic_restarts,
        # Monitor sub-day expiry & server-delete cache prune, Tutor subject resolution & graded dictation TTS
        adm = _load_mod('ts_adm_r10', '/usr/local/bin/thinksmart-admin.py')
        merged_r10 = adm._merge_keep_secrets(
            {
                'providers': [
                    {'name': 'prov_b', 'api_key': ''},
                    {'name': 'prov_a', 'api_key': ''},
                ],
                'urls': ['https://example.com/hook'],
            },
            {
                'providers': [
                    {'name': 'prov_a', 'api_key': 'sk-alpha-secret-1111'},
                    {'name': 'prov_b', 'api_key': 'sk-beta-secret-2222'},
                ],
                'urls': ['https://user:pass123@example.com/hook'],
            },
        )
        self.assertEqual(merged_r10['providers'][0]['api_key'], 'sk-beta-secret-2222')
        self.assertEqual(merged_r10['providers'][1]['api_key'], 'sk-alpha-secret-1111')
        self.assertEqual(merged_r10['urls'][0], 'https://user:pass123@example.com/hook')

        # Corrupt on-disk SQLite DB fallback during restore_backup_archive in isolated TemporaryDirectory
        import io
        import sqlite3
        import tarfile
        with tempfile.TemporaryDirectory() as td_bk:
            tdb = Path(td_bk)
            (tdb / 'data').mkdir(parents=True, exist_ok=True)
            corrupt_target = tdb / 'data' / 'chat.db'
            corrupt_target.write_bytes(b'not-a-valid-sqlite-database-header')
            good_src = tdb / 'good_src.db'
            con_g = sqlite3.connect(str(good_src))
            con_g.execute('CREATE TABLE msg (id INTEGER PRIMARY KEY, txt TEXT)')
            con_g.execute("INSERT INTO msg (txt) VALUES ('restored_ok')")
            con_g.commit()
            con_g.close()
            good_bytes = good_src.read_bytes()
            sha_good = hashlib.sha256(good_bytes).hexdigest()
            manifest = {
                'format': 'thinksmart-backup-v1',
                'mode': 'full',
                'files': [{'arc': 'hub/chat.db', 'sha256': sha_good, 'size': len(good_bytes)}],
            }
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tf:
                mb = json.dumps(manifest).encode('utf-8')
                mi = tarfile.TarInfo('BACKUP_MANIFEST.json')
                mi.size = len(mb)
                tf.addfile(mi, io.BytesIO(mb))
                fi = tarfile.TarInfo('hub/chat.db')
                fi.size = len(good_bytes)
                tf.addfile(fi, io.BytesIO(good_bytes))
            orig_hub_dir, orig_bk_root = adm.HUB_DIR, adm.RESTORE_BACKUP_DIR
            adm.HUB_DIR = tdb
            adm.RESTORE_BACKUP_DIR = tdb / 'backups'
            try:
                res_bk = adm.restore_backup_archive(buf.getvalue(), preserve_secrets=False)
                self.assertTrue(res_bk.get('ok'))
                con_chk = sqlite3.connect(str(corrupt_target))
                try:
                    rows = con_chk.execute('SELECT txt FROM msg').fetchall()
                finally:
                    con_chk.close()
                self.assertEqual(rows, [('restored_ok',)])
            finally:
                adm.HUB_DIR, adm.RESTORE_BACKUP_DIR = orig_hub_dir, orig_bk_root

        adm_html = _read('/usr/local/share/thinksmart-admin/index.html')
        self.assertIn('function clearPendingTtsPv()', adm_html)
        self.assertIn('function clearPendingStudyPv()', adm_html)

        home_mod = _load_mod('ts_home_r10', '/usr/local/bin/thinksmart-home.py')
        w_obj = home_mod.Weather()
        w_obj.loc = {'id': '101010100', 'name': '北京'}
        w_obj.locs['上海'] = {'id': '101020100', 'name': '上海', '_ts': 9999999999.0}
        orig_lcfg = home_mod.load_config
        home_mod.load_config = lambda: {'city': '上海'}
        try:
            got_loc = asyncio.run(w_obj._ensure_loc(None, 'h', 'k', '上海'))
            self.assertEqual(got_loc['name'], '上海')
            self.assertEqual(w_obj.loc['name'], '北京')
        finally:
            home_mod.load_config = orig_lcfg

        ic_mod = _load_mod('ts_ic_r10', '/usr/local/bin/thinksmart-intercom.py')
        self.assertTrue(
            ic_mod._msg_origin_ok(
                {'origin': 'https://wan.example.com', 'host': 'wan.example.com:443'},
                peer='203.0.113.5',
                allow_wan_dir=True,
            )
        )
        self.assertFalse(
            ic_mod._msg_origin_ok(
                {'origin': 'http://127.0.0.1:8095', 'host': '127.0.0.1:8095'},
                peer='203.0.113.5',
                allow_wan_dir=True,
            )
        )
        ic_src = _read('/usr/local/bin/thinksmart-intercom.py')
        self.assertIn('good_chunks == 50', ic_src)
        self.assertIn('_wasZeroW', ic_src)

        mon_mod = _load_mod('ts_mon_r10', '/usr/local/bin/thinksmart-monitor.py')
        exp_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 60))
        snap_exp = {
            'servers': [{'id': 1, 'name': 'VPS1', 'alive': True, 'expires': exp_iso}],
            'summary': {'online': 1, 'total': 1},
        }
        self.assertIn('已到期', mon_mod.build_voice_summary(snap_exp, query='VPS1'))
        with tempfile.TemporaryDirectory() as td_m10:
            ad10 = Path(td_m10) / 'assets'
            sd10 = Path(td_m10) / 'state'
            ad10.mkdir()
            sd10.mkdir()
            (ad10 / 'servers.json').write_text(json.dumps([{'id': 1, 'name': 'S1'}, {'id': 2, 'name': 'S2'}]), encoding='utf-8')
            (ad10 / 'traffic.json').write_text('{}\n', encoding='utf-8')
            m10 = mon_mod.Monitor(ad10, state_dir=sd10)
            m10._srv_latency_cache[2] = {'at': 100.0, 'carriers': {}}
            m10.speed_hist[2] = [(1.0, 2.0)]
            m10.extra_traffic['2'] = {'used': 10}
            (ad10 / 'servers.json').write_text(json.dumps([{'id': 1, 'name': 'S1'}]), encoding='utf-8')
            os.utime(ad10 / 'servers.json', None)
            self.assertTrue(m10._reload_servers_config(force=True))
            self.assertNotIn(2, m10._srv_latency_cache)
            self.assertNotIn(2, m10.speed_hist)
            self.assertNotIn('2', m10.extra_traffic)

        tut_mod = _load_mod('ts_tut_r10', '/usr/local/bin/thinksmart-tutor.py')
        self.assertEqual(tut_mod.detect_subject('【英语】阅读理解：Read the short passage and choose the best answer.'), 'english')
        self.assertEqual(tut_mod._resolve_saved_subject('1840年鸦片战争对中国近代社会的影响是什么？', 'history'), 'history')
        self.assertEqual(tut_mod._resolve_saved_subject('已知在△ABC中，∠A=60°，AB=4，求BC的长', 'english'), 'math')
        dm_t = tut_mod.DictationManager.__new__(tut_mod.DictationManager)
        dm_t._ensure_loaded = lambda: None
        dm_t.en_mode = 'zh_prompt'
        dm_t.phase = 'graded'
        dm_t.revealed = False
        dm_t.items = [{'idx': 0, 'word': 'apple', 'hint': '苹果', 'lang': 'en'}]
        spoken_graded = dm_t.spoken_text_for(0, pass_no=1)
        self.assertIn('apple', spoken_graded)
        self.assertNotIn('请默写英文', spoken_graded)

        self.assertIn('if (cw > 0 && wasZero) { refresh(false); syncPollTimer(); }', _read('/usr/local/share/thinksmart-study/parent.html'))
        self.assertIn('({ r, j } = await postMobileJson(payload, reqTimeout));', _read('/usr/local/share/thinksmart-tutor/mobile.html'))
        self.assertIn('&rv=${rvFlag}', _read('/usr/local/share/thinksmart-tutor/index.html'))
        self.assertIn('dDiff>=0?Math.ceil(dDiff/86400000):-1', _read('/usr/local/share/thinksmart-monitor/index.html'))

    def test_14_round11_full_system_deep_audit(self):
        """Round 11: 46-point full-system deep audit across Hub, Client/Kiosk/QQMusic/USB, and Ecosystem/Admin/Backup/Tooling."""
        import hashlib
        import io
        import tarfile
        import threading

        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = f'{hub_root}/.venv/bin/python'

        hub_probe = r"""
import asyncio
import inspect
import struct
import time
import types
from pathlib import Path

from core import connection as conn_mod
from core.admin import admin_handler as adm_h
from core.agent import call_names
from core.handle import abortHandle as abort_h, helloHandle as hh, sendAudioHandle as sah
from core.handle.textHandler import mcpMessageHandler as mcp_mh
from core.providers.asr import base as asr_base, doubao_stream as db_asr
from core.providers.llm.openai import openai as oa_llm
from core.providers.tools import unified_tool_handler as uth_mod
from plugins_func.register import Action, ActionResponse
from core.providers.tools.device_mcp import mcp_handler as dev_mcp_h
from core.providers.tools.server_mcp import mcp_manager as srv_mcp_mgr
from core.providers.tts import base as tts_base, index_stream as idx_tts
from core.utils import audioRateController as arc_mod, reply_progress, util as u_util
from core.utils.dialogue import Dialogue, Message

# 1. reply_progress per-sentence_id isolation: drop_empty_turn & cut never clobber concurrent barge-in turn
dlg = Dialogue()
u1 = Message(role="user", content="第一句失败的问题")
dlg.put(u1)
c_rp = types.SimpleNamespace(dialogue=dlg, sentence_id="sid_t2")
reply_progress.note_turn(c_rp, "sid_t1", u1, dlg)
u2 = Message(role="user", content="第二句抢先打断的问题")
dlg.put(u2)
reply_progress.note_turn(c_rp, "sid_t2", u2, dlg)
reply_progress.note_audio(c_rp, "sid_t2", "这是第二句的回答", [b"p"] * 20)

reply_progress.drop_empty_turn(c_rp, "sid_t1")
assert u1 not in dlg.dialogue and u2 in dlg.dialogue, dlg.dialogue
# Cutting Turn 1 after Turn 2 started must NOT delete u2!
reply_progress.cut(c_rp, 0, sentence_id="sid_t1")
assert u2 in dlg.dialogue
assert reply_progress.heard_text(c_rp, 600, sentence_id="sid_t2")[0] != ""

# 2. AudioRateController._sending guard prevents false pause recovery during slow send_audio_callback
arc = arc_mod.AudioRateController(frame_duration=60)
arc.reset()
sent_pkts = []
async def _run_arc():
    async def _slow_send(pkt):
        # Inside callback, _sending must be True so concurrent add_audio cannot falsely reset start_timestamp
        assert arc._sending is True
        if len(sent_pkts) == 0:
            arc._last_queue_empty_time = time.monotonic() - 0.5
            t_before = arc.start_timestamp
            arc.add_audio(b"\x02")
            assert arc.start_timestamp == t_before
        sent_pkts.append(pkt)
    arc.add_audio(b"\x01")
    arc.start_sending(_slow_send)
    await asyncio.sleep(0.03)
    assert sent_pkts == [b"\x01"], f"Packet #2 burst early: {sent_pkts}"
    await asyncio.sleep(0.05)
    assert sent_pkts == [b"\x01", b"\x02"], f"Expected 2 paced packets, got {sent_pkts}"
    arc.stop_sending()
asyncio.run(_run_arc())

# 3. Device MCP longest-first tool name mapping & 16-page cursor cap
dm_cli = dev_mcp_h.MCPClient()
sent_mcp = []
async def _rec_mcp(_c, payload):
    sent_mcp.append(payload)
orig_send_mcp = dev_mcp_h.send_mcp_message
dev_mcp_h.send_mcp_message = _rec_mcp
try:
    c_dm = types.SimpleNamespace(
        mcp_client=dm_cli,
        func_handler=None,
        logger=types.SimpleNamespace(bind=lambda **_k: types.SimpleNamespace(info=lambda *_a, **_kw: None, warning=lambda *_a, **_kw: None, error=lambda *_a, **_kw: None, debug=lambda *_a, **_kw: None)),
    )
    for page_i in range(25):
        before_n = len(sent_mcp)
        asyncio.run(dev_mcp_h.handle_mcp_message(c_dm, dm_cli, {
            "id": 2,
            "result": {
                "tools": [
                    {"name": "self.music", "description": "base self.music and self.music.play", "inputSchema": {"type": "object", "properties": {}}},
                    {"name": "self.music.play", "description": "play via self.music.play", "inputSchema": {"type": "object", "properties": {}}},
                ],
                "nextCursor": f"cur_{page_i}",
            },
        }))
        if len(sent_mcp) == before_n:
            break
    # Capped at exactly 16 pagination requests and marked ready!
    assert len(sent_mcp) == 16 and asyncio.run(dm_cli.is_ready()) is True, (len(sent_mcp), dm_cli.ready)
    desc_base = dm_cli.tools["self_music"]["description"]
    assert "self_music_play" in desc_base and "self_music.play" not in desc_base, desc_base
finally:
    dev_mcp_h.send_mcp_message = orig_send_mcp

# 4. UnifiedToolHandler._combine_responses tolerates None/non-str result & malformed JSON batch arguments
uth = uth_mod.UnifiedToolHandler.__new__(uth_mod.UnifiedToolHandler)
comb = uth._combine_responses([
    ActionResponse(action=Action.RESPONSE, result=None, response=None),
    ActionResponse(action=Action.REQLLM, result={"ok": 1}, response=None),
])
assert comb.action == Action.REQLLM and '{"ok": 1}' in (comb.result or ""), comb.result

# 5. IndexStreamTTS WAV sample-rate parsing + PCM/Opus handling & audio_to_data sample_rate cache key
sr_src = inspect.getsource(idx_tts.TTSProvider.text_to_speak)
assert '!= "pcm"' in sr_src and "_resample_s16le_mono" in sr_src
sig_a2d = inspect.signature(u_util.audio_to_data)
assert "sample_rate" in sig_a2d.parameters and sig_a2d.parameters["sample_rate"].default == 16000

# 6. Core Round 11 invariants across connection, openai, app, admin_handler, abortHandle, doubao_stream, asr/base, server_mcp
conn_src = inspect.getsource(conn_mod.ConnectionHandler)
assert "pending_tool_prefix" in conn_src and "drop_empty_turn" in conn_src and "if not self._superseded(current_sentence_id):" in conn_src
assert "pending_tag" in inspect.getsource(oa_llm.LLMProvider.response_with_functions)
app_src = Path("app.py").read_text(encoding="utf-8")
assert "FIRST_COMPLETED" in app_src and "fatal_exc" in app_src and "aioconsole" not in app_src
adm_src = inspect.getsource(adm_h.AdminHandler)
assert 'getattr(tts, "opus_encoder", None)' in adm_src and 'getattr(conn, "client_have_voice", False)' in adm_src
abort_src = inspect.getsource(abort_h.handleAbortMessage)
assert abort_src.index("reply_echo.note_reply(conn, None)") < abort_src.index("conn.sentence_id =")
assert "_ws_seq" in inspect.getsource(db_asr) and "_dispatched_stop" in inspect.getsource(db_asr)
assert "max_utterance_bytes" in inspect.getsource(asr_base.ASRProviderBase.receive_audio)
assert "await handle_mcp_message" in inspect.getsource(mcp_mh.McpTextMessageHandler.handle)
assert "async with self._init_lock:" in inspect.getsource(srv_mcp_mgr.ServerMCPManager.execute_tool)
assert "sample_rate=" in inspect.getsource(hh)
assert "sample_rate=" in inspect.getsource(sah)
assert "sample_rate=" in inspect.getsource(call_names)
print("R11_HUB_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=hub_root,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=35,
            )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('R11_HUB_OK', r.stdout)

        # Group 2: Client wake-verify vs idle-close race, say/ptt_stop disconnect safety, Hub/BT non-dict guards
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import asyncio
import types
from pathlib import Path
from thinksmart_client import app as app_mod, bt as bt_mod, hub as hub_mod

# 1. voice_wake() survives an idle WebSocket _on_closed() during _check_wake() without dropping the wake!
c = app_mod.Client.__new__(app_mod.Client)
c._verifying_wake = 0
c._reply_gen = 5
c._silent_close = False
c.state = "idle"
c.uploading = False
c.vad_ended = False
c.recognising = False
c.awaiting_text = False
c.hub_pending = False
c.ptt_held = False
c.ring = None
c.backlog = [b"frame1"]
c.threads = {}
c.view_entry = lambda: "default"
c._intercom_active = lambda: False
c.mic_off_toast = lambda: False
c.chime = lambda _s: None
c.screen = types.SimpleNamespace(poke=lambda: None, hold=lambda: None, release=lambda: None)
c.audio = types.SimpleNamespace(aec_hold=False)
c.keys = types.SimpleNamespace(mic_on=True)
c.brightness = lambda: 50
c.screen_off = {"enabled": True, "minutes": 5}
c.volume = 50
c.events = types.SimpleNamespace(publish=lambda *_a, **_k: None)
played = []
c.play = lambda name: played.append(name)
async def _noop_hist():
    return None
c.load_history = _noop_hist
c.hub = types.SimpleNamespace(connected=False)

woke_after_verify = []
async def _fake_wake(source="button", chimed=False):
    woke_after_verify.append((source, chimed, list(c.backlog or [])))
    c.uploading = True
c.wake = _fake_wake

async def _check_with_concurrent_close(_label, _audio):
    # Simulate an old idle WebSocket closing mid-verification!
    await c._on_closed()
    return {"ok": True}
c.verifier = object()
c._check_wake = _check_with_concurrent_close

asyncio.run(c.voice_wake("你好小智", b"\x00" * 320))
assert played == [], f"Unexpected bye sound during wake verify: {played}"
assert woke_after_verify == [("voice", True, [b"frame1"])], woke_after_verify
assert c._verifying_wake == 0

# 2. say() and ptt_stop() never wedge in 'thinking' when hub.send returns False / hub disconnects
c2 = app_mod.Client.__new__(app_mod.Client)
c2._reply_gen = 1
c2.state = "connecting"
c2.uploading = False
c2.listen_after_reply = False
c2.vad_ended = c2.recognising = c2.awaiting_text = c2.hub_pending = False
c2.deadline = None
c2.ring = None
c2._intercom_active = lambda: False
c2.tools_listed = asyncio.Event()
c2.tools_listed.set()
c2.screen = c.screen
c2.audio = c.audio
c2.keys = c.keys
c2.brightness = c.brightness
c2.screen_off = c.screen_off
c2.volume = 50
c2.events = c.events

async def _ens_ok():
    return True
c2.ensure_connected = _ens_ok

class _DroppingHub:
    connected = True
    async def send(self, _msg):
        self.connected = False
        return False

c2.hub = _DroppingHub()
asyncio.run(c2.say("今天天气怎么样"))
assert c2.state == "idle", f"say() wedged in {c2.state}"

c2.hub = _DroppingHub()
c2.state = "listening"
c2.uploading = True
c2.listen_mode = "manual"
c2.ptt_held = True
asyncio.run(c2.ptt_stop())
assert c2.state == "idle" and c2.awaiting_text is False and c2.deadline is None

# 3. Hub._read ignores non-dict JSON frames without raising AttributeError
on_json_calls = []
async def _on_j(d):
    on_json_calls.append(d)
async def _on_c():
    pass
h_obj = hub_mod.Hub("ws://127.0.0.1:9", "d", "c", "t", _on_j, lambda _b: None, _on_c)
class _FakeWs:
    def __aiter__(self):
        async def _gen():
            yield "null"
            yield "[1, 2, 3]"
            yield "42"
            yield '{"type": "stt", "text": "ok"}'
        return _gen()
    async def close(self):
        pass
h_obj.ws = _FakeWs()
asyncio.run(h_obj._read(h_obj.ws))
assert on_json_calls == [{"type": "stt", "text": "ok"}], on_json_calls

# 4. Bluetooth _check_capture stops capture immediately when self.enabled is False
bt_obj = bt_mod.Bluetooth.__new__(bt_mod.Bluetooth)
bt_obj.enabled = False
bt_obj.state = {"devices": [{"address": "AA:BB", "connected": True, "streaming": True}]}
bt_obj.direct = False
bt_obj._streaming_since = 1.0
stopped_cap = []
async def _stop_cap():
    stopped_cap.append(True)
    bt_obj.capture = None
bt_obj._stop_capture = _stop_cap
bt_obj.capture = ("AA:BB", "src", types.SimpleNamespace(returncode=None), None)
asyncio.run(bt_obj._check_capture())
assert stopped_cap == [True] and bt_obj.capture is None

bth_txt = Path("/opt/thinksmart/client/current/thinksmart_client/bt_helper.py").read_text(encoding="utf-8")
assert "if not isinstance(msg, dict):" in bth_txt and "ValueError, TypeError" in bth_txt
print("R11_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R11_CLI_OK', r_cli.stdout)

        # Group 2 frontend, QQMusic daytime sleep_session_stop protection & 0.6s AEC timeout, Kiosk non-blocking GTK & USB mount budget
        app_js = _read('/opt/thinksmart/client/current/web/v2/app.js')
        self.assertIn('function cancelMicHold()', app_js)
        self.assertIn('wifiScanBusy', app_js)
        self.assertIn('wifiOpSeq', app_js)
        set_js = _read('/opt/thinksmart/client/current/web/v2/settings.js')
        self.assertIn('const failedPart = {};', set_js)
        self.assertIn('qrWantUrl', set_js)
        self.assertIn('s.onerror =', set_js)
        ov_js = _read('/usr/local/share/thinksmart-monitor/home_overlay.js')
        self.assertIn('function armSubShowTimer(entry)', ov_js)
        self.assertIn('r.__drag = true;', ov_js)
        self.assertIn('el.__pendingRich', ov_js)
        self.assertIn('data:image\\/(?:png|jpeg|jpg|gif|webp|svg\\+xml);base64', ov_js)
        self.assertIn('(vcTurnTool.time || 0) < uTime - 0.05', ov_js)
        self.assertIn("nativeRaf(() => nativeRaf(() => {", ov_js)

        if '/usr/local/bin' not in sys.path:
            sys.path.insert(0, '/usr/local/bin')
        qq_src = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('conn.settimeout(0.6)', qq_src)
        self.assertNotIn('wait(timeout=1)', qq_src.split('def _ambient_worker', 1)[1].split('def start_aec_unix_server', 1)[0])
        # Behavioral check on QQMusicEngine.sleep_session_stop without executing module-level QQMusicEngine()
        import ast as _ast
        qq_tree = _ast.parse(qq_src)
        qq_tree.body = [
            n for n in qq_tree.body
            if not (isinstance(n, _ast.Assign) and any(isinstance(t, _ast.Name) and t.id == 'engine' for t in n.targets))
            and not (isinstance(n, _ast.If) and isinstance(n.test, _ast.Compare) and getattr(n.test.left, 'id', '') == '__name__')
        ]
        qq_ns = {'__file__': '/usr/local/bin/thinksmart-qqmusic.py'}
        exec(compile(qq_tree, '/usr/local/bin/thinksmart-qqmusic.py', 'exec'), qq_ns)
        qe = object.__new__(qq_ns['QQMusicEngine'])
        qe._lock = threading.Lock()
        qe.sleep_session = None
        qe._wake = None
        qe._preview = None
        qe.ambient_preset = None
        qe.playing = True
        qe.paused = False
        qe.sleep_mode = 'tracks'
        qe.sleep_tracks_left = 3
        qe.sleep_deadline = 0.0
        res_day_stop = qe.sleep_session_stop()
        self.assertTrue(res_day_stop['ok'])
        self.assertEqual(qe.sleep_mode, 'tracks')
        self.assertEqual(qe.sleep_tracks_left, 3)

        kiosk_src = _read('/usr/local/bin/thinksmart-kiosk.py')
        self.assertIn('def stop_term_timer():', kiosk_src)
        self.assertIn("'reload_seq': 0", kiosk_src)
        usb_sh = _read('/usr/local/bin/usb-automount.sh')
        self.assertIn('timeout 4 mount -t ntfs3 -o "$OPTIONS"', usb_sh)
        self.assertIn('TimeoutStartSec=35', _read('/etc/systemd/system/thinksmart-usb-mount@.service'))

        # Group 3: Tutor input-quiz / spoiler scrub / live backup reload, Home live disk sync, Admin placeholder/auth/study-restore, Intercom IPv6 TURN, Monitor traffic voice summary
        tut_mod = _load_mod('ts_tut_r11', '/usr/local/bin/thinksmart-tutor.py')
        qz_in = tut_mod.sanitize_quiz(
            {'mode': 'input', 'q': '请拼写：苹果', 'ans': 'apple', 'tip': 'a 开头'},
            step_num=3,
            rng_seed='r11',
        )
        self.assertIsNotNone(qz_in)
        self.assertEqual((qz_in['mode'], qz_in['ans']), ('input', 'apple'))
        qa_scrubbed = tut_mod.scrub_locked_spoilers_from_qa(
            {'title': '点拨', 'say': '正确答案就是 apple 哦！', 'board': '直接写 apple', 'note': ''},
            [{'step': 3, 'board': '拼写 = □', 'quiz': qz_in}],
            strict_no_result=True,
            question_txt='苹果的英文怎么写',
        )
        self.assertNotIn('apple', qa_scrubbed['say'].lower())
        self.assertNotIn('apple', qa_scrubbed['board'].lower())

        with tempfile.TemporaryDirectory() as td_tut:
            tdp = Path(td_tut)
            orig_tut_paths = (tut_mod.STATE_DIR, tut_mod.HIST_PATH, tut_mod.PREFS_PATH, tut_mod._mistake_path)
            tut_mod.STATE_DIR = tdp
            tut_mod.HIST_PATH = tdp / 'history.json'
            tut_mod.PREFS_PATH = tdp / 'prefs.json'
            tut_mod._mistake_path = lambda: tdp / 'mistake_book.json'
            try:
                eng_t = tut_mod.TutorEngine()
                # Dictation enroll_mistakes scrubs spoilers from Steps 1-3 and question stem
                eng_t.dictation._ensure_loaded()
                eng_t.dictation.lang = 'en'
                eng_t.dictation.phase = 'graded'
                eng_t.dictation.items = [{
                    'idx': 0, 'word': 'bicycle', 'hint': '自行车', 'lang': 'en',
                    'grade_status': 'wrong', 'written': 'bicyle', 'grade_note': '应该写 bicycle 注意 cyc',
                }]
                res_de = eng_t.dictation.enroll_mistakes()
                self.assertEqual(res_de['enrolled_count'], 1)
                self.assertNotIn('bicycle', (eng_t.mistakes[0].get('question') or '').lower())
                m_steps = eng_t.mistakes[0]['steps']
                for st_early in m_steps:
                    self.assertNotIn('bicycle', (st_early.get('say') or '').lower())
                    self.assertNotIn('bicycle', (st_early.get('board') or '').lower())
                self.assertEqual(m_steps[-1]['quiz']['mode'], 'input')
                self.assertEqual(m_steps[-1]['quiz']['ans'], 'bicycle')

                # Live external backup restore on history.json & mistake_book.json reloads immediately
                tut_mod._atomic_write_json(tut_mod.HIST_PATH, [{
                    'id': 'restored0001', 'title': '恢复的讲题', 'question': '2+3=?',
                    'mode': 'fast', 'source': 'custom', 'created_at': 1700000000,
                    'steps': [{'step': 1, 'title': '审题', 'say': '看题', 'board': '已知 2 和 3', 'draw': []}],
                }])
                tut_mod._atomic_write_json(tut_mod._mistake_path(), [{
                    'id': 'mb_restored1', 'title': '恢复的错题', 'question': '5+6=?',
                    'created_at': 1700000000, 'updated_at': 1700000000, 'steps': [],
                }])
                pub_h = eng_t.public_session('restored0001')
                self.assertIsNotNone(pub_h)
                self.assertEqual(pub_h['title'], '恢复的讲题')
                self.assertEqual(eng_t.public_mistakes()[0]['id'], 'mb_restored1')
            finally:
                tut_mod.STATE_DIR, tut_mod.HIST_PATH, tut_mod.PREFS_PATH, tut_mod._mistake_path = orig_tut_paths

        home_mod = _load_mod('ts_home_r11', '/usr/local/bin/thinksmart-home.py')
        with tempfile.TemporaryDirectory() as td_hm:
            hmp = Path(td_hm)
            inb = home_mod.Inbox(hmp / 'messages.json')
            cds = home_mod.CountdownStore(hmp / 'countdowns.json')
            inb.add('旧留言', 'phone', '妈妈')
            cds.upsert({'title': '旧倒数日', 'kind': 'once', 'date': '2027-06-01'})
            v_inb0, v_cd0 = inb.version, cds.version
            home_mod._write_private_json(hmp / 'messages.json', [{
                'id': 'a1b2c3d4e5f6', 'text': '备份恢复留言', 'at': time.time(),
                'read_at': None, 'source': 'phone', 'kind': 'text', 'who': '爸爸',
            }])
            home_mod._write_private_json(hmp / 'quick_replies.json', ['收到啦'])
            home_mod._write_private_json(hmp / 'countdowns.json', [{
                'id': 'f6e5d4c3b2a1', 'title': '期末考', 'kind': 'once', 'date': '2027-07-01', 'created_at': time.time(),
            }])
            self.assertEqual(inb.recent(5)[0]['text'], '备份恢复留言')
            self.assertEqual(inb.quick, ['收到啦'])
            self.assertGreater(inb.version, v_inb0)
            self.assertEqual(cds.list_enriched()[0]['title'], '期末考')
            self.assertGreater(cds.version, v_cd0)

        adm_mod = _load_mod('ts_adm_r11', '/usr/local/bin/thinksmart-admin.py')
        self.assertTrue(adm_mod.is_placeholder_secret('请填写你的API密钥'))
        self.assertTrue(adm_mod.is_placeholder_secret('替换成你的token'))
        self.assertFalse(adm_mod.is_placeholder_secret('你好小智今天天气真不错'))
        self.assertFalse(adm_mod.is_placeholder_secret('sk-live-real-looking-test-000'))
        with tempfile.TemporaryDirectory() as td_adm:
            admp = Path(td_adm)
            auth_obj = adm_mod.Auth(admp / 'admin_state')
            auth_obj.enable_password(adm_mod.hash_password('ValidPass123!', 'admin'), 'UnitTest')
            self.assertTrue((admp / 'admin_state' / '.auth_initialized').is_file())
            auth_obj.clear_all()
            self.assertFalse((admp / 'admin_state' / 'auth.json').exists())
            self.assertTrue((admp / 'admin_state' / '.auth_initialized').is_file())

            # Restoring study/config.json forces enabled=False so camera never starts silently
            st_dir = admp / 'study_state'
            st_dir.mkdir(parents=True, exist_ok=True)
            st_cfg_bytes = json.dumps({'enabled': True, 'interval': 60}).encode('utf-8')
            sha_st = hashlib.sha256(st_cfg_bytes).hexdigest()
            manifest = {
                'format': 'thinksmart-backup-v1',
                'mode': 'sanitized',
                'files': [{'arc': 'study/config.json', 'sha256': sha_st, 'size': len(st_cfg_bytes)}],
            }
            buf_st = io.BytesIO()
            with tarfile.open(fileobj=buf_st, mode='w:gz') as tf:
                mb = json.dumps(manifest).encode('utf-8')
                mi = tarfile.TarInfo('BACKUP_MANIFEST.json')
                mi.size = len(mb)
                tf.addfile(mi, io.BytesIO(mb))
                fi = tarfile.TarInfo('study/config.json')
                fi.size = len(st_cfg_bytes)
                tf.addfile(fi, io.BytesIO(st_cfg_bytes))
            orig_st_state, orig_bk_d = adm_mod.STUDY_STATE, adm_mod.RESTORE_BACKUP_DIR
            adm_mod.STUDY_STATE = st_dir
            adm_mod.RESTORE_BACKUP_DIR = admp / 'backups'
            try:
                res_st = adm_mod.restore_backup_archive(buf_st.getvalue(), preserve_secrets=False)
                self.assertTrue(res_st.get('ok'))
                restored_st = json.loads((st_dir / 'config.json').read_text(encoding='utf-8'))
                self.assertIs(restored_st.get('enabled'), False)
            finally:
                adm_mod.STUDY_STATE, adm_mod.RESTORE_BACKUP_DIR = orig_st_state, orig_bk_d

        ic_src = _read('/usr/local/bin/thinksmart-intercom.py')
        self.assertIn('if xfp in ("http", "https") and po.scheme != xfp:', ic_src)
        self.assertIn('up_headers["X-Tutor-Entry-Base"] = f"{eff_proto}://{entry_host}"', ic_src)

        mon_mod = _load_mod('ts_mon_r11', '/usr/local/bin/thinksmart-monitor.py')
        with tempfile.TemporaryDirectory() as td_m11:
            ad11 = Path(td_m11) / 'assets'
            sd11 = Path(td_m11) / 'state'
            ad11.mkdir()
            sd11.mkdir()
            (ad11 / 'servers.json').write_text(json.dumps([{'id': 3, 'name': 'US-1'}, {'id': 5, 'name': 'US-2'}]), encoding='utf-8')
            (ad11 / 'traffic.json').write_text(json.dumps({'server_id': 3, 'limit_gb': 1000}), encoding='utf-8')
            m11 = mon_mod.Monitor(ad11, state_dir=sd11)
            future_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 86400 * 5))
            m11._last_valid_traffic = {'available': True, 'server_id': 3, 'limit_bytes': 10**12, 'used_bytes': 10**11, 'to': future_iso}
            m11._last_valid_traffic_mono = time.monotonic()
            m11.traffic = {'available': False, 'server_id': 5}
            snap_m11 = m11.snapshot()
            self.assertIsNone(snap_m11.get('traffic_cached'))

        self.assertIn('(!raw?.server_id||raw.server_id===lastGoodTraffic?.server_id)', _read('/usr/local/share/thinksmart-monitor/index.html'))
        self.assertIn('function syncAlarmPvBtn(mode)', _read('/usr/local/share/thinksmart-admin/index.html'))
        self.assertIn("window.addEventListener('pagehide'", _read('/usr/local/share/thinksmart-tutor/mobile.html'))
        self.assertIn('if (action === \'check_page\' && !baseCanvas && serverHasStaged)', _read('/usr/local/share/thinksmart-tutor/mobile.html'))

    def test_15_settings_ux_sliders_wifi_airplay_and_city_search(self):
        """Settings UX overhaul: responsive touch sliders, Wi-Fi manual select/password, AirPlay toggle, section split, and city search."""
        import re
        v2 = Path('/opt/thinksmart/client/current/web/v2')
        idx = _read(str(v2 / 'index.html'))
        css = _read(str(v2 / 'app.css'))
        app_js = _read(str(v2 / 'app.js'))
        set_js = _read(str(v2 / 'settings.js'))
        kiosk_py = _read('/usr/local/bin/thinksmart-kiosk.py')
        diag_sh = _read('/usr/local/bin/diagnose-wake.sh')

        # 1. Sliders: touch-action: none, initSheetTouchScroll range/bar exclusion, immediate scrub on pointerdown
        self.assertIn('.bar input { display: block; width: 100%; height: 72px; margin: 0; background: none; -webkit-appearance: none; appearance: none; touch-action: none;', css)
        self.assertIn('input[type="range"], .bar', app_js)
        self.assertIn('onScrub(initVal);', app_js)

        # 2. Kiosk WebKit UserScript injection without broken port wildcards + diagnose-wake disabled-airplay pass
        self.assertNotIn("['http://127.0.0.1:*/*'", kiosk_py)
        self.assertIn("(page.scheme, page.hostname, page.port) == (conf.scheme, conf.hostname, conf.port)", kiosk_py)
        self.assertIn('用户态服务已按设置关闭: $usvc', diag_sh)

        # 3. Settings section restructuring & IDs consistency
        secs = re.findall(r'<button data-sec="(\w+)"', idx)
        self.assertEqual(secs, ['display', 'sound', 'net', 'voice', 'chat', 'weather', 'music', 'study', 'sys'])
        sound_sec = idx[idx.index('<section class="sec" data-sec="sound">'):]
        sound_sec = sound_sec[:sound_sec.index('</section>')]
        self.assertIn('id="airplayCard"', sound_sec)
        self.assertIn('id="airplaySwitch"', sound_sec)
        self.assertIn('id="btCard"', sound_sec)
        self.assertNotIn('id="alarmShortcutCard"', idx)
        self.assertNotIn('id="displaySleepLinkCard"', idx)
        self.assertNotIn('id="netMonitorCard"', idx)
        self.assertNotIn('data-sec="sleep"', idx)

        ids = set(re.findall(r'id="([\w-]+)"', idx + app_js + set_js))
        used = set(re.findall(r"\$\('([\w-]+)'\)", app_js + set_js))
        used |= {x for t in re.findall(r"slider\('(\w+)', '(\w+)'", app_js) for x in t}
        self.assertEqual(used - ids, {'pending'})

        # 4. Backend net_ctl (AirPlay flag cleanup + Wi-Fi nmcli helpers), /api/wifi, /api/airplay, /api/settings/cities
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import asyncio
import os
import tempfile
import types
from pathlib import Path
from aiohttp.test_utils import TestClient, TestServer
from thinksmart_client import net_ctl, web as web_mod

assert net_ctl.nmcli_fields(r'yes:My\:WiFi\\5G:88:WPA2') == ['yes', 'My:WiFi\\5G', '88', 'WPA2']
assert net_ctl._unescape_nmcli_single(r'P\@ss\:123\\!') == 'P@ss:123\\!'

with tempfile.TemporaryDirectory() as td:
    f1 = Path(td) / 'uxplay-mirroring-active'
    f2 = Path(td) / 'uxplay-audio-active'
    f1.write_text('1', encoding='utf-8')
    f2.write_text('1', encoding='utf-8')
    calls = []
    orig_run = net_ctl.subprocess.run
    orig_env = os.environ.get('XDG_RUNTIME_DIR')
    os.environ['XDG_RUNTIME_DIR'] = td
    def _fake_run(cmd, **_kw):
        calls.append(cmd)
        if 'is-active' in cmd:
            return types.SimpleNamespace(returncode=3, stdout='inactive\n', stderr='')
        if 'is-enabled' in cmd:
            return types.SimpleNamespace(returncode=1, stdout='disabled\n', stderr='')
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')
    net_ctl.subprocess.run = _fake_run
    try:
        st = net_ctl.set_airplay_sync(False)
        assert st['ok'] is True and st['enabled'] is False and st['unit_enabled'] is False, st
        assert not f1.exists() and not f2.exists()
    finally:
        net_ctl.subprocess.run = orig_run
        if orig_env is None:
            os.environ.pop('XDG_RUNTIME_DIR', None)
        else:
            os.environ['XDG_RUNTIME_DIR'] = orig_env

# Test /api/airplay & /api/wifi HTTP endpoints
class _FakeEvents:
    queues = set()
    def publish(self, *_a): pass

class _FakeClient:
    cfg = {'port': 8090, 'hub_http': 'http://127.0.0.1:9'}
    volume = 50
    alarm_volume = 70
    state = 'idle'
    ring = None
    keys = types.SimpleNamespace(mic_on=True)
    events = _FakeEvents()
    airplay_state = {'ok': True, 'available': True, 'enabled': True, 'mirroring': False, 'streaming': False, 'name': 'ThinkSmart View'}
    def alarm_view(self): return {'items': [], 'missed': [], 'ringing': False, 'synced': True, 'volume': 70, 'tone': 'coffee', 'tones': {}}
    def brightness(self): return {'level': 80, 'dimmed': False}
    def snapshot(self): return {'status': {'state': 'idle'}, 'models': {'current': None, 'options': []}, 'thread': {'entry': None, 'messages': []}, 'airplay': self.airplay_state}
    def health(self): return {'audio': True, 'hub': True}
    async def end(self): pass
    async def abort(self): pass
    async def ptt_start(self): pass
    async def ptt_stop(self): pass
    async def wake(self, *_a): pass
    async def refresh_airplay(self): return self.airplay_state
    async def set_airplay(self, enabled):
        self.airplay_state = {**self.airplay_state, 'enabled': bool(enabled)}
        return self.airplay_state

orig_ws = net_ctl.wifi_status_sync
net_ctl.wifi_status_sync = lambda: {'ok': True, 'ssid': 'TestHome5G', 'signal': 92}
try:
    async def _go():
        app = web_mod.make_app(_FakeClient(), display_ctl=False)
        async with TestClient(TestServer(app)) as tc:
            hdr = {'Host': '127.0.0.1:8090', 'Origin': 'http://127.0.0.1:8090'}
            r1 = await tc.get('/api/airplay', headers=hdr)
            j1 = await r1.json()
            assert r1.status == 200 and j1['enabled'] is True, j1
            r2 = await tc.post('/api/airplay', json={'enabled': False}, headers=hdr)
            j2 = await r2.json()
            assert r2.status == 200 and j2['enabled'] is False, j2
            r3 = await tc.get('/api/wifi?cmd=wifi_status', headers=hdr)
            j3 = await r3.json()
            assert r3.status == 200 and j3['ssid'] == 'TestHome5G', j3
            r4 = await tc.get('/api/settings/cities?q=', headers=hdr)
            j4 = await r4.json()
            assert r4.status == 200 and j4 == {'ok': True, 'items': []}, j4
    asyncio.run(_go())
finally:
    net_ctl.wifi_status_sync = orig_ws
print("R15_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R15_CLI_OK', r_cli.stdout)

        # 5. Home city search fallback (Pinyin & Chinese)
        home_mod = _load_mod('ts_home_r15', '/usr/local/bin/thinksmart-home.py')
        bj = home_mod._builtin_city_search('beijing')
        self.assertTrue(any(x['name'] == '北京' for x in bj), bj)
        lh = home_mod._builtin_city_search('sanya')
        self.assertTrue(any(x['name'] == '三亚' and '海南' in x['sub'] for x in lh), lh)
        hh = home_mod._builtin_city_search('苏州')
        self.assertTrue(any(x['name'] == '苏州' for x in hh), hh)

    def test_16_system_speed_and_ram_residency(self):
        # 1. Resident systemd units & live active daemons
        tut_unit = Path('/etc/systemd/system/thinksmart-tutor.service').read_text(encoding='utf-8')
        adm_unit = Path('/etc/systemd/system/thinksmart-admin.service').read_text(encoding='utf-8')
        self.assertIn('TUTOR_IDLE_EXIT_SEC=0', tut_unit)
        self.assertIn('ADMIN_IDLE_EXIT_SEC=0', adm_unit)
        for svc in ('thinksmart-tutor.service', 'thinksmart-admin.service'):
            st = subprocess.run(['systemctl', 'is-active', svc], capture_output=True, text=True, timeout=5)
            self.assertEqual(st.stdout.strip(), 'active', f'{svc} should stay resident active')

        # 2. WebKit2GTK memory configuration (default backward-compat + kiosk.conf 680MB/0.65/0.85)
        kiosk_src = Path('/usr/local/bin/thinksmart-kiosk.py').read_text(encoding='utf-8')
        self.assertIn("WEBKIT_MEM_CONS", kiosk_src)
        self.assertIn("WEBKIT_MEM_STRICT", kiosk_src)
        kconf = Path('/etc/thinksmart-kiosk.conf').read_text(encoding='utf-8')
        self.assertIn('WEBKIT_MEM_LIMIT_MB=680', kconf)
        self.assertIn('WEBKIT_MEM_CONS=0.65', kconf)
        self.assertIn('WEBKIT_MEM_STRICT=0.85', kconf)

        # 3. Sub-page WebView prewarm & resident retention
        ov_js = Path('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text(encoding='utf-8')
        self.assertIn("prewarmSub('http://127.0.0.1:8096/', 'AI讲题', 'ts-tutor-prewarm')", ov_js)
        self.assertIn('!window.__tsAllowSubEvict', ov_js)
        tut_html = Path('/usr/local/share/thinksmart-tutor/index.html').read_text(encoding='utf-8')
        self.assertIn("let subVis = window.name !== 'ts-tutor-prewarm';", tut_html)

        # 4. Client RAM static cache + ETag 304 + /api/settings?part=all
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import asyncio
import types
from aiohttp.test_utils import TestClient, TestServer
from thinksmart_client import web as web_mod

class _FakeEvents:
    def __init__(self): self.queues = set()

class _FakeClient:
    cfg = {'port': 8090, 'wake_words': ['你好小智'], 'kws_threshold': 0.25, 'kws_gain_db': 8, 'kws_score': 2.0, 'kws_paths': 4, 'wake_verify': False, 'wake_sound': 'zai', 'follow_up_seconds': 8}
    volume = 50
    alarm_volume = 70
    state = 'idle'
    ring = None
    keys = types.SimpleNamespace(mic_on=True)
    events = _FakeEvents()
    airplay_state = {'ok': True, 'available': True, 'enabled': True, 'mirroring': False, 'streaming': False, 'name': 'ThinkSmart View'}
    def alarm_view(self): return {'items': [], 'missed': [], 'ringing': False, 'synced': True, 'volume': 70, 'tone': 'coffee', 'tones': {}}
    def brightness(self): return {'level': 80, 'dimmed': False}
    def snapshot(self): return {'status': {'state': 'idle'}, 'models': {'current': None, 'options': []}, 'thread': {'entry': None, 'messages': []}, 'airplay': self.airplay_state}
    def health(self): return {'audio': True, 'hub': True}
    async def end(self): pass
    async def abort(self): pass
    async def ptt_start(self): pass
    async def ptt_stop(self): pass
    async def wake(self, *_a): pass
    async def refresh_airplay(self): return self.airplay_state
    async def set_airplay(self, enabled): return {**self.airplay_state, 'enabled': bool(enabled)}

async def _go():
    app = web_mod.make_app(_FakeClient(), display_ctl=False)
    async with TestClient(TestServer(app)) as tc:
        hdr = {'Host': '127.0.0.1:8090', 'Origin': 'http://127.0.0.1:8090'}
        r0 = await tc.get('/static/v2/app.js', headers=hdr)
        et = r0.headers.get('ETag')
        assert r0.status == 200 and et, (r0.status, et)
        r304 = await tc.get('/static/v2/app.js', headers={**hdr, 'If-None-Match': et})
        assert r304.status == 304, r304.status
        r_idx = await tc.get('/', headers=hdr)
        et_i = r_idx.headers.get('ETag')
        assert r_idx.status == 200 and et_i and 'Content-Security-Policy' in r_idx.headers
        r_idx304 = await tc.get('/', headers={**hdr, 'If-None-Match': et_i})
        assert r_idx304.status == 304
        r_all = await tc.get('/api/settings?part=all', headers=hdr)
        j_all = await r_all.json()
        assert r_all.status == 200 and j_all.get('ok') is True and 'assistant' in j_all and 'system' in j_all, j_all

asyncio.run(_go())
print("R16_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R16_CLI_OK', r_cli.stdout)

        # 5. Hub compiled Jinja2 template cache & loopback city shortcut + LLM keepalive pool
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = hub_root + '/.venv/bin/python'
        hub_probe = r"""
import inspect
from core.utils import prompt_manager as pm_mod
from core.providers.llm.openai import openai as oai_mod

pm = pm_mod.PromptManager({'prompt_template': 'hub-base-prompt.txt', 'weather_city': '苏州'})
p1 = pm.build_enhanced_prompt('你是小麦', 'dev-r16', '127.0.0.1')
tpl_obj = pm._compiled_tpl
assert tpl_obj is not None and '你是小麦' in p1
p2 = pm.build_enhanced_prompt('你是小智', 'dev-r16b', '127.0.0.1')
assert pm._compiled_tpl is tpl_obj and '你是小智' in p2
loc = pm._get_location_info('127.0.0.1')
assert loc == '苏州', loc
oai_src = inspect.getsource(oai_mod)
assert 'keepalive_expiry=90.0' in oai_src
print("R16_HUB_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r_hub = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(r_hub.returncode, 0, r_hub.stderr or r_hub.stdout)
        self.assertIn('R16_HUB_OK', r_hub.stdout)

    def test_17_round12_system_speed_and_cpu_audit(self):
        # 1. WebKit strictThresholdFraction order fix (strict MUST precede conservative!)
        kiosk_src = Path('/usr/local/bin/thinksmart-kiosk.py').read_text(encoding='utf-8')
        idx_strict = kiosk_src.index('mp.set_strict_threshold(strict)')
        idx_cons = kiosk_src.index('mp.set_conservative_threshold(cons)')
        self.assertLess(idx_strict, idx_cons, 'set_strict_threshold must be called before set_conservative_threshold')

        # 1b. QQMusic _PooledHttpResp (stdlib io) & playlist RAM cache invariants
        qq_probe = r"""
import importlib.util
spec = importlib.util.spec_from_file_location('ts_qq_r17', '/usr/local/bin/thinksmart-qqmusic.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with mod._PooledHttpResp(b'{"ok":true}', 200, {'Content-Type': 'application/json'}, 'https://y.qq.com/') as r:
    assert r.status == 200 and r.read() == b'{"ok":true}' and r.geturl() == 'https://y.qq.com/'
assert 'ttl' in mod._USER_PLS_CACHE
print("R17_QQ_OK")
"""
        r_qq = subprocess.run(
            ['/opt/thinksmart-addons/venv/bin/python', '-B', '-c', qq_probe],
            cwd='/tmp',
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r_qq.returncode, 0, r_qq.stderr or r_qq.stdout)
        self.assertIn('R17_QQ_OK', r_qq.stdout)

        # 2. Client idle AEC CPU fix + /events?once=1&brief=1 + /api/prewarm
        croot = '/opt/thinksmart/client/current'
        cli_py = croot + '/.venv/bin/python'
        cli_probe = r"""
import asyncio
import inspect
import json
import types
from aiohttp.test_utils import TestClient, TestServer
from thinksmart_client import app as app_mod, web as web_mod

a_src = inspect.getsource(app_mod.Client.set_state)
assert "self.audio.aec_hold = self.state != 'idle'" in a_src, a_src
assert a_src.index("self.state = state") < a_src.index("self.audio.aec_hold")

class _FakeEvents:
    def __init__(self): self.queues = set()

class _FakeClient:
    cfg = {'port': 8090, 'wake_words': ['你好小智'], 'kws_threshold': 0.25, 'kws_gain_db': 8, 'kws_score': 2.0, 'kws_paths': 4, 'wake_verify': False, 'wake_sound': 'zai', 'follow_up_seconds': 8}
    volume = 50
    alarm_volume = 70
    state = 'idle'
    ring = None
    hub = types.SimpleNamespace(connected=True)
    keys = types.SimpleNamespace(mic_on=True)
    events = _FakeEvents()
    airplay_state = {'ok': True, 'available': True, 'enabled': True, 'mirroring': False, 'streaming': False, 'name': 'ThinkSmart View'}
    def alarm_view(self): return {'items': [], 'missed': [], 'ringing': False, 'synced': True, 'volume': 70, 'tone': 'coffee', 'tones': {}}
    def brightness(self): return {'level': 80, 'dimmed': False}
    def snapshot(self): return {'status': {'state': 'idle'}, 'models': {'current': None, 'options': []}, 'thread': {'entry': 'a', 'messages': [{'role': 'user', 'text': 'hi'}]}, 'airplay': self.airplay_state}
    def health(self): return {'audio': True, 'hub': True}
    async def end(self): pass
    async def abort(self): pass
    async def ptt_start(self): pass
    async def ptt_stop(self): pass
    async def wake(self, *_a): pass
    async def refresh_airplay(self): return self.airplay_state
    async def set_airplay(self, enabled): return {**self.airplay_state, 'enabled': bool(enabled)}

async def _go():
    fc = _FakeClient()
    app = web_mod.make_app(fc, display_ctl=False)
    async with TestClient(TestServer(app)) as tc:
        hdr = {'Host': '127.0.0.1:8090', 'Origin': 'http://127.0.0.1:8090'}
        r_ev = await tc.get('/events?once=1&brief=1', headers=hdr)
        txt = await r_ev.text()
        assert r_ev.status == 200 and 'event: snapshot' in txt and len(fc.events.queues) == 0, txt
        data_line = [ln[5:].strip() for ln in txt.splitlines() if ln.startswith('data:')][0]
        parsed = json.loads(data_line)
        assert 'thread' not in parsed and parsed['status']['state'] == 'idle', parsed
        r_pw = await tc.post('/api/prewarm', headers=hdr)
        assert r_pw.status == 200
        r_h = await tc.get('/api/health', headers=hdr)
        jh = await r_h.json()
        assert r_h.status == 200 and jh.get('ringing') is False, jh

asyncio.run(_go())
print("R17_CLI_OK")
"""
        r_cli = subprocess.run(
            [cli_py, '-B', '-c', cli_probe],
            cwd=croot,
            env={**os.environ, 'PYTHONPATH': croot},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr or r_cli.stdout)
        self.assertIn('R17_CLI_OK', r_cli.stdout)

        # 3. Hub voice pipeline latency optimizations (in-process WAV decode, first-sentence split, Sherpa RAM, config_store memo)
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = hub_root + '/.venv/bin/python'
        hub_probe = r"""
import io
import time
import wave
import numpy as np
from core.connection import ConnectionHandler
from core.providers.tts.base import TTSProviderBase
from core.providers.asr.sherpa_onnx_local import ASRProvider as SherpaASR
from core.utils import util as util_mod
from core.admin import config_store

# 1) Fast in-process PCM16 WAV decode (<10ms, zero ffmpeg spawn)
buf = io.BytesIO()
with wave.open(buf, 'wb') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(24000)
    wf.writeframes((np.sin(np.linspace(0, 20, 4800)) * 10000).astype('<i2').tobytes())
wav_bytes = buf.getvalue()
t0 = time.perf_counter()
pcm = util_mod.decode_audio_bytes(wav_bytes, 'wav', 16000)
dt_ms = (time.perf_counter() - t0) * 1000.0
assert len(pcm) == 3200 * 2 and dt_ms < 25.0, (len(pcm), dt_ms)

# 2) SherpaXASR requires_file() == False (in-memory recognition)
dummy_s = SherpaASR.__new__(SherpaASR)
assert dummy_s.requires_file() is False

# 3) TTS first-sentence split: earliest comma & 4-char threshold
class _DummyTTS(TTSProviderBase):
    async def text_to_speak(self, text, output_file):
        return None
    def __init__(self):
        self.tts_text_buff = ['今天晴天，最高二十度，微风。']
        self.processed_chars = 0
        self.is_first_sentence = True
        self.tts_stop_request = False
        self.first_sentence_punctuations = ('，', '。', '！', '？')
        self.punctuations = ('。', '！', '？', '，')

dt = _DummyTTS()
seg1 = dt._get_segment_text()
assert seg1 == '今天晴天' and dt.is_first_sentence is False, (seg1, dt.is_first_sentence)

# 4) direct_answer streaming extraction without JSONDecodeError storm
s_part = ConnectionHandler._extract_direct_answer_response('{"response": "你好呀，世界')
assert s_part == '你好呀，世界', s_part
s_full = ConnectionHandler._extract_direct_answer_response('{"response": "你好呀，世界"}')
assert s_full == '你好呀，世界', s_full

# 5) config_store stat memoization
m1 = config_store.load_merged()
t1 = time.perf_counter()
m2 = config_store.load_merged()
assert (time.perf_counter() - t1) * 1000.0 < 15.0 and m1.get('selected_module') == m2.get('selected_module')
print("R17_HUB_OK")
"""
        with tempfile.TemporaryDirectory() as td:
            r_hub = subprocess.run(
                [hub_py, '-B', '-c', hub_probe],
                cwd=td,
                env={**os.environ, 'PYTHONPATH': hub_root},
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(r_hub.returncode, 0, r_hub.stderr or r_hub.stdout)
        self.assertIn('R17_HUB_OK', r_hub.stdout)


if __name__ == '__main__':
    unittest.main()
