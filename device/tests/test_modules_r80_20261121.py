#!/opt/thinksmart-addons/venv/bin/python
"""Round 1 (R80, 2026-11-21) non-Xiaozhi full module deep-audit regression suite."""
import asyncio
import codecs
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest
import wave
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')
STAGE_ROOT = pathlib.Path(os.environ.get('STAGE_ROOT', '/'))


def _p(abs_path: str) -> pathlib.Path:
    if str(STAGE_ROOT) != '/':
        cand = STAGE_ROOT / abs_path.lstrip('/')
        if cand.exists() or cand.parent.exists():
            return cand
    return pathlib.Path(abs_path)


if str(_p('/usr/local/bin')) not in sys.path:
    sys.path.insert(0, str(_p('/usr/local/bin')))

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _load_mod(name: str, path: str, strip_main: bool = True):
    p = pathlib.Path(path)
    src = p.read_text(encoding='utf-8')
    if strip_main and "\nif __name__ ==" in src:
        src = src.split("\nif __name__ ==", 1)[0] + "\n"
    mod = types.ModuleType(name)
    mod.__file__ = str(p)
    sys.modules[name] = mod
    exec(compile(src, str(p), 'exec'), mod.__dict__)
    return mod


class _ChunkedStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _n):
        for c in self._chunks:
            await asyncio.sleep(0)
            yield c

    async def read(self, _n=-1):
        # Deliberately return only first chunk to prove callers use iter_chunked!
        return self._chunks[0] if self._chunks else b''


