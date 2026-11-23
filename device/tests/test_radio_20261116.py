#!/opt/thinksmart-addons/venv/bin/python
"""Automated test suite for Radio R3: National/Local/HK-MO FM, Playable Albums, Title Hygiene, Fast Live Tune, Resume & Episode Picker (2026-11-16)."""
import importlib.util
import inspect
import os
import pathlib
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

import os as _ts_os
import pathlib as _ts_pathlib

STAGE_ROOT = _ts_pathlib.Path(_ts_os.environ.get('STAGE_ROOT', '/'))


def _p(abs_path: str) -> _ts_pathlib.Path:
    if str(STAGE_ROOT) != '/':
        cand = STAGE_ROOT / abs_path.lstrip('/')
        if cand.exists() or cand.parent.exists():
            return cand
    return _ts_pathlib.Path(abs_path)


sys.path.insert(0, str(_p('/usr/local/bin')))

from aiohttp.test_utils import AioHTTPTestCase


def _load_mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


qm_mod = _load_mod('ts_qm_radio_test', str(_p('/usr/local/bin/thinksmart-qqmusic.py')))
home_mod = _load_mod('ts_home_radio_test', str(_p('/usr/local/bin/thinksmart-home.py')))
sc_mod = _load_mod('ts_sc_radio_test', str(_p('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py')))


