#!/usr/bin/env python3
"""Unit tests for NetEase Cloud Music integration in thinksmart-qqmusic.py."""
import ast
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

if 'aiohttp' not in sys.modules:
    _aiohttp = types.ModuleType('aiohttp')
    _aiohttp_web = types.ModuleType('aiohttp.web')
    _aiohttp_web.Application = object
    _aiohttp_web.Response = object
    _aiohttp_web.HTTPBadRequest = Exception
    _aiohttp_web.json_response = lambda *a, **kw: None
    _aiohttp.web = _aiohttp_web
    sys.modules['aiohttp'] = _aiohttp
    sys.modules['aiohttp.web'] = _aiohttp_web

SRC = pathlib.Path(__file__).resolve().parents[1] / 'qqmusic' / 'thinksmart-qqmusic.py'


def load_module():
    t = ast.parse(SRC.read_text(encoding='utf-8'))
    t.body = [
        n for n in t.body
        if not (isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == 'engine' for x in n.targets))
    ]
    t.body = [
        n for n in t.body
        if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and getattr(n.test.left, 'id', '') == '__name__')
    ]
    m = types.ModuleType('thinksmart_qqmusic_test')
    m.__file__ = str(SRC)
    exec(compile(t, str(SRC), 'exec'), m.__dict__)
    return m


Q = load_module()


class NeteaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_cfg_path = Q.CONFIG_PATH
        Q.CONFIG_PATH = pathlib.Path(self.tmpdir.name) / 'config.json'
        Q._CFG_CACHE = None
        Q._CFG_SIG = None
        Q._LYRIC_CACHE.clear()
        with Q._NETEASE_LIKED_LOCK:
            Q._NETEASE_LIKED_CACHE.update({'uid': '', 'ts': 0.0, 'ids': set()})
        self._patches = []
        if not hasattr(os, 'getuid'):
            self._patches.append(mock.patch.object(Q.os, 'getuid', return_value=0, create=True))
            self._patches.append(mock.patch.object(Q.os, 'fchmod', lambda *a, **kw: None, create=True))
            self._patches.append(mock.patch.object(Q.os, 'chmod', lambda *a, **kw: None, create=True))
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        Q.CONFIG_PATH = self.orig_cfg_path
        Q._CFG_CACHE = None
        Q._CFG_SIG = None
        self.tmpdir.cleanup()

    def test_mid_and_url_normalization(self):
        self.assertTrue(Q._is_netease_mid('ncm:123456'))
        self.assertFalse(Q._is_netease_mid('002hY0WV09MJBV'))
        self.assertFalse(Q._is_netease_mid('ncm:abc'))
        self.assertEqual(Q._netease_song_id('ncm:987654321'), '987654321')
        self.assertEqual(Q._netease_song_id('bad'), '')

        self.assertEqual(Q._normalize_netease_api_url('http://192.168.100.1:3000/'), 'http://192.168.100.1:3000')
        self.assertEqual(Q._normalize_netease_api_url('https://ncm.local:3443/api/'), 'https://ncm.local:3443/api')
        self.assertEqual(Q._normalize_netease_api_url('ftp://192.168.100.1:3000'), '')
        self.assertEqual(Q._normalize_netease_api_url('http://user:pass@192.168.100.1:3000'), '')
        self.assertEqual(Q._normalize_netease_api_url('http://192.168.100.1:3000?a=1'), '')
        self.assertEqual(Q._normalize_netease_api_url('not a url\n'), '')

    def test_format_song_and_parse_lrc(self):
        s = {
            'id': 186016,
            'name': '晴天',
            'ar': [{'name': '周杰伦'}, {'name': '嘉宾'}],
            'al': {'name': '叶惠美', 'picUrl': 'http://p1.music.126.net/abc/123.jpg'},
            'dt': 269000,
            'fee': 1,
        }
        tr = Q._format_netease_song(s, default_q='SQ 无损 FLAC')
        self.assertEqual(tr['mid'], 'ncm:186016')
        self.assertEqual(tr['title'], '晴天')
        self.assertEqual(tr['artist'], '周杰伦 / 嘉宾')
        self.assertEqual(tr['album'], '叶惠美')
        self.assertEqual(tr['cover'], 'https://p1.music.126.net/abc/123.jpg')
        self.assertEqual(tr['thumb'], 'https://p1.music.126.net/abc/123.jpg?param=200y200')
        self.assertEqual(tr['duration'], 269)
        self.assertTrue(tr['vip'])
        self.assertEqual(tr['source'], '网易云')

        lrc = Q._parse_lrc_text('[00:12.34]第一句\n[00:01.00][00:25.50]副歌\n[offset:0]\n')
        self.assertEqual([x['time'] for x in lrc], [1.0, 12.34, 25.5])
        self.assertEqual([x['text'] for x in lrc], ['副歌', '第一句', '副歌'])

    def test_qr_create_check_and_logout_preserves_qq(self):
        Q.save_config({
            'uin': '10086',
            'qm_keyst': 'qq_secret',
            'cookies_str': 'uin=10086',
            'quality_level': 'lossless',
            'user_profile': {'userId': '10086', 'nickname': 'QQUser', 'loginType': 'qq'},
            'netease_api_url': 'http://192.168.100.1:3000',
            'netease_cookie': '',
            'netease_profile': None,
        })

        with mock.patch.object(Q, '_netease_api', side_effect=[
            {'code': 200, 'data': {'unikey': 'uni-123'}},
            {'code': 200, 'data': {'qrimg': 'data:image/png;base64,AAAA'}},
        ]):
            res = Q.netease_qr_create()
        self.assertTrue(res['ok'])
        self.assertEqual(res['qrsig'], 'uni-123')
        self.assertEqual(res['mode'], 'netease')

        with mock.patch.object(Q, '_netease_api', return_value={'code': 800, 'message': 'expired'}):
            self.assertEqual(Q.netease_qr_check('uni-123')['code'], 65)
        with mock.patch.object(Q, '_netease_api', return_value={'code': 801, 'message': 'wait'}):
            self.assertEqual(Q.netease_qr_check('uni-123')['code'], 66)
        with mock.patch.object(Q, '_netease_api', return_value={'code': 802, 'message': 'confirm'}):
            self.assertEqual(Q.netease_qr_check('uni-123')['code'], 67)

        with mock.patch.object(Q, '_netease_api', side_effect=[
            {'code': 803, 'cookie': 'MUSIC_U=abc123; __csrf=xyz'},
            {'data': {'code': 200, 'account': {'id': 7788, 'vipType': 11}, 'profile': {'userId': 7788, 'nickname': '黑胶大佬', 'avatarUrl': 'http://p2.music.126.net/ava.jpg', 'vipType': 11}}},
        ]):
            ok_res = Q.netease_qr_check('uni-123')
        self.assertTrue(ok_res['ok'])
        self.assertEqual(ok_res['code'], 0)
        self.assertTrue(ok_res['profile']['isVip'])
        cfg = Q.load_config()
        self.assertEqual(cfg['netease_cookie'], 'MUSIC_U=abc123; __csrf=xyz')
        self.assertEqual(cfg['uin'], '10086')
        self.assertEqual(cfg['qm_keyst'], 'qq_secret')

        Q.netease_logout()
        cfg2 = Q.load_config()
        self.assertEqual(cfg2['netease_cookie'], '')
        self.assertIsNone(cfg2['netease_profile'])
        self.assertEqual(cfg2['uin'], '10086')
        self.assertEqual(cfg2['netease_api_url'], 'http://192.168.100.1:3000')

    def test_playlists_tracks_url_lyric_search_and_fav(self):
        Q.save_config({
            'uin': '',
            'qm_keyst': '',
            'cookies_str': '',
            'quality_level': 'lossless',
            'user_profile': None,
            'netease_api_url': 'http://192.168.100.1:3000',
            'netease_cookie': 'MUSIC_U=vipcookie',
            'netease_profile': {'userId': '7788', 'nickname': '黑胶用户', 'isVip': True, 'loginType': 'netease'},
        })

        with mock.patch.object(Q, '_netease_api', return_value={
            'code': 200,
            'playlist': [
                {'id': 111, 'name': '我喜欢的音乐', 'trackCount': 42, 'specialType': 5, 'coverImgUrl': 'http://p1.music.126.net/c1.jpg'},
                {'id': 222, 'name': '夜跑歌单', 'trackCount': 15, 'specialType': 0, 'coverImgUrl': 'https://p1.music.126.net/c2.jpg'},
            ],
        }):
            pls = Q.netease_user_playlists()
        self.assertEqual(len(pls), 2)
        self.assertEqual(pls[0]['id'], 'ncm:111')
        self.assertTrue(pls[0]['name'].startswith('❤️'))
        self.assertEqual(pls[0]['source'], '网易云')
        self.assertEqual(pls[1]['id'], 'ncm:222')

        with mock.patch.object(Q, '_netease_api', return_value={
            'code': 200,
            'songs': [{'id': 999, 'name': '夜曲', 'ar': [{'name': '周杰伦'}], 'al': {'name': '十一月的萧邦', 'picUrl': 'http://p1.music.126.net/p.jpg'}, 'dt': 226000, 'fee': 1}],
        }):
            tracks = Q.netease_playlist_tracks('111')
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]['mid'], 'ncm:999')

        # Skip 30s preview (freeTrialInfo) at lossless, fall back to exhigh full URL
        with mock.patch.object(Q, '_netease_api', side_effect=[
            {'code': 200, 'data': [{'url': 'http://m7.music.126.net/trial.mp3', 'freeTrialInfo': {'start': 0, 'end': 30}}]},
            {'code': 200, 'data': [{'url': 'http://m8.music.126.net/full.mp3', 'level': 'exhigh', 'type': 'mp3', 'time': 226000, 'freeTrialInfo': None}]},
        ]):
            resolved = Q.resolve_netease_url('ncm:999')
        self.assertEqual(resolved['url'], 'http://m8.music.126.net/full.mp3')
        self.assertEqual(resolved['quality'], 'HQ 极高')
        self.assertEqual(resolved['duration'], 226)

        with mock.patch.object(Q, '_netease_api', return_value={
            'code': 200, 'lrc': {'lyric': '[00:05.00]为你弹奏萧邦的夜曲'}
        }):
            lrc = Q.netease_lyric('ncm:999')
        self.assertEqual(lrc, [{'time': 5.0, 'text': '为你弹奏萧邦的夜曲'}])

        with mock.patch.object(Q, '_netease_api', return_value={
            'code': 200,
            'result': {'songs': [{'id': 888, 'name': '稻香', 'ar': [{'name': '周杰伦'}], 'al': {'name': '魔杰座', 'picUrl': ''}, 'dt': 223000, 'fee': 8}]},
        }):
            srch = Q.netease_search('稻香', limit=10, page=1)
        self.assertEqual(srch[0]['mid'], 'ncm:888')

        with mock.patch.object(Q, '_netease_api', return_value={'code': 200, 'ids': [999, 888]}):
            self.assertTrue(Q.netease_fav_state('ncm:999'))
            self.assertFalse(Q.netease_fav_state('ncm:777'))
        with mock.patch.object(Q, '_netease_api', return_value={'code': 200}):
            fav_res = Q.netease_fav_set('ncm:777', True)
        self.assertTrue(fav_res['ok'])
        self.assertTrue(fav_res['liked'])
        self.assertTrue(Q.netease_fav_state('ncm:777'))

    def test_safe_cover_regex_accepts_netease_domains(self):
        src = SRC.read_text(encoding='utf-8')
        m = next((line for line in src.splitlines() if '_SAFE_COVER_RE =' in line), '')
        self.assertIn('126\\.net|163\\.com', m)


if __name__ == '__main__':
    unittest.main(verbosity=2)