class TestR80NonXiaozhiModules(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.disp = _load_mod('r80_disp', str(_p('/usr/local/bin/thinksmart_display.py')))
        cls.mon = _load_mod('r80_mon', str(_p('/usr/local/bin/thinksmart-monitor.py')))
        cls.home = _load_mod('r80_home', str(_p('/usr/local/bin/thinksmart-home.py')))
        cls.intercom = _load_mod('r80_intercom', str(_p('/usr/local/bin/thinksmart-intercom.py')))
        cls.study = _load_mod('r80_study', str(_p('/usr/local/bin/thinksmart-study.py')))
        cls.qq = _load_mod('r80_qq', str(_p('/usr/local/bin/thinksmart-qqmusic.py')))
        cls.tutor = _load_mod('r80_tutor', str(_p('/usr/local/bin/thinksmart-tutor.py')))
        cls.admin = _load_mod('r80_admin', str(_p('/usr/local/bin/thinksmart-admin.py')))

    def test_daemons_and_tunnel_guards(self):
        ua = _p('/usr/local/bin/usb-automount.sh').read_text(encoding='utf-8')
        self.assertIn('9>&-', ua)
        self.assertIn('flock -w 5 -x 9', ua)
        self.assertIn('umount -l "$MNT"', ua)
        wd = _p('/usr/local/sbin/thinksmart-usb-watchdog').read_text(encoding='utf-8')
        self.assertIn('LOCK_NB', wd)
        self.assertIn('close_fds=True', wd)
        self.assertIn('stop_flag', wd)
        t = _p('/usr/local/bin/thinksmart-intercom-tunnel.sh').read_text(encoding='utf-8')
        self.assertIn('port_held=1', t)
        self.assertIn('STREAK_FILE', t)
        ap = _p('/usr/local/bin/thinksmart-airplay.py').read_text(encoding='utf-8')
        self.assertIn('threading.Timer(1.5', ap)
        sw = _p('/usr/local/bin/screen-wake-daemon.py').read_text(encoding='utf-8')
        self.assertIn('def _resolve_accel_dir', sw)
        ib = _p('/usr/local/bin/idle-blanker.py').read_text(encoding='utf-8')
        self.assertIn('mode_explicitly_saved', ib)

    def test_display_wake_preserves_preview_and_monitor_traffic_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            sock = pathlib.Path(td) / 'disp.sock'
            pv = self.disp.BrightnessPreview(sock)
            pv.open()
            try:
                self.disp.send_preview({'level': 62}, sock)
                pv.receive()
                self.assertEqual(pv.current(), 62)
                self.disp.send_preview({'wake': True}, sock)
                pv.receive()
                self.assertEqual(pv.current(), 62)
                self.disp.send_preview({'clear': True}, sock)
                pv.receive()
                self.assertIsNone(pv.current())
            finally:
                pv.close()

            now_wall = 1795200000.0
            now_dt = dt.datetime.fromtimestamp(now_wall, tz=self.mon.SH_TZ)
            start_dt, end_dt = self.mon.compute_billing_cycle(1, now_dt)
            m_obj = object.__new__(self.mon.Monitor)
            m_obj.traffic_config = {'local_rules': {'1': {'server_id': 1, 'reset_day': 1}}}
            m_obj.servers = [{'id': 1, 'alive': True, 'net_in_transfer': 9_800_000, 'net_out_transfer': 4_900_000, 'uptime': 50000.0, 'mem_used': 1024}]
            m_obj._traffic_ledger = {
                '1': {
                    'from': start_dt.isoformat(),
                    'to': end_dt.isoformat(),
                    'acc_in': 10_000_000,
                    'acc_out': 5_000_000,
                    'last_in': 10_000_000,
                    'last_out': 5_000_000,
                    'complete': True,
                    'updated_at': now_wall - 10.0,
                }
            }
            m_obj._ledger_dirty = False
            m_obj._ledger_saved_mono = time.monotonic()
            m_obj._save_traffic_ledger = lambda: None
            # Virtual interface teardown drops counter by 200KB while uptime is 50000s (NO reboot!)
            m_obj._update_traffic_ledger(now_wall)
            self.assertEqual(m_obj._traffic_ledger['1']['acc_in'], 10_000_000)
            self.assertEqual(m_obj._traffic_ledger['1']['acc_out'], 5_000_000)
            self.assertEqual(m_obj._traffic_ledger['1']['last_in'], 9_800_000)

    async def test_home_chunked_stream_and_yearly_countdown_rules(self):
        cs = _ChunkedStream([b'{"hello":', b'"world",', b'"n":123}'])
        raw = await self.home._read_bounded(cs, 1024)
        self.assertEqual(json.loads(raw.decode()), {'hello': 'world', 'n': 123})

        today = dt.date(2026, 11, 21)
        # Solar Feb 29 yearly rule in non-leap 2026 must NOT clamp solar_d to 28!
        c_feb29 = self.home.convert_calendar_preview('solar', 2026, 2, 29, False, today, yearly_rule=True)
        self.assertEqual(c_feb29['solar_d'], 29)
        self.assertEqual(c_feb29['max_days'], 29)
        self.assertEqual(c_feb29['next_yearly_solar'], '2027-02-28')

        # Lunar leap month & day 30 yearly rule must NOT drop leap=True or clamp day 30 to 29!
        if self.home.Solar is not None:
            c_leap = self.home.convert_calendar_preview('lunar', 2026, 6, 15, True, today, yearly_rule=True)
            self.assertTrue(c_leap['lunar_leap'])
            self.assertEqual(c_leap['max_days'], 30)
            c_d30 = self.home.convert_calendar_preview('lunar', 2026, 12, 30, False, today, yearly_rule=True)
            self.assertEqual(c_d30['lunar_d'], 30)

    async def test_intercom_chunked_streams_and_ui_guards(self):
        big = b'A' * 30000 + b'B' * 40000
        cs = _ChunkedStream([big[:20000], big[20000:50000], big[50000:]])
        got = await self.intercom._read_stream_bounded(cs, 80000)
        self.assertEqual(got, big)
        html = self.intercom.HTML_PAGE
        self.assertIn('.hdr-btn:not(.theme-btn) use', html)
        self.assertIn('function maybeAckVisibleReplies()', html)
        self.assertIn('if (total < 16000)', html)

    async def test_study_chunked_json_secret_unlink_wav_strip_and_focus_date(self):
        payload = {'ok': True, 'pad': 'x' * 90000}
        enc = json.dumps(payload).encode()
        fake_resp = types.SimpleNamespace(content=_ChunkedStream([enc[:30000], enc[30000:65000], enc[65000:]]))
        parsed = await self.study._bounded_json(fake_resp, max_bytes=262144)
        self.assertEqual(parsed['ok'], True)
        self.assertEqual(len(parsed['pad']), 90000)

        with tempfile.TemporaryDirectory() as td:
            sp = pathlib.Path(td) / 'secret'
            sp.write_text('aa' * 32 + '\n', encoding='utf-8')
            os.chmod(sp, 0o600)
            comp = object.__new__(self.study.Companion)
            comp.secret = None
            comp._secret_sig = None
            with mock.patch.object(self.study, 'SECRET_PATH', sp):
                self.assertEqual(len(comp._ensure_secret()), 32)
                sp.unlink()
                with self.assertRaises(PermissionError):
                    comp._ensure_secret()
                self.assertIsNone(comp.secret)

            # 24kHz mono 16-bit WAV -> stripped raw PCM without 44-byte RIFF header
            buf = io.BytesIO()
            with wave.open(buf, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(b'\x12\x34' * 240)
            pcm = await comp._audio_bytes_to_pcm24k(buf.getvalue())
            self.assertEqual(pcm, b'\x12\x34' * 240)

    def test_qqmusic_login_sleep_preview_local_sort_and_radio_query(self):
        # Q_H_L_ key with stale wxuin= cookie must still be recognized as QQ login (not WeChat)!
        self.assertFalse(self.qq._is_wx_login({
            'qm_keyst': 'Q_H_L_999abc',
            'cookies_str': 'uin=12345; wxuin=12345; qm_keyst=Q_H_L_999abc',
            'user_profile': {'loginType': 'qq'},
        }))
        self.assertTrue(self.qq._is_wx_login({
            'qm_keyst': 'W_X_999abc',
            'cookies_str': 'wxuin=1152921504',
            'user_profile': {'loginType': 'wx'},
        }))

        # _next_sleep_restore_at at 06:29:10 must restore at 06:30:00 (50s later, NOT +24h!)
        t_629 = time.mktime((2026, 11, 21, 6, 29, 10, 0, 0, -1))
        rest_at = self.qq._next_sleep_restore_at(t_629)
        self.assertAlmostEqual(rest_at - t_629, 50.0, delta=2.0)

        # Radio voice query: song title with artist must NOT be hijacked to crosstalk radio!
        self.assertIsNone(self.qq.match_radio_voice_query('播放岳云鹏的五环之歌', '岳云鹏的五环之歌'))
        self.assertEqual(self.qq.match_radio_voice_query('播放岳云鹏的相声', '岳云鹏的相声'), 'ct_yueyunpeng')

        # search_local skips hidden/AppleDouble/0-byte files and sorts naturally BEFORE limit slice
        with tempfile.TemporaryDirectory() as td:
            mroot = pathlib.Path(td)
            (mroot / '.Trashes').mkdir()
            (mroot / '.Trashes' / '00_deleted.mp3').write_bytes(b'ID3')
            (mroot / '._01_ghost.mp3').write_bytes(b'ID3')
            (mroot / 'empty.mp3').write_bytes(b'')
            for n in ('10_ten.mp3', '02_two.mp3', '01_one.mp3', '03_three.mp3'):
                (mroot / n).write_bytes(b'ID3')
            with mock.patch.object(self.qq, 'LOCAL_DIRS', [mroot]):
                top2 = self.qq.search_local('', limit=2)
            self.assertEqual([x['title'] for x in top2], ['01_one', '02_two'])

    def test_tutor_uvc_1080p_crop_exclude_sid_and_utf8_sse(self):
        # Incremental UTF-8 decoder across split 3-byte Chinese char '解' (\xe8\xa7\xa3)
        dec = codecs.getincrementaldecoder('utf-8')(errors='replace')
        s1 = dec.decode(b'data: {"choices":[{"delta":{"content":"\xe8', final=False)
        s2 = dec.decode(b'\xa7\xa3\xe6\x9e\x90"}}]}\n', final=False)
        line = (s1 + s2).strip()
        obj = json.loads(line[5:].strip())
        self.assertEqual(obj['choices'][0]['delta']['content'], '解析')

        # PhoneStudio.save_page exclude_sid prevents double-rotating Phase-1 crop_box
        with tempfile.TemporaryDirectory() as td:
            st_dir = pathlib.Path(td)
            with mock.patch.object(self.tutor, 'STATE_DIR', st_dir), \
                 mock.patch.object(self.tutor, 'PREFS_PATH', st_dir / 'prefs.json'):
                ps = self.tutor.PhoneStudioManager()
                fake_jpg = b'\xff\xd8\xff\xe0' + b'\x00' * 256 + b'\xff\xd9'
                pid = ps.save_page(fake_jpg, client_key='ck1')
                ps.record_done_crop(pid, 's_old', {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4})
                ps.record_done_crop(pid, 's_new', {'x': 0.5, 'y': 0.5, 'w': 0.2, 'h': 0.2})
                ps.save_page(fake_jpg, page_id=pid, rot_steps=1, exclude_sid='s_new')
                pg = next(p for p in ps.pages if p['id'] == pid)
                by_sid = {d['sid']: d['crop'] for d in pg['done_crops']}
                # s_new stayed untouched; s_old rotated 90 deg CW!
                self.assertAlmostEqual(by_sid['s_new']['x'], 0.5, places=3)
                self.assertNotAlmostEqual(by_sid['s_old']['x'], 0.1, places=2)

    async def test_admin_ambient_music_status_and_unchanged_trash_purge(self):
        adm = object.__new__(self.admin.Admin)

        async def fake_qq(_m, _p, _b=None):
            return {'playing': False, 'paused': False, 'volume': 28, 'track': None,
                    'ambient': {'preset': 'rain', 'name': '细雨轻声', 'primary_name': '细雨'}}
        adm.qq = fake_qq
        resp = await adm.music_status(None)
        body = json.loads(resp.body.decode())
        self.assertEqual((body['playing'], body['title'], body['artist']), (True, '细雨轻声', '助眠声景'))

        with tempfile.TemporaryDirectory() as td:
            sd = pathlib.Path(td)
            tr = self.admin.Trash(sd)
            with mock.patch.object(tr, 'save') as m_save, \
                 mock.patch.object(self.admin, 'root_base', return_value=None):
                removed = tr.purge()
                self.assertEqual(removed, 0)
                m_save.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
