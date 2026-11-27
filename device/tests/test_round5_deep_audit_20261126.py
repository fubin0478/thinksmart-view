#!/opt/thinksmart-addons/venv/bin/python -B
"""Round 4 deep system audit regression suite (2026-11-26)."""
import asyncio
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
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


class Round4DeepAuditTests(unittest.TestCase):
    def test_01_workstream_a_security_network_and_sandbox(self):
        # 1. Intercom tunnel hardening permits /run flag files under ProtectSystem=strict
        for unit_conf in (
            '/etc/systemd/system/thinksmart-intercom-tunnel.service.d/hardening.conf',
            '/etc/systemd/system/thinksmart-intercom-tunnel-jp.service.d/hardening.conf',
        ):
            txt = _read(unit_conf)
            self.assertIn('ProtectSystem=strict', txt)
            self.assertIn('ReadWritePaths=-/run', txt)

        # 2. Xiaozhi hub config binds loopback cleanly and omits fake MAC
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        cfg_txt = _read(f'{hub_root}/config.yaml')
        self.assertRegex(cfg_txt, r'(?m)^\s*ip:\s*127\.0\.0\.1\b')
        self.assertNotIn('11:22:33:44:55:66', cfg_txt)

        # 3. Hub HTTP, WS, and OTA handlers enforce peer-IP boundaries
        http_srv = _read(f'{hub_root}/core/http_server.py')
        self.assertIn('def _is_loopback_ip', http_srv)
        self.assertIn('def _is_local_or_lan_ip', http_srv)
        self.assertIn('"127.0.0.1"', http_srv)

        ws_srv = _read(f'{hub_root}/core/websocket_server.py')
        self.assertIn('if remote_addr is not None:', ws_srv)
        self.assertIn('"127.0.0.1"', ws_srv)

        # 4. AirPlay tuning validates PID owner UID 1000 and uxplay/stdbuf cmdline
        tune_src = _read('/usr/local/sbin/thinksmart-airplay-tune')
        self.assertIn('[ "$uid" = "1000" ]', tune_src)
        self.assertIn('*uxplay*|*stdbuf*', tune_src)

        # 5. Admin security fixes (behavioral + source)
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
                adm = _load_mod('ts_admin_r4', '/usr/local/bin/thinksmart-admin.py')

                # clash_body_ok blocks case-variant bypasses
                self.assertTrue(adm.clash_body_ok('PUT', 'configs', b''))
                self.assertTrue(adm.clash_body_ok('PUT', 'configs', b'{"path": "", "payload": ""}'))
                self.assertFalse(adm.clash_body_ok('PUT', 'configs', b'{"Path": ""}'))
                self.assertFalse(adm.clash_body_ok('PUT', 'configs', b'{"path": "/etc/passwd"}'))

                # _same_origin_url protects settings/test secret fallback from SSRF exfiltration
                adm_src = _read('/usr/local/bin/thinksmart-admin.py')
                self.assertIn(".split(',')[-1]", adm_src)
                self.assertIn('def _same_origin_url', adm_src)
                self.assertGreaterEqual(adm_src.count('_same_origin_url('), 8)
                # guard() checks remote loopback before X-TS-* headers
                idx_remote = adm_src.index('if request.remote and not _is_loopback(request.remote):')
                idx_xts = adm_src.index("peer = request.headers.get('X-TS-Peer')")
                self.assertLess(idx_remote, idx_xts)
                # Admin.session masks fields when logged out
                sess_block = re.search(r'async def session\(self[\s\S]*?\n    async def ', adm_src).group(0)
                self.assertIn("username=self.auth.username() if logged_in else ''", sess_block)
                self.assertIn("vps_url=os.environ.get('ADMIN_VPS_URL', '').strip() if logged_in else ''", sess_block)
            finally:
                os.environ.clear()
                os.environ.update(env_bk)

    def test_02_workstream_b_frontends_and_display_daemons(self):
        # 1. idle-blanker: startup hint_seq seed, overlay SIGCONT recovery, back FD priority
        ib_src = _read('/usr/local/bin/idle-blanker.py')
        self.assertIn('init_hint = read_json(HINT_PATH)', ib_src)
        self.assertIn('prev_overlay_active', ib_src)
        self.assertIn('set_blank_render_paused(False)', ib_src)
        self.assertIn('if back is not None:\n                poll_wait = 0.2', ib_src)

        # 2. mobile.html synced to tutor web root
        mob = _read('/usr/local/share/thinksmart-tutor/mobile.html')
        self.assertIn('new Set([...j.enrolled_ids', mob)
        self.assertIn('new Set([...freshIds', mob)
        self.assertIn('pendingCheckPageId', mob)
        self.assertIn('if (cwRemoteBusy) { toast(', mob)

        # 3. monitor/index.html AbortController latency check + WebKit text-node guard
        idx = _read('/usr/local/share/thinksmart-monitor/index.html')
        self.assertIn('new AbortController()', idx)
        self.assertIn('nodeType===3', idx)

    def test_03_workstream_c_voice_client_and_qqmusic(self):
        croot = '/opt/thinksmart/client/current'
        cli_py = f'{croot}/.venv/bin/python'

        probe = """
import asyncio, json, tempfile
from pathlib import Path
from thinksmart_client import config as c_cfg, mcp as c_mcp, settings_api as c_sapi

# 1. config.load refuses non-dict JSON safely
orig_path = c_cfg.PATH
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / 'client.json'
    p.write_text('["not", "dict"]', encoding='utf-8')
    c_cfg.PATH = p
    try:
        loaded = c_cfg.load()
        assert isinstance(loaded, dict) and 'hub_ws' in loaded
    finally:
        c_cfg.PATH = orig_path

# 2. _wpctl handles OSError cleanly
async def _test_wpctl():
    out = await c_mcp._wpctl('__nonexistent_flag__')
    assert isinstance(out, str)
asyncio.run(_test_wpctl())

# 3. settings_api rejects non-string/non-dict writer calls cleanly
import inspect
assert 'if not isinstance(part, str) or part not in writers' in inspect.getsource(c_sapi)
print('CLIENT_BEHAVIOR_OK')
"""
        r = subprocess.run([cli_py, '-B', '-c', probe], cwd=croot, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('CLIENT_BEHAVIOR_OK', r.stdout)

        app_src = _read(f'{croot}/thinksmart_client/app.py')
        self.assertIn('kws = self.kws', app_src)
        self.assertIn('if r.status != 200:', app_src)
        self.assertIn("log.exception('ticker error')", app_src)
        self.assertIn("log.exception('alarm release_speaker failed')", app_src)

        hub_src = _read(f'{croot}/thinksmart_client/hub.py')
        self.assertIn('deadline = asyncio.get_running_loop().time() + 10.0', hub_src)
        self.assertIn('except (ConnectionClosed, OSError):', hub_src)

        bt_src = _read(f'{croot}/thinksmart_client/bt.py')
        self.assertIn('with contextlib.suppress(ProcessLookupError):', bt_src)

        qq_src = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('read_bufsize=2 * 1024 * 1024', qq_src)
        self.assertIn('if not _is_loopback_remote(req.remote):', qq_src)

    def test_04_workstream_d_xiaozhi_hub_runtime(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = f'{hub_root}/.venv/bin/python'

        probe = """
import asyncio, inspect, os, sys, threading, time
sys.path.insert(0, os.environ['R4_HUB_ROOT'])
from plugins_func.functions import get_time
from core.providers.tools.server_plugins.plugin_executor import ServerPluginExecutor
from plugins_func.register import all_function_registry, FunctionItem, Action, ActionResponse, ToolType

# 1. get_lunar remains sync (test_round3 invariant) while WAIT plugin runs off event loop via to_thread
assert not inspect.iscoroutinefunction(get_time.get_lunar)

def _slow_sync(x=1):
    time.sleep(0.05)
    return ActionResponse(action=Action.RESPONSE, response=f'ok:{threading.get_ident()}:{x}')

class _DummyConn:
    config = {}

all_function_registry['__r4_test_wait__'] = FunctionItem('__r4_test_wait__', {}, _slow_sync, ToolType.WAIT)
try:
    async def _run():
        main_tid = threading.get_ident()
        ex = ServerPluginExecutor(_DummyConn())
        res = await ex.execute(_DummyConn(), '__r4_test_wait__', {'x': 42, 'ignored': 1})
        assert res.response.startswith('ok:') and res.response.endswith(':42')
        worker_tid = int(res.response.split(':')[1])
        assert worker_tid != main_tid, 'WAIT plugin ran on event loop thread!'
    asyncio.run(_run())
finally:
    all_function_registry.pop('__r4_test_wait__', None)

print('HUB_BEHAVIOR_OK')
"""
        r = subprocess.run([hub_py, '-B', '-c', probe], cwd=hub_root, env={**os.environ, 'R4_HUB_ROOT': hub_root}, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('HUB_BEHAVIOR_OK', r.stdout)

        asr_base = _read(f'{hub_root}/core/providers/asr/base.py')
        self.assertIn('content_for_length_check = str(raw_text.get("content") or "")', asr_base)
        self.assertIn('content_for_length_check = raw_text or ""', asr_base)

        conn_src = _read(f'{hub_root}/core/connection.py')
        self.assertIn('future.cancel()', conn_src)
        self.assertIn('fn = getattr(tool_call, "function", None)', conn_src)
        self.assertIn('关闭TTS时出错:', conn_src)

        edge_src = _read(f'{hub_root}/core/providers/tts/edge.py')
        self.assertIn('asyncio.wait_for(_collect(), timeout=30.0)', edge_src)

        hello_src = _read(f'{hub_root}/core/handle/helloHandle.py')
        self.assertIn('async with _wakeup_response_lock:', hello_src)
        self.assertIn('_wakeup_response_lock.locked()', hello_src)

        ds_src = _read(f'{hub_root}/core/providers/asr/doubao_stream.py')
        self.assertIn('open_timeout=10', ds_src)

    def test_05_second_turn_follow_up_not_killed_by_qqmusic(self):
        # 1. QQMusic control('open_ui') and control('shuffle') must not set just_started
        qq_mod = _load_mod('ts_qqmusic_r5_t05', '/usr/local/bin/thinksmart-qqmusic.py')
        eng = qq_mod.QQMusicEngine()
        eng.ducked = True
        eng.playing = True
        eng.paused = False
        eng.just_started = False
        r_ui = eng.control('open_ui')
        self.assertTrue(r_ui.get('ok'))
        self.assertFalse(eng.just_started)
        eng.set_play_mode = lambda *_a, **_kw: None
        r_mode = eng.control('shuffle')
        self.assertTrue(r_mode.get('ok'))
        self.assertFalse(eng.just_started)

        # 2. QQMusicEngine.set_listen_duck and SSE watcher soft-ducking contract
        eng.set_listen_duck(True)
        self.assertTrue(eng.listen_duck)
        eng.set_listen_duck(False)
        self.assertFalse(eng.listen_duck)
        qq_src = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('/api/listening/finish', qq_src)
        self.assertIn("voice_busy = st in ('connecting', 'thinking', 'speaking')", qq_src)
        self.assertIn("listen_duck = (st == 'listening')", qq_src)
        self.assertNotIn('127.0.0.1:8090/api/end', qq_src)

        # 3. Client.finish_follow_up behavioral gating
        croot = '/opt/thinksmart/client/current'
        cli_py = f'{croot}/.venv/bin/python'
        probe_cli = """
import asyncio, time
from thinksmart_client.app import Client

class _DummyHub:
    def __init__(self):
        self.closed = False
    async def close(self):
        self.closed = True

class _DummyClient:
    finish_follow_up = Client.finish_follow_up
    def __init__(self):
        now = time.monotonic()
        self.hub = _DummyHub()
        self.state = 'listening'
        self.uploading = True
        self.heard = True
        self.recognising = False
        self.vad_ended = False
        self.hub_pending = False
        self.listen_since = now - 6.0
        self.speech_end = now - 10.0
        self.deadline = now + 4.0
        self.ended_missed = None
    async def stop_uploading(self, missed=False):
        self.uploading = False
        self.deadline = None
        self.ended_missed = missed
        self.state = 'idle'

async def _run():
    # Refuses when user spoke recently (< 1.8s ago after the initial 0.4s echo window)
    c = _DummyClient()
    c.speech_end = time.monotonic() - 0.5
    c.listen_since = c.speech_end - 3.0
    assert (await c.finish_follow_up()) is False and c.uploading is True

    # Allows finish when only a brief TTS echo tail occurred right at start (<= 0.4s after listen_since)
    c = _DummyClient()
    c.speech_end = c.listen_since + 0.1
    assert (await c.finish_follow_up()) is True and c.uploading is False

    # Allows finish when older noise (> 1.8s silence since speech_end) happened earlier
    c = _DummyClient()
    now = time.monotonic()
    c.listen_since = now - 6.0
    c.speech_end = c.listen_since + 1.0
    assert (await c.finish_follow_up()) is True and c.uploading is False

    # Refuses for active/in-flight flags or non-follow-up wake listen
    for attr, val in (('recognising', True), ('vad_ended', True), ('hub_pending', True), ('heard', False)):
        c = _DummyClient()
        setattr(c, attr, val)
        assert (await c.finish_follow_up()) is False, f'expected False for {attr}={val}'
        assert c.uploading is True

    # Finishes cleanly when idle in follow-up without closing hub
    c = _DummyClient()
    assert (await c.finish_follow_up()) is True
    assert c.uploading is False and c.state == 'idle' and c.ended_missed is False
    assert c.hub.closed is False

asyncio.run(_run())
print('CLIENT_FOLLOWUP_OK')
"""
        r = subprocess.run([cli_py, '-B', '-c', probe_cli], cwd=croot, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('CLIENT_FOLLOWUP_OK', r.stdout)

        # 4. ConnectionHandler.reset_audio_states zeroes Silero VAD recurrent tensors
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = f'{hub_root}/.venv/bin/python'
        probe_hub = """
import numpy as np, os, sys
sys.path.insert(0, os.environ['R4_HUB_ROOT'])
from core.connection import ConnectionHandler

class _DummyConn:
    reset_audio_states = ConnectionHandler.reset_audio_states
    def __init__(self):
        self.client_audio_buffer = bytearray(b'1234')
        self.client_have_voice = True
        self.client_voice_stop = True
        self.client_voice_window = [True]
        self.last_is_voice = True
        self.vad_last_voice_time = 123.0
        self.asr_audio = [b'x']
        self.asr_voice_end = 1
        self.asr_segment_start = 1
        self.asr_segments = ['seg']
        self._vad_state = np.ones((2, 1, 128), dtype=np.float32)
        self._vad_context = np.ones((1, 64), dtype=np.float32)
        class _Logger:
            def bind(self, **_kw):
                return self
            def debug(self, *_a):
                pass
        self.logger = _Logger()

c = _DummyConn()
c.reset_audio_states()
assert np.all(c._vad_state == 0.0)
assert np.all(c._vad_context == 0.0)
print('HUB_VAD_RESET_OK')
"""
        r_hub = subprocess.run(
            [hub_py, '-B', '-c', probe_hub],
            cwd=hub_root,
            env={**os.environ, 'R4_HUB_ROOT': hub_root},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(r_hub.returncode, 0, r_hub.stderr or r_hub.stdout)
        self.assertIn('HUB_VAD_RESET_OK', r_hub.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
