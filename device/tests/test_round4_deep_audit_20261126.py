#!/opt/thinksmart-addons/venv/bin/python
"""Round 3 deep system audit regression tests (2026-11-26):
Covers all 29 verified bugs across systemd/daemons/installer, study/tutor/intercom/monitor,
home/qqmusic, voice client/hub, and unified admin web UI.
"""
import asyncio
import datetime as dt
import importlib.util
import io
import json
import pathlib
import re
import subprocess
import sys
import types
import unittest
import wave


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding='utf-8')


class Round4DeepAuditTests(unittest.TestCase):
    def test_01_systemd_daemons_and_baremetal_contracts(self):
        hc = _read('/etc/systemd/system/thinksmart-intercom.service.d/hardening.conf')
        self.assertIn('ProtectSystem=strict', hc)
        self.assertIn('ReadWritePaths=-/run/user/1000', hc)

        zram = _read('/etc/systemd/system/thinksmart-zram.service')
        self.assertIn('ExecStartPre=-/sbin/modprobe zram', zram)
        self.assertIn('ExecStartPre=-/bin/sh -c', zram)

        swd = _read('/usr/local/bin/screen-wake-daemon.py')
        self.assertIn('if not entered_off:', swd)

        uwd = _read('/usr/local/sbin/thinksmart-usb-watchdog')
        self.assertIn('(wd._dead_kmsg_pending or wd._host_restore_pending) and time.monotonic() - wd.last_recover_mono >= wd.cooldown_sec', uwd)

        # Bare-metal install template in hub config (not overwritten on live if skipped by sync_device, check util + app + llm_router behavior in test_02)
        u_py = _read('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/utils/util.py')
        self.assertIn('def is_placeholder_secret(', u_py)

    def test_02_hub_placeholders_client_bt_and_handlers(self):
        hub_root = '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'
        hub_py = f'{hub_root}/.venv/bin/python'
        probe = r"""
import asyncio, inspect, sys
sys.path.insert(0, '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
from core.utils.util import is_placeholder_secret, check_model_key
from core.agent.llm_router import _placeholder
from core.admin.config_store import is_placeholder
from plugins_func.functions.web_search import settings_of
from core.handle.textHandler.mcpMessageHandler import McpTextMessageHandler
from core.handle.helloHandle import checkWakeupWords

for bad in (None, '', '   ', '你的api_key', 'YOUR_DEEPSEEK_API_KEY', 'REPLACE_WITH_RANDOM_32_CHAR_SECRET', 'CHANGE_ME', 'EXAMPLE', '__REDACTED__'):
    assert is_placeholder_secret(bad), bad
    assert _placeholder(bad), bad
    assert is_placeholder(bad), bad
assert check_model_key('LLM', None) is not None
assert check_model_key('LLM', 'YOUR_OPENAI_API_KEY') is not None
assert not is_placeholder_secret('real-secret-token-123')
ws = settings_of({'plugins': {'web_search': {'api_key': 'YOUR_SEARCH_API_KEY'}}})
assert ws['api_key'] == ''

class DummyConn:
    def __init__(self):
        self.config = {'wakeup_words': None, 'enable_wakeup_words_response_cache': True}
        self.tts = True

async def _run():
    h = McpTextMessageHandler()
    c = DummyConn()
    await h.handle(c, {'type': 'mcp', 'payload': {'jsonrpc': '2.0'}})
    ok = await checkWakeupWords(c, '你好小智')
    assert ok is False

asyncio.run(_run())
import core.connection as cc
src = inspect.getsource(cc.ConnectionHandler._handle_function_result)
rec_blk = src[src.index('if record_tools:'):src.index('if need_llm_tools:')]
assert 'tool_call_data["id"] = str(uuid.uuid4())' in rec_blk
print('HUB_R4_OK')
"""
        r = subprocess.run([hub_py, '-B', '-c', probe], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('HUB_R4_OK', r.stdout)

        client_py = '/opt/thinksmart/client/current/.venv/bin/python'
        c_probe = r"""
import contextlib, sys
sys.path.insert(0, '/opt/thinksmart/client/current')
import thinksmart_client.bt as bt
assert hasattr(bt, 'contextlib') and bt.contextlib is contextlib
print('CLIENT_BT_OK')
"""
        r2 = subprocess.run([client_py, '-B', '-c', c_probe], capture_output=True, text=True, timeout=15)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn('CLIENT_BT_OK', r2.stdout)

    def test_03_tutor_intercom_monitor_and_study_fixes(self):
        tutor_html = _read('/usr/local/share/thinksmart-tutor/index.html')
        self.assertNotIn('mistakesList', tutor_html)
        self.assertIn('window.setDictDraft =', tutor_html)
        self.assertIn('oninput="setDictDraft(this.value)"', tutor_html)
        self.assertIn('if (Array.isArray(j.mistakes)) mistakeList = j.mistakes;', tutor_html)

        ic_py = _read('/usr/local/bin/thinksmart-intercom.py')
        idx_bind = ic_py.index('function bindVideoTrackEnd(')
        idx_toggle = ic_py.index('async function toggleCall()')
        self.assertLess(idx_bind, idx_toggle)
        m_sw = re.search(r'async function switchPhoneCam\(\)\s*\{([\s\S]*?)\n\}', ic_py)
        self.assertIsNotNone(m_sw)
        self.assertIn('vStream.getTracks().forEach', m_sw.group(1))
        self.assertIn('let added = false;', m_sw.group(1))

        # Tutor behavioral checks
        spec = importlib.util.spec_from_file_location('ts_tutor_r4', '/usr/local/bin/thinksmart-tutor.py')
        tutor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tutor)
        eng = tutor.TutorEngine()
        dm = eng.dictation
        self.assertFalse(dm.is_busy())
        dm._extracting = 1
        self.assertTrue(dm.is_busy())
        dm._extracting = 0
        self.assertFalse(tutor._tutor_busy(eng))
        eng._inflight_reqs = 1
        self.assertTrue(tutor._tutor_busy(eng))
        eng._inflight_reqs = 0
        em_empty = dm.enroll_mistakes()
        self.assertIn('mistake_summary', em_empty)
        self.assertIn('if self.session_id != my_sid or len(self.items) != items_len:', _read('/usr/local/bin/thinksmart-tutor.py'))

        # Monitor _clean_nezha_origin malformed input
        if '/usr/local/bin' not in sys.path:
            sys.path.insert(0, '/usr/local/bin')
        mspec = importlib.util.spec_from_file_location('ts_mon_r4', '/usr/local/bin/thinksmart-monitor.py')
        mon = importlib.util.module_from_spec(mspec)
        mspec.loader.exec_module(mon)
        self.assertEqual(mon.Monitor._clean_nezha_origin('http://[bad'), '')
        self.assertEqual(mon.Monitor._clean_nezha_origin('host:99999'), '')
        self.assertEqual(mon.Monitor._clean_nezha_origin('nezha.example.com/'), 'https://nezha.example.com')

        # Study voice_lock guard in _announce_retry
        st_py = _read('/usr/local/bin/thinksmart-study.py')
        self.assertIn('not self.stopping and not self.voice_lock.locked() and not self.media_busy()', st_py)

    def test_04_home_qqmusic_and_admin_ui_fixes(self):
        home_py = _read('/usr/local/bin/thinksmart-home.py')
        self.assertIn("cooldown = 2.0 if _ACCEL_CACHE.get('seen') else 30.0", home_py)
        self.assertIn("_ACCEL_CACHE['checked'] = 0.0", home_py)
        self.assertIn("asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)", home_py)
        self.assertIn("_holidays_extra() if '_holidays_extra' in globals() else {}", home_py)
        self.assertIn("(y, mo, today_iso, cd_ver, hol_stamp)", home_py)

        # Verify _is_workday still works in isolated globals() dict (as tested by test_workdays_claude)
        hspec = importlib.util.spec_from_file_location('ts_home_r4', '/usr/local/bin/thinksmart-home.py')
        hmod = importlib.util.module_from_spec(hspec)
        hspec.loader.exec_module(hmod)
        ns = {'HolidayUtil': hmod.HolidayUtil, 'dt': dt}
        import inspect
        exec(inspect.getsource(hmod._is_workday), ns)
        self.assertIsInstance(ns['_is_workday'](dt.date(2026, 11, 26)), bool)

        qq_html = _read('/usr/local/share/thinksmart-qqmusic/index.html')
        self.assertIn(r'\/api\/music\/radio\/emblem\/[A-Za-z0-9_-]+\.svg$', qq_html)

        qq_py = _read('/usr/local/bin/thinksmart-qqmusic.py')
        self.assertIn('def _call_set_ambient_internal(self, *args, **kwargs):', qq_py)
        self.assertIn('self._preview_op_tls = tls', qq_py)
        self.assertIn('_sleep_starting_count', qq_py)
        for alias in ("'下一台'", "'换台'", "'下一集'", "'上一台'", "'上一集'"):
            self.assertIn(alias, qq_py)

        adm_html = _read('/usr/local/share/thinksmart-admin/index.html')
        m_jump = re.search(r"\$\('btnJumpBackupCard'\)\.addEventListener\('click',\s*\(\)\s*=>\s*\{([\s\S]*?)\}\);", adm_html)
        self.assertIsNotNone(m_jump)
        self.assertIn("switchCfgTab('extra', false);", m_jump.group(1))
        self.assertNotIn("switchCfgTab('device'", m_jump.group(1))
        m_arm = re.search(r'function armTap\(btn, label, fn\)\s*\{([\s\S]*?)\n\}', adm_html)
        self.assertIsNotNone(m_arm)
        self.assertIn('if (btn.dataset.old !== undefined) btn.innerHTML = btn.dataset.old;', m_arm.group(1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
