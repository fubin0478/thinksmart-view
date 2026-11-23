#!/opt/thinksmart-addons/venv/bin/python
"""Regression suite for R91 Item 2 optimization:
1. Eliminate duplicate idle :8091 polling from kiosk_overlay.js by relaying QQ Music open_ui_* via :8095 /api/home/state.
2. Relax home_overlay.js /api/badge idle poll from 60s to 240s when healthy (:0:0).
3. Cache idle :8093 /api/study status in thinksmart-home.py with zero-fork disk-signature invalidation.
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

STAGE_ROOT = pathlib.Path(os.environ.get('STAGE_ROOT', '/'))


def _p(abs_path: str) -> pathlib.Path:
    cand = STAGE_ROOT / abs_path.lstrip('/')
    return cand if cand.exists() else pathlib.Path(abs_path)


def _load_py(mod_name: str, path: pathlib.Path):
    import importlib.util
    if '/usr/local/bin' not in sys.path:
        sys.path.insert(0, '/usr/local/bin')
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestR91PollDeduplicationStatic(unittest.TestCase):
    def test_music_public_exposes_open_ui_fields(self):
        home = _load_py('r91_home_static', _p('/usr/local/bin/thinksmart-home.py'))
        pub = home._music_public({
            'playing': True,
            'paused': False,
            'volume': 40,
            'play_mode': 'loop',
            'boot_id': 987654,
            'open_ui_seq': 3,
            'open_ui_fresh': True,
            'open_ui_tab': 'radio',
        })
        self.assertEqual(pub['boot_id'], 987654)
        self.assertEqual(pub['open_ui_seq'], 3)
        self.assertTrue(pub['open_ui_fresh'])
        self.assertEqual(pub['open_ui_tab'], 'radio')

    def test_overlays_and_home_wiring(self):
        home_h = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn("cmd: 'musicOpenSync'", home_h)
        self.assertIn('lastMusicOpenSig', home_h)

        ho_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('240000', ho_js)
        self.assertIn("lastSrvBadgeSig.endsWith(':0:0')", ho_js)
        self.assertIn("d.cmd === 'musicOpenSync'", ho_js)
        self.assertIn('window.__tsQqSyncStatus(d.st)', ho_js)

        ko_js = _p('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js').read_text('utf-8')
        self.assertIn('window.__tsQqSyncStatus = applyMusicStatus;', ko_js)
        self.assertIn('if (delayMs === undefined && !voiceWindow)', ko_js)
        self.assertNotIn(': 6000)', ko_js)
        # Preserve legacy invariants
        for tok in (
            "tab === 'fav' ? 'openFav'",
            'openRadio',
            'let tickDueAt = 0;',
            'pendingOpenAt',
            'tickDueAt = 0',
            'restarted && seq > 0 && fresh',
            'wantOpenOnWake',
        ):
            self.assertIn(tok, ko_js)
        for forbidden in ('tsQqMusicDock', 'tsQqMiniCtrls', 'window.location.href'):
            self.assertNotIn(forbidden, ko_js)


class TestR91IdleStudyCacheAndRelay(unittest.IsolatedAsyncioTestCase):
    async def test_study_idle_cache_hits_and_invalidates_on_disk_sig(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        home = _load_py('r91_home_http', _p('/usr/local/bin/thinksmart-home.py'))
        music_calls = 0
        study_calls = 0
        study_state = {'enabled': False, 'focus': {'phase': None}, 'today': {'focus': {'sessions': 0, 'minutes': 0}}}

        async def m_ep(_req):
            nonlocal music_calls
            music_calls += 1
            return web.json_response({
                'playing': False, 'paused': False, 'volume': 40,
                'boot_id': 42, 'open_ui_seq': 7, 'open_ui_fresh': True, 'open_ui_tab': 'fav',
            })

        async def s_ep(_req):
            nonlocal study_calls
            study_calls += 1
            return web.json_response(study_state)

        m_app = web.Application(); m_app.router.add_get('/api/music/status', m_ep)
        s_app = web.Application(); s_app.router.add_get('/api/study', s_ep)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            s_dir = td_p / 'study_state'
            s_dir.mkdir()
            cfg_p = s_dir / 'config.json'
            cfg_p.write_text('{"enabled":false}', encoding='utf-8')
            async with TestClient(TestServer(m_app)) as mc, TestClient(TestServer(s_app)) as sc:
                with mock.patch.object(home, 'MUSIC', f'http://127.0.0.1:{mc.port}/api/music'), \
                     mock.patch.object(home, 'STUDY', f'http://127.0.0.1:{sc.port}'), \
                     mock.patch.dict(os.environ, {'STUDY_STATE_DIR': str(s_dir)}):
                    app = home.make_app(inbox=home.Inbox(td_p / 'msg.json'), countdowns=home.CountdownStore(td_p / 'cd.json'))
                    async with TestClient(TestServer(app)) as cli:
                        h = {'Host': '127.0.0.1:8095'}
                        # 1st fetch: hits :8093 and :8091
                        r1 = await cli.get('/api/home/state', headers=h)
                        self.assertEqual(r1.status, 200)
                        j1 = await r1.json()
                        self.assertEqual(j1['music']['open_ui_seq'], 7)
                        self.assertTrue(j1['music']['open_ui_fresh'])
                        self.assertEqual(j1['music']['open_ui_tab'], 'fav')
                        self.assertEqual(study_calls, 1)

                        # Expire 0.8s state_cache without changing study files -> :8093 NOT called!
                        import asyncio
                        await asyncio.sleep(0.85)
                        r2 = await cli.get('/api/home/state', headers=h)
                        self.assertEqual(r2.status, 200)
                        self.assertEqual(study_calls, 1)
                        self.assertEqual(music_calls, 2)

                        # Simulate study toggle updating config.json -> disk signature changes -> instant :8093 re-fetch!
                        await asyncio.sleep(0.85)
                        study_state['enabled'] = True
                        cfg_p.write_text('{"enabled":true,"extra":1}', encoding='utf-8')
                        r3 = await cli.get('/api/home/state', headers=h)
                        j3 = await r3.json()
                        self.assertTrue(j3['study']['enabled'])
                        self.assertEqual(study_calls, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
