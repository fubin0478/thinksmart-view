#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R81 (Non-Xiaozhi Modules Round 2) regression test suite."""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
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

def _load(name: str, path: str):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


class TestModulesRound2R81(unittest.TestCase):
    def test_01_mic_usb_proximity_and_system_daemons(self):
        mic = _p('/usr/local/bin/thinksmart-mic').read_text('utf-8')
        self.assertIn('--fatal-errors', mic)
        self.assertIn("'-c', 'cd18781y'", mic)
        self.assertIn('rec.terminate()', mic)

        usb_mode = _p('/usr/local/bin/usb-mode').read_text('utf-8')
        self.assertIn('wait_role host || exit 1', usb_mode)

        usb_mnt = _p('/etc/systemd/system/thinksmart-usb-mount@.service').read_text('utf-8')
        self.assertIn('TimeoutStartSec=35', usb_mnt)

        wd = _load('wd_r81', str(_p('/usr/local/sbin/thinksmart-usb-watchdog')))
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            role_f = root / 'role'
            role_f.write_text('host\n', 'utf-8')
            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd[-1] if isinstance(cmd, list) else str(cmd))
                if isinstance(cmd, list) and cmd[-1] == 'device':
                    raise subprocess.TimeoutExpired(cmd, 10)
                return mock.MagicMock(returncode=0, stdout='')

            dog = wd.UsbWatchdog(
                role_path=str(role_f), usb1_path=str(root / 'usb1'),
                usb_mode_bin=str(_p('/usr/local/bin/usb-mode')), mnt_dir=str(root / 'mnt'),
                lock_path=str(root / 'lck'), cooldown_sec=5.0, settle_sec=0.0, role_gap_sec=0.0,
            )
            with mock.patch.object(wd.subprocess, 'run', side_effect=fake_run):
                ok = dog.recover('timeout test')
            self.assertFalse(ok)
            # Even when 'device' step times out, 'host' restore MUST execute in finally!
            self.assertIn('host', calls)

        px = _load('px_r81', str(_p('/usr/local/bin/vcnl4200-proximity-daemon.py')))
        with mock.patch.object(px.signal, 'signal'):
            s = px.ProximitySensor()
        word_writes = []
        s.i2c_set_word = lambda reg, val: (word_writes.append((reg, val)), True)[1]
        s.enable_sensor()
        self.assertTrue(s.sensor_enabled)
        self.assertIn((px.PS_CONF1_REG, 0x08CA), word_writes)
        s.disable_sensor()
        self.assertFalse(s.sensor_enabled)
        self.assertIn((px.PS_CONF1_REG, 0x08CB), word_writes)

        px_txt = _p('/usr/local/bin/vcnl4200-proximity-daemon.py').read_text('utf-8')
        self.assertIn("['sudo', '-n', 'tee'", px_txt)
        self.assertIn('prox = None', px_txt)

        gpu = _p('/usr/local/bin/gpu-watchdog.py').read_text('utf-8')
        self.assertIn('--since=-20s', gpu)

        td_mod = _load('td_r81', str(_p('/usr/local/bin/thinksmart_display.py')))
        with tempfile.TemporaryDirectory() as td:
            als_p = pathlib.Path(td) / 'als.json'
            als_p.write_text(json.dumps({'lux': 42.5, 'at': time.time() + 0.8}), 'utf-8')
            self.assertEqual(td_mod.ambient(als_p), 42.5)

        ap_txt = _p('/usr/local/bin/thinksmart-airplay.py').read_text('utf-8')
        self.assertIn('not _audio_flag_path().exists()', ap_txt)

        tas = _p('/home/ubuntu/.config/systemd/user/tas5782m-preinit.service').read_text('utf-8')
        self.assertIn('alsa_output.platform-c051000.sound-card', tas)
        self.assertIn('wireplumber.service', tas)
        self.assertFalse(_p('/home/ubuntu/.config/systemd/user/idle-blanker.service').exists())

    def test_02_home_overlay_display_and_clip_player(self):
        ho = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('if (isBusy() && xzAuto())', ho)
        self.assertIn('orTopDown = false', ho)
        self.assertIn('html.ts-portrait #ring,html.ts-portrait #powerOff{', ho)

        dp = _p('/usr/local/share/thinksmart-monitor/display.html').read_text('utf-8')
        self.assertIn('if(isActive())refresh(true)', dp)
        self.assertIn('#feedback{color:var(--ok-ink);font-weight:700}', dp)

        hm_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('html[data-theme="light"] .sb-svg { left: 0; width: 768px; }', hm_html)
        self.assertIn("addEventListener('lostpointercapture', endRange, true);", hm_html)

        hm = _load('hm_r81', str(_p('/usr/local/bin/thinksmart-home.py')))
        cp = hm.SpeakerClipPlayer()
        self.assertIsNone(cp.current_path)

    def test_03_qqmusic_lyrics_playlists_fallback_and_local_search(self):
        qq = _load('qq_r81', str(_p('/usr/local/bin/thinksmart-qqmusic.py')))
        # 1. qq_lyric unescapes &#10; BEFORE splitlines and parses [mm:ss:xx]
        fake_resp = json.dumps({
            'retcode': 0,
            'lyric': '[00:01.50]第一句&#10;[00:05:25]第二句&#13;&#10;[00:09.00]第三句',
        }).encode('utf-8')

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return fake_resp

        with qq._LYRIC_LOCK:
            qq._LYRIC_CACHE.pop('mid_r81_test', None)
        with mock.patch.object(qq, '_safe_urlopen', return_value=FakeResp()), \
                mock.patch.object(qq, '_read_http_bytes', return_value=fake_resp), \
                mock.patch.object(qq, 'load_config', return_value={}):
            lrc = qq.qq_lyric('mid_r81_test')
        self.assertEqual(len(lrc), 3)
        self.assertEqual(lrc[0], {'time': 1.5, 'text': '第一句'})
        self.assertEqual(lrc[1], {'time': 5.25, 'text': '第二句'})
        self.assertEqual(lrc[2], {'time': 9.0, 'text': '第三句'})

        # 2. search_local matches artist/album subfolder names while preserving exact signature
        with tempfile.TemporaryDirectory() as td:
            artist_dir = pathlib.Path(td) / '周杰伦精选'
            artist_dir.mkdir()
            song_f = artist_dir / '01_晴天.mp3'
            song_f.write_bytes(b'ID3test')
            with mock.patch.object(qq, 'LOCAL_DIRS', [pathlib.Path(td)]):
                hits = qq.search_local('周杰伦', 10)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]['title'], '01_晴天')

        # 3. resolve_hires_fallback rejects mismatched/overlong videos instead of picking results[0]
        fake_bili = json.dumps({
            'data': {'result': [{'bvid': 'BV1xx411c7mD', 'duration': '180:00', 'title': '3小时串烧'}]}
        }).encode('utf-8')

        class FakeOpener:
            def open(self, req, timeout=5):
                m = mock.MagicMock()
                m.__enter__.return_value = m
                return m

        with mock.patch.object(qq.urllib.request, 'build_opener', return_value=FakeOpener()), \
                mock.patch.object(qq, '_read_http_bytes', return_value=fake_bili):
            self.assertIsNone(qq.resolve_hires_fallback('周杰伦 晴天', title_hint='晴天', artist_hint='周杰伦'))

        qq_html = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        self.assertIn('.bb-left .mode-btn { padding: 0 10px; font-size: 15px; font-weight: 400; gap: 5px; }', qq_html)
        self.assertIn('let radioReqSeq = 0, pickerReqSeq = 0;', qq_html)

    def test_04_tutor_admin_intercom_and_study(self):
        tut_txt = _p('/usr/local/bin/thinksmart-tutor.py').read_text('utf-8')
        self.assertIn("sp['_advancing'] = True", tut_txt)
        self.assertIn("sess_rst['_replaying'] = True", tut_txt)
        self.assertIn("if not inflight.cancelled():", tut_txt)
        self.assertIn("'pass': False", tut_txt)

        adm_txt = _p('/usr/local/bin/thinksmart-admin.py').read_text('utf-8')
        self.assertIn("if (not j.get('playing')) and amb_lbl:", adm_txt)

        adm_html = _p('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertIn('if (!live.length) { queue.length = 0;', adm_html)

        ic_ui = _p('/usr/local/bin/thinksmart-intercom-ui.py').read_text('utf-8')
        self.assertIn('def _apply_chrome_orientation(self, srot: int):', ic_ui)
        self.assertIn('elif ftype == 4:', ic_ui)
        self.assertNotIn('import cairo', ic_ui)

        ic_txt = _p('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
        self.assertIn('def _resolve_accel_xy(self):', ic_txt)
        self.assertIn('def get_speaker_mute(self):', ic_txt)
        self.assertIn('"%2e" in path_only.lower()', ic_txt)
        self.assertIn('shutting_down = [False]', ic_txt)

        st_txt = _p('/usr/local/bin/thinksmart-study.py').read_text('utf-8')
        self.assertIn('cm.reschedule(new_dl)', st_txt)
        self.assertIn("'seconds': 28", st_txt)
        self.assertIn('def _effective_auto_off(cfg):', st_txt)

        p_html = _p('/usr/local/share/thinksmart-study/parent.html').read_text('utf-8')
        self.assertIn("const t = localStorage.getItem(isEmbed ? 'ts-theme' : 'study-theme');", p_html)
        self.assertIn('if (!document.documentElement.dataset.embed)', p_html)


if __name__ == '__main__':
    unittest.main()
