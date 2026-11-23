#!/opt/thinksmart-addons/venv/bin/python
"""Antigravity 2026-11-21 R82: Round 3 Deep Non-Xiaozhi Audit & Regression Tests."""
import ast
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')

E = os.environ.get
ROOT = pathlib.Path(E('R82_ROOT', '/'))


def _p(rel: str) -> pathlib.Path:
    return ROOT / rel.lstrip('/')


def _load_py_clean(name: str, path: pathlib.Path, strip_names=()):
    for d in (str(path.parent), '/usr/local/bin'):
        if d not in sys.path:
            sys.path.insert(0, d)
    src = path.read_text('utf-8')
    tree = ast.parse(src, filename=str(path))
    strip_set = set(strip_names)
    new_body = []
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and getattr(node.test.left, 'id', '') == '__name__':
            continue
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in strip_set for t in node.targets):
            continue
        new_body.append(node)
    tree.body = new_body
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    exec(compile(tree, str(path), 'exec'), mod.__dict__)
    return mod


def _load_script(name: str, path: pathlib.Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


class TestQqMusicR82(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qq_path = _p('/usr/local/bin/thinksmart-qqmusic.py')
        cls.qq_src = cls.qq_path.read_text('utf-8')
        cls.qq = _load_py_clean('r82_qq', cls.qq_path, strip_names=('engine',))

    def test_shutdown_deadlock_removed_and_sunrise_recovered(self):
        fn_src = ast.get_source_segment(self.qq_src, next(
            n for n in ast.parse(self.qq_src).body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == 'main'
        ))
        self.assertIn('_sunrise_mark_stopped()', fn_src)
        self.assertIn('_write_sleep_state(st_wake_restore)', fn_src)
        sess_idx = fn_src.index('with _SESSION_LOCK:')
        self.assertNotIn('engine.save_session()', fn_src[sess_idx:])

    def test_cookie_regex_boundaries_and_wx_headers(self):
        with mock.patch.object(self.qq, 'load_config', return_value={
            'uin': 'o9988',
            'qm_keyst': 'W_X_newkey',
            'cookies_str': 'foo_qm_keyst=keepme; qm_keyst=oldval; bar=1',
            'user_profile': {'loginType': 'wx'},
        }):
            hdrs = self.qq._qq_headers()
        ck = hdrs['Cookie']
        self.assertIn('foo_qm_keyst=keepme', ck)
        self.assertIn('qm_keyst=W_X_newkey', ck)
        self.assertNotIn('qm_keyst=oldval', ck)
        self.assertIn('wxuin=o9988', ck)
        self.assertIn('tmeLoginType=1', ck)

    def test_oauth_partial_creds_rejected(self):
        with mock.patch.object(self.qq, 'save_config') as m_save, \
             mock.patch.object(self.qq, '_safe_urlopen', side_effect=RuntimeError('boom')):
            res = self.qq._exchange_code_for_musickey('code123', fallback_uin='123456')
        self.assertIsNone(res)
        m_save.assert_not_called()

    def test_refresh_boundary_and_content_type(self):
        seen_hdrs = []

        class _FakeResp:
            def __init__(self, body, hdrs=None):
                self._b = body
                self.headers = hdrs or {}

            def read(self, *_a):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=5):
            seen_hdrs.append(dict(req.header_items()))
            return _FakeResp(json.dumps({'req': {'data': {'musickey': 'Q_H_L_NEW99'}}}).encode('utf-8'))

        class _FakeOpener:
            def open(self, req, timeout=5):
                return _FakeResp(b'', {'Location': 'https://y.qq.com/?code=abc123'})

        cfg = {'uin': '12345', 'qm_keyst': 'Q_H_L_OLD', 'cookies_str': 'pt4_p_skey=wrong; p_skey=real_pskey; qm_keyst=Q_H_L_OLD'}
        saved = []
        with mock.patch.object(self.qq, 'load_config', return_value=cfg), \
             mock.patch.object(self.qq, 'save_config', side_effect=lambda c: saved.append(dict(c))), \
             mock.patch.object(self.qq, '_has_only_global_ips', return_value=True), \
             mock.patch.object(self.qq.urllib.request, 'build_opener', return_value=_FakeOpener()), \
             mock.patch.object(self.qq, '_safe_urlopen', side_effect=fake_open):
            ok = self.qq._refresh_musickey_from_cookies()
        self.assertTrue(ok)
        self.assertEqual(seen_hdrs[-1].get('Content-type'), 'application/json')
        self.assertEqual(saved[-1]['qm_keyst'], 'Q_H_L_NEW99')
        self.assertIn('pt4_p_skey=wrong', saved[-1]['cookies_str'])

    def test_vkey_and_playable_tracks_anon_fallback(self):
        calls = []

        class _Resp:
            def __init__(self, data):
                self._b = json.dumps(data).encode('utf-8')

            def read(self, *_a):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_vkey_open(req, timeout=5):
            calls.append(dict(req.header_items()))
            has_cookie = 'Cookie' in calls[-1]
            if has_cookie:
                return _Resp({'code': 1000, 'req_0': {'code': 1000, 'data': {'midurlinfo': []}}})
            return _Resp({'code': 0, 'req_0': {'code': 0, 'data': {
                'sip': ['https://dl.stream.qqmusic.qq.com/'],
                'midurlinfo': [{'songmid': 'm1', 'filename': 'C400m1.m4a', 'purl': 'C400m1.m4a?vkey=ok'}],
            }}})

        cfg = {'uin': '12345', 'qm_keyst': 'Q_H_L_EXPIRED', 'cookies_str': 'uin=12345; qm_keyst=Q_H_L_EXPIRED'}
        with mock.patch.object(self.qq, 'load_config', return_value=cfg), \
             mock.patch.object(self.qq, '_refresh_musickey_from_cookies', return_value=False), \
             mock.patch.object(self.qq, '_safe_urlopen', side_effect=fake_vkey_open):
            res = self.qq.resolve_qq_vkey('m1')
            self.assertIsNotNone(res)
            self.assertIn('vkey=ok', res['url'])
            tracks = self.qq._qq_playable_tracks([{'mid': 'm1'}])
            self.assertEqual(len(tracks), 1)

    def test_non_ascii_uin_rejected(self):
        with mock.patch.object(self.qq, 'load_config', return_value={'uin': '١٢٣'}):
            self.assertEqual(self.qq.qq_user_playlists(), [])

    def test_engine_wake_busy_and_advance_on_eof_safe(self):
        eng = self.qq.QQMusicEngine()
        eng.playing = True
        eng.paused = False
        eng.queue = [{'mid': '1', 'title': 'A'}]
        eng.index = 0
        eng.just_started = True
        called = []
        eng.save_session = lambda: called.append(0)
        with mock.patch.object(eng, '_send_aec_flush') as m_flush:
            eng._advance_on_eof(eng._gen, force_next=True)
        self.assertTrue(eng.paused)
        self.assertFalse(eng.just_started)
        self.assertEqual(called, [0])
        m_flush.assert_called_once()
        # Verify sleep_wake_tick runs before sleep_housekeeping in sleep_watcher source
        wt_idx = self.qq_src.index('engine.sleep_wake_tick)')
        hk_idx = self.qq_src.index('engine.sleep_housekeeping)')
        self.assertLess(wt_idx, hk_idx)

    def test_qq_html_fixes_and_contracts(self):
        html = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        self.assertIn('curKey === key', html)
        self.assertIn('favMid = \'\';\n    favLiked = null;', html)
        self.assertIn('prevRadioId', html)
        self.assertIn('#myPlaylistChips .pl-card[data-plid="dir_201"] .cnt', html)
        self.assertIn('volDragPid', html)
        self.assertIn("if (e.type === 'pointerup')", html)
        self.assertIn("addEventListener('lostpointercapture', endRange, true);", html)


class TestUsbUdevR82(unittest.TestCase):
    def test_efi_filter_and_watchdog_lock(self):
        rules = _p('/etc/udev/rules.d/99-usb-automount.rules').read_text('utf-8')
        self.assertIn('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', rules)
        self.assertIn('0xef', rules)
        sh = _p('/usr/local/bin/usb-automount.sh').read_text('utf-8')
        self.assertIn('ID_PART_ENTRY_TYPE', sh)
        for sub in ('mount -t "$FSTYPE" -o "$OPTIONS"', 'mount -t "$FSTYPE" -o "${OPTIONS/#rw,/ro,}"', '9>&-', 'flock -w 5 -x 9', 'umount -l "$MNT"'):
            self.assertIn(sub, sh)
        wd = _load_script('r82_usb_wd', _p('/usr/local/sbin/thinksmart-usb-watchdog'))
        with tempfile.TemporaryDirectory() as td:
            lf = pathlib.Path(td) / 'usb.lock'
            with mock.patch.object(wd.fcntl, 'flock', side_effect=BlockingIOError), \
                 mock.patch.object(wd.time, 'sleep', return_value=None), \
                 mock.patch.object(wd.time, 'monotonic', side_effect=[0.0, 0.0, 5.0]), \
                 mock.patch.object(wd.subprocess, 'run') as m_run:
                wd.unmount_stale_usb(mnt_dir='/tmp/none', lock_path=str(lf))
            m_run.assert_not_called()


class TestTutorR82(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tutor_path = _p('/usr/local/bin/thinksmart-tutor.py')
        cls.tutor_src = cls.tutor_path.read_text('utf-8')
        cls.tutor = _load_py_clean('r82_tutor', cls.tutor_path, strip_names=('eng', 'engine', 'app'))

    def test_update_progress_skips_noop_hist_write(self):
        with tempfile.TemporaryDirectory() as td:
            hp = pathlib.Path(td) / 'history.json'
            with mock.patch.object(self.tutor, 'HIST_PATH', hp):
                eng = self.tutor.TutorEngine.__new__(self.tutor.TutorEngine)
                eng.lock = __import__('threading').RLock()
                eng.sessions = {}
                eng.history = [{'id': 's1', 'progress': {'solved': True, 'step': 2}}]
                eng.mistakes = []
                eng._enroll_or_refresh_mistake = lambda *a, **k: None
                with mock.patch.object(self.tutor, '_atomic_write_json') as m_write:
                    eng.update_progress('s1', {'reset_run': True, 'step': 0})
                m_write.assert_not_called()
                self.assertTrue(eng.history[0]['progress']['solved'])

    def test_phone_studio_noop_crop_and_stale_check(self):
        with tempfile.TemporaryDirectory() as td:
            pref = pathlib.Path(td) / 'prefs.json'
            with mock.patch.object(self.tutor, 'PREFS_PATH', pref), \
                 mock.patch.object(self.tutor, 'STATE_DIR', pathlib.Path(td)):
                (pathlib.Path(td) / 'phone_stage_p1.jpg').write_bytes(b'\xff\xd8\xff\xe0' + b'\x00' * 160 + b'\xff\xd9')
                pm = self.tutor.PhoneStudioManager()
                pm.pages = [{'id': 'p1', 'seq': 3, 'last_crop': {'x': 0.1, 'y': 0.1, 'w': 0.5, 'h': 0.5}, 'done_crops': []}]
                pm.active_id = 'p1'
                with mock.patch.object(pm, '_save_prefs') as m_save:
                    pm.update_active_crop({'x': 0.1, 'y': 0.1, 'w': 0.5, 'h': 0.5})
                    m_save.assert_not_called()
                    pm.update_active_crop({'x': 0.2, 'y': 0.1, 'w': 0.5, 'h': 0.5})
                    m_save.assert_called_once()

        eng = self.tutor.TutorEngine.__new__(self.tutor.TutorEngine)
        ps = self.tutor.PhoneStudioManager.__new__(self.tutor.PhoneStudioManager)
        ps.pages = [{'id': 'p1', 'seq': 1, 'done_crops': [], 'check_marks': []}]
        ps.active_id = 'p1'
        ps._valid_pages = lambda: list(ps.pages)
        ps._active_page = lambda: ps.pages[0] if ps.pages else None
        ps.has_page = lambda: True
        async def _fake_rot(_pid):
            return b'\xff\xd8\xff\xd9'
        ps.get_rotated_stage_jpeg = _fake_rot
        ps.public_state = lambda: {'has_page': True, 'pages': []}
        called_marks = []
        ps.set_check_marks = lambda *a, **k: called_marks.append(a)
        eng.phone_studio = ps
        async def _fake_chat(*_a, **_kw):
            ps.pages[0]['seq'] = 99
            return '{"summary":"ok","items":[{"no":"1","status":"wrong","box":[0.1,0.1,0.4,0.2],"note":"n"}]}'
        eng._post_chat_once = _fake_chat
        res = asyncio.run(eng.check_staged_worksheet_page('p1'))
        self.assertTrue(res.get('stale'))
        self.assertEqual(called_marks, [])

    def test_capture_preview_raw_cache_includes_zoom_bright(self):
        self.assertIn('(f_seq, self.zoom, self.bright_boost)', self.tutor_src)
        self.assertIn('(seq, self.zoom, self.bright_boost)', self.tutor_src)

    def test_tutor_html_and_mobile_fixes(self):
        t_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        m_html = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('stopCameraPreviewLoop(true);', t_html)
        self.assertIn('pointerId: e.pointerId', t_html)
        self.assertIn('clearSoloTimer();', t_html)
        self.assertIn('@media (max-width: 400px)', m_html)
        self.assertIn('pageLoadSeq', m_html)
        self.assertIn('abortStageSilent()', m_html)
        self.assertIn("'If-None-Match': mobileStateEtag", m_html)
        self.assertIn('if (r.status === 304)', m_html)


class TestIntercomAndStudyR82(unittest.TestCase):
    def test_intercom_ui_pure_gtk_and_rot_vert(self):
        src = _p('/usr/local/bin/thinksmart-intercom-ui.py').read_text('utf-8')
        self.assertNotIn('import cairo', src)
        self.assertIn('.avatar-circle.rot-vert', src)
        self.assertIn('self.f1_seq', src)
        self.assertIn('def _apply_chrome_orientation(self, srot: int):', src)

    def test_intercom_server_and_html_fixes(self):
        src = _p('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
        self.assertIn('self.ui_proc is not None and self.ui_proc.returncode is not None', src)
        self.assertIn('urllib.parse.unquote(path_only).split("/")', src)
        self.assertIn('request.method != "HEAD" and r.status not in (204, 304)', src)
        self.assertIn("audioCtx.state === 'interrupted'", src)
        self.assertIn('elapsedMs >= 61500', src)
        self.assertIn('Date.UTC(tpNow.getUTCFullYear()', src)
        self.assertIn('sc = 480 / Math.max(vw, vh)', src)
        self.assertIn('window.tsEmbedSetActive', src)

    def test_study_parse_think_and_midnight(self):
        mod = _load_py_clean('r82_study', _p('/usr/local/bin/thinksmart-study.py'))
        out = mod.parse_result('<think>{"foo": 1}\n</think>\n{"present":true,"posture":"good","too_close":false,"activity":"studying","focused":true,"note":"ok"}')
        self.assertTrue(out['present'])
        self.assertEqual(out['activity'], 'studying')
        c = mod.Companion.__new__(mod.Companion)
        ts_2020 = mod.dt.datetime(2020, 1, 1, 23, 59, tzinfo=mod.LOCAL_TZ).timestamp()
        c.last = {'at': ts_2020, 'note': 'old'}
        c.last_day = '2020-01-01'
        c._load_day_last = lambda _d: None
        self.assertIsNone(c.last_today('2099-01-02'))
        self.assertIsNotNone(c.last_today('2020-01-01'))
        self.assertIsNone(c.last_today())
        self.assertIsNone(c.last)

    def test_study_parent_html_fixes(self):
        html = _p('/usr/local/share/thinksmart-study/parent.html').read_text('utf-8')
        self.assertIn("setVal($('tFocus'), [s.focus_sessions || 0, '次番茄'])", html)
        self.assertIn('lastTrendW = CW > 0 ? CW : 0;', html)


class TestHomeMonitorAdminDaemonsR82(unittest.TestCase):
    def test_home_weather_failed_city_isolated(self):
        home = _load_py_clean('r82_home', _p('/usr/local/bin/thinksmart-home.py'))
        w = home.Weather()
        w.loc = {'query': '北京', 'name': '北京', 'id': '1'}
        w.data = {'ok': True, 'city': '北京'}
        w.fetched = home.time.monotonic()
        w.failed = home.time.monotonic()
        w.failed_city = '上海'
        w.cred_sig = home.weather_credentials()
        with mock.patch.object(home, 'load_config', return_value={'city': '北京'}):
            d, _ = asyncio.run(w.get_with_seq(None, force=False))
        self.assertEqual(d['city'], '北京')

    def test_home_and_admin_html_fixes(self):
        h_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('w.cities', h_html)
        self.assertIn('scrPlayInflightId', h_html)
        self.assertIn('if (!cpOpen()) return;', h_html)
        self.assertIn('convKey !== curKey', h_html)
        self.assertIn('clearInterval(spTryI); spTryI = 0;', h_html)
        a_html = _p('/usr/local/share/thinksmart-admin/index.html').read_text('utf-8')
        self.assertIn("return cur === 'cfg' && cfgUi.tab === 'clash' && $('clashFull').hidden;", a_html)
        self.assertIn("if (cfgUi.tab === 'clash') { if ($('clashFull').hidden) refreshCfgOrClash(); }", a_html)

    def test_monitor_display_overlay_admin_daemons(self):
        m_src = _p('/usr/local/bin/thinksmart-monitor.py').read_text('utf-8')
        self.assertIn('carriers_sig', m_src)
        self.assertIn('if not self.viewed_recently() and not self.connected:', m_src)
        m_html = _p('/usr/local/share/thinksmart-monitor/index.html').read_text('utf-8')
        self.assertIn('wrap._ptrId=e.pointerId', m_html)
        self.assertIn('if(selectedId===sid)renderDetailUnicom(sid);', m_html)
        d_html = _p('/usr/local/share/thinksmart-monitor/display.html').read_text('utf-8')
        self.assertIn('dragPid=event.pointerId', d_html)
        self.assertIn(':(8090|8092)$/.test(e.origin)', d_html)
        ov_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('subPauseTimers.delete(cur.url)', ov_js)
        self.assertIn('if (!document.hidden) applyAutoTheme()', ov_js)
        adm_src = _p('/usr/local/bin/thinksmart-admin.py').read_text('utf-8')
        self.assertIn('except FileNotFoundError:', adm_src)
        self.assertIn('if unlinked and tid in d:', adm_src)
        sw = _load_py_clean('r82_sw', _p('/usr/local/bin/screen-wake-daemon.py'))
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / 'real.txt'
            link = pathlib.Path(td) / 'sym.txt'
            target.write_text('{"dim":true}', 'utf-8')
            link.symlink_to(target)
            with self.assertRaises(OSError):
                sw._read_small_text(link)
        ib = _load_py_clean('r82_ib', _p('/usr/local/bin/idle-blanker.py'))
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / 'bl'
            link = pathlib.Path(td) / 'bl_sym'
            target.write_text('100\n', 'utf-8')
            link.symlink_to(target)
            with mock.patch.object(ib, 'BACKLIGHT_PATH', str(link)):
                self.assertFalse(ib.set_brightness(200))

    def test_mihomo_hardening_and_portrait_css_contracts(self):
        hc = _p('/etc/systemd/system/mihomo.service.d/hardening.conf').read_text('utf-8')
        for key in ('LimitNOFILE=', 'MemoryHigh=', 'MemoryMax=', 'TasksMax='):
            self.assertIn(key, hc)
        for rel in (
            '/usr/local/share/thinksmart-home/index.html',
            '/usr/local/share/thinksmart-qqmusic/index.html',
            '/usr/local/share/thinksmart-monitor/index.html',
            '/usr/local/share/thinksmart-tutor/index.html',
        ):
            css_src = _p(rel).read_text('utf-8')
            for blk in re.finditer(r'@media\s*\(orientation:\s*portrait\)\s*\{', css_src):
                i = blk.end()
                depth = 1
                while i < len(css_src) and depth > 0:
                    if css_src[i] == '{':
                        depth += 1
                    elif css_src[i] == '}':
                        depth -= 1
                    i += 1
                block = css_src[blk.end():i - 1]
                for m in re.finditer(r'font-size\s*:\s*([^;\}]+)', block):
                    val = m.group(1).strip().replace('!important', '').strip()
                    pm = re.fullmatch(r'(\d+)px', val)
                    self.assertIsNotNone(pm, f'{rel} non-integer font-size {val!r}')
                    self.assertGreaterEqual(int(pm.group(1)), 15, f'{rel} font-size {val!r} < 15px')
                for m in re.finditer(r'font-weight\s*:\s*([^;\}]+)', block):
                    val = m.group(1).strip().replace('!important', '').strip()
                    self.assertIn(val, ('400', '700'), f'{rel} font-weight {val!r}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
