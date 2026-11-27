#!/opt/thinksmart-addons/venv/bin/python -B
"""Round 3 deep system audit regression suite (Round 2 user request, 2026-11-26)."""
import asyncio
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections import deque
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


class Round3DeepAuditTests(unittest.TestCase):
    def test_01_frontend_slider_shell_wifi_settings_overlay_home_fixes(self):
        app_js = _read('/opt/thinksmart/client/current/web/v2/app.js')
        # bindSmartSlider attaches window listeners inside pointerdown and detaches on finish/abort
        m_bs = re.search(r'function bindSmartSlider\([\s\S]*?\n\}', app_js)
        self.assertIsNotNone(m_bs)
        bs = m_bs.group(0)
        self.assertIn('function detach()', bs)
        self.assertIn("removeEventListener('pointermove', onMove, true)", bs)
        self.assertGreater(bs.index("el.addEventListener('pointerdown'"), bs.index("removeEventListener('pointermove'"))
        # shellCall clears timeout in thinksmartReply; wifiPaneTimer clears/guards stale pane jumps
        self.assertIn('clearTimeout(entry.timer)', app_js)
        self.assertIn('let wifiPaneTimer = 0;', app_js)
        self.assertIn('clearWifiPaneTimer()', app_js)
        self.assertIn('wifi.target === curTarget', app_js)

        settings_js = _read('/opt/thinksmart/client/current/web/v2/settings.js')
        self.assertIn('const inflightVals = {};', settings_js)
        self.assertIn('inflightVals[part] = p.values;', settings_js)
        self.assertIn('if (r && !r.__drag)', settings_js)
        self.assertNotIn('if (sleepVolEdit === null && v !== null) r.value = v;', settings_js)

        ov_js = _read('/usr/local/share/thinksmart-monitor/home_overlay.js')
        m_vc = re.search(r'function vcShow\(on\) \{[\s\S]*?\n  \}', ov_js)
        self.assertIsNotNone(m_vc)
        self.assertIn('syncNotice();', m_vc.group(0))
        self.assertIn('vcSinceAt = Date.now() / 1000;', ov_js)
        self.assertIn('byId.get(String(el.dataset.id', ov_js)

        home_html = _read('/usr/local/share/thinksmart-home/index.html')
        m_om = re.search(r'async function openMessages\(initTab\) \{([\s\S]*?)openModal\(', home_html)
        self.assertIsNotNone(m_om)
        self.assertIn('closeWeatherPage()', m_om.group(1))
        self.assertIn('closeTimerPage()', m_om.group(1))
        self.assertIn('closeCountdownPage()', m_om.group(1))
        self.assertIn('!wpCities.includes(wpCurCity)', home_html)
        self.assertIn("正在读取 ' + wpCurCity + ' 实时气象", home_html)

        kiosk_py = _read('/usr/local/bin/thinksmart-kiosk.py')
        m_ws = re.search(r'def wifi_status\(_msg, _progress\):([\s\S]*?)\n\n\ndef ', kiosk_py)
        self.assertIsNotNone(m_ws)
        self.assertIn('ssid = _active_wifi_ssid()', m_ws.group(1))

    def test_02_hub_core_behavioral_in_hub_venv(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = f'{hub_root}/.venv/bin/python'
        probe = r"""
import asyncio, inspect, tempfile, threading
from pathlib import Path
from core.utils.dialogue import Dialogue, Message
from core.agent.chat_history import ChatDB
from core.utils.audioRateController import AudioRateController
from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from core.providers.tools.device_mcp.mcp_handler import MCPClient, _safe_msg_id
from core.providers.tools.device_mcp.mcp_executor import DeviceMCPExecutor
from core.providers.tools.server_plugins.plugin_executor import ServerPluginExecutor
from plugins_func.register import Action, ActionResponse
from plugins_func.functions import get_time

# 1. Dialogue: inter-turn dummy tool flush + backslash memory substitution
d = Dialogue()
d.put(Message(role='system', content='sys\n<memory>\nold\n</memory>'))
d.put(Message(role='assistant', tool_calls=[{'id': 'call_1', 'type': 'function', 'function': {'name': 't', 'arguments': '{}'}}]))
d.put(Message(role='user', content='next turn'))
fixed = d._ensure_tool_calls_complete(d.dialogue)
roles = [m.role for m in fixed]
assert roles == ['system', 'assistant', 'tool', 'user'], roles
rendered = d.get_llm_dialogue_with_memory(memory_str=r'C:\Users\1\test\n')
assert any(r'C:\Users\1\test\n' in m['content'] for m in rendered if m['role'] == 'system')

# 2. ChatDB concurrency + atomic clear
with tempfile.TemporaryDirectory() as td:
    db = ChatDB(Path(td) / 'chat.db')
    errs = []
    def worker(idx):
        try:
            for j in range(15):
                db.insert('dev1', 'm1', 'user', f'{idx}-{j}')
                db.recent('dev1', 'm1', 5)
                db.memory('dev1')
        except Exception as e:
            errs.append(repr(e))
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errs, errs
    db.set_memory('dev1', 'memo', 'summary')
    assert db.clear('dev1') >= 60
    assert db.memory('dev1')['summary'] == '' and db.memory('dev1')['source'] == 'cleared'
    db.close()

# 3. AudioRateController finally sets queue_empty_event when loop crashes/cancels
async def _chk_arc():
    arc = AudioRateController(frame_duration=60)
    arc.queue_empty_event.clear()
    async def boom(): raise RuntimeError('ws gone')
    t = arc.start_sending(boom)
    arc.queue_has_data_event.set()
    await asyncio.wait_for(arc.queue_empty_event.wait(), timeout=2.0)
asyncio.run(_chk_arc())

# 4. UnifiedToolHandler deduplicates play_music when sanitized self_music_play exists
uth = UnifiedToolHandler.__new__(UnifiedToolHandler)
class _TM:
    def has_tool(self, n): return n == 'self_music_play'
    def get_function_descriptions(self):
        return [{'type':'function','function':{'name':'play_music'}}, {'type':'function','function':{'name':'self_music_play'}}]
    def get_supported_tool_names(self): return ['play_music', 'self_music_play']
uth.tool_manager = _TM()
import logging; uth.logger = logging.getLogger('t')
assert [f['function']['name'] for f in uth.get_functions()] == ['self_music_play']
assert uth.current_support_functions() == ['self_music_play']

# 5. MCPClient next_id >= 10 and _safe_msg_id handles None/bad strings
assert MCPClient().next_id >= 10
assert _safe_msg_id(None) == 0 and _safe_msg_id('bad') == 0 and _safe_msg_id('12') == 12

# 6. ServerPluginExecutor filters extra LLM kwargs and hides raw TypeError
class _BadPlug:
    def __init__(self, f):
        self.func = f
        self.type = type('T', (), {'code': 2})()
spe = ServerPluginExecutor.__new__(ServerPluginExecutor)
from core.providers.tools.server_plugins import plugin_executor as pe_mod
def _ok_fn(city): return ActionResponse(Action.REQLLM, result=city)
def _boom_fn(city): raise RuntimeError('secret internal stack')
pe_mod.all_function_registry['t_ok'] = _BadPlug(_ok_fn)
pe_mod.all_function_registry['t_boom'] = _BadPlug(_boom_fn)
try:
    r_ok = asyncio.run(spe.execute(None, 't_ok', {'city': '北京', 'extra_llm_arg': 123}))
    assert r_ok.result == '北京', r_ok
    r_err = asyncio.run(spe.execute(None, 't_boom', {'city': '北京'}))
    assert r_err.action == Action.ERROR and 'secret internal stack' not in r_err.response
finally:
    pe_mod.all_function_registry.pop('t_ok', None)
    pe_mod.all_function_registry.pop('t_boom', None)

# 7. get_lunar accepts non-str date cleanly and keeps synchronous signature
assert not inspect.iscoroutinefunction(get_time.get_lunar)
bad_lunar = get_time.get_lunar(date=12345, query='今天')
assert 'YYYY-MM-DD' in bad_lunar.result

print('HUB_R3_ALL_OK')
"""
        r = subprocess.run([hub_py, '-B', '-c', probe], cwd=hub_root, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('HUB_R3_ALL_OK', r.stdout)

        conn_src = _read(f'{hub_root}/core/connection.py')
        self.assertIn('self.timeout_task is not asyncio.current_task()', conn_src)
        self.assertIn('tool_calls_list = [tc for tc in tool_calls_list if isinstance(tc, dict) and tc.get("name")]', conn_src)
        self.assertIn('else "(无返回内容)"', conn_src)
        self.assertIn('return "" if data["response"] is None else str(data["response"])', conn_src)

        tts_src = _read(f'{hub_root}/core/providers/tts/base.py')
        self.assertIn('self.processed_chars = len(full_text)', tts_src)
        asr_src = _read(f'{hub_root}/core/providers/asr/base.py')
        self.assertIn('content_for_length_check = raw_text or ""', asr_src)
        me_src = _read(f'{hub_root}/core/providers/tools/device_mcp/mcp_executor.py')
        self.assertIn('resultJson["action"] in Action.__members__', me_src)

    def test_03_client_aec_bt_alarms_and_mcp_validation(self):
        croot = '/opt/thinksmart/client/current'
        if croot not in sys.path:
            sys.path.insert(0, croot)
        aec_src = _read(f'{croot}/thinksmart_client/aec.py')
        self.assertIn('deque(maxlen=_FAR_PENDING_MAX)', aec_src)
        self.assertIn('except IndexError:', aec_src)
        bt_src = _read(f'{croot}/thinksmart_client/bt.py')
        self.assertIn('await p.wait()', bt_src)
        cli_py = f'{croot}/.venv/bin/python'
        probe = (
            "import tempfile\n"
            "from pathlib import Path\n"
            "from thinksmart_client import alarms\n"
            "with tempfile.TemporaryDirectory() as td:\n"
            "    st = alarms.Alarms(Path(td), clock=lambda: 1000.0)\n"
            "    for bad in (True, False, 0, -5, 61, 1.5):\n"
            "        try:\n"
            "            st.snooze(bad)\n"
            "            raise AssertionError(f'expected ValueError for {bad!r}')\n"
            "        except ValueError:\n"
            "            pass\n"
            "print('CLIENT_ALARMS_SNOOZE_OK')\n"
        )
        r = subprocess.run([cli_py, '-B', '-c', probe], cwd=croot, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertIn('CLIENT_ALARMS_SNOOZE_OK', r.stdout)
        mcp_src = _read(f'{croot}/thinksmart_client/mcp.py')
        self.assertIn("'error': str(e)", mcp_src)
        self.assertIn('if isinstance(raw_id, bool):', mcp_src)

    def test_04_admin_sqlite_wal_restore_sanitize_claude_and_study_probe(self):
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
                adm = _load_mod('ts_admin_r3', '/usr/local/bin/thinksmart-admin.py')

                # 1. monitor/config.json is json_sanitized in sanitized mode (strips nezha_host)
                (root / 'monitor').mkdir(parents=True)
                (root / 'monitor' / 'config.json').write_text(
                    json.dumps({'limit_gb': 200, 'nezha_host': 'https://priv.nezha.internal/ws'}),
                    encoding='utf-8',
                )
                blob, _ = adm.build_backup_archive('sanitized')
                with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tf:
                    mon = json.loads(tf.extractfile('monitor/config.json').read().decode('utf-8'))
                    self.assertNotIn('nezha_host', mon)
                    self.assertEqual(mon['limit_gb'], 200)

                # 2. Live SQLite WAL inode preservation + backup/restore fidelity
                (root / 'hub' / 'data').mkdir(parents=True)
                db_p = root / 'hub' / 'data' / 'chat.db'
                live_c = sqlite3.connect(str(db_p))
                live_c.execute('PRAGMA journal_mode=WAL')
                live_c.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
                live_c.execute("INSERT INTO t (v) VALUES ('before_backup')")
                live_c.commit()
                old_ino = db_p.stat().st_ino

                full_blob, _ = adm.build_backup_archive('full')
                live_c.execute("INSERT INTO t (v) VALUES ('new_in_wal_only')")
                live_c.commit()  # stays in WAL
                res = adm.restore_backup_archive(full_blob, preserve_secrets=True)
                self.assertEqual(db_p.stat().st_ino, old_ino)
                rows_after = [r[0] for r in live_c.execute('SELECT v FROM t').fetchall()]
                self.assertEqual(rows_after, ['before_backup'])
                # Safety backup captured the WAL row too
                bk_db = Path(res['backup_dir']) / 'hub' / 'chat.db'
                bk_c = sqlite3.connect(str(bk_db))
                bk_rows = [r[0] for r in bk_c.execute('SELECT v FROM t').fetchall()]
                bk_c.close()
                live_c.close()
                self.assertIn('new_in_wal_only', bk_rows)
            finally:
                os.environ.clear()
                os.environ.update(env_bk)

        adm_src = _read('/usr/local/bin/thinksmart-admin.py')
        self.assertIn("foc.get('sessions'", adm_src)
        self.assertIn("lo.get('at')", adm_src)

    def test_05_tutor_iframe_and_intercom_wan_http_study(self):
        from aiohttp.test_utils import TestClient, TestServer
        tutor = _load_mod('ts_tutor_r3', '/usr/local/bin/thinksmart-tutor.py')

        async def _chk():
            app = tutor.make_app()
            async with TestClient(TestServer(app)) as cli:
                r_if = await cli.get('/', headers={
                    'Host': f'127.0.0.1:{tutor.PORT}',
                    'Sec-Fetch-Site': 'cross-site',
                    'Sec-Fetch-Dest': 'iframe',
                })
                r_bad = await cli.get('/api/tutor/badge', headers={
                    'Host': f'127.0.0.1:{tutor.PORT}',
                    'Sec-Fetch-Site': 'cross-site',
                })
                return r_if.status, r_bad.status

        st_if, st_bad = asyncio.run(_chk())
        self.assertEqual(st_if, 200)
        self.assertEqual(st_bad, 403)

        ic_src = _read('/usr/local/bin/thinksmart-intercom.py')
        self.assertIn('if not allow_wan_dir and po.scheme == "http" and not _local_hn(po_hn):', ic_src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
