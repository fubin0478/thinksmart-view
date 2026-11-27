#!/opt/thinksmart-addons/venv/bin/python
"""Regression suite (2026-11-20 R63): DocCam read-aloud completeness, stall failover, 1-tap snap retry, & zero-Gemini GPT/DeepSeek routing."""
import asyncio
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

TUTOR_PY = os.environ.get('TUTOR_PY', '/usr/local/bin/thinksmart-tutor.py')
SHARE = pathlib.Path(os.environ.get('TUTOR_SHARE', '/usr/local/share/thinksmart-tutor'))


def _load_tutor():
    spec = importlib.util.spec_from_file_location('ts_tutor_r63_test', TUTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['ts_tutor_r63_test'] = mod
    spec.loader.exec_module(mod)
    return mod


tutor = _load_tutor()

NOON_Q1 = (
    '如图，在等边△ABC中，∠ABC与∠ACB的平分线相交于点O，且OD∥AB，OE∥AC。'
    '（1）试判定△ODE的形状，并说明你的理由。（2）若BC＝10，求△ODE的周长。'
)
NOON_Q2 = '如图，DE⊥AB于E，DF⊥AC于F，若BD=CD、BE=CF。求证：AD平分∠BAC。'


class TestNoonDocCamUnit(unittest.TestCase):
    def test_geometry_never_misclassified_as_english(self):
        self.assertEqual(tutor.detect_subject(NOON_Q1), 'math')
        self.assertEqual(tutor.detect_subject(NOON_Q1, teach_mode='diagnose_draft'), 'math')
        self.assertEqual(tutor.detect_subject(NOON_Q2), 'math')
        self.assertEqual(
            tutor.detect_subject('如图，在▱EFGH中，连接EG、FH交于点O，求证：△OEH≌△OFG（可按SAS或ASA判定）。'),
            'math',
        )
        fake_steps = [
            {'step': 1, 'title': '角平分线与平行', 'board': '∠OBD=∠ODB ⇒ BD=OD', 'quiz': {'mode': 'choice', 'q': '选哪项？', 'opts': ['OD=BD', 'OD=AB', 'OE=BC'], 'ans': 0}},
            {'step': 4, 'title': '复盘', 'board': '△ODE是等边三角形，周长=10'},
        ]
        self.assertIsNone(tutor.extract_read_aloud_target(NOON_Q1, fake_steps))
        self.assertIsNone(tutor.extract_read_aloud_target(NOON_Q2, fake_steps))

        # Genuine English grammar problems still classify as english & extract read_aloud_target
        en_q = 'He ___ (live) in Shanghai since 2021. Please choose has lived or lived.'
        self.assertEqual(tutor.detect_subject(en_q), 'english')
        en_target = tutor.extract_read_aloud_target(en_q, [
            {'step': 3, 'title': '选时态', 'board': 'has lived', 'quiz': {'mode': 'choice', 'q': '选哪项', 'opts': ['has lived', 'lived', 'lives'], 'ans': 0}},
            {'step': 4, 'title': '复盘', 'board': 'He has lived in Shanghai since 2021.'},
        ])
        self.assertIsNotNone(en_target)
        self.assertEqual(en_target['kind'], 'english')

    def test_public_mistakes_heals_stale_english_subject_on_geometry(self):
        with tempfile.TemporaryDirectory() as td:
            tp = pathlib.Path(td)
            old_s, old_h, old_p = tutor.STATE_DIR, tutor.HIST_PATH, tutor.PREFS_PATH
            try:
                tutor.STATE_DIR = tp
                tutor.HIST_PATH = tp / 'history.json'
                tutor.PREFS_PATH = tp / 'prefs.json'
                (tp / 'mistake_book.json').write_text(json.dumps([{
                    'id': 'mb_f022e4862a',
                    'title': '辅助线与周长',
                    'question': NOON_Q1,
                    'subject': 'english',
                    'teach_mode': 'diagnose_draft',
                    'weak_step': 1,
                    'stage': 0,
                    'graduated': False,
                    'created_at': 1795146000,
                    'updated_at': 1795146000,
                    'next_review_at': 1795232400,
                }], ensure_ascii=False), encoding='utf-8')
                eng = tutor.TutorEngine()
                pub_m = eng.public_mistakes()
                self.assertEqual(len(pub_m), 1)
                self.assertEqual(pub_m[0]['subject'], 'math')
            finally:
                tutor.STATE_DIR, tutor.HIST_PATH, tutor.PREFS_PATH = old_s, old_h, old_p

    def test_full_question_tts_and_step0_not_truncated(self):
        spoken = tutor.clean_math_for_tts(NOON_Q1)
        self.assertIn('三角形ABC', spoken)
        self.assertIn('平行于', spoken)
        self.assertIn('求三角形ODE的周长', spoken)
        sc0 = tutor.build_step0_scaffold(NOON_Q1, teach_mode='standard', from_ocr=True)
        self.assertIn('求三角形ODE的周长', sc0['say'])

    def test_zero_gemini_models_in_all_chains_and_targets(self):
        with tempfile.TemporaryDirectory() as td:
            tp = pathlib.Path(td)
            cfg = tp / 'hub_cfg.json'
            cfg.write_text(json.dumps({
                'LLM': {
                    'OpenAILLM': {'base_url': 'http://127.0.0.1:19999/v1', 'api_key': 'k1', 'model_name': 'gpt-6.1-terra'},
                    'GeminiAPI': {'base_url': 'http://127.0.0.1:19999/v1', 'api_key': 'k2', 'model_name': 'gemini-4-hydrogen'},
                    'DeepSeekLLM': {'url': 'http://127.0.0.1:19999/v1', 'api_key': 'k3', 'model_name': 'deepseek-v4.1-flash-sg'},
                }
            }), encoding='utf-8')
            old_c, old_h, old_p = tutor.HUB_CFG_PATH, tutor.HIST_PATH, tutor.PREFS_PATH
            try:
                tutor.HUB_CFG_PATH = cfg
                tutor.HIST_PATH = tp / 'hist.json'
                tutor.PREFS_PATH = tp / 'prefs.json'
                eng = tutor.TutorEngine()
                for kind in ('fast', 'deep', 'backup', 'aux', 'geo'):
                    _, _, chain = eng._resolve_llm_chain(kind)
                    for m_name, extra in chain:
                        self.assertNotIn('gemini', m_name.lower(), f'Gemini leaked in chain({kind}): {m_name}')
                        if m_name.startswith('deepseek'):
                            self.assertEqual(extra, {})
                    targets = eng._iter_llm_targets(kind)
                    for _, _, m_t, ex_t in targets:
                        self.assertNotIn('gemini', m_t.lower(), f'Gemini leaked in targets({kind}): {m_t}')
                        if m_t.startswith('deepseek'):
                            self.assertEqual(ex_t, {})
            finally:
                tutor.HUB_CFG_PATH, tutor.HIST_PATH, tutor.PREFS_PATH = old_c, old_h, old_p

    def test_frontend_invariants_and_zero_gemini(self):
        html = (SHARE / 'index.html').read_text('utf-8')
        css = html[html.index('<style>'):html.index('</style>')]
        self.assertNotIn('gemini', html.lower())
        for sym in (
            'function ddChunks(', 'function ddPrefetchNext(', 'ddSwitching', 'function sentencesFrom(',
            'ddPendingQuestionSid', 'function hasRealSteps(', 'window.retryFailedSession',
            'function syncWaitEscapeCard(', 'function addErrorCard(',
        ):
            self.assertIn(sym, html)
        self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', css)) <= {'400', '700'})
        self.assertEqual(re.findall(r'\d+\.\d+px', css), [])


class TestDocCamFailoverAndRetryHttp(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        tp = pathlib.Path(self._td.name)
        self._saved = {k: getattr(tutor, k) for k in ('STATE_DIR', 'SNAP_DIR', 'HIST_PATH', 'PREFS_PATH', 'HUB_CFG_PATH')}
        self._had_sig = 'TUTOR_OPEN_SIGNAL' in os.environ
        self._orig_sig = os.environ.get('TUTOR_OPEN_SIGNAL')
        os.environ['TUTOR_OPEN_SIGNAL'] = str(tp / 'tutor-open.json')
        tutor.STATE_DIR = tp / 'state'
        tutor.SNAP_DIR = tutor.STATE_DIR / 'snaps'
        tutor.HIST_PATH = tutor.STATE_DIR / 'history.json'
        tutor.PREFS_PATH = tutor.STATE_DIR / 'prefs.json'
        self.tts_fail = False
        self.stall_terra = False
        self.called_models = []

        up = web.Application()

        async def mock_chat(req):
            body = await req.json()
            m_name = body.get('model') or ''
            self.called_models.append(m_name)
            if body.get('stream'):
                resp = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
                await resp.prepare(req)
                if m_name == 'gpt-6.1-terra' and self.stall_terra:
                    for _ in range(8):
                        await resp.write(b': keep-alive\n\n')
                        await asyncio.sleep(0.08)
                    return resp
                steps = [
                    {'step': 1, 'ocr': NOON_Q2, 'title': '找全等直角△', 'say': '先看两个直角三角形', 'board': 'Rt△BED 与 Rt△CFD：BD=CD，BE=CF', 'quiz': {'mode': 'choice', 'q': '用哪条全等判定？', 'opts': ['HL', 'AAA', 'SSA'], 'ans': 0}, 'draw': []},
                    {'step': 2, 'title': '证垂线段相等', 'say': '全等推出DE=DF', 'board': 'Rt△BED≌Rt△CFD(HL) ⇒ DE=DF', 'quiz': {'mode': 'choice', 'q': '由此可得哪两条线段相等？', 'opts': ['DE=DF', 'AB=AC', 'AE=BE'], 'ans': 0}, 'draw': []},
                    {'step': 3, 'title': '逆定理锁角平分线', 'say': '到角两边距离相等的点在平分线上', 'board': 'DE⊥AB，DF⊥AC，DE=DF ⇒ AD平分∠BAC', 'quiz': {'mode': 'choice', 'force_choice': True, 'q': '根据哪条定理证得AD平分∠BAC？', 'opts': ['角平分线判定定理', '中位线定理', '勾股定理'], 'ans': 0}, 'draw': []},
                    {'step': 4, 'title': '满分书写复盘', 'say': 'HL全等配角平分线逆定理，一步到位！', 'board': '证毕：HL证全等 ⇒ DE=DF ⇒ AD平分∠BAC ✓', 'draw': []},
                ]
                for st in steps:
                    pkt = {'choices': [{'delta': {'content': json.dumps(st, ensure_ascii=False) + '\n'}}]}
                    await resp.write(('data: ' + json.dumps(pkt, ensure_ascii=False) + '\n\n').encode('utf-8'))
                await resp.write(b'data: [DONE]\n\n')
                await resp.write_eof()
                return resp
            return web.json_response({'choices': [{'message': {'content': json.dumps({'title': '角平分线证明', 'ocr': NOON_Q2, 'say': '一起来看这道几何证明题'}, ensure_ascii=False)}}]})

        async def mock_tts(_req):
            if self.tts_fail:
                return web.Response(status=503, text='down')
            return web.Response(body=b'\x02\x00' * 4800, content_type='application/octet-stream')

        up.router.add_post('/v1/chat/completions', mock_chat)
        up.router.add_post('/tts', mock_tts)
        self._up_srv = TestServer(up)
        await self._up_srv.start_server()
        up_base = f'http://127.0.0.1:{self._up_srv.port}'

        cfg = tp / 'gw.json'
        cfg.write_text(json.dumps({
            'LLM': {
                'OpenAILLM': {'base_url': up_base, 'api_key': 'k', 'model_name': 'gpt-6.1-terra'},
                'GeminiAPI': {'base_url': up_base, 'api_key': 'k', 'model_name': 'gemini-4-hydrogen'},
                'DeepSeekLLM': {'url': up_base, 'api_key': 'k', 'model_name': 'deepseek-v4.1-flash-sg'},
            },
            'TTS': {'IndexStreamTTS': {'api_url': f'{up_base}/tts', 'voice': 'xiaohe'}},
        }), encoding='utf-8')
        tutor.HUB_CFG_PATH = cfg
        self.eng = tutor.TutorEngine()
        self._p_music = mock.patch.object(self.eng, 'pause_music_if_playing', new=mock.AsyncMock())
        self._p_music.start()
        return tutor.make_app(self.eng)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self._p_music.stop()
        await self._up_srv.close()
        for k, v in self._saved.items():
            setattr(tutor, k, v)
        if self._had_sig:
            os.environ['TUTOR_OPEN_SIGNAL'] = self._orig_sig
        else:
            os.environ.pop('TUTOR_OPEN_SIGNAL', None)
        self._td.cleanup()

    async def test_tts_fallback_never_sets_max_age_and_long_hint_works(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}
        self.tts_fail = True
        r_fb = await self.client.get('/api/tutor/tts_hint?text=' + NOON_Q1, headers=H)
        self.assertEqual(r_fb.status, 200)
        self.assertEqual(r_fb.headers.get('Cache-Control'), 'no-store')

        self.tts_fail = False
        r_ok = await self.client.get('/api/tutor/tts_hint?text=' + NOON_Q1, headers=H)
        self.assertEqual(r_ok.status, 200)
        self.assertEqual(r_ok.headers.get('Cache-Control'), 'private, max-age=600')

    async def test_retry_snap_and_keepalive_stall_failover(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}
        tutor.SNAP_DIR.mkdir(parents=True, exist_ok=True)
        snap_name = '1795146422_8e7f11.jpg'
        (tutor.SNAP_DIR / snap_name).write_bytes(b'\xff\xd8\xff\xe0' + b'\x11' * 400 + b'\xff\xd9')

        # Reject path traversal on retry_snap
        r_bad = await self.client.post('/api/tutor/start', headers=H, json={'retry_snap': '/api/tutor/snap/../../etc/passwd'})
        self.assertEqual(r_bad.status, 400)

        # Simulate SSE keepalive stall on gpt-6.1-terra and verify fast failover to gpt-6.1-sol
        self.stall_terra = True
        with mock.patch.object(tutor, '_FIRST_TOKEN_TIMEOUT_FAST', 0.2):
            r_go = await self.client.post('/api/tutor/start', headers=H, json={
                'retry_snap': f'/api/tutor/snap/{snap_name}',
                'mode': 'fast',
                'source': 'doccam',
            })
            j_go = await r_go.json()
            self.assertTrue(j_go['ok'])
            sid = j_go['session']['id']
            for _ in range(30):
                await asyncio.sleep(0.05)
                pub = self.eng.public_session(sid)
                if pub['status'] in ('ready', 'error'):
                    break
        pub = self.eng.public_session(sid)
        self.assertEqual(pub['status'], 'ready', f"Expected failover to succeed, got error={pub.get('error')}")
        self.assertEqual(pub['subject'], 'math')
        self.assertGreaterEqual(len([s for s in pub['steps'] if s.get('step', 0) > 0]), 4)
        self.assertIn('gpt-6.1-sol', self.called_models)
        for m in self.called_models:
            self.assertNotIn('gemini', m.lower())


if __name__ == '__main__':
    unittest.main()