class TestRadioCatalogHygieneAndVoice(unittest.TestCase):
    def test_catalog_structure_even_grid_no_foreign_and_safe_urls(self):
        self.assertGreaterEqual(len(qm_mod.RADIO_STATIONS), 74)
        ids = [s['id'] for s in qm_mod.RADIO_STATIONS]
        self.assertEqual(len(ids), len(set(ids)))
        cats = {s['cat'] for s in qm_mod.RADIO_STATIONS}
        self.assertEqual(cats, {'fm', 'music_fm', 'kids', 'crosstalk'})
        for c in ('fm', 'music_fm', 'kids', 'crosstalk'):
            n_cat = sum(1 for s in qm_mod.RADIO_STATIONS if s['cat'] == c)
            self.assertEqual(n_cat % 2, 0, f'Category {c} count {n_cat} should be even for 2-column grid')
        self.assertGreaterEqual(sum(1 for s in qm_mod.RADIO_STATIONS if s['cat'] == 'music_fm'), 26)
        for req_id in (
            'cnr_zgzs', 'tj_xiangsheng', 'cnr_xczs', 'bj_1039', 'gd_1262',
            'cnr_yyzs', 'xm_2629', 'asia_pop', 'asia_classic', 'asia_yue', 'asia_hit',
            'sh_1017', 'sh_1037', 'gd_993', 'sz_971', 'bj_974', 'qt_4875',
            'cnr_dszs', 'sh_947', 'qt_jazz', 'qt_4804', 'qt_4915', 'zj_968', 'js_897', 'sc_955', 'cq_1947', 'cs_886',
            'sh_990', 'gd_914', 'zj_93', 'sd_xwgb', 'hb_1038', 'nm_cyzs',
            'cnr_dwq', 'hk_rthk1', 'hk_rthk2', 'hk_rthk_pth', 'mo_fm995',
            'kids_mixiaoquan', 'kids_oxford', 'kids_nce', 'kids_en_listen', 'kids_poem',
            'kids_shenglv', 'kids_holmes', 'kids_honglou', 'kids_dino',
            'ct_guodegang', 'ct_liubaorui', 'ct_sanguo', 'ct_shuihu', 'ct_pingshu_mix', 'ct_yangjia',
        ):
            self.assertIn(req_id, qm_mod.RADIO_BY_ID)
        for s in qm_mod.RADIO_STATIONS:
            u_low = s.get('url', '').lower()
            for banned in ('somafm', 'bbc.co', 'npr.org', 'voanews', 'nhk.or.jp'):
                self.assertNotIn(banned, u_low)
            self.assertTrue(s['cover'].startswith('data:image/svg+xml;base64,'))
            if s['kind'] == 'live':
                self.assertTrue(qm_mod._is_safe_stream_url(s['url']), f"Unsafe URL: {s['url']}")
                self.assertTrue(s['url'].startswith('https://'), f"Live stream should be HTTPS AAC/HLS: {s['url']}")
                self.assertNotIn('64k.mp3', s['url'])

    def test_ep_title_cleaner_dedup_and_graded_sorter(self):
        cln = qm_mod._clean_radio_ep_title
        self.assertEqual(cln('我是小学生01米小圈就是我 (DJ长音频)'), '我是小学生01米小圈就是我')
        self.assertEqual(cln('三国演义0001（DJ长音频）'), '三国演义0001')
        self.assertEqual(cln('【6岁+学成语】坐井观天【宝宝巴士国学-成语故事'), '坐井观天')
        self.assertEqual(cln('第1集_女娲顽石到人间.1mp3'), '第1集 女娲顽石到人间')

        ext = qm_mod._extract_ep_num
        self.assertLess(ext('牛津树1-05 A Good Trick'), ext('牛津树1-25 The Headache'))
        self.assertLess(ext('牛津树1-40 Reds and Blues'), ext('牛津树2-01 New Trainer'))
        self.assertLess(ext('Book 1 Level A'), ext('Book 53 Level E The Surprise'))

        fake_results = [
            {'mid': 'pop1', 'title': '滚滚长江东逝水', 'artist': '杨洪基', 'duration': 181, 'cover': '', 'url': 'https://a.qq.com/p.m4a'},
            {'mid': 'ger1', 'title': 'Kapitel 2 - Teil 47', 'artist': 'Sherlock', 'duration': 600, 'cover': '', 'url': 'https://a.qq.com/g.m4a'},
            {'mid': 'ps11', 'title': '三国演义 第011回 刘皇叔北海救孔融 (DJ长音频)', 'artist': '袁阔成', 'duration': 1750, 'cover': '', 'url': 'https://a.qq.com/11.m4a'},
            {'mid': 'ps2', 'title': '三国演义 第002回 张翼德怒鞭督邮', 'artist': '袁阔成', 'duration': 1741, 'cover': '', 'url': 'https://a.qq.com/2.m4a'},
            {'mid': 'ps2_dup', 'title': '三国演义 第002回 张翼德怒鞭督邮 (DJ版)', 'artist': '袁阔成', 'duration': 1740, 'cover': '', 'url': 'https://a.qq.com/2b.m4a'},
            {'mid': 'ps1', 'title': '三国演义 第001回 桃园豪杰三结义', 'artist': '袁阔成', 'duration': 1734, 'cover': '', 'url': 'https://a.qq.com/1.m4a'},
        ]
        qm_mod._RADIO_CH_CACHE.clear()
        with mock.patch.object(qm_mod, 'qq_search', return_value=fake_results), \
             mock.patch.object(qm_mod, 'qq_album_tracks', return_value=[]):
            tracks = qm_mod.resolve_radio_channel_tracks(qm_mod.RADIO_BY_ID['ct_sanguo'], shuffle=False)
        self.assertEqual([t['mid'] for t in tracks], ['ps1', 'ps2', 'ps11'])
        self.assertNotIn('DJ长音频', tracks[2]['title'])

    def test_voice_query_matching_and_fast_live_decoder_flags(self):
        m = qm_mod.match_radio_voice_query
        self.assertEqual(m('播放中国之声', '中国之声'), 'cnr_zgzs')
        self.assertEqual(m('放天津相声广播', '天津相声广播'), 'tj_xiangsheng')
        self.assertEqual(m('放上海新闻广播', '上海新闻广播'), 'sh_990')
        self.assertEqual(m('听浙江交通之声', '浙江交通之声'), 'zj_93')
        self.assertEqual(m('播放香港普通话台', '香港普通话台'), 'hk_rthk_pth')
        self.assertEqual(m('放澳门电台', '澳门电台'), 'mo_fm995')
        self.assertEqual(m('放音乐电台', '音乐电台'), 'cnr_yyzs')
        self.assertEqual(m('播放亚洲音乐台', '亚洲音乐台'), 'asia_pop')
        self.assertEqual(m('放爵士轻音乐台', '爵士轻音乐台'), 'qt_jazz')
        self.assertEqual(m('放粤语音乐台', '粤语音乐台'), 'asia_yue')
        self.assertEqual(m('放米小圈上学记', '米小圈上学记'), 'kids_mixiaoquan')
        self.assertEqual(m('放恐龙故事', '恐龙故事'), 'kids_dino')
        self.assertEqual(m('继续听三国演义评书', '继续听三国演义评书'), 'ct_sanguo')
        self.assertIsNone(m('播放音乐', '音乐'))
        self.assertIsNone(m('播放周杰伦的晴天', '周杰伦的晴天'))

        dec_src = inspect.getsource(qm_mod.QQMusicEngine._decode_worker)
        self.assertIn('nobuffer', dec_src)
        self.assertIn('32768', dec_src)
        self.assertIn('300000', dec_src)
        self.assertIn("'-vn'", dec_src)

        af_live = qm_mod._build_audio_filter({'radio_id': 'cnr_zgzs', 'is_live': True}, 'hifi')
        self.assertIn('highpass=f=52', af_live)
        self.assertIn('acompressor', af_live)
        self.assertIn('alimiter', af_live)
        af_spk = qm_mod._build_audio_filter({'radio_id': 'ct_sanguo', 'is_live': False}, 'hifi')
        self.assertIn('highpass=f=65', af_spk)
        af_song = qm_mod._build_audio_filter({'mid': 'abc', 'radio_id': ''}, 'hifi')
        self.assertNotIn('acompressor', af_song)


