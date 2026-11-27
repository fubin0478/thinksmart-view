#!/opt/thinksmart-addons/venv/bin/python
"""2026-11-12 (Claude): QQ 音乐「我喜欢」 for the playing song (thinksmart-qqmusic.py /api/music/fav).
No network and no account change: the QQ calls are faked; the request shape is what was tested live on 11-12 22:15
(add then remove one song, the list put back).
    /opt/thinksmart-addons/venv/bin/python -B /opt/thinksmart-addons/test_qqfav_20261112.py
QQFAV_ROOT=<dir> tests the copy in <dir>."""
import ast
import asyncio
import os
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = os.environ.get('QQFAV_ROOT')
SRC = pathlib.Path(ROOT, 'thinksmart-qqmusic.py') if ROOT else pathlib.Path('/usr/local/bin/thinksmart-qqmusic.py')
sys.path.insert(0, '/opt/thinksmart-addons/vendor')


def load():
    t = ast.parse(SRC.read_text(encoding='utf-8'))
    t.body = [n for n in t.body if not (isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == 'engine' for x in n.targets))]
    t.body = [n for n in t.body if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and getattr(n.test.left, 'id', '') == '__name__')]
    m = types.ModuleType('qq_fav_test')
    m.__file__ = str(SRC)
    exec(compile(t, str(SRC), 'exec'), m.__dict__)
    return m


Q = load()
CFG = {'uin': '123456', 'qm_keyst': 'Q_H_TEST', 'cookies_str': 'uin=123456; p_skey=x', 'user_profile': {'loginType': 'qq'}}


class FavTests(unittest.TestCase):
    def setUp(self):
        Q._SONG_ID_CACHE.clear()
        self.calls = []
        self.codes = []

    def fake(self, comm, req, timeout=8):
        self.calls.append((comm, req))
        if req['module'] == 'music.pf_song_detail_svr':
            return 0, {'track_info': {'id': 8136, 'type': 0, 'name': 'x'}}
        if req['module'] == 'music.musicasset.SongFavRead':
            return 0, {'m_fan': {req['param']['v_songMid'][0]: True}}
        return (self.codes.pop(0) if self.codes else 0), {}

    def run_set(self, on, codes=(), cfg=CFG, refresh=False):
        self.codes = list(codes)
        with mock.patch.object(Q, 'load_config', return_value=dict(cfg)), mock.patch.object(Q, '_qq_musicu_one', self.fake), \
                mock.patch.object(Q, '_refresh_musickey_from_cookies', return_value=refresh):
            return Q.qq_fav_set('001fsNdn1zuZnA', on)

    def test_add_and_remove_request_shape(self):
        r = self.run_set(True)
        self.assertEqual((r['ok'], r['liked'], r['message']), (True, True, '已加入「我喜欢」'))
        comm, req = self.calls[-1]
        self.assertEqual((req['module'], req['method']), ('music.musicasset.PlaylistDetailWrite', 'AddSonglist'))
        self.assertEqual(req['param'], {'dirId': 201, 'v_songInfo': [{'songType': 0, 'songId': 8136}]})
        self.assertEqual((comm['tmeAppID'], comm['ct'], comm['qq'], comm['authst'], comm['tmeLoginType']), ('qqmusic', '11', '123456', 'Q_H_TEST', '2'))
        r = self.run_set(False)
        self.assertEqual((r['ok'], r['liked'], self.calls[-1][1]['method']), (True, False, 'DelSonglist'))
        self.assertEqual(sum(1 for c in self.calls if c[1]['module'] == 'music.pf_song_detail_svr'), 1)   # the id is cached

    def test_wechat_login_type(self):
        self.run_set(True, cfg=dict(CFG, qm_keyst='W_X_TEST', user_profile={'loginType': 'wx'}))
        self.assertEqual(self.calls[-1][0]['tmeLoginType'], '1')

    def test_errors(self):
        r = self.run_set(True, codes=[1000])
        self.assertEqual((r['ok'], r.get('login')), (False, False))
        self.assertIn('重新扫码', r['error'])
        r = self.run_set(True, codes=[1000, 0], refresh=True)        # a silent refresh, then it works
        self.assertTrue(r['ok'])
        r = self.run_set(True, codes=[80105])
        self.assertFalse(r['ok'])
        self.assertIn('80105', r['error'])
        r = self.run_set(True, cfg={'uin': '', 'qm_keyst': ''})
        self.assertEqual((r['ok'], r.get('login')), (False, False))
        with mock.patch.object(Q, 'load_config', return_value=dict(CFG)), \
                mock.patch.object(Q, '_qq_musicu_one', side_effect=OSError('down')):
            self.assertEqual(Q.qq_fav_set('001fsNdn1zuZnA', True)['ok'], False)
            self.assertIsNone(Q.qq_fav_state('001fsNdn1zuZnA'))

    def test_state(self):
        with mock.patch.object(Q, 'load_config', return_value=dict(CFG)), mock.patch.object(Q, '_qq_musicu_one', self.fake):
            self.assertIs(Q.qq_fav_state('001fsNdn1zuZnA'), True)
        with mock.patch.object(Q, 'load_config', return_value={'uin': '1'}):
            self.assertIsNone(Q.qq_fav_state('001fsNdn1zuZnA'))

    def test_http_route(self):
        from aiohttp.test_utils import TestClient, TestServer

        async def go():
            eng = object.__new__(Q.QQMusicEngine)
            with mock.patch.object(Q, 'engine', eng, create=True), \
                    mock.patch.object(Q, 'qq_fav_state', return_value=True), \
                    mock.patch.object(Q, 'qq_fav_set', side_effect=lambda mid, on: {'ok': True, 'mid': mid, 'liked': on}):
                app = Q.make_app()
                app.on_startup.clear()   # no background watchers in a test
                app.on_cleanup.clear()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    h = {'Host': '127.0.0.1:8091'}
                    r = await client.get('/api/music/fav?mid=001fsNdn1zuZnA', headers=h)
                    a = await r.json()
                    r = await client.post('/api/music/fav', json={'mid': '001fsNdn1zuZnA', 'on': False}, headers=h)
                    b = await r.json()
                    bad = (await client.post('/api/music/fav', json={'mid': '../x', 'on': True}, headers=h)).status
                    bad2 = (await client.post('/api/music/fav', json={'mid': '001fsNdn1zuZnA', 'on': 'yes'}, headers=h)).status
                    return a, b, bad, bad2
                finally:
                    await client.close()
        a, b, bad, bad2 = asyncio.run(go())
        self.assertEqual((a['liked'], b['liked'], bad, bad2), (True, False, 400, 400))


if __name__ == '__main__':
    unittest.main(verbosity=2)