class TestRadioHttpResumeClearAndLoopGuardSilent(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        tp = pathlib.Path(self._td.name)
        self._orig_cfg_p = qm_mod.CONFIG_PATH
        self._orig_sess_p = qm_mod.SESSION_PATH
        self._orig_prog_p = qm_mod.RADIO_PROG_PATH
        qm_mod.CONFIG_PATH = tp / 'config.json'
        qm_mod.SESSION_PATH = tp / 'session.json'
        qm_mod.RADIO_PROG_PATH = tp / 'radio_progress.json'
        qm_mod._CFG_CACHE = None
        qm_mod._CFG_SIG = None
        qm_mod.save_config({'uin': '123456', 'qm_keyst': 'keep_me', 'cookies_str': '', 'quality_level': 'lossless'})

        self.test_engine = qm_mod.QQMusicEngine()
        self.test_engine.play_mode = 'shuffle'  # simulate prior song shuffle state!
        self._dec_patch = mock.patch.object(self.test_engine, '_decode_worker', return_value=None)
        self._dec_patch.start()
        self._pre_patch = mock.patch.object(qm_mod, '_prewarm_live_radio_hosts', return_value=None)
        self._pre_patch.start()
        self._eng_patch = mock.patch.object(qm_mod, 'engine', self.test_engine)
        self._eng_patch.start()
        self._watch_patch = mock.patch.object(qm_mod, 'watch_xiaozhi_events', side_effect=self._noop_coro)
        self._watch_patch.start()
        return qm_mod.make_app()

    @staticmethod
    async def _noop_coro():
        return

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self._watch_patch.stop()
        self._eng_patch.stop()
        self._pre_patch.stop()
        self._dec_patch.stop()
        qm_mod.CONFIG_PATH = self._orig_cfg_p
        qm_mod.SESSION_PATH = self._orig_sess_p
        qm_mod.RADIO_PROG_PATH = self._orig_prog_p
        qm_mod._CFG_CACHE = None
        qm_mod._CFG_SIG = None
        self._td.cleanup()

    async def test_live_play_episode_picker_resume_loop_guard_and_clear(self):
        H = {'Host': '127.0.0.1:8091'}
        fake_eps = [
            {'mid': 'sg01', 'title': '三国演义 第001回', 'artist': '袁阔成', 'duration': 1700, 'cover': '', 'url': 'https://a.qq.com/1.m4a', '_url_ts': 9e12},
            {'mid': 'sg02', 'title': '三国演义 第002回', 'artist': '袁阔成', 'duration': 1720, 'cover': '', 'url': 'https://a.qq.com/2.m4a', '_url_ts': 9e12},
            {'mid': 'sg03', 'title': '三国演义 第003回', 'artist': '袁阔成', 'duration': 1750, 'cover': '', 'url': 'https://a.qq.com/3.m4a', '_url_ts': 9e12},
        ]
        qm_mod._RADIO_CH_CACHE.clear()
        with mock.patch.object(qm_mod, 'qq_search', return_value=fake_eps), \
             mock.patch.object(qm_mod, 'qq_album_tracks', return_value=[]):
            rep = await self.client.get('/api/music/radio/episodes?id=ct_sanguo', headers=H)
            self.assertEqual(rep.status, 200)
            jep = await rep.json()
            self.assertTrue(jep['ok'])
            self.assertEqual(len(jep['episodes']), 3)

            # Playing serialized channel must switch play_mode from 'shuffle' to 'loop'!
            rp1 = await self.client.post('/api/music/radio/play', headers=H, json={'id': 'ct_sanguo', 'track_index': 1})
            jp1 = await rp1.json()
            self.assertTrue(jp1['ok'])
            self.assertEqual(self.test_engine.play_mode, 'loop')

            r_sk = await self.client.post('/api/music/seek', headers=H, json={'sec': 745})
            self.assertTrue((await r_sk.json())['ok'])

            # Switch to live radio -> saves ct_sanguo progress (0600) and keeps 'loop' mode
            self.test_engine.play_mode = 'single'
            rp_live = await self.client.post('/api/music/radio/play', headers=H, json={'id': 'zj_93'})
            self.assertTrue((await rp_live.json())['ok'])
            self.assertEqual(self.test_engine.play_mode, 'loop')
            self.assertEqual(stat.S_IMODE(os.lstat(qm_mod.RADIO_PROG_PATH).st_mode), 0o600)

            # Simulate restart & auto-resume
            eng_reloaded = qm_mod.QQMusicEngine()
            with mock.patch.object(eng_reloaded, '_decode_worker', return_value=None):
                res_resume = eng_reloaded.play_radio('ct_sanguo')
                self.assertTrue(res_resume['radio']['resumed'])
                self.assertEqual(res_resume['radio']['index'], 1)
                self.assertAlmostEqual(res_resume['radio']['sec'], 745.0, places=0)

            # Clear progress via POST /api/music/radio/progress
            rc = await self.client.post('/api/music/radio/progress', headers=H, json={'id': 'ct_sanguo', 'clear': True})
            self.assertTrue((await rc.json())['ok'])
            cat_j = await (await self.client.get('/api/music/radio', headers=H)).json()
            sg_card = next(s for s in cat_j['stations'] if s['id'] == 'ct_sanguo')
            self.assertIsNone(sg_card['resume'])
            self.assertEqual(cat_j['radio_audio_mode'], 'hifi')

            # Radio audio mode switching API (voice -> hifi)
            rm1 = await self.client.post('/api/music/radio/audio_mode', headers=H, json={'mode': 'voice'})
            self.assertEqual((await rm1.json())['radio_audio_mode'], 'voice')
            st_j = await (await self.client.get('/api/music/status', headers=H)).json()
            self.assertIn('清澈人声', st_j['track']['quality'])
            rm2 = await self.client.post('/api/music/radio/audio_mode', headers=H, json={'mode': 'hifi'})
            self.assertEqual((await rm2.json())['radio_audio_mode'], 'hifi')
            st_j2 = await (await self.client.get('/api/music/status', headers=H)).json()
            self.assertIn('臻享增强', st_j2['track']['quality'])

            # Audit fixes:
            # 1) Persisted loop mode across reload when force_loop applied
            self.assertEqual(qm_mod.load_config().get('play_mode'), 'loop')
            # 2) Track-based sleep timer auto-converts to time mode on live radio
            r_slp = await self.client.post('/api/music/sleep', headers=H, json={'tracks': 2})
            j_slp = await r_slp.json()
            self.assertTrue(j_slp['ok'])
            self.assertEqual(j_slp['sleep']['mode'], 'time')
            await self.client.post('/api/music/sleep', headers=H, json={'cancel': True})
            # 3) Shuffling/reposting live & channel queues via /api/music/play_tracks preserves radio_id, is_live, cover & url
            q_live = st_j2.get('queue') or (await (await self.client.get('/api/music/status?qseq=0', headers=H)).json())['queue']
            r_pt = await self.client.post('/api/music/play_tracks', headers=H, json={'tracks': q_live[:4], 'index': 1})
            self.assertTrue((await r_pt.json())['ok'])
            cur_live = self.test_engine.current_track()
            self.assertTrue(cur_live['is_live'])
            self.assertTrue(cur_live['radio_id'])
            self.assertTrue(cur_live['url'].startswith('https://'))
            # 4) Voice stop/open-ui commands for radio do not fall through to song search
            r_ui = await self.client.post('/api/music/play', headers=H, json={'query': '打开电台界面'})
            j_ui = await r_ui.json()
            self.assertTrue(j_ui.get('mode_only'))
            st_lite = await (await self.client.get('/api/music/status?lite=1', headers=H)).json()
            self.assertEqual(st_lite['open_ui_tab'], 'radio')
            self.assertTrue(st_lite['track']['desc'])
            r_stop_r = await self.client.post('/api/music/play', headers=H, json={'query': '关掉电台'})
            self.assertTrue((await r_stop_r.json()).get('mode_only'))
            self.assertTrue(self.test_engine.paused)


class TestRadioStaticUiAndTypographyInvariants(unittest.TestCase):
    def test_ui_r3_features_and_integer_px_typography(self):
        qm_ui = _p('/usr/local/share/thinksmart-qqmusic/index.html').read_text('utf-8')
        for tok in (
            'id="tabBtnRadio"', 'id="viewRadio"', 'id="radioGrid"', 'id="radioEpBox"',
            'id="radioRecentWrap"', 'id="repBtnSort"', 'id="btnRadioDsp"', 'id="radioQualSeg"',
            'setRadioAudioMode', 'togglePickerSort', 'clearRadioProgress',
            'openRadioEpisodes', 'playRadioEpisode', 'data-rcat="music_fm"', '高音质音乐电台', '实时直播中',
        ):
            self.assertIn(tok, qm_ui)
        self.assertNotIn('data-rcat="local_hk"', qm_ui)

        # Verify R3..R5 CSS block strictly follows 400/700 font-weight and integer px font-sizes (including Account overrides)
        r3_idx = qm_ui.index('2026-11-16 R3:')
        r3_css = qm_ui[r3_idx:qm_ui.index('</style>', r3_idx)]
        self.assertIn('.acc-setting-card h3', r3_css)
        self.assertIn('.qual-btn {', r3_css)
        weights = set(re.findall(r'font-weight:\s*([^;}\s]+)', r3_css))
        self.assertTrue(weights.issubset({'400', '700'}), f'Disallowed font-weights in R3 CSS: {weights}')
        frac_px = re.findall(r'\d+\.\d+px', r3_css)
        self.assertEqual(frac_px, [], f'Fractional px in R3 CSS: {frac_px}')

        home_ui = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('!t.is_live', home_ui)
        self.assertIn("subCmd: isRadioNow ? 'openRadio' : ''", home_ui)

        kiosk_ov = _p('/usr/local/share/thinksmart-qqmusic/kiosk_overlay.js').read_text('utf-8')
        self.assertIn('openRadio', kiosk_ov)

        play_tool = next(t for t in sc_mod.MUSIC_TOOLS if t['name'] == 'self.music.play')
        self.assertIn('天津相声广播', play_tool['description'])
        self.assertIn('上海新闻广播', play_tool['description'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
