#!/opt/thinksmart-addons/venv/bin/python
"""Regression & integration suite for R92 Grade-9 Silent Homework Co-Writer ('高拍仪静默伴写看板')."""
import asyncio
import base64
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

if '/usr/lib/python3/dist-packages' not in sys.path:
    sys.path.append('/usr/lib/python3/dist-packages')
from PIL import Image, ImageDraw

STAGE_ROOT = pathlib.Path(os.environ.get('STAGE_ROOT', '/'))


def _p(abs_path: str) -> pathlib.Path:
    cand = STAGE_ROOT / abs_path.lstrip('/')
    return cand if cand.exists() else pathlib.Path(abs_path)


def _load_tutor(mod_name: str = 'r92_tutor'):
    path = _p('/usr/local/bin/thinksmart-tutor.py')
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_worksheet_jpeg(ink_step: int = 0, brightness_shift: int = 0, flip_page: bool = False) -> bytes:
    bg = max(0, min(255, 242 + brightness_shift))
    im = Image.new('RGB', (960, 720), (bg, bg, bg))
    dr = ImageDraw.Draw(im)
    fg = max(0, min(255, 28 + brightness_shift))
    if flip_page:
        for y in range(60, 680, 34):
            dr.rectangle([50, y, 900, y + 12], fill=(fg, fg, fg))
    else:
        # Problem 1 stem + answer
        dr.rectangle([60, 80, 860, 92], fill=(fg, fg, fg))
        dr.rectangle([80, 120, 420, 134], fill=(fg, fg, fg))
        if ink_step >= 1:
            # Problem 2 handwritten work inside box [0.06, 0.30, 0.86, 0.28]
            dr.rectangle([80, 250, 760, 268], fill=(fg, fg, fg))
            dr.rectangle([90, 295, 640, 315], fill=(fg, fg, fg))
            dr.line([90, 340, 580, 348], fill=(fg, fg, fg), width=7)
        if ink_step >= 2:
            # Revision inside Problem 2 box
            dr.rectangle([420, 330, 810, 392], fill=(fg, fg, fg))
            dr.line([410, 280, 820, 395], fill=(fg, fg, fg), width=9)
    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=88)
    return buf.getvalue()


class TestR92CoWriteUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tut = _load_tutor('r92_tutor_unit')

    def test_01_gray_thumb_ambient_light_immunity_and_ink_detection(self):
        cw_cls = self.tut.CoWriteMonitor
        j0 = _make_worksheet_jpeg(ink_step=0, brightness_shift=0)
        j_bright = _make_worksheet_jpeg(ink_step=0, brightness_shift=22)
        j_ink = _make_worksheet_jpeg(ink_step=1, brightness_shift=0)
        j_flip = _make_worksheet_jpeg(ink_step=0, flip_page=True)

        t0 = cw_cls._make_gray_thumb_sync(j0)
        self.assertEqual(len(t0), 160 * 120)
        t_bright = cw_cls._make_gray_thumb_sync(j_bright)
        t_ink = cw_cls._make_gray_thumb_sync(j_ink)
        t_flip = cw_cls._make_gray_thumb_sync(j_flip)

        # Identical
        d_same = cw_cls._thumb_diff_metrics(t0, t0)
        self.assertEqual(d_same['changed_cells'], 0)
        self.assertEqual(d_same['mean_diff'], 0.0)

        # Pure ambient brightness drift must NOT trigger changed ink cells
        d_amb = cw_cls._thumb_diff_metrics(t0, t_bright)
        self.assertEqual(d_amb['changed_cells'], 0)

        # Real pen strokes trigger changed_cells & max_cell_diff
        d_ink = cw_cls._thumb_diff_metrics(t0, t_ink)
        self.assertGreaterEqual(d_ink['changed_cells'], 2)
        self.assertGreaterEqual(d_ink['max_cell_diff'], 9.0)

        # Full page flip triggers >= 42 changed cells
        d_flip = cw_cls._thumb_diff_metrics(t0, t_flip)
        self.assertGreaterEqual(d_flip['changed_cells'], 42)

    def test_02_anti_spoiler_scrubber_and_study_seal_parity(self):
        scrub = self.tut._scrub_cowrite_spoilers
        # Must strip answers/options/non-zero results while keeping "= 0" / "≠ 0" domain checks!
        s1 = scrub('去分母常数项漏乘，正确答案是 x = -3，且别忘了检验分母是否为 = 0')
        self.assertNotIn('-3', s1)
        self.assertIn('= 0', s1)

        s2 = scrub('主谓一致需留意，此处应选 B，因为主语是单数')
        self.assertNotIn('选 B', s2)

        s3 = scrub('含参二次项系数需核对 a ≠ 0，计算结果 = 12.5 有误')
        self.assertIn('≠ 0', s3)
        self.assertNotIn('12.5', s3)

        sec = bytes.fromhex('11' * 32)
        obj = {'prompt': 'hello', 'rotate': 0, 'max_tokens': 480}
        env = self.tut._study_seal(sec, 'req', obj)
        dec = self.tut._study_unseal(sec, 'req', env)
        self.assertEqual(dec, obj)
        bad_env = dict(env, tag=base64.b64encode(b'\x00' * 32).decode('ascii'))
        with self.assertRaises(PermissionError):
            self.tut._study_unseal(sec, 'req', bad_env)

    def test_03_frontend_cowrite_dom_zero_audio_and_strict_css(self):
        h = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for token in (
            'id="coWriteBoard"', 'id="cwCtrlPanel"', 'id="tabCamModeSnap"', 'id="tabCamModeCowrite"',
            'id="cwSnapModal"', 'id="btnCwToggle"', 'id="btnCwCheckNow"', 'setCameraSubMode',
            'stopCameraPreviewLoop(true)', "img.removeAttribute('src')",
        ):
            self.assertIn(token, h)
        # Preserve R90 strict CSS rules
        css = h[h.index('<style>'):h.index('</style>')]
        css_nc = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
        self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', css_nc)) <= {'400', '700'})
        self.assertEqual(re.findall(r'\d+\.\d+px', css_nc), [])
        # Ensure Co-Write UI block contains zero TTS/audio playback calls
        cw_js = h[h.index('R92 Grade-9 Silent Co-Write Board'):h.index('let lastCamCropSig')]
        self.assertNotIn('speakHintText', cw_js)
        self.assertNotIn('audioEl.play', cw_js)


class TestR92CoWriteAsyncFunnel(unittest.IsolatedAsyncioTestCase):
    async def test_04_four_stage_funnel_and_http_routes(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        tut = _load_tutor('r92_tutor_http')
        secret_bytes = bytes.fromhex('2a' * 32)
        gate_replies = []
        bridge_hits = []

        async def mock_study_v1(req: web.Request):
            env_in = await req.json()
            payload = tut._study_unseal(secret_bytes, 'req', env_in)
            bridge_hits.append(payload)
            nxt = gate_replies.pop(0) if gate_replies else {
                'hand_blocking': False, 'writing_incomplete': False, 'page_summary': '桌面平稳', 'new_completed': []
            }
            rep = {'ok': True, 're': env_in['nonce'], 'text': json.dumps(nxt, ensure_ascii=False), 'rotated': 0, 'frames': 1, 'ms': 420}
            return web.json_response(tut._study_seal(secret_bytes, 'resp', rep))

        b_app = web.Application()
        b_app.router.add_post('/study/v1', mock_study_v1)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            study_cfg = td_p / 'study_cfg.json'
            study_sec = td_p / 'study_secret'
            study_sec.write_text(secret_bytes.hex(), encoding='utf-8')
            os.chmod(study_sec, 0o600)

            async with TestClient(TestServer(b_app)) as b_cli:
                study_cfg.write_text(json.dumps({'vision_url': f'http://127.0.0.1:{b_cli.port}/study/v1'}), encoding='utf-8')
                with mock.patch.object(tut, 'STATE_DIR', state_d), \
                     mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                     mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                     mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                     mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                     mock.patch.dict(os.environ, {
                         'TUTOR_STUDY_CONFIG': str(study_cfg),
                         'TUTOR_STUDY_SECRET': str(study_sec),
                         'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'),
                         'TUTOR_WARM_GOLDEN': '0',
                     }):
                    eng = tut.TutorEngine()
                    review_outputs = []

                    async def fake_post_chat(kind, messages, temperature=0.25, timeout_s=26.0):
                        if review_outputs:
                            return json.dumps(review_outputs.pop(0), ensure_ascii=False)
                        return json.dumps({
                            'verdict': 'ok', 'confidence': 0.96, 'no': '第1题',
                            'topic': '一元二次方程', 'step_where': '', 'checkpoint': '判别式与求根过程严谨全对',
                        }, ensure_ascii=False)

                    eng._post_chat_once = fake_post_chat
                    eng.start_live_problem = mock.Mock(return_value={
                        'id': 'sess_cw_1', 'title': '第2题 伴写错因精讲', 'status': 'streaming', 'steps': [],
                    })

                    app = tut.make_app(engine=eng)
                    async with TestClient(TestServer(app)) as cli:
                        h = {'Host': f'127.0.0.1:{cli.server.port}'}

                        # 1. Stage a worksheet page via PhoneStudio so check_now works without physical UVC hardware
                        eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=1), note='九年级数学')

                        # 2. Toggle Co-Write ON -> _tutor_busy becomes True; OFF -> _tutor_busy becomes False
                        r_on = await cli.post('/api/tutor/cowrite/toggle', json={'enabled': True, 'subject': 'math'}, headers=h)
                        self.assertEqual(r_on.status, 200)
                        j_on = await r_on.json()
                        self.assertTrue(j_on['cowrite']['enabled'])
                        self.assertEqual(j_on['cowrite']['subject'], 'math')
                        self.assertTrue(tut._tutor_busy(eng))

                        r_off = await cli.post('/api/tutor/cowrite/toggle', json={'enabled': False}, headers=h)
                        j_off = await r_off.json()
                        self.assertFalse(j_off['cowrite']['enabled'])
                        await asyncio.sleep(0.05)
                        self.assertFalse(tut._tutor_busy(eng))

                        # 3. Gatekeeper returns writing_incomplete -> Reviewer NOT called
                        gate_replies.append({
                            'hand_blocking': False, 'writing_incomplete': True,
                            'page_summary': '第2题未写完', 'new_completed': [],
                        })
                        r_inc = await cli.post('/api/tutor/cowrite/check_now', json={}, headers=h)
                        j_inc = await r_inc.json()
                        self.assertTrue(j_inc['ok'])
                        self.assertEqual(j_inc['checked_new'], 0)
                        self.assertEqual(j_inc['cowrite']['stats']['review_calls'], 0)
                        self.assertEqual(j_inc['cowrite']['gate_engine'], 'local_qwen3.5_9b')

                        # 4. Gatekeeper returns 2 items: #1 unclear (confidence 0.62 -> skipped!), #2 warn (with leaked answer scrubbed!)
                        gate_replies.append({
                            'hand_blocking': False, 'writing_incomplete': False,
                            'page_summary': '新完成2题',
                            'new_completed': [
                                {'no': '第1题', 'subject': 'math', 'box': [0.06, 0.08, 0.86, 0.16], 'stem_brief': '模糊草稿'},
                                {'no': '第2题', 'subject': 'math', 'box': [0.06, 0.30, 0.86, 0.28], 'stem_brief': '解分式方程'},
                            ],
                        })
                        review_outputs.extend([
                            {
                                'verdict': 'unclear', 'confidence': 0.62, 'no': '第1题',
                                'topic': '模糊', 'step_where': '', 'checkpoint': '',
                            },
                            {
                                'verdict': 'warn', 'confidence': 0.95, 'no': '第2题',
                                'topic': '分式方程增根', 'step_where': '去分母第2行',
                                'checkpoint': '去分母常数项漏乘，正确答案是 x = 5，且需检验分母是否为 = 0',
                            },
                        ])
                        r_ck = await cli.post('/api/tutor/cowrite/check_now', json={}, headers=h)
                        j_ck = await r_ck.json()
                        self.assertEqual(j_ck['checked_new'], 1)
                        cw_st = j_ck['cowrite']
                        self.assertEqual(cw_st['warn_open_count'], 1)
                        self.assertEqual(cw_st['stats']['unclear_skipped'], 1)
                        item2 = cw_st['items'][0]
                        self.assertEqual(item2['no'], '第2题')
                        self.assertEqual(item2['status'], 'warn')
                        self.assertNotIn('5', item2['checkpoint'])
                        self.assertIn('= 0', item2['checkpoint'])
                        self.assertTrue(item2['snap_url'].startswith('/api/tutor/snap/c0_'))

                        # Verify cropped snap image is served cleanly via api_snap_image
                        r_snap = await cli.get(item2['snap_url'], headers=h)
                        self.assertEqual(r_snap.status, 200)
                        raw_snap = await r_snap.read()
                        self.assertTrue(raw_snap.startswith(b'\xff\xd8\xff'))

                        # 5. Student revises inside Problem 2 box -> re-check upgrades warn -> ok (corrected_pass=True)!
                        eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=2), note='九年级数学修订')
                        gate_replies.append({
                            'hand_blocking': False, 'writing_incomplete': False,
                            'page_summary': '第2题已改',
                            'new_completed': [
                                {'no': '第2题', 'subject': 'math', 'box': [0.06, 0.30, 0.86, 0.28], 'stem_brief': '解分式方程'},
                            ],
                        })
                        review_outputs.append({
                            'verdict': 'ok', 'confidence': 0.97, 'no': '第2题',
                            'topic': '分式方程增根', 'step_where': '', 'checkpoint': '已补齐常数项并完成检验舍去增根 ✓',
                        })
                        r_rev = await cli.post('/api/tutor/cowrite/check_now', json={}, headers=h)
                        j_rev = await r_rev.json()
                        self.assertEqual(j_rev['checked_new'], 1)
                        self.assertEqual(j_rev['cowrite']['warn_open_count'], 0)
                        self.assertEqual(j_rev['cowrite']['ok_count'], 1)
                        self.assertTrue(j_rev['cowrite']['items'][0]['corrected_pass'])

                        # 6. 1-Tap Explain Handoff -> starts diagnose_draft blackboard session
                        r_exp = await cli.post('/api/tutor/cowrite/explain', json={'id': item2['id'], 'mode': 'deep'}, headers=h)
                        self.assertEqual(r_exp.status, 200)
                        j_exp = await r_exp.json()
                        self.assertEqual(j_exp['session']['id'], 'sess_cw_1')
                        eng.start_live_problem.assert_called_once()
                        self.assertEqual(eng.open_ui_target, 'board')

                        # 7. Ring buffer cap <= 16 c0_*.jpg files
                        for _ in range(22):
                            eng.cowrite._save_cowrite_snap(raw_snap)
                        c0_files = list(snap_d.glob('c0_*.jpg'))
                        self.assertLessEqual(len(c0_files), 16)

                        # 8. Clear all removes board items and unlinks snaps
                        r_clr = await cli.post('/api/tutor/cowrite/resolve', json={'clear_all': True}, headers=h)
                        j_clr = await r_clr.json()
                        self.assertEqual(j_clr['cowrite']['total_count'], 0)
                        self.assertTrue(len(bridge_hits) >= 3)

    def test_05_r93_subquestion_section_iou_dedup_and_shadow_norm(self):
        tut = _load_tutor('r93_tutor_unit')
        cw_cls = tut.CoWriteMonitor

        # Subquestion & section normalization
        self.assertEqual(cw_cls._norm_prob_key('第15（1）题'), '15(1)')
        self.assertEqual(cw_cls._norm_prob_key('15②'), '15(2)')
        self.assertEqual(cw_cls._norm_prob_key('一、1'), 's1:1')
        self.assertEqual(cw_cls._norm_prob_key('二、1'), 's2:1')
        self.assertEqual(cw_cls._norm_prob_key('选择题 3'), 'mc:3')

        # Distinct subquestions 15(1) vs 15(2) must NOT collide even with overlapping boxes
        ent_15_1 = {'prob_key': '15(1)', 'box': {'x': 0.06, 'y': 0.25, 'w': 0.86, 'h': 0.30}}
        self.assertFalse(cw_cls._matches_checked_entry('15(2)', {'x': 0.06, 'y': 0.28, 'w': 0.86, 'h': 0.30}, ent_15_1))
        # Same subquestion / repeated crop with IoU >= 0.44 DOES match
        self.assertTrue(cw_cls._matches_checked_entry('15(1)', {'x': 0.07, 'y': 0.26, 'w': 0.84, 'h': 0.29}, ent_15_1))
        # Same raw number "1" at top of page vs bottom of page (disjoint sections) must NOT collide
        ent_top_1 = {'prob_key': '1', 'box': {'x': 0.06, 'y': 0.08, 'w': 0.84, 'h': 0.14}}
        self.assertFalse(cw_cls._matches_checked_entry('1', {'x': 0.06, 'y': 0.62, 'w': 0.84, 'h': 0.16}, ent_top_1))

        # Flat-field desk-lamp / hand shadow removal test
        im = Image.new('RGB', (800, 360), (242, 242, 242))
        dr = ImageDraw.Draw(im)
        # Heavy shadow on right half (132 gray) + dark ink line inside shadow
        dr.rectangle([400, 0, 799, 359], fill=(132, 132, 132))
        dr.rectangle([100, 150, 700, 166], fill=(24, 24, 24))
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=92)
        out_jpg = cw_cls._crop_problem_jpeg_sync(buf.getvalue(), {'x': 0.02, 'y': 0.05, 'w': 0.96, 'h': 0.90}, pencil_boost=True)
        with Image.open(io.BytesIO(out_jpg)) as out_im:
            g = out_im.convert('L')
            ow, oh = g.size
            # Sample shadowed paper background away from ink stroke -> must be brightened cleanly
            bg_shadow_px = g.getpixel((int(ow * 0.78), int(oh * 0.20)))
            self.assertGreaterEqual(bg_shadow_px, 215)
            # Sample ink stroke center -> must remain dark
            ink_vals = [g.getpixel((int(ow * 0.72), y)) for y in range(int(oh * 0.30), int(oh * 0.60))]
            self.assertLessEqual(min(ink_vals), 90)

    def test_06_r93_frontend_mobile_silent_handoff_and_strict_css(self):
        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in (
            'id="cwDigestBar"', 'toggleCoWriteDeepHint', 'enrollCoWriteMistake',
            'silentBoardMode', 'goBackToCoWriteBoard', 'id="btnCwPencil"',
            'cwPeekRotate', 'const fromCamLesson = ', 'fromCowriteLesson',
        ):
            self.assertIn(tok, h_idx)

        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('id="btnMobCowrite"', 'cowrite_check', 'j.cowrite'):
            self.assertIn(tok, h_mob)
        css_m = h_mob[h_mob.index('<style>'):h_mob.index('</style>')]
        css_m_nc = re.sub(r'/\*.*?\*/', '', css_m, flags=re.S)
        self.assertTrue(set(re.findall(r'font-weight:\s*([^;}\s]+)', css_m_nc)) <= {'400', '700'})
        self.assertEqual(re.findall(r'\d+\.\d+px', css_m_nc), [])

    async def test_07_r93_progressive_hints_mistake_enroll_silent_solve_and_mobile_sync(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        tut = _load_tutor('r93_tutor_http')
        secret_bytes = bytes.fromhex('3b' * 32)
        gate_replies = []

        async def mock_study_v1(req: web.Request):
            env_in = await req.json()
            tut._study_unseal(secret_bytes, 'req', env_in)
            nxt = gate_replies.pop(0) if gate_replies else {
                'hand_blocking': False, 'writing_incomplete': False, 'page_summary': '完成两小问', 'new_completed': [],
            }
            rep = {'ok': True, 're': env_in['nonce'], 'text': json.dumps(nxt, ensure_ascii=False), 'rotated': 0, 'frames': 1, 'ms': 310}
            return web.json_response(tut._study_seal(secret_bytes, 'resp', rep))

        b_app = web.Application()
        b_app.router.add_post('/study/v1', mock_study_v1)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            study_cfg = td_p / 'study_cfg.json'
            study_sec = td_p / 'study_secret'
            study_sec.write_text(secret_bytes.hex(), encoding='utf-8')
            os.chmod(study_sec, 0o600)

            async with TestClient(TestServer(b_app)) as b_cli:
                study_cfg.write_text(json.dumps({'vision_url': f'http://127.0.0.1:{b_cli.port}/study/v1'}), encoding='utf-8')
                with mock.patch.object(tut, 'STATE_DIR', state_d), \
                     mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                     mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                     mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                     mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                     mock.patch.dict(os.environ, {
                         'TUTOR_STUDY_CONFIG': str(study_cfg),
                         'TUTOR_STUDY_SECRET': str(study_sec),
                         'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'),
                         'TUTOR_WARM_GOLDEN': '0',
                     }):
                    eng = tut.TutorEngine()
                    mock_pause_music = mock.AsyncMock()
                    eng.pause_music_if_playing = mock_pause_music
                    review_outputs = []

                    async def fake_post_chat(kind, messages, temperature=0.25, timeout_s=26.0):
                        return json.dumps(review_outputs.pop(0), ensure_ascii=False)

                    async def fake_run_pipeline(sess, q, image_bytes, use_mode, use_ped):
                        sess['steps'].append({
                            'step': 1, 'title': '找错定位', 'say': '静音测试', 'board': '核对常数项',
                            'quiz': {'q': '常数项乘了吗？', 'options': ['漏乘了', '乘了', '不确定'], 'answer': 0},
                            'draw': [],
                        })
                        sess['status'] = 'ready'

                    eng._post_chat_once = fake_post_chat
                    eng._run_live_pipeline = fake_run_pipeline

                    app = tut.make_app(engine=eng)
                    async with TestClient(TestServer(app)) as cli:
                        h = {'Host': f'127.0.0.1:{cli.server.port}'}
                        hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                        # 1. Mobile upload cowrite_check with 2 overlapping subquestions 15(1) & 15(2)
                        gate_replies.append({
                            'hand_blocking': False, 'writing_incomplete': False,
                            'page_summary': '完成第15题两小问',
                            'new_completed': [
                                {'no': '15（1）', 'subject': 'math', 'box': [0.06, 0.20, 0.86, 0.26], 'stem_brief': '求二次函数顶点'},
                                {'no': '15（2）', 'subject': 'math', 'box': [0.06, 0.24, 0.86, 0.30], 'stem_brief': '区间最值'},
                            ],
                        })
                        review_outputs.extend([
                            {
                                'verdict': 'ok', 'confidence': 0.96, 'no': '15(1)',
                                'topic': '二次函数顶点', 'step_where': '', 'checkpoint': '配方法与对称轴准确 ✓',
                                'deep_hint': '', 'ocr_stem': '已知抛物线 y=x^2-4x+3，(1)求顶点坐标',
                            },
                            {
                                'verdict': 'warn', 'confidence': 0.94, 'no': '15(2)',
                                'topic': '二次函数区间最值', 'step_where': '求最大值最后一行',
                                'checkpoint': '请核对对称轴是否在给定自变量区间内',
                                'deep_hint': '对称轴 x = 2 在区间内，最小值结果 = -1 请自查端点距离',
                                'ocr_stem': '已知抛物线 y=x^2-4x+3，(2)当 0<=x<=5 时求最值',
                            },
                        ])
                        page_b64 = base64.b64encode(_make_worksheet_jpeg(ink_step=1)).decode('ascii')
                        r_up = await cli.post('/api/tutor/mobile/upload', json={
                            'action': 'cowrite_check', 'page_b64': page_b64, 'pencil_boost': True,
                        }, headers=hm)
                        self.assertEqual(r_up.status, 200)
                        j_up = await r_up.json()
                        self.assertTrue(j_up['ok'])
                        self.assertEqual(eng.open_ui_target, 'cowrite')
                        self.assertEqual(j_up['cowrite']['total_count'], 2)
                        self.assertEqual(j_up['cowrite']['digest']['accuracy_pct'], 50)
                        self.assertEqual(j_up['cowrite']['digest']['unrecorded_warn_count'], 1)

                        warn_item = next(x for x in j_up['cowrite']['items'] if x['status'] == 'warn')
                        self.assertNotIn('-1', warn_item['deep_hint'])
                        self.assertTrue(len(warn_item['deep_hint']) > 4)

                        # 2. Mobile state exposes cowrite brief
                        r_ms = await cli.get('/api/tutor/mobile/state', headers=hm)
                        self.assertEqual(r_ms.status, 200)
                        j_ms = await r_ms.json()
                        self.assertEqual(j_ms['cowrite']['total_count'], 2)
                        self.assertEqual(j_ms['cowrite']['warn_open_count'], 1)

                        # 3. 1-Tap Mistake Book enrollment + snap ring-buffer / clear_all protection
                        protected_snap = snap_d / warn_item['snap_url'].rsplit('/', 1)[-1]
                        self.assertTrue(protected_snap.exists())
                        r_enr = await cli.post('/api/tutor/cowrite/enroll_mistake', json={'enroll_all_warns': True}, headers=h)
                        j_enr = await r_enr.json()
                        self.assertTrue(j_enr['ok'])
                        self.assertEqual(j_enr['enrolled_count'], 1)
                        self.assertEqual(j_enr['cowrite']['digest']['unrecorded_warn_count'], 0)
                        self.assertEqual(j_enr['mistake_summary']['active_count'], 1)

                        # Flood 20 transient snaps -> protected Mistake-Book snap must survive!
                        dummy_jpg = _make_worksheet_jpeg(ink_step=0)
                        for _ in range(20):
                            eng.cowrite._save_cowrite_snap(dummy_jpg)
                        self.assertTrue(protected_snap.exists())

                        # 4. Silent Blackboard Explain -> zero pause_music_if_playing, silent_start=True, solve auto-resolves item!
                        mock_pause_music.reset_mock()
                        r_exp = await cli.post('/api/tutor/cowrite/explain', json={
                            'id': warn_item['id'], 'mode': 'deep', 'silent': True,
                        }, headers=h)
                        j_exp = await r_exp.json()
                        self.assertTrue(j_exp['ok'])
                        self.assertTrue(j_exp['silent'])
                        self.assertTrue(j_exp['session']['silent_start'])
                        self.assertEqual(j_exp['session']['cowrite_item_id'], warn_item['id'])
                        mock_pause_music.assert_not_called()

                        await asyncio.sleep(0.05)
                        r_prog = await cli.post('/api/tutor/progress', json={
                            'id': j_exp['session']['id'], 'solved': True,
                        }, headers=h)
                        self.assertEqual(r_prog.status, 200)
                        st_after = eng.cowrite.public_state()
                        updated_w = next(x for x in st_after['items'] if x['id'] == warn_item['id'])
                        self.assertTrue(updated_w['resolved'])
                        self.assertTrue(updated_w['blackboard_solved'])

                        # 5. Clear all board items -> protected Mistake-Book snap still survives on disk!
                        await cli.post('/api/tutor/cowrite/resolve', json={'clear_all': True}, headers=h)
                        self.assertTrue(protected_snap.exists())

    def test_08_r94_scrubber_chinese_safety_radicals_coords_and_ui_tokens(self):
        tut = _load_tutor('r94_tutor_unit')
        scrub = tut._scrub_cowrite_spoilers

        # Legitimate Grade-9 Chinese pedagogical terms MUST remain completely intact (never mangled!)
        s_clean1 = scrub('请检验去分母后是否产生增根，避免原分式方程无解')
        self.assertIn('增根', s_clean1)
        self.assertIn('无解', s_clean1)

        s_clean2 = scrub('请核对因式分解十字相乘的常数项符号，注意 Δ ≥ 0 且 a ≠ 0')
        self.assertIn('因式分解', s_clean2)
        self.assertIn('Δ ≥ 0', s_clean2)
        self.assertIn('a ≠ 0', s_clean2)

        s_clean3 = scrub('选择题第3题选项中的主谓一致需核对')
        self.assertIn('选择题', s_clean3)
        self.assertIn('选项中', s_clean3)

        # Grade-9 radicals, π, coordinates, and English/chemical answer leaks MUST be scrubbed!
        self.assertNotIn('2√3', scrub('勾股定理计算有误，斜边长 = 2√3'))
        self.assertNotIn('4π', scrub('弧长公式代错半径，扇形面积 = 4π/3'))
        self.assertNotIn('(2, -1)', scrub('配方法符号算反，顶点坐标为 (2, -1)'))
        self.assertNotIn('has gone', scrub('现在完成时瞬间动词注意延续性，此处应改为 has gone'))

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in (
            'setCoWriteFilter', 'recheckCoWriteItem', 'variantCoWriteItem',
            'applyCoWriteRotateHint', 'toggleCoWriteSnapZoom', 'id="cwSnapHintBar"', 'id="cwRotAlert"',
        ):
            self.assertIn(tok, h_idx)

        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('id="btnRemoteCwEnroll"', 'cowrite_enroll_warns'):
            self.assertIn(tok, h_mob)

    async def test_09_r94_4item_parallel_backlog_recheck_variant_rotate_and_remote_enroll(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        tut = _load_tutor('r94_tutor_http')
        secret_bytes = bytes.fromhex('4c' * 32)
        gate_replies = []

        async def mock_study_v1(req: web.Request):
            env_in = await req.json()
            tut._study_unseal(secret_bytes, 'req', env_in)
            nxt = gate_replies.pop(0) if gate_replies else {
                'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': False,
                'page_summary': '桌面平稳', 'new_completed': [],
            }
            rep = {'ok': True, 're': env_in['nonce'], 'text': json.dumps(nxt, ensure_ascii=False), 'rotated': 0, 'frames': 1, 'ms': 280}
            return web.json_response(tut._study_seal(secret_bytes, 'resp', rep))

        b_app = web.Application()
        b_app.router.add_post('/study/v1', mock_study_v1)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            study_cfg = td_p / 'study_cfg.json'
            study_sec = td_p / 'study_secret'
            study_sec.write_text(secret_bytes.hex(), encoding='utf-8')
            os.chmod(study_sec, 0o600)

            async with TestClient(TestServer(b_app)) as b_cli:
                study_cfg.write_text(json.dumps({'vision_url': f'http://127.0.0.1:{b_cli.port}/study/v1'}), encoding='utf-8')
                with mock.patch.object(tut, 'STATE_DIR', state_d), \
                     mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                     mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                     mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                     mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                     mock.patch.dict(os.environ, {
                         'TUTOR_STUDY_CONFIG': str(study_cfg),
                         'TUTOR_STUDY_SECRET': str(study_sec),
                         'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'),
                         'TUTOR_WARM_GOLDEN': '0',
                     }):
                    eng = tut.TutorEngine()
                    mock_pause_music = mock.AsyncMock()
                    eng.pause_music_if_playing = mock_pause_music
                    review_map = {}
                    aux_reply = {'title': '同类变式：二次函数最值', 'question': '已知二次函数 y = x² - 6x + 5，当 1 ≤ x ≤ 6 时，求 y 的最小值与最大值。'}

                    async def fake_post_chat(kind, messages, temperature=0.25, timeout_s=26.0):
                        if kind == 'aux':
                            return json.dumps(aux_reply, ensure_ascii=False)
                        msg_txt = str(messages[0]['content'][0]['text'])
                        for k_no, payload in list(review_map.items()):
                            if f'目标题号：{k_no}' in msg_txt:
                                return json.dumps(payload, ensure_ascii=False)
                        return json.dumps({'verdict': 'ok', 'confidence': 0.96, 'no': '第1题', 'topic': '基础', 'checkpoint': '正确 ✓'}, ensure_ascii=False)

                    async def fake_run_pipeline(sess, q, image_bytes, use_mode, use_ped):
                        sess['steps'].append({'step': 1, 'title': '变式审题', 'say': '静音变式', 'board': q[:32], 'quiz': None, 'draw': []})
                        sess['status'] = 'ready'

                    eng._post_chat_once = fake_post_chat
                    eng._run_live_pipeline = fake_run_pipeline

                    app = tut.make_app(engine=eng)
                    async with TestClient(TestServer(app)) as cli:
                        h = {'Host': f'127.0.0.1:{cli.server.port}'}
                        hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                        eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=1), note='初三综合页')

                        # 1. Upside-down orientation hint (need_rotate=180) + auto-clear on rotate
                        gate_replies.append({
                            'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 180, 'has_more': False,
                            'page_summary': '纸张倒置', 'new_completed': [],
                        })
                        r_rot = await cli.post('/api/tutor/cowrite/check_now', json={'pencil_boost': True}, headers=h)
                        j_rot = await r_rot.json()
                        self.assertEqual(j_rot['cowrite']['rotate_hint'], 180)
                        self.assertTrue(j_rot['cowrite']['pencil_boost'])
                        await cli.post('/api/tutor/phone_stage/rotate', json={'rotate': 180}, headers=h)
                        self.assertEqual(eng.cowrite.public_state()['rotate_hint'], 0)

                        # 2. 4-item parallel batch review + has_more=True keeps saw_activity=True for backlog drain!
                        gate_replies.append({
                            'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': True,
                            'page_summary': '本页先核4题',
                            'new_completed': [
                                {'no': '1', 'subject': 'math', 'box': [0.05, 0.06, 0.88, 0.14], 'stem_brief': '选择题1'},
                                {'no': '2', 'subject': 'math', 'box': [0.05, 0.22, 0.88, 0.14], 'stem_brief': '填空题2'},
                                {'no': '3', 'subject': 'physics', 'box': [0.05, 0.40, 0.88, 0.18], 'stem_brief': '欧姆定律'},
                                {'no': '4', 'subject': 'chemistry', 'box': [0.05, 0.62, 0.88, 0.20], 'stem_brief': '化学配平'},
                            ],
                        })
                        review_map.update({
                            '1': {'verdict': 'ok', 'confidence': 0.96, 'no': '第1题', 'topic': '旋转对称', 'checkpoint': '正确 ✓'},
                            '2': {'verdict': 'ok', 'confidence': 0.95, 'no': '第2题', 'topic': '判别式', 'checkpoint': '正确 ✓'},
                            '3': {'verdict': 'warn', 'confidence': 0.94, 'no': '第3题', 'topic': '欧姆定律串并联', 'step_where': '支路电流', 'checkpoint': '请核对滑动变阻器滑片移动时电压表测哪段电路', 'ocr_stem': '如图所示串联电路，求滑片右移时电表示数变化'},
                            '4': {'verdict': 'warn', 'confidence': 0.95, 'no': '第4题', 'topic': '化学方程式配平', 'step_where': '氧原子守恒', 'checkpoint': '请核对反应前后氧原子个数与气体符号', 'ocr_stem': '写出高锰酸钾受热分解的化学方程式'},
                        })
                        r_b4 = await cli.post('/api/tutor/cowrite/check_now', json={}, headers=h)
                        j_b4 = await r_b4.json()
                        self.assertEqual(j_b4['checked_new'], 4)
                        self.assertEqual(j_b4['cowrite']['total_count'], 4)
                        self.assertEqual(j_b4['cowrite']['warn_open_count'], 2)
                        self.assertTrue(eng.cowrite.saw_activity)

                        # 3. 1-Tap Targeted Single-Item Recheck (skips Gatekeeper completely -> gate_calls unchanged!)
                        gate_before = eng.cowrite.stats['gate_calls']
                        item3 = next(x for x in j_b4['cowrite']['items'] if x['no'] == '第3题')
                        review_map['第3题'] = {'verdict': 'ok', 'confidence': 0.97, 'no': '第3题', 'topic': '欧姆定律串并联', 'checkpoint': '串并联状态与单位核对无误 ✓'}
                        r_rc = await cli.post('/api/tutor/cowrite/recheck_item', json={'id': item3['id']}, headers=h)
                        j_rc = await r_rc.json()
                        self.assertTrue(j_rc['ok'])
                        self.assertTrue(j_rc['updated'])
                        self.assertEqual(j_rc['verdict'], 'ok')
                        self.assertEqual(eng.cowrite.stats['gate_calls'], gate_before)
                        item3_after = next(x for x in j_rc['cowrite']['items'] if x['id'] == item3['id'])
                        self.assertTrue(item3_after['corrected_pass'])

                        # 4. 1-Tap Silent Isomorphic Variant Drill from Co-Write card
                        item4 = next(x for x in j_rc['cowrite']['items'] if x['no'] == '第4题')
                        mock_pause_music.reset_mock()
                        r_var = await cli.post('/api/tutor/cowrite/variant', json={'id': item4['id'], 'silent': True}, headers=h)
                        j_var = await r_var.json()
                        self.assertTrue(j_var['ok'])
                        self.assertTrue(j_var['silent'])
                        self.assertTrue(j_var['session']['silent_start'])
                        self.assertEqual(j_var['session']['source'], 'cowrite')
                        mock_pause_music.assert_not_called()

                        # 5. Parent Mobile 1-Tap Remote Enroll of Co-Write warnings
                        r_rem = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_enroll_warns'}, headers=hm)
                        j_rem = await r_rem.json()
                        self.assertTrue(j_rem['ok'])
                        self.assertGreaterEqual(j_rem['enrolled_count'], 1)
                        self.assertEqual(j_rem['cowrite']['digest']['unrecorded_warn_count'], 0)

    async def test_10_r95_direct_gray_thumb_unfreeze_chinese_subject_archive_and_home_jump(self):
        tut = _load_tutor('r95_tutor_unit')

        # 1. Direct zero-reencode grayscale thumbnail helper across rotations
        raw_jpg = _make_worksheet_jpeg(ink_step=1)
        for rot in (0, 90, 180, 270):
            tb = tut.UVCCameraManager._pil_direct_gray_thumb_sync(raw_jpg, rot, 160, 120)
            self.assertEqual(len(tb), 160 * 120)

        # 2. 5th subject 'chinese' normalization
        self.assertEqual(tut.CoWriteMonitor._norm_subject('chinese'), 'chinese')

        # 3. Frontend & Home Focus 1-tap Co-Write jump hooks
        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in ('data-subj="chinese"', 'id="btnQuickCowrite"', "d.cmd === 'cowrite'", 'openCoWriteQuick'):
            self.assertIn(tok, h_idx)
        h_home = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        for tok in ('id="tpCowriteBtn"', 'id="trCowriteBtn"', "subCmd: 'cowrite'"):
            self.assertIn(tok, h_home)

        # 4. Auto-unfreeze camera on Co-Write start + bounded 7-session archive on clear_all + undo resolve
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.dict(os.environ, {'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.camera._frozen_raw_jpg = raw_jpg
                await eng.cowrite.start(subject='chinese')
                self.assertIsNone(eng.camera._frozen_raw_jpg)
                self.assertEqual(eng.cowrite.subject, 'chinese')
                await eng.cowrite.stop()

                # Populate 2 items, resolve + undo resolve, then archive 9 times -> strictly bounded <= 7!
                for k in range(9):
                    eng.cowrite.items = [
                        {'id': f'cw_a{k}', 'no': '第1题', 'prob_key': '1', 'page_epoch': 1, 'status': 'ok', 'subject': 'chinese', 'topic': '古诗文默写', 'step_where': '', 'checkpoint': '无错字 ✓', 'deep_hint': '', 'ocr_stem': '', 'snap_name': '', 'box': None, 'ts': 100 + k, 'resolved': False, 'corrected_pass': False, 'enrolled_mistake': False, 'mistake_id': '', 'blackboard_solved': False},
                        {'id': f'cw_b{k}', 'no': '第2题', 'prob_key': '2', 'page_epoch': 1, 'status': 'warn', 'subject': 'chinese', 'topic': '岳阳楼记默写', 'step_where': '下半句', 'checkpoint': '核对同音字偏旁', 'deep_hint': '', 'ocr_stem': '', 'snap_name': '', 'box': None, 'ts': 101 + k, 'resolved': False, 'corrected_pass': False, 'enrolled_mistake': False, 'mistake_id': '', 'blackboard_solved': False},
                    ]
                    if k == 0:
                        st_r1 = eng.cowrite.resolve_action(item_id='cw_b0', resolved=True)
                        self.assertEqual(st_r1['warn_open_count'], 0)
                        st_r0 = eng.cowrite.resolve_action(item_id='cw_b0', resolved=False)
                        self.assertEqual(st_r0['warn_open_count'], 1)
                    st_clr = eng.cowrite.resolve_action(clear_all=True)
                self.assertEqual(len(st_clr['recent_sessions']), 7)
                self.assertEqual(st_clr['recent_sessions'][0]['accuracy_pct'], 50)
                self.assertIn('岳阳楼记默写', st_clr['recent_sessions'][0]['weak_topics'])

    async def test_11_r96_gate_downscale_dark_blur_guard_and_check_crop(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r96_tutor_crop')
        cw_cls = tut.CoWriteMonitor

        # 1. Grouped objective block range keys preserved distinctly
        self.assertEqual(cw_cls._norm_prob_key('选择题1~5'), 'mc:1-5')
        self.assertEqual(cw_cls._norm_prob_key('完形21—25'), 'cz:21-25')
        self.assertNotEqual(cw_cls._norm_prob_key('选择题1~5'), cw_cls._norm_prob_key('选择题1'))

        # 2. Gatekeeper JPEG downscaler caps 2400x1800 -> <=960px long-edge while keeping small JPEGs untouched
        hi_im = Image.new('RGB', (2400, 1800), (242, 242, 242))
        dr_hi = ImageDraw.Draw(hi_im)
        dr_hi.rectangle([120, 200, 2200, 240], fill=(20, 20, 20))
        hi_buf = io.BytesIO()
        hi_im.save(hi_buf, format='JPEG', quality=90)
        hi_bytes = hi_buf.getvalue()
        gate_scaled = cw_cls._make_gate_jpeg_sync(hi_bytes, max_side=960)
        with Image.open(io.BytesIO(gate_scaled)) as gs_im:
            self.assertEqual(gs_im.size, (960, 720))

        # 3. Dark frame (<26 luma) & auto motion-blur frame (<2.1 edge_mean) skip Gatekeeper cleanly
        dark_im = Image.new('RGB', (640, 480), (10, 10, 10))
        dark_buf = io.BytesIO()
        dark_im.save(dark_buf, format='JPEG', quality=82)
        dark_jpg = dark_buf.getvalue()

        blur_im = Image.new('RGB', (640, 480), (168, 168, 168))
        blur_buf = io.BytesIO()
        blur_im.save(blur_buf, format='JPEG', quality=82)
        blur_jpg = blur_buf.getvalue()

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                gate_mock = mock.AsyncMock()
                eng.cowrite._call_gatekeeper = gate_mock

                cnt_dark = await eng.cowrite._run_funnel_once(dark_jpg, lambda: asyncio.sleep(0, result=dark_jpg), manual=True)
                self.assertEqual(cnt_dark, 0)
                self.assertEqual(eng.cowrite.lighting_warn, 'dark')
                self.assertEqual(eng.cowrite.stats['dark_skipped'], 1)
                gate_mock.assert_not_called()

                cnt_blur = await eng.cowrite._run_funnel_once(blur_jpg, lambda: asyncio.sleep(0, result=blur_jpg), manual=False)
                self.assertEqual(cnt_blur, 0)
                self.assertEqual(eng.cowrite.lighting_warn, '')
                self.assertEqual(eng.cowrite.stats['blur_skipped'], 1)
                gate_mock.assert_not_called()

                # 4. Direct check_crop API & mobile single-crop cowrite_check bypass Gatekeeper (~6s path!)
                review_queue = [
                    {
                        'verdict': 'warn', 'confidence': 0.95, 'no': '第23(2)题',
                        'topic': '圆的切线与相似比', 'step_where': '证相似对应边',
                        'checkpoint': '请核对公共角对应的两组邻边比例顺序',
                        'deep_hint': '△ABP 与 △ACB 共用 ∠A，斜边长 = 2√5 请重核比例式分母',
                        'ocr_stem': '如图 AB 为 ⊙O 直径，PC 切 ⊙O 于 C，(2)求证 PB·AC = PC·BC',
                    },
                    {
                        'verdict': 'warn', 'confidence': 0.93, 'no': '选择1-5',
                        'topic': '欧姆定律与电路动态', 'step_where': '第3小题',
                        'checkpoint': '第3小题滑片右移时请核对电压表测量对象',
                        'deep_hint': '滑片右移接入电阻变大，应选 B，请核对串联分压方向',
                        'ocr_stem': '选择题1~5：动态电路与电功率综合辨析',
                    },
                ]

                async def fake_post_chat(kind, messages, temperature=0.1, timeout_s=36.0):
                    return json.dumps(review_queue.pop(0), ensure_ascii=False)

                eng._post_chat_once = fake_post_chat
                ws_jpg = _make_worksheet_jpeg(ink_step=1)
                pid = eng.phone_studio.save_page(ws_jpg)

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    r_crop = await cli.post('/api/tutor/cowrite/check_crop', json={
                        'crop': {'x': 0.48, 'y': 0.18, 'w': 0.46, 'h': 0.36},
                        'page_id': pid,
                        'pencil_boost': True,
                    }, headers=h)
                    self.assertEqual(r_crop.status, 200)
                    j_crop = await r_crop.json()
                    self.assertTrue(j_crop['ok'])
                    self.assertTrue(j_crop['updated'])
                    gate_mock.assert_not_called()
                    it0 = j_crop['cowrite']['items'][0]
                    self.assertEqual(it0['no'], '第23(2)题')
                    self.assertNotIn('2√5', it0['deep_hint'])

                    # Mobile single-question crop cowrite_check also bypasses Gatekeeper & pinpoints grouped choice blank!
                    r_mob_c = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_check',
                        'page_id': pid,
                        'crop_box': {'x': 0.04, 'y': 0.05, 'w': 0.88, 'h': 0.24},
                    }, headers=hm)
                    self.assertEqual(r_mob_c.status, 200)
                    j_mob_c = await r_mob_c.json()
                    self.assertTrue(j_mob_c['ok'])
                    gate_mock.assert_not_called()
                    it_grp = j_mob_c['cowrite']['items'][0]
                    self.assertEqual(it_grp['no'], '选择1-5')
                    self.assertEqual(it_grp['step_where'], '第3小题')
                    self.assertNotIn('选 B', it_grp['deep_hint'])

                    # Mobile enroll returns enrolled_ids for 1-tap A4 Clean Worksheet export
                    r_me = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_enroll_warns'}, headers=hm)
                    j_me = await r_me.json()
                    self.assertEqual(j_me['enrolled_count'], 2)
                    self.assertEqual(len(j_me['enrolled_ids']), 2)

    async def test_12_r96_silent_followup_propagation_sprint_and_nl_voice_tool(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r96_tutor_silent')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in (
            'checkCoWriteCropFromCam', 'checkPhoneCropInCoWrite', 'id="btnPhoneCwCrop"',
            'applyCoWriteLightFix', 'startCoWriteSilentSprint',
        ):
            self.assertIn(tok, h_idx)
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('id="btnRemoteCwPrint"', 'openCwPrintSheet'):
            self.assertIn(tok, h_mob)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                mock_pause = mock.AsyncMock()
                mock_tts = mock.AsyncMock(return_value=b'RIFF....WAVE')
                eng.pause_music_if_playing = mock_pause
                eng.synthesize_wav = mock_tts

                async def fake_post_chat(kind, messages, temperature=0.25, timeout_s=26.0):
                    return json.dumps({
                        'title': '点拨线索', 'say': '先看分母最简公分母', 'board': '最简公分母 x(x-2)',
                        'note': '去分母不漏乘', 'question': '解分式方程：2/(x-1) + 1 = 3/(2x-2)',
                    }, ensure_ascii=False)

                async def fake_run_pipeline(sess, q, image_bytes, use_mode, use_ped):
                    sess['steps'].append({
                        'step': 1, 'title': '找公分母', 'say': '静音测试', 'board': '公分母 (x-1)',
                        'quiz': {'q': '公分母是什么？', 'options': ['x-1', 'x+1', 'x'], 'answer': 0},
                        'draw': [],
                    })
                    sess['status'] = 'ready'

                eng._post_chat_once = fake_post_chat
                eng._run_live_pipeline = fake_run_pipeline

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    ht = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 1. Natural-language voice command '打开静默伴写' -> enables cowrite, NEVER starts fake math lesson!
                    r_v1 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '打开静默伴写'}, headers=ht)
                    j_v1 = await r_v1.json()
                    self.assertTrue(j_v1['ok'])
                    self.assertTrue(eng.cowrite.enabled)
                    self.assertEqual(eng.open_ui_target, 'cowrite')
                    self.assertEqual(eng.active_session_id, '')

                    r_v2 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '关闭静默伴写'}, headers=ht)
                    j_v2 = await r_v2.json()
                    self.assertTrue(j_v2['ok'])
                    self.assertFalse(eng.cowrite.enabled)

                    # 2. Silent Mistake Sprint + Silent Follow-up Ask + Silent Foundation + Silent Variant -> ZERO audio/music calls!
                    eng.cowrite.items = [{
                        'id': 'cw_s1', 'no': '第12题', 'prob_key': '12', 'page_epoch': 1, 'status': 'warn',
                        'subject': 'math', 'topic': '分式方程增根', 'step_where': '去分母',
                        'checkpoint': '核对常数项是否同乘最简公分母', 'deep_hint': '',
                        'ocr_stem': '已知关于 x 的分式方程 2/(x-2) + 1 = m/(x-2)，（1）当 m=4 时求解；（2）若方程有增根求 m 的值。',
                        'snap_name': '', 'box': None, 'ts': 200, 'resolved': False, 'corrected_pass': False,
                        'enrolled_mistake': False, 'mistake_id': '', 'blackboard_solved': False,
                    }]
                    eng.cowrite.enroll_mistake(enroll_all_warns=True)

                    r_sp = await cli.post('/api/tutor/mistakes/sprint_start', json={
                        'count': 2, 'mode': 'fast', 'pedagogy': 'junior', 'silent': True,
                    }, headers=h)
                    j_sp = await r_sp.json()
                    self.assertTrue(j_sp['ok'])
                    self.assertTrue(j_sp['silent'])
                    self.assertTrue(j_sp['session']['silent_start'])
                    sid0 = j_sp['session']['id']
                    await asyncio.sleep(0.05)

                    r_ask = await cli.post('/api/tutor/ask', json={
                        'id': sid0, 'question': '题眼在哪里？', 'upto_idx': 0, 'unsolved_gate': True, 'silent': True,
                    }, headers=h)
                    self.assertEqual((await r_ask.json())['ok'], True)

                    r_fnd = await cli.post('/api/tutor/foundation', json={
                        'id': sid0, 'pedagogy': 'junior', 'silent': True,
                    }, headers=h)
                    self.assertEqual((await r_fnd.json())['ok'], True)

                    r_sp2 = await cli.post('/api/tutor/mistakes/sprint_next', json={
                        'sprint_id': j_sp['sprint_id'], 'mode': 'fast', 'pedagogy': 'junior', 'silent': True,
                    }, headers=h)
                    j_sp2 = await r_sp2.json()
                    self.assertTrue(j_sp2['ok'])
                    self.assertTrue(j_sp2['session']['silent_start'])

                    r_var = await cli.post('/api/tutor/variant', json={
                        'id': j_sp2['session']['id'], 'mode': 'fast', 'pedagogy': 'junior', 'silent': True,
                    }, headers=h)
                    j_var = await r_var.json()
                    self.assertTrue(j_var['ok'])
                    self.assertTrue(j_var['session']['silent_start'])

                    # Ironclad Zero-Audio Proof: neither music-pause nor TTS synthesis was ever invoked!
                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_13_r97_compact_gate_display_wake_sock_and_state_etag(self):
        import socket as py_sock
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r97_tutor_etag')
        cw_cls = tut.CoWriteMonitor

        # 1. Default Gatekeeper downscaler caps 2400x1800 -> 728x546 (~390 ViT tokens vs 720!)
        hi_im = Image.new('RGB', (2400, 1800), (242, 242, 242))
        hi_buf = io.BytesIO()
        hi_im.save(hi_buf, format='JPEG', quality=88)
        gate_728 = cw_cls._make_gate_jpeg_sync(hi_buf.getvalue())
        with Image.open(io.BytesIO(gate_728)) as g_im:
            self.assertEqual(g_im.size, (728, 546))

        # Auto-convert Qwen-VL 0..1000 [xmin, ymin, xmax, ymax] into normalized {'x','y','w','h'} without clamping to 0.96!
        nb_1000 = cw_cls._normalize_gate_box([60, 120, 880, 320])
        self.assertAlmostEqual(nb_1000['x'], 0.06, places=2)
        self.assertAlmostEqual(nb_1000['y'], 0.12, places=2)
        self.assertAlmostEqual(nb_1000['w'], 0.82, places=2)
        self.assertAlmostEqual(nb_1000['h'], 0.20, places=2)

        # 2. Unix datagram display-wake keepalive socket + Compact Gatekeeper wire JSON + GET /api/tutor/state 304 ETag
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'st'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            sock_path = str(td_p / 'disp.sock')
            rx_sock = py_sock.socket(py_sock.AF_UNIX, py_sock.SOCK_DGRAM)
            rx_sock.bind(sock_path)
            rx_sock.setblocking(False)
            call_flag = td_p / 'call_flag'
            mirror_flag = td_p / 'mirror_flag'
            try:
                with mock.patch.object(tut, 'STATE_DIR', state_d), \
                     mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                     mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                     mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                     mock.patch.object(tut, 'CALL_FLAG', call_flag), \
                     mock.patch.object(tut, 'MIRROR_FLAG', mirror_flag), \
                     mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                     mock.patch.dict(os.environ, {
                         'TUTOR_DISPLAY_SOCK': sock_path,
                         'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'),
                         'TUTOR_WARM_GOLDEN': '0',
                     }):
                    eng = tut.TutorEngine()
                    await eng.cowrite.start(subject='auto')
                    pkt = json.loads(rx_sock.recv(256).decode('utf-8'))
                    self.assertEqual(pkt, {'wake': True})
                    # Throttled within 20s when force=False
                    self.assertFalse(eng.cowrite._poke_display_wake(force=False))
                    # Suppressed during active Intercom / AirPlay mirror
                    mirror_flag.write_text('1', encoding='utf-8')
                    eng.cowrite._last_disp_wake_mono = 0.0
                    self.assertFalse(eng.cowrite._poke_display_wake(force=True))
                    mirror_flag.unlink()
                    await eng.cowrite.stop()

                    # Compact Gatekeeper wire keys ('hb', 'wi', 'rot', 'more', 'sum', 'nc', 's') + Qwen 0..1000 box normalization
                    async def fake_gate_post(kind, messages, temperature=0.1, timeout_s=28.0):
                        return json.dumps({
                            'hb': False, 'wi': False, 'rot': 180, 'more': True, 'sum': '完成两题',
                            'nc': [
                                {'no': '第1题', 's': 'phys', 'box': [0.06, 0.12, 0.88, 0.18]},
                                {'no': '第2题', 's': 'chi', 'box': [60, 420, 940, 600]},
                            ],
                        }, ensure_ascii=False)

                    eng._post_chat_once = fake_gate_post
                    g_res = await eng.cowrite._call_gatekeeper(_make_worksheet_jpeg(1), [], 'auto')
                    self.assertEqual(g_res['need_rotate'], 180)
                    self.assertTrue(g_res['has_more'])
                    self.assertEqual(len(g_res['new_completed']), 2)
                    self.assertEqual(g_res['new_completed'][0]['subject'], 'physics')
                    self.assertEqual(g_res['new_completed'][1]['subject'], 'chinese')
                    b2 = g_res['new_completed'][1]['box']
                    self.assertAlmostEqual(b2['x'], 0.06, places=2)
                    self.assertAlmostEqual(b2['y'], 0.42, places=2)
                    self.assertAlmostEqual(b2['w'], 0.88, places=2)
                    self.assertAlmostEqual(b2['h'], 0.18, places=2)

                    app = tut.make_app(engine=eng)
                    async with TestClient(TestServer(app)) as cli:
                        h = {'Host': f'127.0.0.1:{cli.server.port}'}
                        r1 = await cli.get('/api/tutor/state', headers=h)
                        self.assertEqual(r1.status, 200)
                        etag1 = r1.headers.get('ETag')
                        self.assertTrue(bool(etag1))

                        r2 = await cli.get('/api/tutor/state', headers={**h, 'If-None-Match': etag1})
                        self.assertEqual(r2.status, 304)
                        self.assertEqual(await r2.read(), b'')

                        await cli.post('/api/tutor/cowrite/resolve', json={'new_page': True}, headers=h)
                        r3 = await cli.get('/api/tutor/state', headers={**h, 'If-None-Match': etag1})
                        self.assertEqual(r3.status, 200)
                        self.assertNotEqual(r3.headers.get('ETag'), etag1)
            finally:
                rx_sock.close()

    async def test_14_r97_stage_page_checkmarks_sync_silent_nudge_and_modal_carousel(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r97_tutor_sync')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in (
            'lastTutorStateEtag', 'id="cwNudgeBanner"', 'dismissCoWriteNudge',
            'stepCoWriteSnapModal', 'recheckCoWriteModalItem',
            'id="btnCwSnapPrev"', 'id="btnCwSnapNext"', 'id="btnCwModalRecheck"',
        ):
            self.assertIn(tok, h_idx)

        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('给伴写看板递静默锦囊', '代回验增根'):
            self.assertIn(tok, h_mob)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                mock_pause = mock.AsyncMock()
                mock_tts = mock.AsyncMock(return_value=b'RIFF....WAVE')
                eng.pause_music_if_playing = mock_pause
                eng.synthesize_wav = mock_tts

                pid1 = eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=1))
                pid2 = eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=2))
                self.assertEqual(eng.phone_studio.active_id, pid2)

                rev_q = [
                    {
                        'verdict': 'warn', 'confidence': 0.95, 'no': '第9题',
                        'topic': '分式方程增根', 'step_where': '检验步',
                        'checkpoint': '请把解得的 x 代回原分母检验是否为 0',
                        'deep_hint': '分母 x - 2 是否为 0？',
                        'ocr_stem': '解分式方程 x/(x-2) - 1 = 2/(x-2)',
                    },
                    {
                        'verdict': 'ok', 'confidence': 0.97, 'no': '第9题',
                        'topic': '分式方程增根', 'step_where': '',
                        'checkpoint': '已补上验根说明原方程无解 ✓',
                        'deep_hint': '',
                        'ocr_stem': '解分式方程 x/(x-2) - 1 = 2/(x-2)',
                    },
                ]

                async def fake_post_chat(kind, messages, temperature=0.1, timeout_s=36.0):
                    return json.dumps(rev_q.pop(0), ensure_ascii=False)

                eng._post_chat_once = fake_post_chat

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 1. check_crop on pid1 binds stage_page_id & populates pid1 check_marks
                    r_c1 = await cli.post('/api/tutor/cowrite/check_crop', json={
                        'crop': {'x': 0.06, 'y': 0.25, 'w': 0.86, 'h': 0.30},
                        'page_id': pid1,
                    }, headers=h)
                    j_c1 = await r_c1.json()
                    self.assertTrue(j_c1['ok'])
                    item_id = j_c1['item_id']
                    pg1_pub = next(p for p in eng.phone_studio.public_state()['pages'] if p['id'] == pid1)
                    self.assertEqual(len(pg1_pub['check_marks']), 1)
                    self.assertEqual(pg1_pub['check_marks'][0]['status'], 'wrong')

                    # Switch active page back to pid2, then recheck_item -> auto-selects pid1 and flips check_marks to 'ok'!
                    eng.phone_studio.select_page(pid2)
                    r_rc = await cli.post('/api/tutor/cowrite/recheck_item', json={'id': item_id}, headers=h)
                    j_rc = await r_rc.json()
                    self.assertTrue(j_rc['ok'])
                    self.assertEqual(j_rc['verdict'], 'ok')
                    self.assertEqual(eng.phone_studio.active_id, pid1)
                    pg1_after = next(p for p in eng.phone_studio.public_state()['pages'] if p['id'] == pid1)
                    self.assertEqual(pg1_after['check_marks'][0]['status'], 'ok')

                    # 2. Silent parent nudge while Co-Write board has items -> silent=True, zero audio!
                    r_nd = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'parent_nudge',
                        'text': '写得很专注，记得代回验根哦！',
                    }, headers=hm)
                    j_nd = await r_nd.json()
                    self.assertTrue(j_nd['ok'])
                    self.assertTrue(j_nd['silent'])
                    self.assertIn('零语音', j_nd['message'])
                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_15_r98_scrubber_precision_multipage_epoch_and_fresh_recheck(self):
        import time
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r98_tutor_sim1')

        # 1. Scrubber keeps '得出 k', '填 + 还是 -', '选 A 点' intact while scrubbing Chinese dictation '改为“沧”字'
        s_k = tut._scrub_cowrite_spoilers('请把 x = 0 代入原式得出 k 的约束条件，再看二次项系数 ≠ 0。', 80)
        self.assertIn('得出 k 的约束条件', s_k)
        self.assertIn('≠ 0', s_k)
        s_pm = tut._scrub_cowrite_spoilers('请检查第 2 空括号前是 - 号，去括号后填 + 还是 -？', 80)
        self.assertIn('填 + 还是 -', s_pm)
        s_geo = tut._scrub_cowrite_spoilers('连接 OC，若选 A 点与切点构造直角三角形，∠OCA 为多少度？', 80)
        self.assertIn('选 A 点', s_geo)
        s_chi = tut._scrub_cowrite_spoilers('末句“苍”字写错，应改为“沧”字。', 80)
        self.assertNotIn('沧', s_chi)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.cowrite._load_study_bridge_creds = lambda: ('', None)
                gate_q, rev_q = [], []

                async def fake_post_chat(kind, messages, temperature=0.1, timeout_s=36.0):
                    txt_p = str(messages[0]['content'])
                    if '静默伴写守门员' in txt_p:
                        return json.dumps(gate_q.pop(0), ensure_ascii=False)
                    return json.dumps(rev_q.pop(0), ensure_ascii=False)

                eng._post_chat_once = fake_post_chat

                # 2. Multi-page PhoneStudio: Page 1 & Page 2 both have 第1题 & 第2题 -> auto-advances page_epoch without overwriting Page 1!
                pid1 = eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=1))
                gate_q.append({'hb': False, 'wi': False, 'nc': [
                    {'no': '第1题', 's': 'math', 'box': [0.06, 0.12, 0.86, 0.22]},
                    {'no': '第2题', 's': 'math', 'box': [0.06, 0.42, 0.86, 0.25]},
                ]})
                rev_q.extend([
                    {'verdict': 'ok', 'confidence': 0.96, 'no': '第1题', 'topic': '二次根式', 'checkpoint': '正确'},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第2题', 'topic': '分式方程', 'step_where': '验根', 'checkpoint': '漏验增根'},
                ])
                await eng.cowrite.check_now()
                self.assertEqual(len(eng.cowrite.items), 2)

                pid2 = eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=2))
                gate_q.append({'hb': False, 'wi': False, 'nc': [
                    {'no': '第1题', 's': 'phys', 'box': [0.06, 0.12, 0.86, 0.22]},
                    {'no': '第2题', 's': 'phys', 'box': [0.06, 0.42, 0.86, 0.25]},
                ]})
                rev_q.extend([
                    {'verdict': 'ok', 'confidence': 0.96, 'no': '第1题', 'topic': '欧姆定律', 'checkpoint': '正确'},
                    {'verdict': 'ok', 'confidence': 0.96, 'no': '第2题', 'topic': '电功率', 'checkpoint': '正确'},
                ])
                res_p2 = await eng.cowrite.check_now()
                self.assertEqual(res_p2['checked_new'], 2)
                self.assertEqual(len(eng.cowrite.items), 4)
                p1_warn = next(x for x in eng.cowrite.items if x['stage_page_id'] == pid1 and x['no'] == '第2题')
                self.assertEqual(p1_warn['status'], 'warn')

                # 3. Student fixes paper & uploads fresh photo pid3 STRICTLY AFTER p1_warn.ts -> recheck_item uses fresh pid3!
                p1_warn['ts'] = int(time.time()) - 5
                pid3 = eng.phone_studio.save_page(_make_worksheet_jpeg(ink_step=3))
                rev_q.append({'verdict': 'ok', 'confidence': 0.97, 'no': '第2题', 'topic': '分式方程', 'checkpoint': '已补上验根'})
                rc_res = await eng.cowrite.recheck_item(p1_warn['id'])
                self.assertEqual(rc_res['verdict'], 'ok')
                self.assertEqual(eng.phone_studio.active_id, pid3)
                self.assertEqual(p1_warn['stage_page_id'], pid3)

                # 4. Mental-pause writing_incomplete does NOT poison anchor_thumb!
                t_blank = await asyncio.to_thread(eng.cowrite._make_gray_thumb_sync, _make_worksheet_jpeg(0))
                jpg_half = _make_worksheet_jpeg(1)
                t_half = await asyncio.to_thread(eng.cowrite._make_gray_thumb_sync, jpg_half)
                eng.cowrite.anchor_thumb = t_blank
                gate_q.append({'hb': False, 'wi': True, 'nc': []})
                await eng.cowrite._run_funnel_once(jpg_half, lambda: asyncio.sleep(0, result=jpg_half), cur_thumb=t_half, manual=False)
                self.assertEqual(eng.cowrite.anchor_thumb, t_blank)

                # 5. Mobile upload with cowrite_scope='page' runs full-page Gatekeeper even when crop_box is small!
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}
                    gate_q.append({'hb': False, 'wi': False, 'nc': [{'no': '第6题', 's': 'math', 'box': [0.06, 0.70, 0.86, 0.20]}]})
                    rev_q.append({'verdict': 'ok', 'confidence': 0.96, 'no': '第6题', 'topic': '反比例函数', 'checkpoint': '正确'})
                    r_mp = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_check',
                        'page_id': pid2,
                        'crop_box': {'x': 0.05, 'y': 0.12, 'w': 0.80, 'h': 0.22},
                        'cowrite_scope': 'page',
                    }, headers=hm)
                    j_mp = await r_mp.json()
                    self.assertTrue(j_mp['ok'])
                    self.assertTrue(any(x['no'] == '第6题' for x in j_mp['cowrite']['items']))

    async def test_16_r98_mistake_dedup_delete_sync_and_silent_retry(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r98_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in ('cwExplicitNavUntil', 'payload.cowrite_item_id', 'silent: keepSil'):
            self.assertIn(tok, h_idx)
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('userSelectedCrop', 'cowrite_scope', 'pendingCowriteSeq'):
            self.assertIn(tok, h_mob)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                mock_pause = mock.AsyncMock()
                eng.pause_music_if_playing = mock_pause
                eng.cowrite._load_study_bridge_creds = lambda: ('', None)

                async def fake_post_chat(kind, messages, temperature=0.1, timeout_s=36.0):
                    txt_p = str(messages[0]['content'])
                    if '静默伴写守门员' in txt_p:
                        return json.dumps({'hb': False, 'wi': False, 'nc': [{'no': '第8题', 's': 'math', 'box': [0.06, 0.30, 0.86, 0.25]}]}, ensure_ascii=False)
                    return json.dumps({
                        'verdict': 'warn', 'confidence': 0.95, 'no': '第8题',
                        'topic': '一元二次判别式', 'step_where': '含参系数', 'checkpoint': '二次项系数不能为0',
                        'ocr_stem': '关于 x 的一元二次方程 kx^2-2x+1=0 有实根，求 k 范围',
                    }, ensure_ascii=False)

                async def fake_pipeline(sess, q, image_bytes, use_mode, use_ped):
                    sess['question'] = '关于 x 的一元二次方程 kx^2 - 2x + 1 = 0 有两个实数根，求 k 的取值范围'
                    sess['steps'].append({
                        'step': 1, 'title': '隐含条件', 'say': '静音拆解', 'board': 'k ≠ 0 且 Δ ≥ 0',
                        'quiz': {'q': '二次项系数满足什么？', 'options': ['k ≠ 0', 'k = 0'], 'answer': 0}, 'draw': [],
                    })
                    sess['status'] = 'ready'
                    eng._save_to_history(sess)

                eng._post_chat_once = fake_post_chat
                eng._run_live_pipeline = fake_pipeline

                eng.phone_studio.save_page(_make_worksheet_jpeg(1))
                await eng.cowrite.check_now()
                w8 = eng.cowrite.items[0]

                # 1. Enroll mistake first, then explain_item -> must reuse SAME mistake_id (count=1, NOT 2!) and keep '伴写·' tag!
                enr1 = eng.cowrite.enroll_mistake(item_id=w8['id'])
                mid0 = enr1['enrolled_ids'][0]
                self.assertEqual(len(eng.mistakes), 1)

                await eng.cowrite.explain_item(w8['id'], silent=True)
                await asyncio.sleep(0.05)
                self.assertEqual(len(eng.mistakes), 1)
                self.assertEqual(eng.mistakes[0]['id'], mid0)
                self.assertTrue(eng.mistakes[0]['error_tag'].startswith('伴写·'))

                # 2. Deleting the mistake automatically clears w8['enrolled_mistake'] so user can re-enroll cleanly!
                eng.delete_mistake(mid=mid0)
                self.assertEqual(len(eng.mistakes), 0)
                self.assertFalse(w8['enrolled_mistake'])
                self.assertEqual(w8['mistake_id'], '')

                # 3. Silent retry via POST /api/tutor/start preserves silent_start & cowrite_item_id without pausing music!
                mock_pause.reset_mock()
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    r_rt = await cli.post('/api/tutor/start', json={
                        'mode': 'backup', 'pedagogy': 'junior', 'teach_mode': 'diagnose_draft',
                        'source': 'cowrite', 'title': '重试错因诊断', 'question': '解方程 kx^2-2x+1=0',
                        'silent': True, 'cowrite_item_id': w8['id'],
                    }, headers=h)
                    j_rt = await r_rt.json()
                    self.assertTrue(j_rt['ok'])
                    self.assertTrue(j_rt['session']['silent_start'])
                    self.assertEqual(j_rt['session']['cowrite_item_id'], w8['id'])
                    mock_pause.assert_not_called()

                    await asyncio.sleep(0.05)
                    await cli.post('/api/tutor/progress', json={'id': j_rt['session']['id'], 'solved': True}, headers=h)
                    self.assertTrue(w8['resolved'])
                    self.assertTrue(w8['blackboard_solved'])

    async def test_17_r99_uvc_zoom_fps_clear_race_and_stage_rotate_sync(self):
        tut = _load_tutor('r99_tutor_sim1')

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                raw_sheet = _make_worksheet_jpeg(2)

                # 1. UVC zoom=1.5 parity: _pil_direct_gray_thumb_sync matches zoomed capture_jpeg (zero false >=42 page flip!)
                jpg_z = tut.UVCCameraManager._pil_transform_sync(
                    raw_sheet, rot=90, crop_box=None, high_res=False, zoom=1.5, bright_boost=False, pencil_boost=False,
                )
                th_gate = tut.CoWriteMonitor._make_gray_thumb_sync(jpg_z)
                th_dir = tut.UVCCameraManager._pil_direct_gray_thumb_sync(raw_sheet, rot=90, tw=160, th=120, zoom=1.5)
                diff_z = tut.CoWriteMonitor._thumb_diff_metrics(th_gate, th_dir)
                self.assertLess(diff_z['changed_cells'], 5)

                # 2. 5fps stream stays at low_fps=True across Co-Write capture_jpeg & auto-downshifts after preview stops
                low_fps_seen = []

                async def fake_ensure_stream(dev_path, stream_res, low_fps=False):
                    low_fps_seen.append(bool(low_fps))
                    eng.camera._stream_proc = mock.Mock(returncode=None)
                    eng.camera._stream_dev = dev_path
                    eng.camera._stream_res = stream_res
                    eng.camera._stream_low_fps = bool(low_fps)
                    eng.camera._raw_stream_jpg = raw_sheet
                    eng.camera._raw_stream_ts = asyncio.get_running_loop().time()
                    eng.camera._stream_start_ts = eng.camera._raw_stream_ts - 3.0
                    return True

                eng.camera.discover = lambda: {'connected': True, 'dev': '/dev/video99', 'name': 'MockUVC', 'rotate': 0, 'zoom': 1.0, 'bright_boost': False, 'frozen': False}
                eng.camera._is_real_chardev = lambda _d: True
                eng.camera.probe_resolutions = mock.AsyncMock(return_value={'preview_res': '1280x720', 'hi_res': '1280x720', 'input_fmt': 'mjpeg', 'probed': True})
                eng.camera._ensure_stream = fake_ensure_stream
                eng.camera.stop_stream = mock.AsyncMock()

                await eng.cowrite.start()
                await eng.camera.capture_gray_thumb(160, 120)
                await eng.camera.capture_jpeg(high_res=False, crop_box=None, pencil_boost=False)
                self.assertEqual(low_fps_seen, [True, True])
                await eng.cowrite.stop()

                # 3. Clear-all during in-flight Gatekeeper/Reviewer NEVER resurrects zombie cards!
                gate_entered = asyncio.Event()
                gate_release = asyncio.Event()

                async def slow_gate(_g, _c, _s):
                    gate_entered.set()
                    await gate_release.wait()
                    return {
                        'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': False, 'page_summary': 'ok',
                        'new_completed': [{'no': '第1题', 'prob_key': '1', 'subject': 'math', 'box': {'x': 0.1, 'y': 0.2, 'w': 0.8, 'h': 0.25}, 'stem_brief': '旧题'}],
                    }

                eng.cowrite._call_gatekeeper = slow_gate
                eng.cowrite._fetch_review_eval = mock.AsyncMock(return_value=(
                    {'verdict': 'warn', 'confidence': 0.96, 'no': '第1题', 'topic': '分式增根', 'checkpoint': '验分母≠0'}, raw_sheet,
                ))
                t_inf = asyncio.create_task(eng.cowrite._run_funnel_once(raw_sheet, lambda: asyncio.sleep(0, result=raw_sheet), manual=True))
                await gate_entered.wait()
                eng.cowrite.resolve_action(clear_all=True)
                gate_release.set()
                cnt_inf = await t_inf
                self.assertEqual(cnt_inf, 0)
                self.assertEqual(len(eng.cowrite.items), 0)

                # 4. PhoneStudio rotate & clear_all keep Co-Write item boxes and check_marks 100% synchronized!
                pid = eng.phone_studio.save_page(raw_sheet, note='p1')
                orig_box = {'x': 0.10, 'y': 0.20, 'w': 0.70, 'h': 0.25}
                eng.cowrite.items = [{
                    'id': 'cw_rot1', 'no': '第3题', 'prob_key': '3', 'page_epoch': eng.cowrite.page_epoch,
                    'status': 'warn', 'subject': 'math', 'topic': '圆的切线', 'step_where': '辅助线',
                    'checkpoint': '连半径证垂直', 'deep_hint': '', 'ocr_stem': '求证PA是切线',
                    'snap_name': '', 'box': dict(orig_box), 'ts': 100, 'resolved': False,
                    'corrected_pass': False, 'enrolled_mistake': False, 'mistake_id': '',
                    'blackboard_solved': False, 'stage_page_id': pid,
                }]
                eng.cowrite._sync_page_check_marks(pid)
                eng.phone_studio.select_page(pid)
                eng.phone_studio.set_rotate(90)
                self.assertEqual(eng.cowrite.items[0]['box'], tut.PhoneStudioManager._rotate_crop_cw90(orig_box))

                eng.cowrite.resolve_action(clear_all=True)
                pg_after = next(p for p in eng.phone_studio.pages if p['id'] == pid)
                self.assertEqual(pg_after['check_marks'], [])

                # 5. Distinct numbered problems ('1' vs '2') with generous overlapping padded boxes (IoU ~0.52) never merge!
                b_a = {'x': 0.10, 'y': 0.15, 'w': 0.80, 'h': 0.35}
                b_b = {'x': 0.10, 'y': 0.28, 'w': 0.80, 'h': 0.35}
                self.assertGreater(tut.CoWriteMonitor._box_iou(b_a, b_b), 0.44)
                self.assertFalse(tut.CoWriteMonitor._matches_checked_entry('2', b_b, {'prob_key': '1', 'box': b_a}))
                self.assertTrue(tut.CoWriteMonitor._matches_checked_entry('crop_10_28', b_b, {'prob_key': '1', 'box': b_a}))

    async def test_18_r99_silent_aux_tools_and_print_sheet_cowrite_integrity(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r99_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in ('keepSilSolo', 'keepSilSc', 'canBackCwOnSprint', 'wasColdOpen'):
            self.assertIn(tok, h_idx)
        self.assertEqual(tut.detect_subject('第2题. 补写出曹操《观沧海》开篇名句：______，以观______。'), 'chinese')

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                mock_pause = mock.AsyncMock()
                mock_tts = mock.AsyncMock(return_value=b'RIFF')
                eng.pause_music_if_playing = mock_pause
                eng.synthesize_wav = mock_tts
                raw_sheet = _make_worksheet_jpeg(1)
                b64_sheet = base64.b64encode(raw_sheet).decode('ascii')

                async def mock_aux(kind, msgs, temperature=0.2, timeout_s=24.0):
                    return json.dumps({
                        'title': '巧解二', 'badge': '拓展', 'steps': '移项因式分解', 'compare': '交叉验算',
                        'say': '不应在静音会话预取语音', 'script': '解：(x-2)(x+2)=0', 'checkpoints': ['写解', '写答'],
                        'cases': ['n=1', 'n=2', 'n=3'], 'rule_q': '规律？', 'bridge': '代回',
                        'headers': ['骨相', '皮相', '变式'], 'rows': [['等量', '原题', '新题']],
                        'core_bone': '同构', 'insight': '识破', 'pass': True, 'comment': '逻辑清晰',
                        'board': '亮点肯定', 'note': '继续', 'question': '变式新题：解方程 x^2 - 9 = 0',
                    }, ensure_ascii=False)

                eng._post_chat_once = mock_aux

                # 1. CoWrite -> Mistake Book -> prepare_print_sheet(original): clean ans_check with zero UI button text leak!
                eng.cowrite.items = [{
                    'id': 'cw_ps1', 'no': '第5题', 'prob_key': '5', 'page_epoch': 1,
                    'status': 'warn', 'subject': 'chinese', 'topic': '古诗文默写', 'step_where': '下句第2字',
                    'checkpoint': '注意偏旁三点水', 'deep_hint': '结合沧海意象核对字形',
                    'ocr_stem': '补写出《观沧海》名句：东临碣石，以观____。',
                    'snap_name': '', 'box': {'x': 0.1, 'y': 0.2, 'w': 0.8, 'h': 0.2},
                    'ts': 300, 'resolved': False, 'corrected_pass': False, 'enrolled_mistake': False,
                    'mistake_id': '', 'blackboard_solved': False, 'stage_page_id': '',
                }]
                enr = eng.cowrite.enroll_mistake(enroll_all_warns=True)
                ps_orig = await eng.prepare_print_sheet(ids=enr['enrolled_ids'], sheet_mode='original')
                ans_chk = ps_orig['items'][0]['ans_check']
                self.assertNotIn('换数复测', ans_chk)
                self.assertNotIn('先独做', ans_chk)
                self.assertIn('自查：古诗文默写', ans_chk)

                # 2. All auxiliary #viewBoard tools on a silent Co-Write session trigger ZERO music pause & ZERO TTS synthesis!
                eng.sessions['sil_cw_1'] = {
                    'id': 'sil_cw_1', 'title': '静默诊断', 'question': '解方程 x^2 - 4 = 0', 'mode': 'fast',
                    'source': 'cowrite', 'teach_mode': 'diagnose_draft', 'pedagogy': 'junior',
                    'silent_start': True, 'cowrite_item_id': 'cw_ps1', 'status': 'ready', 'seq': 2,
                    'created_at': 100, 't0_mono': 1.0, 'progress': {'solved': True, 'current_step': 4},
                    'steps': [{'step': 1, 'title': 'S1', 'say': 'S1', 'board': 'x=±2 ✓', 'quiz': None, 'draw': []}],
                }
                eng._run_live_pipeline = mock.AsyncMock()
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    for ep, body in (
                        ('/api/tutor/alt_method', {'id': 'sil_cw_1', 'silent': True}),
                        ('/api/tutor/exam_rubric', {'id': 'sil_cw_1', 'silent': True}),
                        ('/api/tutor/exam_rubric', {'id': 'sil_cw_1', 'silent': True}),
                        ('/api/tutor/polya_simplify', {'id': 'sil_cw_1', 'silent': True}),
                        ('/api/tutor/archetype_map', {'id': 'sil_cw_1', 'silent': True}),
                        ('/api/tutor/check_scratch', {'id': 'sil_cw_1', 'silent': True, 'image_b64': b64_sheet}),
                        ('/api/tutor/feynman', {'id': 'sil_cw_1', 'silent': True, 'text': '平方根有两个互为相反数的解'}),
                        ('/api/tutor/variant', {'id': 'sil_cw_1', 'silent': True}),
                    ):
                        r_aux = await cli.post(ep, json=body, headers=h)
                        j_aux = await r_aux.json()
                        self.assertTrue(j_aux['ok'], f'{ep} failed: {j_aux}')
                        if ep == '/api/tutor/variant':
                            self.assertEqual(j_aux['session']['source'], 'cowrite')
                            self.assertTrue(j_aux['session']['silent_start'])
                    await asyncio.sleep(0.05)
                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_19_r100_snap_quota_partial_gather_crop_subject_and_spoilers(self):
        tut = _load_tutor('r100_tutor_sim1')
        self.assertEqual(tut.CoWriteMonitor.MAX_COWRITE_SNAPS, tut.CoWriteMonitor.MAX_BOARD_ITEMS)

        # 1. Unquoted Chinese character leaks & Unicode subscript formula leaks scrubbed while keeping safe math hints
        for leak_in, forbidden in (
            ('注意海前边的字，不是苍而是沧', '沧'),
            ('误把沧写成了苍，请核对偏旁', '沧'),
            ('此处应写沧字', '沧'),
            ('生成物应改为 Fe₃O₄', 'Fe₃O₄'),
            ('气体符号漏标，应补上 O₂↑', 'O₂'),
        ):
            self.assertNotIn(forbidden, tut._scrub_cowrite_spoilers(leak_in))
        self.assertEqual(tut._scrub_cowrite_spoilers('想一想括号外去负号后应填 + 还是 -'), '想一想括号外去负号后应填 + 还是 -')

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                raw_sheet = _make_worksheet_jpeg(2)

                # 2. Saving >20 active Co-Write items + 6 orphan c0_*.jpg files NEVER deletes active card snaps!
                for k in range(6):
                    orphan_p = snap_d / f'c0_ffffffff000{k}.jpg'
                    orphan_p.parent.mkdir(parents=True, exist_ok=True)
                    orphan_p.write_bytes(b'orphan')
                for idx in range(22):
                    sn_i = eng.cowrite._save_cowrite_snap(raw_sheet)
                    eng.cowrite.items.insert(0, {
                        'id': f'cw_s{idx}', 'no': f'第{idx + 1}题', 'prob_key': str(idx + 1), 'page_epoch': 1,
                        'status': 'warn' if idx == 0 else 'ok', 'subject': 'math', 'topic': '方程',
                        'step_where': '', 'checkpoint': 'ok', 'deep_hint': '', 'ocr_stem': '',
                        'snap_name': sn_i, 'box': {'x': 0.1, 'y': 0.1, 'w': 0.5, 'h': 0.2}, 'ts': 100 + idx,
                        'resolved': False, 'corrected_pass': False, 'enrolled_mistake': False,
                        'mistake_id': '', 'blackboard_solved': False, 'stage_page_id': '',
                    })
                eng.cowrite._prune_cowrite_snaps()
                pub_st = eng.cowrite.public_state()
                self.assertEqual(len(pub_st['items']), 22)
                for it_pub in pub_st['items']:
                    self.assertTrue(it_pub['snap_url'].startswith('/api/tutor/snap/c0_'))
                    sn_fn = it_pub['snap_url'].rsplit('/', 1)[-1]
                    self.assertTrue((snap_d / sn_fn).is_file(), f'Missing active snap {sn_fn}')

                # 3. Partial gather failure in _run_funnel_once keeps succeeded sibling cards and flags _has_more_pending=True!
                eng.cowrite.resolve_action(clear_all=True)
                eng.cowrite._call_gatekeeper = mock.AsyncMock(return_value={
                    'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': False,
                    'page_summary': '2题完成',
                    'new_completed': [
                        {'no': '第1题', 'prob_key': '1', 'subject': 'physics', 'box': {'x': 0.1, 'y': 0.1, 'w': 0.8, 'h': 0.3}, 'stem_brief': '欧姆定律'},
                        {'no': '第2题', 'prob_key': '2', 'subject': 'chemistry', 'box': {'x': 0.1, 'y': 0.5, 'w': 0.8, 'h': 0.3}, 'stem_brief': '方程式'},
                    ],
                })

                async def flaky_review(hi_b, cand_b, subj_pref, pencil_boost=False):
                    if cand_b['no'] == '第2题':
                        raise TimeoutError('upstream transient timeout')
                    return ({
                        'verdict': 'ok', 'confidence': 0.96, 'no': '第1题', 'topic': '串联电流',
                        'ocr_stem': '电阻 R1=4Ω 串联 R2=6Ω', 'checkpoint': '正确 ✓',
                    }, raw_sheet)

                eng.cowrite._fetch_review_eval = flaky_review
                cnt_ok = await eng.cowrite._run_funnel_once(raw_sheet, lambda: asyncio.sleep(0, result=raw_sheet), manual=True)
                self.assertEqual(cnt_ok, 1)
                self.assertEqual(len(eng.cowrite.items), 1)
                self.assertEqual(eng.cowrite.items[0]['no'], '第1题')
                self.assertTrue(eng.cowrite._has_more_pending)

                # 4. check_crop in default 'auto' mode infers non-math subject ('chemistry') from ocr_stem + topic!
                async def chem_crop_review(hi_b, cand_b, subj_pref, pencil_boost=False):
                    return ({
                        'verdict': 'warn', 'confidence': 0.95, 'no': '第8题', 'topic': '化学方程式配平',
                        'ocr_stem': '实验室用过氧化氢和二氧化锰制氧气：H2O2 -> H2O + O2',
                        'step_where': '系数与气体符号', 'checkpoint': '请核对氧原子个数与气体生成符号',
                        'deep_hint': '反应前后氢氧原子数目是否相等？',
                    }, raw_sheet)

                eng.cowrite._fetch_review_eval = chem_crop_review
                eng.cowrite.subject = 'auto'
                eng.phone_studio.save_page(raw_sheet)
                res_cc = await eng.cowrite.check_crop(crop_box={'x': 0.1, 'y': 0.2, 'w': 0.8, 'h': 0.3})
                self.assertTrue(res_cc['ok'])
                cc_item = next(x for x in eng.cowrite.items if x['id'] == res_cc['item_id'])
                self.assertEqual(cc_item['subject'], 'chemistry')

    async def test_20_r100_silent_visibility_mobile_subject_preserve_and_sprint_ocr(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r100_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('resumeSilentStep0IfStranded', 'stopLessonPlayback(true)', 'forceUnmute'):
            self.assertIn(tok, h_idx)
        self.assertIn('isSilNudge', h_mob)
        self.assertIn('(baseCanvas || serverHasStaged) && userSelectedCrop', h_mob)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                mock_pause = mock.AsyncMock()
                mock_tts = mock.AsyncMock(return_value=b'RIFF')
                eng.pause_music_if_playing = mock_pause
                eng.synthesize_wav = mock_tts
                eng._run_live_pipeline = mock.AsyncMock()
                raw_sheet = _make_worksheet_jpeg(1)
                b64_sheet = base64.b64encode(raw_sheet).decode('ascii')

                # 1. Enroll a Chinese Co-Write item whose ocr_stem was empty (only c0_*.jpg) -> sprint & retest OCR-recover stem & preserve chinese subject!
                sn_ch = eng.cowrite._save_cowrite_snap(raw_sheet)
                eng.cowrite.items = [{
                    'id': 'cw_ch1', 'no': '第3题', 'prob_key': '3', 'page_epoch': 1,
                    'status': 'warn', 'subject': 'chinese', 'topic': '古诗文默写', 'step_where': '下句末二字',
                    'checkpoint': '核对偏旁', 'deep_hint': '与大海相关用三点水',
                    'ocr_stem': '', 'snap_name': sn_ch, 'box': {'x': 0.1, 'y': 0.2, 'w': 0.8, 'h': 0.2},
                    'ts': 500, 'resolved': False, 'corrected_pass': False, 'enrolled_mistake': False,
                    'mistake_id': '', 'blackboard_solved': False, 'stage_page_id': '',
                }]
                enr = eng.cowrite.enroll_mistake(enroll_all_warns=True)
                mid_ch = enr['enrolled_ids'][0]
                eng._ocr_question_from_image = mock.AsyncMock(return_value='补写出李白《行路难》名句：长风破浪会有时，____________。')
                eng._post_chat_once = mock.AsyncMock(return_value='{"title":"特训：行路难","question":"请默写《行路难》结尾鼓舞信心的两句诗。"}')

                sp_res = await eng.start_mistake_sprint(count=2, mode='fast', pedagogy='junior', silent_tts=True)
                self.assertEqual(sp_res['total'], 2)
                # Both sprint items come from the student's actual Chinese mistake (progressive加固), NOT random elementary sample problems!
                sp_obj = eng.sprints[sp_res['sprint_id']]
                self.assertEqual(sp_obj['items'][0]['mid'], mid_ch)
                self.assertEqual(sp_obj['items'][1]['mid'], mid_ch)
                self.assertIn('行路难', eng.mistakes[0]['question'])

                # 2. Mobile cowrite_check without subject preserves user's big-screen subject ('chinese'), and mobile retest stays silent!
                eng.cowrite.subject = 'chinese'
                eng.cowrite.check_now = mock.AsyncMock(return_value={'ok': True, 'cowrite': eng.cowrite.public_state()})
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}
                    r_mcw = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_check', 'page_b64': b64_sheet, 'cowrite_scope': 'page',
                    }, headers=hm)
                    self.assertTrue((await r_mcw.json())['ok'])
                    eng.cowrite.check_now.assert_awaited_once_with(subject=None, pencil_boost=False)
                    self.assertEqual(eng.cowrite.subject, 'chinese')

                    r_mrt = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'trigger_mistake_retest', 'mistake_id': mid_ch,
                    }, headers=hm)
                    j_mrt = await r_mrt.json()
                    self.assertTrue(j_mrt['ok'])
                    rt_sid = j_mrt['session']['id']
                    self.assertTrue(eng.sessions[rt_sid]['silent_start'])
                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_21_r101_short_ink_latex_unicode_xyxy_and_corrupt_state(self):
        tut = _load_tutor('r101_tutor_sim1')

        # 1. LaTeX math converted to clean Unicode BEFORE spoiler scrubbing
        s_ltx = tut._scrub_cowrite_spoilers(r'请核对判别式 $\Delta = b^2 - 4ac \ge 0$，计算 $x = \frac{3}{2}$ 有误')
        self.assertIn('Δ = b² - 4ac ≥ 0', s_ltx)
        self.assertNotIn('\\Delta', s_ltx)
        self.assertNotIn('3/2', s_ltx)
        self.assertNotIn('5', tut._scrub_cowrite_spoilers(r'计算得 $= -\sqrt{5}$ 不合题意'))

        # 2. Short MC/fill-in answer strokes ('B' / 'x=3') detected via ink_pixels & max_px_diff while noise stays 0
        b0 = bytes([242] * (160 * 120))
        b_mc = bytearray(b0)
        for dx in range(5):
            b_mc[28 * 160 + 110 + dx] = 180
        dm_mc = tut.CoWriteMonitor._thumb_diff_metrics(b0, bytes(b_mc))
        self.assertGreaterEqual(dm_mc['ink_pixels'], 5)
        self.assertGreaterEqual(dm_mc['max_px_diff'], 55)
        self.assertTrue(tut.CoWriteMonitor._box_has_ink_change(b0, bytes(b_mc), {'x': 0.5, 'y': 0.1, 'w': 0.4, 'h': 0.3}))

        b_noise = bytes([240 if (i % 2 == 0) else 244 for i in range(160 * 120)])
        dm_noise = tut.CoWriteMonitor._thumb_diff_metrics(b0, b_noise)
        self.assertEqual(dm_noise['ink_pixels'], 0)

        # 3. Right-column / bottom-half [xmin, ymin, xmax, ymax] 0..1 float box normalized accurately
        bx_xy = tut.CoWriteMonitor._normalize_gate_box([0.52, 0.60, 0.96, 0.88])
        self.assertAlmostEqual(bx_xy['w'], 0.44, places=2)
        self.assertAlmostEqual(bx_xy['h'], 0.28, places=2)

        # 4. Corrupt cowrite.json degrades gracefully without crashing daemon startup
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            state_d.mkdir(parents=True)
            (state_d / 'cowrite_state.json').write_text(json.dumps({
                'page_epoch': 'bad_epoch', 'seq': 'bad_seq', 'stats': 'bad_stats',
                'items': [{'id': 'cw_c1', 'status': 'warn', 'ts': 'bad_ts', 'page_epoch': 'bad_ep'}],
            }), encoding='utf-8')
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', state_d / 'snaps'), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.dict(os.environ, {'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                self.assertEqual(eng.cowrite.page_epoch, 1)
                self.assertEqual(len(eng.cowrite.items), 1)
                self.assertEqual(eng.cowrite.items[0]['ts'], 0)

    async def test_22_r101_mistake_replay_silent_voice_nl_and_mobile_slice(self):
        import time
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r101_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('m.cowrite_id', h_idx)
        self.assertIn('html.portrait-ui #cwCtrlNote', h_idx)
        self.assertIn('!sessToAttach.silent_start', h_idx)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.start_live_problem = mock.MagicMock()
                raw_sheet = _make_worksheet_jpeg(1)
                pid_s = eng.phone_studio.save_page(raw_sheet)

                # Populate 14 older due mistakes + 1 newly enrolled Co-Write mistake
                now_ts = int(time.time())
                for i in range(14):
                    eng.mistakes.append({
                        'id': f'mb_old_{i}', 'orig_sid': f'm:mb_old_{i}', 'title': f'旧错题{i}',
                        'question': f'x + {i} = 10', 'subject': 'math', 'pedagogy': 'junior',
                        'error_tag': '计算失误', 'stage': 0, 'graduated': False,
                        'created_at': now_ts - 99999, 'updated_at': now_ts - 9999, 'next_review_at': now_ts - 10,
                        'steps': [],
                    })
                eng.cowrite.items = [{
                    'id': 'cw_r101', 'no': '第6题', 'prob_key': '6', 'page_epoch': 1,
                    'status': 'warn', 'subject': 'physics', 'topic': '电功率', 'step_where': '公式选择',
                    'checkpoint': '核对串并联电压关系', 'deep_hint': '', 'ocr_stem': 'R1与R2并联求电功率',
                    'snap_name': '', 'box': {'x': 0.1, 'y': 0.2, 'w': 0.7, 'h': 0.25}, 'ts': now_ts,
                    'resolved': False, 'corrected_pass': False, 'enrolled_mistake': False,
                    'mistake_id': '', 'blackboard_solved': False, 'stage_page_id': pid_s,
                }]
                eng.cowrite._sync_page_check_marks(pid_s)
                pg_mk = eng.phone_studio.public_state()['pages'][0]['check_marks'][0]
                self.assertEqual(pg_mk['cowrite_id'], 'cw_r101')

                enr = eng.cowrite.enroll_mistake(enroll_all_warns=True)
                cw_mid = enr['enrolled_ids'][0]

                # Reopening m:{cw_mid} returns silent_start=True, source='cowrite', subject='physics'!
                m_pub = eng.public_session(f'm:{cw_mid}')
                self.assertTrue(m_pub['silent_start'])
                self.assertEqual(m_pub['source'], 'cowrite')
                self.assertEqual(m_pub['subject'], 'physics')

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}
                    # Mobile state still surfaces the newly enrolled Co-Write mistake even with 14 older due mistakes!
                    r_ms = await cli.get('/api/tutor/mobile/state', headers=hm)
                    j_ms = await r_ms.json()
                    self.assertTrue(any(m['id'] == cw_mid for m in j_ms['mistakes']))

                    # Hydrating m:{cw_mid} via ?activate=1 ALSO preserves silent_start=True, source='cowrite', subject='physics'!
                    r_act_m = await cli.get(f'/api/tutor/session?id=m:{cw_mid}&activate=1', headers=h)
                    j_act_m = (await r_act_m.json())['session']
                    self.assertTrue(j_act_m['silent_start'])
                    self.assertEqual(j_act_m['source'], 'cowrite')
                    self.assertEqual(j_act_m['subject'], 'physics')

                    # Voice NL commands ('暂停伴写', '伴写换新页', '清空静默伴写') route to CoWriteMonitor, NEVER start_live_problem!
                    await eng.cowrite.toggle(enabled=True)
                    r_v1 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '暂停伴写'}, headers=hm)
                    self.assertTrue((await r_v1.json())['ok'])
                    self.assertFalse(eng.cowrite.enabled)

                    ep_before = eng.cowrite.page_epoch
                    r_v2 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '静默伴写换新页'}, headers=hm)
                    self.assertTrue((await r_v2.json())['ok'])
                    self.assertEqual(eng.cowrite.page_epoch, ep_before + 1)

                    r_v3 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '清空静默伴写看板'}, headers=hm)
                    self.assertTrue((await r_v3.json())['ok'])
                    self.assertEqual(eng.cowrite.items, [])
                    eng.start_live_problem.assert_not_called()

    async def test_23_r102_subq_prob_key_micro_watch_and_preserved_warn_trap(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r102_tutor_sim1')
        cw_cls = tut.CoWriteMonitor

        # 1. Subquestion prob_key normalization never collapses (1)/(2) across Chinese exam formats
        self.assertEqual(cw_cls._norm_prob_key('第15题(1)'), '15(1)')
        self.assertEqual(cw_cls._norm_prob_key('第15题(2)'), '15(2)')
        self.assertEqual(cw_cls._norm_prob_key('15.(1)'), '15(1)')
        self.assertEqual(cw_cls._norm_prob_key('15、(2)'), '15(2)')
        self.assertEqual(cw_cls._norm_prob_key('第23题第2问'), '23(2)')

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_DISPLAY_SOCK': str(td_p / 'disp.sock'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                cw = eng.cowrite

                # 2. _watch_loop wakes on quick single-letter 'A' micro-ink stroke after saw_activity=False
                th_base = bytes([236] * (160 * 120))
                th_a = bytearray(th_base)
                for dx in range(6):
                    th_a[24 * 160 + 100 + dx] = 160
                th_after = bytes(th_a)
                cw.POLL_INTERVAL_SEC = 0.01
                cw.PAUSE_SETTLE_SEC = 0.015
                cw.MIN_GATE_INTERVAL_SEC = 0.01
                seq_frames = [th_after, th_after, th_after, th_after, th_after]
                st_w = {'i': 0, 'triggered': False}
                eng.camera.discover = lambda: {'connected': True, 'dev': '/dev/video99', 'name': 'MockCam'}

                async def fake_thumb(tw=160, th=120):
                    i = st_w['i']
                    st_w['i'] += 1
                    if i >= len(seq_frames):
                        cw.enabled = False
                        return th_after
                    return seq_frames[i]

                async def fake_cap_jpg(**kw):
                    return _make_worksheet_jpeg(1)

                async def fake_funnel(*a, **kw):
                    st_w['triggered'] = True
                    cw.enabled = False
                    return 1

                eng.camera.capture_gray_thumb = fake_thumb
                eng.camera.capture_jpeg = fake_cap_jpg
                orig_funnel = cw._run_funnel_once
                cw._run_funnel_once = fake_funnel
                cw.enabled = True
                cw.prev_thumb = th_base
                cw.anchor_thumb = th_base
                cw.saw_activity = False
                cw._still_streak = 2
                cw.last_motion_mono = time.monotonic() - 10.0
                await cw._watch_loop()
                cw._run_funnel_once = orig_funnel
                self.assertTrue(st_w['triggered'])

                # 3. Intra-batch duplicate candidate deduplication in _call_gatekeeper
                async def fake_chat(kind, messages, **kw):
                    if kind == 'fast':
                        return json.dumps({
                            'hb': False, 'wi': False, 'rot': 0, 'more': False, 'sum': '完成第2题',
                            'nc': [
                                {'no': '第2题', 's': 'math', 'box': [0.06, 0.30, 0.88, 0.24]},
                                {'no': '2.', 's': 'math', 'box': [0.07, 0.31, 0.86, 0.23]},
                            ],
                        }, ensure_ascii=False)
                    return json.dumps({
                        'title': '分式方程增根复测', 'question': r'解分式方程 $\frac{x}{x-3} - 1 = \frac{3}{x-3}$',
                        'ans_check': r'解得 $x=3$，经检验 $x-3=0$ 为增根，原方程无解',
                    }, ensure_ascii=False)

                eng._post_chat_once = fake_chat
                cw._load_study_bridge_creds = lambda: ('', None)
                g_out = await cw._call_gatekeeper(_make_worksheet_jpeg(1), [], 'auto')
                self.assertEqual(len(g_out['new_completed']), 1)

                # 4. warn -> blackboard_solved reset on re-warn -> corrected_pass preserves original warn trap for Mistake Book & A4 Print Sheet
                raw_sheet = _make_worksheet_jpeg(2)
                cand_2 = {'no': '第2题', 'prob_key': '2', 'subject': 'math', 'box': {'x': 0.06, 'y': 0.30, 'w': 0.88, 'h': 0.24}}
                r_w1 = cw._upsert_reviewed_candidate(cand_2, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第2题', 'topic': '分式方程增根',
                    'step_where': '去分母', 'checkpoint': '请回查常数项是否漏乘公分母',
                    'deep_hint': '等号右边 -1 乘 (x-2) 了吗？', 'ocr_stem': r'解分式方程 $\frac{x}{x-2} - 1 = \frac{2}{x-2}$',
                }, raw_sheet)
                cw.mark_blackboard_solved(r_w1['id'])
                r_w2 = cw._upsert_reviewed_candidate(dict(cand_2, _force_id=r_w1['id']), {
                    'verdict': 'warn', 'confidence': 0.96, 'no': '第2题', 'topic': '分式方程增根',
                    'step_where': '验根步', 'checkpoint': '请回查所得解是否使原分母为零',
                    'deep_hint': '把 x=2 代入分母 x-2 是否为 0？', 'ocr_stem': r'解分式方程 $\frac{x}{x-2} - 1 = \frac{2}{x-2}$',
                }, raw_sheet)
                self.assertFalse(r_w2['blackboard_solved'])

                cw._upsert_reviewed_candidate(dict(cand_2, _force_id=r_w1['id']), {
                    'verdict': 'ok', 'confidence': 0.97, 'no': '第2题', 'topic': '分式方程增根',
                    'checkpoint': '', 'ocr_stem': r'解分式方程 $\frac{x}{x-2} - 1 = \frac{2}{x-2}$',
                }, raw_sheet)
                enr = cw.enroll_mistake(enroll_all_warns=True)
                self.assertEqual(enr['enrolled_count'], 1)
                mb = eng.mistakes[0]
                self.assertIn('分母为零', mb['weak_focus'])
                self.assertNotIn('正确', mb['weak_focus'])

                sheet_orig = await eng.prepare_print_sheet(ids=[mb['id']], sheet_mode='original', pedagogy='junior')
                ans_orig = sheet_orig['items'][0]['ans_check']
                self.assertIn('分母为零', ans_orig)
                self.assertNotIn('正确 ✓', ans_orig)
                sheet_var = await eng.prepare_print_sheet(ids=[mb['id']], sheet_mode='variant', pedagogy='junior')
                self.assertNotIn('\\frac', sheet_var['items'][0]['question'])
                self.assertNotIn('$', sheet_var['items'][0]['ans_check'])

                # 5. PhoneStudioManager reload across socket-idle wake preserves cowrite_id on check_marks
                pid_st = eng.phone_studio.save_page(raw_sheet, note='p1')
                cw.items[0]['stage_page_id'] = pid_st
                cw.items[0]['status'] = 'warn'
                cw.items[0]['resolved'] = False
                cw._sync_page_check_marks(pid_st)
                ps_reloaded = tut.PhoneStudioManager()
                mk_reloaded = ps_reloaded.public_state()['check_marks'][0]
                self.assertEqual(mk_reloaded['cowrite_id'], r_w1['id'])

    async def test_24_r102_latex_trig_circle_spoilers_silent_read_aloud_and_junior_pedagogy(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r102_tutor_sim2')

        # 1. LaTeX Unicode converter: circle/trig/degree/because macros & zero prefix macro corruption
        u_geo = tut._cowrite_latex_to_unicode(r'在 $\odot O$ 中，$\widehat{AB}$ 所对圆周角 $\angle C = 30^\circ$，核对 $\sin 30^\circ$ 与 $\because OA \perp BC \therefore$ 平分弦')
        self.assertIn('⊙ O', u_geo)
        self.assertIn('弧AB', u_geo)
        self.assertIn('30°', u_geo)
        self.assertNotIn('^°', u_geo)
        self.assertIn('sin 30°', u_geo)
        self.assertIn('∵', u_geo)
        self.assertIn('∴', u_geo)
        u_macro = tut._cowrite_latex_to_unicode(r'\newline \top \leadsto')
        self.assertNotIn('≠wline', u_macro)
        self.assertNotIn('→p', u_macro)
        self.assertNotIn('≤adsto', u_macro)

        # 2. Spoiler scrubber catches parenthesized quadratic/radical formula roots while keeping algebraic formulas intact
        sp_root1 = tut._scrub_cowrite_spoilers(r'求根公式算得 x = \frac{-1 \pm \sqrt{5}}{2}')
        sp_root2 = tut._scrub_cowrite_spoilers(r'求根公式算得 x = \frac{1 + \sqrt{5}}{2}')
        self.assertNotIn('√5', sp_root1)
        self.assertNotIn('√5', sp_root2)
        sp_alg = tut._scrub_cowrite_spoilers('请代回对称轴公式 x = -b/(2a) 核对符号')
        self.assertIn('-b/(2a)', sp_alg)

        # 3. Frontend guards in index.html & mobile.html
        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in ('function sessionEffectivePedagogy()', 'onActiveBoardLesson', 'if (silentBoardMode)', "setCameraSubMode('snap', true)", 'forceSilent = false'):
            self.assertIn(tok, h_idx)
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('m.cowrite_id', h_mob)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_DISPLAY_SOCK': str(td_p / 'disp.sock'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                mock_pause = mock.AsyncMock()
                mock_tts = mock.AsyncMock(return_value=b'RIFF....WAVE')
                eng.pause_music_if_playing = mock_pause
                eng.synthesize_wav = mock_tts

                captured_prompts = []

                async def fake_post_chat(kind, messages, **kw):
                    sys_txt = ' '.join(str(m.get('content') or '') for m in messages)
                    captured_prompts.append(sys_txt)
                    if '朗读' in sys_txt or '跟读' in sys_txt:
                        return json.dumps({'stars': 3, 'badge': '朗读流利', 'praise': '很棒', 'tip': '保持', 'say': '读得真准'}, ensure_ascii=False)
                    if '满分' in sys_txt or '阅卷' in sys_txt:
                        return json.dumps({'badge': '中考规范', 'title': '满分示范', 'script': '解：设...', 'checkpoints': ['设元', '验根'], 'say': '注意验根'}, ensure_ascii=False)
                    return json.dumps({'title': '初三变式', 'question': '【初中物理】定值电阻 R=10Ω 串联求电功率'}, ensure_ascii=False)

                async def fake_run_pipeline(sess, q, image_bytes, use_mode, use_ped):
                    sess['pedagogy'] = use_ped
                    sess['steps'] = [{'step': 1, 'title': 'S1', 'say': 'S1', 'board': 'B1', 'quiz': None, 'draw': []}]
                    sess.setdefault('progress', {})['solved'] = True
                    sess['status'] = 'ready'
                    eng._save_to_history(sess)

                eng._post_chat_once = fake_post_chat
                eng._run_live_pipeline = fake_run_pipeline

                s_cw = eng.start_live_problem(
                    question='【初中英语】He has lived here since 2020.',
                    source='cowrite', pedagogy='junior', silent_tts=True, open_ui=False,
                )
                await asyncio.sleep(0.04)
                sid_cw = s_cw['id']
                eng.sessions[sid_cw]['read_aloud_target'] = {
                    'kind': 'english', 'text': 'He has lived here since 2020.',
                    'display': 'He has lived here since 2020.', 'badge': '语感跟读',
                }

                # History reload preserves subject='english' & pedagogy='junior'
                eng.sessions.clear()
                h_pub = eng.public_session(sid_cw)
                self.assertEqual(h_pub['subject'], 'english')
                self.assertEqual(h_pub['pedagogy'], 'junior')

                # Re-register session for API tests
                s_cw2 = eng.start_live_problem(
                    question='【初中物理】定值电阻 R1=10Ω 与滑动变阻器串联求最大电功率',
                    source='cowrite', pedagogy='junior', silent_tts=True, open_ui=False,
                )
                await asyncio.sleep(0.04)
                sid_p = s_cw2['id']
                eng.sessions[sid_p]['read_aloud_target'] = {
                    'kind': 'english', 'text': 'Power equals voltage times current.',
                    'display': 'P = U I', 'badge': '跟读',
                }

                # Enroll a Co-Write mistake and test voice/default retest_mistake(silent_tts=None)
                eng.cowrite.items = [{
                    'id': 'cw_m24', 'no': '第8题', 'prob_key': '8', 'page_epoch': 1,
                    'status': 'warn', 'subject': 'physics', 'topic': '动态电路', 'step_where': '欧姆定律',
                    'checkpoint': '核对滑动变阻器滑片移动方向', 'deep_hint': '', 'ocr_stem': 'R1与R2串联滑片右移求电压表变化',
                    'snap_name': '', 'box': None, 'ts': 300, 'resolved': False, 'corrected_pass': False,
                    'enrolled_mistake': False, 'mistake_id': '', 'blackboard_solved': False,
                }]
                mb_id = eng.cowrite.enroll_mistake(enroll_all_warns=True)['enrolled_ids'][0]
                rt_res = await eng.retest_mistake(mb_id, mode='fast', pedagogy='elem', open_ui=False)
                await asyncio.sleep(0.04)
                self.assertTrue(rt_res['session']['silent_start'])
                self.assertEqual(rt_res['session']['pedagogy'], 'junior')

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # Silent read_aloud on English Co-Write session -> zero TTS synthesis!
                    r_ra = await cli.post('/api/tutor/read_aloud', json={'id': sid_cw, 'text': 'He has lived here since 2020', 'silent': True}, headers=h)
                    self.assertTrue((await r_ra.json())['ok'])

                    # Sending pedagogy='elem' on a Grade-9 Co-Write session still enforces 初中九年级 rubric & variant!
                    captured_prompts.clear()
                    r_rub = await cli.post('/api/tutor/exam_rubric', json={'id': sid_p, 'pedagogy': 'elem', 'silent': True}, headers=h)
                    self.assertTrue((await r_rub.json())['ok'])
                    self.assertTrue(any('中考' in p or '初中' in p for p in captured_prompts))

                    r_var = await cli.post('/api/tutor/variant', json={'id': sid_p, 'pedagogy': 'elem', 'silent': True}, headers=h)
                    j_var = await r_var.json()
                    self.assertTrue(j_var['ok'])
                    await asyncio.sleep(0.04)
                    self.assertEqual(eng.sessions[j_var['session']['id']]['pedagogy'], 'junior')

                    # Mobile trigger_variant while cowrite.enabled=True inherits silent_start=True!
                    await eng.cowrite.toggle(enabled=True)
                    r_mv = await cli.post('/api/tutor/mobile/upload', json={'action': 'trigger_variant', 'difficulty': 'same'}, headers=hm)
                    j_mv = await r_mv.json()
                    self.assertTrue(j_mv['ok'])
                    self.assertTrue(j_mv['silent'])
                    self.assertTrue(j_mv['session']['silent_start'])
                    await eng.cowrite.stop()

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_25_r103_formula_tip_batch_recheck_pace_and_faint_pencil_lut(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r103_tutor_sim1')

        # 1. Synthetic faint-pencil JPEG (~174 gray on 244 white paper) & glare JPEG (>34% >= 249 in center)
        im_pen = Image.new('RGB', (600, 320), (244, 244, 242))
        dr_p = ImageDraw.Draw(im_pen)
        for yy in range(40, 260, 44):
            dr_p.line([(60, yy), (520, yy)], fill=(174, 174, 174), width=3)
        buf_pen = io.BytesIO()
        im_pen.save(buf_pen, format='JPEG', quality=92)
        jpg_pen = buf_pen.getvalue()

        im_gl = Image.new('RGB', (400, 300), (234, 234, 234))
        ImageDraw.Draw(im_gl).rectangle([(60, 40), (340, 260)], fill=(254, 254, 254))
        buf_gl = io.BytesIO()
        im_gl.save(buf_gl, format='JPEG', quality=92)
        jpg_gl = buf_gl.getvalue()

        # Verify faint-pencil white-anchored LUT preserves white paper (>=236) while darkening 174 pencil strokes (<170)
        crop_b = tut.CoWriteMonitor._crop_problem_jpeg_sync(jpg_pen, {'x': 0.05, 'y': 0.08, 'w': 0.90, 'h': 0.80}, False)
        im_out = Image.open(io.BytesIO(crop_b)).convert('L')
        px_paper = im_out.getpixel((5, 5))
        min_stroke = min(im_out.getpixel((100, y)) for y in range(im_out.height))
        self.assertGreaterEqual(px_paper, 236)
        self.assertLess(min_stroke, 170)

        # Verify central glare metric >= 0.34
        fq_gl = tut.CoWriteMonitor._frame_quality_metrics_sync(jpg_gl)
        self.assertGreaterEqual(fq_gl.get('glare_ratio', 0.0), 0.34)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.camera.discover = mock.MagicMock(return_value={'connected': False, 'dev': '', 'name': ''})
                cw = eng.cowrite

                # Glare frame in _run_funnel_once triggers lighting_warn='glare'
                await cw._run_funnel_once(jpg_gl, lambda: asyncio.sleep(0, result=jpg_gl), manual=False)
                self.assertEqual(cw.lighting_warn, 'glare')

                # Stage worksheet page and upsert two warn items with 3rd-tier formula_tip (including raw LaTeX/spoiler)
                pid_s = eng.phone_studio.save_page(jpg_pen, note='p1')
                c1 = {'no': '第1题', 'prob_key': '1', 'subject': 'math', 'stage_page_id': pid_s, 'box': {'x': 0.05, 'y': 0.08, 'w': 0.90, 'h': 0.40}}
                c2 = {'no': '第2题', 'prob_key': '2', 'subject': 'math', 'stage_page_id': pid_s, 'box': {'x': 0.05, 'y': 0.50, 'w': 0.90, 'h': 0.42}}
                it1 = cw._upsert_reviewed_candidate(c1, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第1题', 'topic': '一元二次方程',
                    'step_where': '判别式', 'checkpoint': '核对两不等实根判别式符号',
                    'deep_hint': 'Δ 能取 0 吗？',
                    'formula_tip': r'判别式 $\Delta = b^2 - 4ac > 0$，解得 x = 3',
                    'ocr_stem': '关于x的一元二次方程 x² - 4x + k = 0 有两个不相等实根，求k范围。',
                }, jpg_pen)
                self.assertNotIn('$', it1['formula_tip'])
                self.assertIn('Δ = b² - 4ac > 0', it1['formula_tip'])
                self.assertNotIn('x = 3', it1['formula_tip'])

                it2 = cw._upsert_reviewed_candidate(c2, {
                    'verdict': 'warn', 'confidence': 0.94, 'no': '第2题', 'topic': '分式方程',
                    'step_where': '去分母', 'checkpoint': '核对常数项是否乘公分母',
                    'deep_hint': '等号右边常数项乘最简公分母了吗？',
                    'formula_tip': '分式方程去分母每一项均需乘最简公分母且须验根',
                    'ocr_stem': '解分式方程 2x/(x-2) - 1 = 4/(x-2)',
                }, jpg_pen)

                # warn -> corrected_pass preserves warn_formula_tip into Mistake Book Step 2 board (【关联定理】)
                cw._upsert_reviewed_candidate(dict(c1, _force_id=it1['id']), {
                    'verdict': 'ok', 'confidence': 0.97, 'no': '第1题', 'topic': '一元二次方程',
                    'checkpoint': '订正通过 ✓', 'ocr_stem': '关于x的一元二次方程 x² - 4x + k = 0 有两个不相等实根，求k范围。',
                }, jpg_pen)
                enr = cw.enroll_mistake(it1['id'])
                self.assertEqual(enr['enrolled_count'], 1)
                mb_b2 = eng.mistakes[0]['steps'][1]['board']
                self.assertIn('【关联定理】', mb_b2)
                self.assertIn('Δ = b² - 4ac > 0', mb_b2)

                # Reset it1 to warn and verify recurring_topics + batch recheck_all_warns (0 gatekeeper calls!)
                it1['status'] = 'warn'
                it1['resolved'] = False
                cw.recent_sessions = [{'weak_topics': ['一元二次方程', '电功率']}]
                dg = cw.build_digest()
                self.assertIn('一元二次方程', dg['recurring_topics'])

                gate_before = cw.stats['gate_calls']
                eng._post_chat_once = mock.AsyncMock(return_value=json.dumps({
                    'verdict': 'ok', 'confidence': 0.96, 'no': '已订正',
                    'topic': '代数', 'checkpoint': '订正已通过 ✓', 'formula_tip': 'Δ = b² - 4ac > 0',
                }, ensure_ascii=False))

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    # Verify pause_mode pace switching & persistence
                    for pm, exp_sec in (('fast', 2.4), ('deep', 6.5), ('std', 3.8)):
                        r_pm = await cli.post('/api/tutor/cowrite/toggle', json={'enabled': True, 'pause_mode': pm}, headers=h)
                        j_pm = await r_pm.json()
                        self.assertTrue(j_pm['ok'])
                        self.assertEqual(j_pm['cowrite']['pause_mode'], pm)
                        self.assertAlmostEqual(j_pm['cowrite']['pause_sec'], exp_sec, places=1)

                    r_fast = await cli.post('/api/tutor/cowrite/toggle', json={'enabled': False, 'pause_mode': 'fast'}, headers=h)
                    self.assertTrue((await r_fast.json())['ok'])
                    cw_reloaded = tut.CoWriteMonitor(eng)
                    cw_reloaded._ensure_loaded()
                    self.assertEqual(cw_reloaded.pause_mode, 'fast')

                    # Batch parallel recheck_all via API
                    r_b = await cli.post('/api/tutor/cowrite/recheck_item', json={'recheck_all': True}, headers=h)
                    j_b = await r_b.json()
                    self.assertTrue(j_b['ok'])
                    self.assertEqual(j_b['checked_count'], 2)
                    self.assertEqual(j_b['passed_count'], 2)
                    self.assertEqual(j_b['still_warn_count'], 0)
                    self.assertEqual(cw.stats['gate_calls'], gate_before)
                    self.assertTrue(all(x['status'] == 'ok' and x['corrected_pass'] for x in cw.items))
                    pg_marks = eng.phone_studio.public_state()['pages'][0]['check_marks']
                    self.assertTrue(all(m['status'] == 'ok' for m in pg_marks))

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_26_r103_instant_step0_seed_parent_remote_console_and_nl_voice(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r103_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('btnCwPace', 'recheckAllCoWriteWarns', 'toggleCoWriteFormulaTip', 'variantCoWriteModalItem'):
            self.assertIn(tok, h_idx)
        for tok in ('mobCwPanel', 'mobCwSnapModal', 'remoteCoWriteCtl', 'openMobCoWriteItem'):
            self.assertIn(tok, h_mob)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.camera.discover = mock.MagicMock(return_value={'connected': False, 'dev': '', 'name': ''})

                async def slow_pipe(*a, **kw):
                    await asyncio.sleep(0.08)

                eng._run_live_pipeline = slow_pipe
                cw = eng.cowrite
                pid_s = eng.phone_studio.save_page(raw_sheet, note='p1')
                c1 = {'no': '第3题', 'prob_key': '3', 'subject': 'math', 'stage_page_id': pid_s, 'box': {'x': 0.08, 'y': 0.15, 'w': 0.84, 'h': 0.30}}
                c2 = {'no': '第4题', 'prob_key': '4', 'subject': 'physics', 'stage_page_id': pid_s, 'box': {'x': 0.08, 'y': 0.50, 'w': 0.84, 'h': 0.30}}
                it1 = cw._upsert_reviewed_candidate(c1, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第3题', 'topic': '二次函数最值',
                    'step_where': '顶点横坐标', 'checkpoint': '核对对称轴是否在给定区间内',
                    'deep_hint': '开口向上时离对称轴越远函数值怎样变？',
                    'formula_tip': 'x = -b/(2a)',
                    'ocr_stem': '已知 y = x² - 4x + 1，x ∈ [0, 3]，求函数最大值与最小值。',
                }, raw_sheet)
                it2 = cw._upsert_reviewed_candidate(c2, {
                    'verdict': 'warn', 'confidence': 0.94, 'no': '第4题', 'topic': '电功率',
                    'step_where': '公式代入', 'checkpoint': '核对额定电压与实际电压区分',
                    'deep_hint': '灯泡电阻不变时实际电功率与电压平方成正比',
                    'formula_tip': 'P = U² / R',
                    'ocr_stem': '标有“6V 3W”的小灯泡接在 3V 电源上，实际功率是多少？',
                }, raw_sheet)
                cw.recent_sessions = [{'weak_topics': ['二次函数最值']}]

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 1. Instant 0ms Step-0 diagnostic seed on /api/tutor/cowrite/explain BEFORE background _run_live_pipeline completes
                    r_exp = await cli.post('/api/tutor/cowrite/explain', json={'id': it1['id'], 'mode': 'deep', 'pedagogy': 'junior', 'silent': True}, headers=h)
                    j_exp = await r_exp.json()
                    self.assertTrue(j_exp['ok'])
                    self.assertTrue(j_exp['silent'])
                    sess_exp = eng.sessions.get(j_exp['session']['id'])
                    self.assertIsNotNone(sess_exp)
                    self.assertTrue(sess_exp['silent_start'])
                    self.assertGreaterEqual(len(sess_exp['steps']), 1)
                    self.assertIn('【已核题干】', sess_exp['steps'][0]['board'])
                    self.assertIn('【自查定位】', sess_exp['steps'][0]['board'])
                    await asyncio.sleep(0.10)

                    # 2. Phone Companion state + cowrite_snap + cowrite_remote_ctl (pace, toggle, new_page, recheck_one, recheck_warns)
                    r_ms = await cli.get('/api/tutor/mobile/state', headers=hm)
                    j_ms = await r_ms.json()
                    self.assertTrue(j_ms['ok'])
                    self.assertIn('pause_mode', j_ms['cowrite'])
                    self.assertIn('二次函数最值', j_ms['cowrite']['digest']['recurring_topics'])
                    self.assertGreaterEqual(len(j_ms['cowrite']['recent_items']), 2)
                    it1_mob = next(x for x in j_ms['cowrite']['recent_items'] if x['id'] == it1['id'])
                    self.assertEqual(it1_mob['formula_tip'], 'x = -b/(2a)')

                    r_sn = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_snap', 'id': it1['id']}, headers=hm)
                    j_sn = await r_sn.json()
                    self.assertTrue(j_sn['ok'])
                    self.assertTrue(j_sn['snap_b64'].startswith('data:image/jpeg;base64,'))
                    self.assertEqual(j_sn['item']['formula_tip'], 'x = -b/(2a)')

                    r_rp = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'pace', 'pause_mode': 'deep'}, headers=hm)
                    self.assertTrue((await r_rp.json())['ok'])
                    self.assertEqual(cw.pause_mode, 'deep')

                    r_rt = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'toggle', 'enabled': True}, headers=hm)
                    self.assertTrue((await r_rt.json())['ok'])
                    self.assertTrue(cw.enabled)

                    eng._post_chat_once = mock.AsyncMock(return_value=json.dumps({
                        'verdict': 'ok', 'confidence': 0.96, 'no': '第3题',
                        'topic': '二次函数最值', 'checkpoint': '已修正 ✓', 'formula_tip': 'x = -b/(2a)',
                    }, ensure_ascii=False))
                    r_rc1 = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'recheck_one', 'id': it1['id']}, headers=hm)
                    j_rc1 = await r_rc1.json()
                    self.assertTrue(j_rc1['ok'])
                    self.assertEqual(it1['status'], 'ok')

                    # 3. NL voice routing for subject + pace + batch recheck
                    r_nl1 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '静默伴写 物理 慢一点'}, headers=hm)
                    self.assertTrue((await r_nl1.json())['ok'])
                    self.assertEqual(cw.subject, 'phys_chem')
                    self.assertEqual(cw.pause_mode, 'deep')

                    r_nl2 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '伴写 数学 快检'}, headers=hm)
                    self.assertTrue((await r_nl2.json())['ok'])
                    self.assertEqual(cw.subject, 'math')
                    self.assertEqual(cw.pause_mode, 'fast')

                    it1['status'] = 'warn'
                    it1['resolved'] = False
                    r_nl3 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '伴写错题复查'}, headers=hm)
                    self.assertTrue((await r_nl3.json())['ok'])
                    self.assertEqual(len([x for x in cw.items if x['status'] == 'warn' and not x.get('resolved')]), 0)

                    ep_b = cw.page_epoch
                    r_rnp = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'new_page'}, headers=hm)
                    self.assertTrue((await r_rnp.json())['ok'])
                    self.assertEqual(cw.page_epoch, ep_b + 1)

                    await cw.stop()
                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_27_r104_zero_touch_auto_recheck_cross_subject_and_stop_archive(self):
        tut = _load_tutor('r104_tutor_sim1')
        raw_sheet = _make_worksheet_jpeg(1)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.camera.discover = mock.MagicMock(return_value={'connected': False, 'dev': '', 'name': ''})
                cw = eng.cowrite

                # 1. Cross-subject ground-truthing overrides leaked global subject ('chinese') cleanly
                cw.subject = 'chinese'
                b_warn = {'x': 0.05, 'y': 0.35, 'w': 0.90, 'h': 0.22}
                it_math = cw._upsert_reviewed_candidate(
                    {'no': '15(2)', 'prob_key': '15(2)', 'subject': 'chinese', 'box': b_warn},
                    {
                        'verdict': 'warn', 'confidence': 0.95, 'no': '15(2)', 'subject': 'chinese',
                        'topic': '分式方程', 'step_where': '去分母',
                        'checkpoint': '核对常数项是否乘公分母',
                        'formula_tip': '等式两边同乘非零式仍相等',
                        'ocr_stem': '解分式方程：2x/(x-2) - 1 = 4/(x-2)',
                    },
                    raw_sheet,
                )
                self.assertEqual(it_math['subject'], 'math')
                self.assertEqual(
                    cw._infer_item_subject({'subject': 'chinese'}, '', '标有6V 3W的小灯泡实际功率是多少', '电功率'),
                    'physics',
                )
                self.assertEqual(
                    cw._infer_item_subject({'subject': 'math'}, '', '补写出李白《行路难》名句', '古诗文默写'),
                    'chinese',
                )

                # 2. Expanded warn box + outside-box ink detection + Pure Zero-Gatekeeper Auto-Recheck
                exp_b = cw._expanded_revision_box(b_warn)
                self.assertGreaterEqual(exp_b['h'], 0.30)
                th0 = bytes([240] * (160 * 120))
                th_in = bytearray(th0)
                for dx in range(10):
                    th_in[73 * 160 + 40 + dx] = 150
                self.assertFalse(cw._has_ink_outside_boxes(th0, bytes(th_in), [exp_b]))

                cw.anchor_thumb = th0
                gate_calls = {'n': 0}
                async def fake_chat_pure(kind, msgs, **kw):
                    if kind == 'fast':
                        gate_calls['n'] += 1
                        return json.dumps({
                            'hb': False, 'wi': False, 'rot': 0, 'more': False, 'sum': '第16题',
                            'nc': [{'no': '第16题', 's': 'math', 'box': [0.05, 0.78, 0.90, 0.16]}],
                        })
                    return json.dumps({
                        'verdict': 'ok', 'confidence': 0.97, 'no': '已订正',
                        'topic': '分式方程', 'checkpoint': '订正通过 ✓',
                    }, ensure_ascii=False)
                eng._post_chat_once = fake_chat_pure
                cnt_pure = await cw._run_funnel_once(raw_sheet, lambda: asyncio.sleep(0, result=raw_sheet), cur_thumb=bytes(th_in), manual=False)
                self.assertEqual(cnt_pure, 1)
                self.assertEqual(gate_calls['n'], 0)
                self.assertEqual(it_math['status'], 'ok')
                self.assertTrue(it_math['corrected_pass'])

                # 3. Mixed revision + new writing runs Gatekeeper for outside item AND keeps auto-revised item!
                it_math['status'] = 'warn'
                it_math['resolved'] = False
                cw.anchor_thumb = th0
                th_out = bytearray(th_in)
                for dx in range(10):
                    th_out[105 * 160 + 40 + dx] = 150
                self.assertTrue(cw._has_ink_outside_boxes(th0, bytes(th_out), [exp_b]))
                cnt_mix = await cw._run_funnel_once(raw_sheet, lambda: asyncio.sleep(0, result=raw_sheet), cur_thumb=bytes(th_out), manual=False)
                self.assertEqual(gate_calls['n'], 1)
                self.assertEqual(cnt_mix, 2)
                self.assertEqual(len(cw.items), 2)

                # 4. stop() deduplicated session archive + focus_min + reload persistence
                cw.started_at = int(cw.items[-1]['ts']) - 480
                cw.enabled = True
                await cw.stop()
                self.assertEqual(len(cw.recent_sessions), 1)
                self.assertGreaterEqual(cw.recent_sessions[0]['focus_min'], 1)
                cw_rel = tut.CoWriteMonitor(eng)
                cw_rel._ensure_loaded()
                self.assertEqual(len(cw_rel.recent_sessions), 1)
                cw.resolve_action(clear_all=True)
                self.assertEqual(len(cw.recent_sessions), 1)

                mock_pause.assert_not_called()
                mock_tts.assert_not_called()

    async def test_28_r104_star_pin_board_quickbar_and_mobile_remote_actions(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r104_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('boardCwQuickBar', 'backAndRecheckCoWriteFromBoard', 'toggleCoWriteStar', 'cw-subj-tag', 'btnCwModalStar'):
            self.assertIn(tok, h_idx)
        for tok in ('btnMobCwStarOne', 'btnMobCwExplainOne', 'btnMobCwVariantOne', 'starMobCwModalItem', 'explainMobCwModalItem', 'variantMobCwModalItem'):
            self.assertIn(tok, h_mob)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.camera.discover = mock.MagicMock(return_value={'connected': False, 'dev': '', 'name': ''})
                async def slow_pipe(*a, **kw):
                    await asyncio.sleep(0.05)
                eng._run_live_pipeline = slow_pipe
                cw = eng.cowrite

                c1 = {'no': '第1题', 'subject': 'physics', 'box': {'x': 0.06, 'y': 0.12, 'w': 0.86, 'h': 0.24}}
                c2 = {'no': '第2题', 'subject': 'math', 'box': {'x': 0.06, 'y': 0.44, 'w': 0.86, 'h': 0.26}}
                it_ok = cw._upsert_reviewed_candidate(c1, {
                    'verdict': 'ok', 'confidence': 0.96, 'no': '第1题', 'topic': '电功率计算',
                    'checkpoint': '步骤严谨，结论正确 ✓', 'ocr_stem': '标有6V 3W灯泡在额定电压下的电流是多少？',
                }, raw_sheet)
                it_w = cw._upsert_reviewed_candidate(c2, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第2题', 'topic': '一元二次方程',
                    'step_where': '判别式', 'checkpoint': '两不等实根需 Δ > 0',
                    'formula_tip': 'Δ = b² - 4ac > 0', 'ocr_stem': 'x² - 4x + k = 0 有两个不相等实数根，求 k。',
                }, raw_sheet)

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 1. Star an ok item without changing resolved=False -> digest counts it for enroll_mistake!
                    r_st = await cli.post('/api/tutor/cowrite/resolve', json={'id': it_ok['id'], 'star': True}, headers=h)
                    j_st = await r_st.json()
                    self.assertTrue(j_st['ok'])
                    self.assertTrue(it_ok['starred'])
                    self.assertFalse(it_ok['resolved'])
                    self.assertEqual(j_st['cowrite']['digest']['starred_count'], 1)
                    self.assertGreaterEqual(j_st['cowrite']['digest']['unrecorded_warn_count'], 2)

                    # Enroll all -> includes starred ok item with ⭐ prefix!
                    enr = cw.enroll_mistake(enroll_all_warns=True)
                    self.assertEqual(enr['enrolled_count'], 2)
                    self.assertTrue(any('⭐' in m['title'] for m in eng.mistakes))

                    # 2. Mobile remote actions: star_one, explain_one (silent), variant_one (silent)
                    r_ms = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'star_one', 'id': it_w['id'], 'star': True}, headers=hm)
                    j_ms = await r_ms.json()
                    self.assertTrue(j_ms['ok'] and j_ms['starred'])

                    r_ex = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'explain_one', 'id': it_w['id']}, headers=hm)
                    j_ex = await r_ex.json()
                    self.assertTrue(j_ex['ok'] and j_ex['silent'])
                    self.assertTrue(j_ex['session']['silent_start'])
                    await asyncio.sleep(0.06)

                    eng._post_chat_once = mock.AsyncMock(return_value='{"title":"同类变式：判别式","question":"方程 x² - 6x + m = 0 有两个不等实根，求 m 范围。"}')
                    r_vr = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'variant_one', 'id': it_w['id']}, headers=hm)
                    j_vr = await r_vr.json()
                    self.assertTrue(j_vr['ok'] and j_vr['silent'])
                    self.assertTrue(j_vr['session']['silent_start'])
                    await asyncio.sleep(0.06)

                    # 3. Colloquial NL voice commands ('快点' / '慢点' / '重查')
                    r_v1 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '伴写 物理 慢点'}, headers=hm)
                    self.assertTrue((await r_v1.json())['ok'])
                    self.assertEqual(cw.subject, 'phys_chem')
                    self.assertEqual(cw.pause_mode, 'deep')

                    r_v2 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '伴写 数学 快点'}, headers=hm)
                    self.assertTrue((await r_v2.json())['ok'])
                    self.assertEqual(cw.subject, 'math')
                    self.assertEqual(cw.pause_mode, 'fast')

                    eng.phone_studio.save_page(raw_sheet, note='rev')
                    eng._post_chat_once = mock.AsyncMock(return_value=json.dumps({'verdict': 'ok', 'confidence': 0.96, 'no': '第2题', 'topic': '一元二次方程', 'checkpoint': '通过 ✓'}))
                    r_v3 = await cli.post('/api/tutor/tool', json={'action': 'explain', 'question': '伴写错题重查'}, headers=hm)
                    self.assertTrue((await r_v3.json())['ok'])
                    self.assertEqual(it_w['status'], 'ok')

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_29_r105_inequality_scrubber_prefer_cowrite_sprint_and_variant_mastery(self):
        import time
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r105_tutor_sim1')

        # 1. Inequality & range spoilers are scrubbed while preserving legitimate domain checks ('Δ > 0', 'Δ ≥ 0', 'a ≠ 0', 'x - 2 ≠ 0')
        s_ineq = tut._scrub_cowrite_spoilers('请先验 Δ > 0 且 a ≠ 0，解得 k < 4 与 x ≠ 2', max_len=120)
        self.assertIn('Δ > 0', s_ineq)
        self.assertIn('a ≠ 0', s_ineq)
        self.assertNotIn('< 4', s_ineq)
        self.assertNotIn('≠ 2', s_ineq)

        s_rng = tut._scrub_cowrite_spoilers('取值范围是 -1 < m ≤ 3，注意 x - 2 ≠ 0 且 Δ ≥ 0', max_len=120)
        self.assertIn('x - 2 ≠ 0', s_rng)
        self.assertIn('Δ ≥ 0', s_rng)
        self.assertNotIn('-1 < m', s_rng)
        self.assertNotIn('≤ 3', s_rng)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng._run_live_pipeline = mock.AsyncMock()
                cw = eng.cowrite

                # Seed an older DUE elementary mistake so standard sort would bury today's Co-Write item!
                old_t = int(time.time()) - 7200
                eng.mistakes = [{
                    'id': 'mb_old_elem', 'title': '旧小学算术题', 'question': '3个苹果加5个苹果共几个？',
                    'subject': 'math', 'pedagogy': 'elem', 'error_tag': '第1关薄弱',
                    'stage': 0, 'graduated': False, 'created_at': old_t, 'updated_at': old_t, 'next_review_at': old_t, 'steps': [],
                }]
                eng._save_mistakes()

                it_w = cw._upsert_reviewed_candidate(
                    {'no': '第12题', 'prob_key': '12', 'subject': 'math', 'box': {'x': 0.06, 'y': 0.14, 'w': 0.84, 'h': 0.24}},
                    {
                        'verdict': 'warn', 'confidence': 0.95, 'no': '第12题', 'topic': '根的判别式',
                        'step_where': '边界校验', 'checkpoint': '请核对 Δ > 0 且解得 k < 4',
                        'formula_tip': 'Δ = b² - 4ac > 0', 'ocr_stem': '关于 x 的方程 x² - 4x + k = 0 有两个不等实根，求 k 的取值范围。',
                    },
                    raw_sheet,
                )
                self.assertIn('Δ > 0', it_w['checkpoint'])
                self.assertNotIn('< 4', it_w['checkpoint'])

                eng._post_chat_once = mock.AsyncMock(return_value='{"title":"特训：判别式","question":"方程 x² - 6x + m = 0 有两不等实根，求 m 的取值范围。"}')
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    # 2. prefer_cowrite=True auto-enrolls today's Co-Write mistake & puts it BEFORE mb_old_elem, with silent=True!
                    r_sp = await cli.post('/api/tutor/mistakes/sprint_start', json={
                        'count': 2, 'mode': 'fast', 'pedagogy': 'junior', 'silent': True, 'prefer_cowrite': True,
                    }, headers=h)
                    j_sp = await r_sp.json()
                    self.assertTrue(j_sp['ok'] and j_sp['silent'])
                    sp_items = eng.sprints[j_sp['sprint_id']]['items']
                    self.assertEqual(sp_items[0]['mid'], it_w['mistake_id'])
                    self.assertTrue(j_sp['session']['silent_start'])

                    # 3. Completing that sprint session without peek promotes originating Co-Write item to variant_passed=True!
                    r_pg = await cli.post('/api/tutor/progress', json={
                        'id': j_sp['session']['id'], 'solved': True, 'peeked': False, 'first_try_count': 3, 'total_gates': 3,
                    }, headers=h)
                    self.assertTrue((await r_pg.json())['ok'])
                    self.assertTrue(it_w['resolved'] and it_w['blackboard_solved'] and it_w['variant_passed'])
                    self.assertEqual(cw.build_digest()['mastered_count'], 1)
                    self.assertTrue(cw.public_state()['items'][0]['variant_passed'])
                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_30_r105_one_step_print_sheet_page_tabs_and_mobile_remote_sprint(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r105_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('cwPageTabs', 'setCoWriteEpochFilter', 'setCoWriteSubjFilter', '👑 变式已通关', '点此静默核查', 'prefer_cowrite'):
            self.assertIn(tok, h_idx)
        for tok in ('printFromCowrite', 'from_cowrite', 'sprint_warns', 'explain_top_warn', '一键伴写错题'):
            self.assertIn(tok, h_mob)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                async def slow_pipe(*a, **kw):
                    await asyncio.sleep(0.05)
                eng._run_live_pipeline = slow_pipe
                cw = eng.cowrite

                c1 = {'no': '第15题', 'subject': 'math', 'box': {'x': 0.06, 'y': 0.14, 'w': 0.84, 'h': 0.24}}
                it_w = cw._upsert_reviewed_candidate(c1, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第15题', 'topic': '韦达定理',
                    'step_where': '两根之积', 'checkpoint': '核对常数项符号与 c/a',
                    'formula_tip': 'x₁x₂ = c/a', 'ocr_stem': '已知 x₁, x₂ 是 x² - 5x + 6 = 0 的两根，求 x₁ + x₂。',
                }, raw_sheet)
                cw.mark_blackboard_solved(item_id=it_w['id'], is_variant=True)
                self.assertTrue(it_w['variant_passed'])
                # Re-evaluating the same item as warn clears stale variant_passed mastery
                it_w = cw._upsert_reviewed_candidate(c1, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第15题', 'topic': '韦达定理',
                    'step_where': '两根之积', 'checkpoint': '再次出现常数项符号漏负号',
                    'formula_tip': 'x₁x₂ = c/a', 'ocr_stem': '已知 x₁, x₂ 是 x² - 5x + 6 = 0 的两根，求 x₁ + x₂。',
                }, raw_sheet)
                self.assertFalse(it_w['variant_passed'])

                eng._post_chat_once = mock.AsyncMock(return_value='{"title":"特训：韦达定理","question":"已知方程 x² - 7x + 12 = 0 的两根为 a, b，求 a + b + ab。"}')
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 1. One-step prepare_print_sheet with from_cowrite=True auto-enrolls & includes theorem anchor
                    r_ps = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'prepare_print_sheet', 'ids': [], 'sheet_mode': 'original', 'from_cowrite': True,
                    }, headers=hm)
                    j_ps = await r_ps.json()
                    self.assertTrue(j_ps['ok'])
                    self.assertTrue(it_w['enrolled_mistake'])
                    self.assertIn('伴写', j_ps['title'])
                    self.assertIn('x₁x₂ = c/a', j_ps['items'][0]['ans_check'])

                    # 2. Remote mobile sprint_warns & explain_top_warn work silently with zero speaker audio
                    r_top = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_remote_ctl', 'sub_action': 'explain_top_warn',
                    }, headers=hm)
                    j_top = await r_top.json()
                    self.assertTrue(j_top['ok'] and j_top['silent'] and j_top['session']['silent_start'])
                    await asyncio.sleep(0.06)

                    r_sw = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_remote_ctl', 'sub_action': 'sprint_warns',
                    }, headers=hm)
                    j_sw = await r_sw.json()
                    self.assertTrue(j_sw['ok'] and j_sw['silent'] and j_sw['session']['silent_start'])
                    await asyncio.sleep(0.06)

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_31_r106_copula_scrubber_usb_cam_phone_stage_and_multipage_recheck(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r106_tutor_sim1')

        # 1. Corrective-copula, contrast-pair, ratio, and multi-root spoiler scrubbing
        scrub = tut._scrub_cowrite_spoilers
        for raw, forbid in (
            ('不对，应为0.75W，重算一下', '0.75W'),
            ('正确答案是 x₁ = 1, x₂ = -3，再用公式验证', '-3'),
            ('比值实为 3:4，注意化简', '3:4'),
            ('不是4A而是2.5A，串联电流相等', '2.5A'),
        ):
            out = scrub(raw, max_len=120)
            self.assertNotIn(forbid.lower(), out.lower(), f'leaked {forbid} in {out}')
        s_dom = scrub('先验 Δ > 0 且 x - 2 ≠ 0，应为 k < 4', max_len=120)
        self.assertIn('Δ > 0', s_dom)
        self.assertIn('x - 2 ≠ 0', s_dom)
        self.assertNotIn('< 4', s_dom)

        jpg1 = _make_worksheet_jpeg(1, brightness_shift=0)
        jpg2 = _make_worksheet_jpeg(1, brightness_shift=-12, flip_page=True)
        jpg1_fix = _make_worksheet_jpeg(2, brightness_shift=4)
        jpg2_fix = _make_worksheet_jpeg(2, brightness_shift=-8, flip_page=True)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng._run_live_pipeline = mock.AsyncMock()
                cw = eng.cowrite

                cam_connected = True
                eng.camera.discover = lambda: {'connected': cam_connected, 'device': '/dev/video99', 'name': 'MockUSBCam'}
                eng.camera.capture_jpeg = mock.AsyncMock(return_value=jpg1)

                pid1 = eng.phone_studio.save_page(jpg1, note='物理P1')
                pid2 = eng.phone_studio.save_page(jpg2, note='数学P2')

                async def fake_gate(gate_jpg, checked_entries, subject):
                    act_p = eng.phone_studio.active_id
                    if act_p == pid1:
                        return {
                            'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0,
                            'has_more': False, 'page_summary': '物理',
                            'new_completed': [{'no': '第1题', 'prob_key': '1', 'subject': 'physics', 'box': {'x': 0.05, 'y': 0.1, 'w': 0.85, 'h': 0.28}, 'stem_brief': '电功率'}],
                        }
                    return {
                        'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0,
                        'has_more': False, 'page_summary': '数学',
                        'new_completed': [{'no': '第2题', 'prob_key': '2', 'subject': 'math', 'box': {'x': 0.05, 'y': 0.45, 'w': 0.85, 'h': 0.28}, 'stem_brief': '分式方程'}],
                    }

                recheck_sources = []

                async def fake_review(hi_jpg, cand, subject_pref, pencil_boost=False):
                    if not cand.get('_force_id') and cand.get('no') == '第1题':
                        return ({
                            'verdict': 'warn', 'confidence': 0.95, 'no': '第1题', 'topic': '电功率',
                            'step_where': '实际功率公式', 'checkpoint': '电压减半时电阻不变应算平方比',
                            'formula_tip': 'P实 = U实² / R', 'ocr_stem': '6V 3W 小灯泡接在 3V 电源上',
                        }, hi_jpg)
                    if not cand.get('_force_id') and cand.get('no') == '第2题':
                        return ({
                            'verdict': 'warn', 'confidence': 0.95, 'no': '第2题', 'topic': '分式方程',
                            'step_where': '增根检验', 'checkpoint': '分式方程去分母后必须检验分母不为 0',
                            'formula_tip': 'x - 2 ≠ 0', 'ocr_stem': '2x/(x-2) - 1 = 4/(x-2)',
                        }, hi_jpg)
                    recheck_sources.append((cand.get('stage_page_id'), hi_jpg == jpg1_fix, hi_jpg == jpg2_fix))
                    if cand.get('stage_page_id') == pid1:
                        return ({
                            'verdict': 'ok', 'confidence': 0.96, 'no': '第1题', 'topic': '电功率',
                            'step_where': '', 'checkpoint': '功率计算正确 ✓', 'formula_tip': '', 'ocr_stem': '6V 3W',
                        }, hi_jpg)
                    return ({
                        'verdict': 'ok', 'confidence': 0.96, 'no': '第2题', 'topic': '分式方程',
                        'step_where': '', 'checkpoint': '增根检验完整 ✓', 'formula_tip': '', 'ocr_stem': '分式方程',
                    }, hi_jpg)

                cw._call_gatekeeper = fake_gate
                cw._fetch_review_eval = fake_review

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 2. Explicit page_id overrides connected USB DocCam on both /check_now & /mobile/upload cowrite_check
                    r_cn = await cli.post('/api/tutor/cowrite/check_now', json={'page_id': pid1}, headers=h)
                    j_cn = await r_cn.json()
                    self.assertTrue(j_cn['ok'] and j_cn.get('source') == 'phone_stage')

                    r_mb = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_check', 'page_id': pid2, 'cowrite_scope': 'page',
                    }, headers=hm)
                    j_mb = await r_mb.json()
                    self.assertTrue(j_mb['ok'] and len(j_mb['cowrite']['items']) == 2)
                    self.assertEqual(len([x for x in cw.items if x['status'] == 'warn']), 2)
                    eng.camera.capture_jpeg.assert_not_called()

                    ep_before = {x['no']: int(x.get('page_epoch') or 0) for x in cw.items}

                    # 3. Disconnect USB cam, update both staged JPEGs, and run 1 multi-page recheck_all_warns
                    cam_connected = False
                    eng.phone_studio.get_rotated_stage_jpeg = mock.AsyncMock(
                        side_effect=lambda pid='': jpg1_fix if pid == pid1 else jpg2_fix
                    )
                    r_rc = await cli.post('/api/tutor/cowrite/recheck_item', json={'id': '__all__', 'recheck_all': True}, headers=h)
                    j_rc = await r_rc.json()
                    self.assertTrue(j_rc['ok'])
                    self.assertEqual(j_rc['checked_count'], 2)
                    self.assertEqual(j_rc['passed_count'], 2)
                    self.assertEqual(j_rc['still_warn_count'], 0)
                    self.assertIn((pid1, True, False), recheck_sources)
                    self.assertIn((pid2, False, True), recheck_sources)

                    ep_after = {x['no']: int(x.get('page_epoch') or 0) for x in cw.items}
                    self.assertEqual(ep_after, ep_before)
                    for it in cw.items:
                        self.assertEqual(it['status'], 'ok')
                        self.assertTrue(it['corrected_pass'])
                        self.assertTrue(it.get('warn_checkpoint'))
                        self.assertTrue(it.get('warn_formula_tip'))

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_32_r106_portrait_swipe_retrospective_modal_solo_variant_and_study_brief(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r106_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in (
            'btnPhoneCwPage', 'checkPhonePageInCoWrite', 'btnCwModalResolve',
            'btnCwModalSoloVar', 'copyCoWriteStudyBrief', '📋 复制小结',
            '已订正复盘', '🏅 已销号',
        ):
            self.assertIn(tok, h_idx)
        for tok in ('btnMobCwSoloVar', 'soloVariantMobCwModalItem', 'copyMobCwBrief', '📋 复制伴写战报'):
            self.assertIn(tok, h_mob)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng._run_live_pipeline = mock.AsyncMock()
                cw = eng.cowrite

                c1 = {'no': '第8题', 'subject': 'physics', 'box': {'x': 0.06, 'y': 0.14, 'w': 0.84, 'h': 0.24}}
                it_phy = cw._upsert_reviewed_candidate(c1, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第8题', 'topic': '电功率计算',
                    'step_where': '实际功率公式', 'checkpoint': '区分额定与实际功率公式',
                    'formula_tip': 'P实 = U实² / R', 'ocr_stem': '标有 6V 3W 灯泡接在 3V 电路中求实际功率。',
                }, raw_sheet)
                c2 = {'no': '第9题', 'subject': 'math', 'box': {'x': 0.06, 'y': 0.45, 'w': 0.84, 'h': 0.24}}
                it_mat = cw._upsert_reviewed_candidate(c2, {
                    'verdict': 'warn', 'confidence': 0.95, 'no': '第9题', 'topic': '分式方程增根',
                    'step_where': '去分母检验', 'checkpoint': '解后必须代入公分母验根',
                    'formula_tip': 'x - 2 ≠ 0', 'ocr_stem': '2x/(x-2) - 1 = 4/(x-2)',
                }, raw_sheet)

                # Enroll & graduate physics mistake to test mistake_graduated sync
                enr = cw.enroll_mistake(item_id=it_phy['id'])
                self.assertTrue(enr['ok'])
                for mk in eng.mistakes:
                    if mk.get('id') == it_phy['mistake_id']:
                        mk['graduated'] = True
                eng._save_mistakes()

                brief = cw.build_study_brief()
                self.assertIn('物理', brief)
                self.assertIn('数学', brief)
                pub_phy = next(x for x in cw.public_state()['items'] if x['id'] == it_phy['id'])
                mob_phy = next(x for x in cw.mobile_brief()['recent_items'] if x['id'] == it_phy['id'])
                self.assertTrue(pub_phy['mistake_graduated'])
                self.assertTrue(mob_phy['mistake_graduated'])

                eng._post_chat_once = mock.AsyncMock(return_value='{"title":"先独做变式","question":"标有 12V 6W 灯泡接在 6V 电源求实际功率。"}')
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 1. /api/tutor/cowrite/variant with solo_challenge=True & silent=True
                    r_cv = await cli.post('/api/tutor/cowrite/variant', json={
                        'id': it_mat['id'], 'mode': 'fast', 'silent': True, 'solo_challenge': True,
                    }, headers=h)
                    j_cv = await r_cv.json()
                    self.assertTrue(j_cv['ok'])
                    s_cv = eng.sessions[j_cv['session']['id']]
                    self.assertTrue(s_cv['solo_challenge'] and s_cv['silent_start'])

                    # 2. Mobile cowrite_remote_ctl variant_one with solo_challenge=True
                    r_mv = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_remote_ctl', 'sub_action': 'variant_one',
                        'id': it_mat['id'], 'solo_challenge': True,
                    }, headers=hm)
                    j_mv = await r_mv.json()
                    self.assertTrue(j_mv['ok'] and j_mv['silent'])
                    s_mv = eng.sessions[j_mv['session']['id']]
                    self.assertTrue(s_mv['solo_challenge'] and s_mv['silent_start'])

                    # 3. Voice NL Co-Write triggers via /api/tutor/tool: '伴写独做特训' & '伴写拆解'
                    r_nl1 = await cli.post('/api/tutor/tool', json={'question': '伴写独做特训'}, headers=hm)
                    j_nl1 = await r_nl1.json()
                    self.assertTrue(j_nl1['ok'] and j_nl1.get('silent'))
                    s_nl1 = eng.sessions[j_nl1['session_id']]
                    self.assertTrue(s_nl1['solo_challenge'] and s_nl1['silent_start'])

                    r_nl2 = await cli.post('/api/tutor/tool', json={'question': '伴写拆解'}, headers=hm)
                    j_nl2 = await r_nl2.json()
                    self.assertTrue(j_nl2['ok'] and j_nl2.get('silent'))
                    self.assertTrue(eng.sessions[j_nl2['session_id']]['silent_start'])

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_33_r107_formula_tip_latex_sequential_revision_and_dark_partial_batch(self):
        import time as _time
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r107_tutor_sim1')

        ft = tut._scrub_cowrite_formula_tip
        sp = tut._scrub_cowrite_spoilers
        self.assertIn('P实 = U实²/R', ft(r'P_{\text{实}} = \frac{U_{\text{实}}^2}{R}'))
        for raw_s in (r'S = \frac{1}{2}ah', r'E_k = \frac{1}{2}mv^2', 'sin30° = 1/2'):
            out_s = ft(raw_s)
            self.assertIn('1/2', out_s)
            self.assertNotIn('？', out_s)
        self.assertIn('10⁶', ft(r'1 kWh = 3.6 \times 10^6 J'))
        out_c = ft(r'c_{\text{水}} = 4.2 \times 10^3 J/(kg\cdot °C)')
        self.assertIn('4.2', out_c)
        self.assertIn('10³', out_c)
        self.assertIn('℃', out_c)
        out_trim = ft('P实 = U实²/R = 0.75W')
        self.assertIn('P实 = U实²/R', out_trim)
        self.assertNotIn('0.75W', out_trim)
        self.assertNotIn('< 4', ft('求得 k < 4'))

        s_e1 = sp('不是 go 而是 went，注意过去式', max_len=120)
        s_e2 = sp('here should be went，主语第三人称', max_len=120)
        self.assertNotIn('went', s_e1)
        self.assertNotIn('went', s_e2)
        s_dom = sp('结果等于 0.75W，先验 Δ > 0 且 x - 2 ≠ 0', max_len=120)
        self.assertNotIn('0.75W', s_dom)
        self.assertIn('Δ > 0', s_dom)
        self.assertIn('x - 2 ≠ 0', s_dom)

        jpg_p1 = _make_worksheet_jpeg(ink_step=1)
        jpg_p2 = _make_worksheet_jpeg(ink_step=2, flip_page=True)
        jpg_rev = _make_worksheet_jpeg(ink_step=3)
        jpg_dark = _make_worksheet_jpeg(ink_step=0, brightness_shift=-235)
        jpg_cam = _make_worksheet_jpeg(ink_step=1)

        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                cam_on = True
                eng.camera.discover = mock.MagicMock(side_effect=lambda: {'connected': cam_on, 'dev': '/dev/video0' if cam_on else '', 'name': 'MockCam' if cam_on else ''})
                eng.camera.capture_jpeg = mock.AsyncMock(return_value=jpg_cam)
                eng._run_live_pipeline = mock.AsyncMock()
                cw = eng.cowrite

                pid1 = eng.phone_studio.save_page(jpg_p1, note='P1', client_key='r107_p1')
                pid2 = eng.phone_studio.save_page(jpg_p2, note='P2', client_key='r107_p2')
                phase_tag = {'stage': 'init', 'check_pid': ''}

                async def fake_gate(gate_jpg, checked_entries, subject):
                    if phase_tag['check_pid'] == pid1:
                        return {
                            'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': False,
                            'page_summary': '第1页',
                            'new_completed': [
                                {'no': '第1题', 'prob_key': '1', 'subject': 'physics', 'box': {'x': 0.05, 'y': 0.1, 'w': 0.8, 'h': 0.25}, 'stem_brief': '动能'},
                                {'no': '第2题', 'prob_key': '2', 'subject': 'math', 'box': {'x': 0.05, 'y': 0.45, 'w': 0.8, 'h': 0.25}, 'stem_brief': '三角'},
                            ],
                        }
                    if phase_tag['check_pid'] == pid2:
                        return {
                            'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': False,
                            'page_summary': '第2页',
                            'new_completed': [
                                {'no': '第3题', 'prob_key': '3', 'subject': 'physics', 'box': {'x': 0.05, 'y': 0.1, 'w': 0.8, 'h': 0.25}, 'stem_brief': '电功率'},
                            ],
                        }
                    return {
                        'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': False,
                        'page_summary': 'USB画面',
                        'new_completed': [],
                    }

                async def fake_review(hi_jpg, cand, subject_pref, pencil_boost=False):
                    no = str(cand.get('no') or '')
                    if phase_tag['stage'] in ('rev', 'batch') and cand.get('stage_page_id') in (pid_rev_holder.get('pid'), pid2):
                        return ({'verdict': 'ok', 'confidence': 0.96, 'no': no, 'topic': '考点A', 'step_where': '', 'checkpoint': '订正通过 ✓', 'formula_tip': '', 'ocr_stem': f'stem {no}'}, hi_jpg)
                    return ({'verdict': 'warn', 'confidence': 0.95, 'no': no, 'topic': '考点A', 'step_where': '步骤', 'checkpoint': '核对公式与单位', 'formula_tip': 'Ek=1/2mv^2', 'ocr_stem': f'stem {no}'}, hi_jpg)

                pid_rev_holder = {'pid': ''}
                cw._call_gatekeeper = fake_gate
                cw._fetch_review_eval = fake_review

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}

                    phase_tag['check_pid'] = pid1
                    rc1 = await cli.post('/api/tutor/cowrite/check_now', json={'page_id': pid1}, headers=h)
                    jc1 = await rc1.json()
                    self.assertTrue(jc1['ok'])
                    self.assertEqual(jc1['cowrite']['active_stage_page_id'], pid1)

                    rrot = await cli.post('/api/tutor/phone_stage/rotate', json={'page_id': pid1, 'step_deg': 90}, headers=h)
                    jrot = await rrot.json()
                    self.assertEqual(rrot.status, 200)
                    self.assertTrue(jrot['ok'])
                    self.assertEqual(jrot['rotate'], 90)

                    phase_tag['check_pid'] = pid2
                    rc2 = await cli.post('/api/tutor/cowrite/check_now', json={'page_id': pid2}, headers=h)
                    self.assertTrue((await rc2.json())['ok'])
                    self.assertEqual(len(cw.items), 3)

                    q1 = next(x for x in cw.items if x['no'] == '第1题')
                    q2 = next(x for x in cw.items if x['no'] == '第2题')
                    q3 = next(x for x in cw.items if x['no'] == '第3题')
                    self.assertEqual(q1.get('orig_stage_page_id'), pid1)
                    self.assertEqual(q2.get('orig_stage_page_id'), pid1)
                    self.assertEqual(q3.get('orig_stage_page_id'), pid2)

                    cam_on = False
                    await asyncio.sleep(0.02)
                    pid_rev = eng.phone_studio.save_page(jpg_rev, note='订正页', client_key='r107_rev')
                    pid_rev_holder['pid'] = pid_rev
                    phase_tag['stage'] = 'rev'

                    rr1 = await cli.post('/api/tutor/cowrite/recheck_item', json={'id': q1['id']}, headers=h)
                    jr1 = await rr1.json()
                    rr2 = await cli.post('/api/tutor/cowrite/recheck_item', json={'id': q2['id']}, headers=h)
                    jr2 = await rr2.json()
                    self.assertEqual(jr1.get('verdict'), 'ok')
                    self.assertEqual(jr2.get('verdict'), 'ok')
                    self.assertTrue(q1.get('corrected_pass') and q2.get('corrected_pass'))

                    # Dark page skipped cleanly in recheck_all_warns (USB cam still disconnected -> use_phone_pool)
                    phase_tag['stage'] = 'batch'
                    q1['status'] = 'warn'
                    q1['resolved'] = False
                    q1['corrected_pass'] = False
                    q1['stage_page_id'] = pid1
                    q1['ts'] = int(_time.time()) + 10
                    q3['status'] = 'warn'
                    q3['resolved'] = False

                    orig_get_rot = eng.phone_studio.get_rotated_stage_jpeg
                    async def mock_dark_rot(pid=''):
                        if pid == pid1:
                            return jpg_dark
                        return jpg_rev if pid in (pid2, pid_rev) else await orig_get_rot(pid)
                    eng.phone_studio.get_rotated_stage_jpeg = mock_dark_rot
                    batch_res = await cw.recheck_all_warns()
                    self.assertEqual(batch_res.get('checked_count'), 1)
                    self.assertEqual(batch_res.get('passed_count'), 1)
                    self.assertEqual(batch_res.get('still_warn_count'), 1)
                    self.assertIn('过暗或反光暂跳过', batch_res.get('note', ''))
                    self.assertEqual(q1.get('status'), 'warn')
                    self.assertEqual(q3.get('status'), 'ok')

                    # Live USB camera check resets active_stage_page_id to ''
                    cam_on = True
                    eng.phone_studio.get_rotated_stage_jpeg = orig_get_rot
                    phase_tag['check_pid'] = ''
                    rc_usb = await cli.post('/api/tutor/cowrite/check_now', json={}, headers=h)
                    jc_usb = await rc_usb.json()
                    self.assertEqual(jc_usb['cowrite']['active_stage_page_id'], '')

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_34_r107_solo_sprint_chain_graduated_filter_nosnap_mobile_and_ui_guards(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r107_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('cwDismissedBoardSid', 'cwSnapSwipeSuppressUntil', 'sprintable_count', 'active_stage_page_id', 'totCnt >= 1', 'step_deg: stepDeg', 'overridePageId', 'session.solo_challenge'):
            self.assertIn(tok, h_idx)
        for tok in ('Math.min(6', 'sprintable_count', '已订正复盘', '已销号', 'variant_passed'):
            self.assertIn(tok, h_mob)

        raw_sheet = _make_worksheet_jpeg(ink_step=1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.camera.discover = mock.MagicMock(return_value={'connected': False, 'dev': '', 'name': ''})
                eng._run_live_pipeline = mock.AsyncMock()
                eng._post_chat_once = mock.AsyncMock(return_value='{"title":"变式巩固","question":"新变式题干","subject":"physics","knowledge_point":"动能","hint":"公式","steps":[{"title":"第一步","speak":"列式"}]}')
                eng.generate_interactive_lesson = mock.AsyncMock(side_effect=lambda *a, **kw: {
                    'title': '伴写巩固', 'question': a[0] if a else 'Q', 'subject': kw.get('subject', 'physics'),
                    'knowledge_point': '动能', 'steps': [{'title': 'S1', 'speak': 'B'}],
                    'pedagogy': kw.get('pedagogy'), 'source': kw.get('source'),
                })
                cw = eng.cowrite

                c1 = {'no': '第1题', 'subject': 'physics', 'box': {'x': 0.06, 'y': 0.12, 'w': 0.86, 'h': 0.24}}
                c2 = {'no': '第2题', 'subject': 'math', 'box': {'x': 0.06, 'y': 0.44, 'w': 0.86, 'h': 0.24}}
                q1 = cw._upsert_reviewed_candidate(c1, {'verdict': 'warn', 'confidence': 0.95, 'no': '第1题', 'topic': '动能', 'step_where': '公式', 'checkpoint': '检查平方', 'ocr_stem': 'Q1'}, raw_sheet)
                q2 = cw._upsert_reviewed_candidate(c2, {'verdict': 'warn', 'confidence': 0.95, 'no': '第2题', 'topic': '函数', 'step_where': '符号', 'checkpoint': '核对符号', 'ocr_stem': 'Q2'}, raw_sheet)
                q2['status'] = 'ok'
                q2['corrected_pass'] = True
                q2['warn_checkpoint'] = '核对符号'
                q1['enrolled_mistake'] = True
                q1['mistake_id'] = 'mk_grad_r107'
                q2['enrolled_mistake'] = True
                q2['mistake_id'] = 'mk_grad_r107'
                eng.mistakes = [{
                    'id': 'mk_grad_r107', 'title': '伴写·已销号', 'question': 'Q_grad', 'subject': 'physics',
                    'error_tag': '伴写·物理', 'graduated': True, 'severity': 1, 'steps': [],
                }]

                self.assertEqual(cw.build_digest().get('sprintable_count'), 0)

                eng.mistakes.append({
                    'id': 'mk_act_r107', 'title': '伴写·待巩固', 'question': '如图求滑轮组机械效率 η。', 'subject': 'physics',
                    'error_tag': '伴写·物理', 'graduated': False, 'severity': 2, 'steps': [],
                })
                q2['mistake_id'] = 'mk_act_r107'
                ps = await eng.prepare_print_sheet([], from_cowrite=True)
                ps_titles = [x.get('title') for x in (ps.get('items') or [])]
                ps_qs = [x.get('question') for x in (ps.get('items') or [])]
                self.assertIn('伴写·待巩固', ps_titles)
                self.assertTrue(any('滑轮组机械效率' in q for q in ps_qs))
                self.assertNotIn('伴写·已销号', ps_titles)
                eng.mistakes = [m for m in eng.mistakes if m['id'] != 'mk_act_r107']
                q2['mistake_id'] = 'mk_grad_r107'

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    r_spw = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'sprint_warns'}, headers=hm)
                    self.assertEqual(r_spw.status, 404)
                    self.assertFalse((await r_spw.json()).get('ok'))

                    snap_f = snap_d / str(q2.get('snap_name') or '')
                    if snap_f.exists():
                        snap_f.unlink()
                    r_sn = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_snap', 'id': q2['id']}, headers=hm)
                    self.assertEqual(r_sn.status, 200)
                    j_sn = await r_sn.json()
                    self.assertTrue(j_sn['ok'])
                    self.assertFalse(j_sn['has_snap'])
                    self.assertEqual(j_sn['snap_b64'], '')
                    self.assertTrue(j_sn['item']['mistake_graduated'])
                    self.assertIn('核对符号', j_sn['item']['checkpoint'])

                    eng.mistakes = [
                        {'id': 'mk_sp1', 'title': '伴写·物理T1', 'question': 'Q_sp1', 'subject': 'physics', 'error_tag': '伴写·物理', 'graduated': False, 'severity': 3, 'steps': []},
                        {'id': 'mk_sp2', 'title': '伴写·数学T2', 'question': 'Q_sp2', 'subject': 'math', 'error_tag': '伴写·数学', 'graduated': False, 'severity': 2, 'steps': []},
                    ]
                    cw.enabled = False
                    r_tl = await cli.post('/api/tutor/tool', json={'question': '伴写独做特训'}, headers=hm)
                    j_tl = await r_tl.json()
                    self.assertTrue(j_tl['ok'] and j_tl['silent'])
                    s1_full = eng.sessions[j_tl['session_id']]
                    self.assertTrue(s1_full.get('solo_challenge') and s1_full.get('silent_start'))

                    r_snxt = await cli.post('/api/tutor/mistakes/sprint_next', json={'sprint_id': s1_full['sprint_meta']['sprint_id'], 'mode': 'fast', 'pedagogy': 'junior'}, headers=hm)
                    j_snxt = await r_snxt.json()
                    self.assertTrue(j_snxt['ok'])
                    s2_full = eng.sessions[j_snxt['session']['id']]
                    self.assertTrue(s2_full.get('solo_challenge') and s2_full.get('silent_start'))

                    r_ret = await cli.post('/api/tutor/mistakes/retest', json={'id': 'mk_sp1', 'pedagogy': 'elem'}, headers=hm)
                    j_ret = await r_ret.json()
                    self.assertTrue(j_ret['ok'])
                    ret_s = j_ret['session']
                    self.assertEqual(ret_s.get('mistake_id'), 'mk_sp1')
                    self.assertEqual(ret_s.get('pedagogy'), 'junior')
                    self.assertTrue(ret_s.get('silent_start'))

                    res_v = await eng.generate_variant_problem(ret_s['id'], pedagogy='elem', solo_challenge=True, silent_tts=True)
                    v_full = eng.sessions[res_v['session']['id']]
                    self.assertEqual(v_full.get('pedagogy'), 'junior')
                    self.assertEqual(v_full.get('source'), 'cowrite')
                    self.assertEqual(v_full.get('mistake_id'), 'mk_sp1')
                    self.assertTrue(v_full.get('silent_start'))

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_35_r108_cross_page_epoch_phone_recheck_usb_cam_and_formula_scrubber(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r108_tutor_sim1')

        ft = tut._scrub_cowrite_formula_tip
        sp = tut._scrub_cowrite_spoilers
        for raw_f, must_keep, forbid in (
            ('h = 1/2 g t²', 'h = 1/2 g t²', '？'),
            ('S扇 = 1/2 L R', '1/2 L R', '？'),
            ('W = 1/2 L I²', '1/2 L I²', '？'),
            ('sin90° = 1', '1', '？'),
            ('cos0° = 1', '1', '？'),
            ('P = 0.75 W', '？', '0.75'),
        ):
            out = ft(raw_f, max_len=90)
            self.assertIn(must_keep, out)
            if forbid:
                self.assertNotIn(forbid, out)

        s_chem = sp('配平有误，化学方程式应为 CuO + H₂SO₄ = CuSO₄ + H₂O', max_len=120)
        self.assertNotIn('CuSO₄', s_chem)
        self.assertNotIn('H₂SO₄', s_chem)
        s_coeff = sp('动能公式漏乘了 1/2 系数，请再核对', max_len=120)
        self.assertIn('1/2', s_coeff)
        self.assertNotIn('对应式', s_coeff)

        exp_b = tut.CoWriteMonitor._expanded_revision_box({'x': 0.10, 'y': 0.20, 'w': 0.50, 'h': 0.20})
        self.assertLessEqual(exp_b['y'], 0.175)
        self.assertGreaterEqual(exp_b['h'], 0.315)

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in ('Number(cowriteState.total_count) > 0', 'stopLessonPlayback(true)', '静默特训连闯中', '静默独做挑战中'):
            self.assertIn(tok, h_idx)

        jpg1 = _make_worksheet_jpeg(1, brightness_shift=0)
        jpg1_rev = _make_worksheet_jpeg(2, brightness_shift=6)
        jpg_cam = _make_worksheet_jpeg(2, brightness_shift=-10, flip_page=True)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng._run_live_pipeline = mock.AsyncMock()
                cw = eng.cowrite
                eng.camera.discover = lambda: {'connected': True, 'device': '/dev/video99', 'name': 'MockUSBCam'}
                eng.camera.capture_jpeg = mock.AsyncMock(return_value=jpg_cam)

                pid1 = eng.phone_studio.save_page(jpg1, note='物理P1')

                async def fake_gate(gate_jpg, checked_entries, subject):
                    return {
                        'hand_blocking': False, 'writing_incomplete': False, 'need_rotate': 0, 'has_more': False,
                        'page_summary': '第1页',
                        'new_completed': [{'no': '第1题', 'prob_key': '1', 'subject': 'physics', 'box': {'x': 0.05, 'y': 0.1, 'w': 0.85, 'h': 0.28}, 'stem_brief': '动能'}],
                    }

                seen_rc = []

                async def fake_review(hi_jpg, cand, subject_pref, pencil_boost=False):
                    if cand.get('_force_id'):
                        seen_rc.append((cand.get('no'), cand.get('stage_page_id'), hi_jpg == jpg1_rev, hi_jpg == jpg_cam))
                        return ({'verdict': 'ok', 'confidence': 0.97, 'no': cand.get('no'), 'topic': '动能', 'step_where': '', 'checkpoint': '订正通过 ✓', 'ocr_stem': 'Q'}, hi_jpg)
                    return ({'verdict': 'warn', 'confidence': 0.95, 'no': '第1题', 'topic': '动能', 'step_where': '公式', 'checkpoint': '核对速度平方', 'formula_tip': 'Ek = 1/2 m v²', 'ocr_stem': '已知质量求动能'}, hi_jpg)

                cw._call_gatekeeper = fake_gate
                cw._fetch_review_eval = fake_review

                await cw.check_now(stage_page_id=pid1)
                q1 = cw.items[0]
                self.assertEqual(q1['page_epoch'], 1)
                cw.resolve_action(new_page=True)
                self.assertEqual(cw.page_epoch, 2)

                await asyncio.sleep(0.02)
                pid_rev = eng.phone_studio.save_page(jpg1_rev, note='物理P1订正')
                eng.camera.capture_jpeg.reset_mock()
                r_rc = await cw.recheck_item(q1['id'])
                self.assertTrue(r_rc['updated'] and r_rc['verdict'] == 'ok')
                self.assertEqual(q1['page_epoch'], 1)
                self.assertEqual(q1['stage_page_id'], pid_rev)
                eng.camera.capture_jpeg.assert_not_called()

                q1['status'] = 'warn'
                q1['resolved'] = False
                cw._upsert_reviewed_candidate(
                    {'no': '第9题', 'subject': 'math', 'box': {'x': 0.1, 'y': 0.5, 'w': 0.7, 'h': 0.2}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第9题', 'topic': '二次函数', 'step_where': '配方', 'checkpoint': '核对符号', 'ocr_stem': '抛物线'},
                    jpg_cam,
                )
                seen_rc.clear()
                eng.camera.capture_jpeg.reset_mock()
                rr = await cw.recheck_all_warns()
                self.assertEqual(rr['checked_count'], 2)
                self.assertIn(('第1题', pid_rev, True, False), seen_rc)
                self.assertIn(('第9题', '', False, True), seen_rc)

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    r404 = await cli.post('/api/tutor/phone_stage/rotate', json={'page_id': 'pg_missing_r108', 'step_deg': 90}, headers=h)
                    self.assertEqual(r404.status, 404)
                    self.assertFalse((await r404.json()).get('ok'))

                mock_pause.assert_not_called()
                mock_tts.assert_not_called()

    async def test_36_r108_variant_graduation_peek_guard_mobile_priority_and_resolve_one(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r108_tutor_sim2')

        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('btnMobCwResolveOne', 'resolveMobCwModalItem', 'resolve_one'):
            self.assertIn(tok, h_mob)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()

                async def fake_pipe(sess_in, *a, **kw):
                    sess_in['steps'] = [{'step': 1, 'title': '关键步', 'speak': '去分母', 'board': '2/(x-2)=1'}]
                    sess_in['status'] = 'ready'

                eng._run_live_pipeline = fake_pipe
                eng._post_chat_once = mock.AsyncMock(return_value=json.dumps({
                    'title': '变式分式方程', 'question': '解分式方程 3/(x-1) + 1 = m/(x-1)', 'subject': 'math',
                    'steps': [{'title': 'S1', 'speak': 'B'}],
                }, ensure_ascii=False))
                eng.generate_interactive_lesson = mock.AsyncMock(return_value={
                    'title': '拆解', 'question': '解分式方程 2/(x-2) = 1', 'subject': 'math',
                    'steps': [{'step': 1, 'title': 'S1', 'speak': '去分母'}],
                })
                cw = eng.cowrite

                q_w = cw._upsert_reviewed_candidate(
                    {'no': '第2题', 'subject': 'math', 'box': {'x': 0.52, 'y': 0.86, 'w': 0.42, 'h': 0.10}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第2题', 'topic': '分式方程', 'step_where': '检验', 'checkpoint': '核对增根', 'ocr_stem': '2/(x-2)=1'},
                    raw_sheet,
                )
                mid_w = cw.enroll_mistake(item_id=q_w['id'])['enrolled_ids'][0]

                exp_res = await cw.explain_item(q_w['id'], silent=True)
                await asyncio.sleep(0.03)
                eng.update_progress(exp_res['session']['id'], {'solved': True, 'peeked': True})
                self.assertFalse(q_w.get('blackboard_solved'))
                self.assertFalse(q_w.get('resolved'))

                v1 = await cw.variant_from_item(q_w['id'], silent=True, solo_challenge=True)
                self.assertEqual(v1['session'].get('mistake_id'), mid_w)
                await asyncio.sleep(0.03)
                p1 = eng.update_progress(v1['session']['id'], {'solved': True, 'peeked': False, 'weak_step': 0, 'solo_ace': True, 'first_try_count': 3, 'total_gates': 3})
                self.assertEqual(p1['mistake_event']['stage'], 1)

                v2 = await cw.variant_from_item(q_w['id'], silent=True, solo_challenge=True)
                await asyncio.sleep(0.03)
                p2 = eng.update_progress(v2['session']['id'], {'solved': True, 'peeked': False, 'weak_step': 0, 'solo_ace': True, 'first_try_count': 3, 'total_gates': 3})
                self.assertTrue(p2['mistake_event']['graduated'])
                self.assertTrue(q_w.get('variant_passed'))

                q_old_w = cw._upsert_reviewed_candidate(
                    {'no': '第10题', 'subject': 'math', 'box': {'x': 0.04, 'y': 0.04, 'w': 0.42, 'h': 0.10}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第10题', 'topic': '最老未订正', 'step_where': 'S', 'checkpoint': '重算', 'ocr_stem': 'W10'},
                    raw_sheet,
                )
                q_old_w['ts'] = 100
                for idx_k, k in enumerate(range(11, 20), start=1):
                    cw._upsert_reviewed_candidate(
                        {'no': f'第{k}题', 'subject': 'math', 'box': {'x': 0.04 if idx_k % 2 == 0 else 0.52, 'y': round(0.04 + (idx_k // 2) * 0.16, 2), 'w': 0.42, 'h': 0.10}},
                        {'verdict': 'ok', 'confidence': 0.97, 'no': f'第{k}题', 'topic': '计算', 'checkpoint': '全对 ✓', 'ocr_stem': f'OK{k}'},
                        raw_sheet,
                    )

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}
                    r_st = await cli.get('/api/tutor/mobile/state', headers=hm)
                    j_st = await r_st.json()
                    rec_ids = [x['id'] for x in j_st['cowrite']['recent_items']]
                    self.assertEqual(len(rec_ids), 8)
                    self.assertEqual(rec_ids[0], q_old_w['id'])

                    r_r1 = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'cowrite_remote_ctl', 'sub_action': 'resolve_one', 'id': q_old_w['id'], 'resolved': True,
                    }, headers=hm)
                    j_r1 = await r_r1.json()
                    self.assertTrue(j_r1['ok'] and j_r1['resolved'])
                    self.assertEqual(j_r1['cowrite']['warn_open_count'], 0)

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_37_r109_enroll_preserve_draft_graduation_delete_sync_and_missing_page_404(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r109_tutor_sim1')

        # 1. Plain °C normalization & un-copula'd choice spoiler scrubber
        self.assertIn('℃', tut._cowrite_latex_to_unicode('比热容 c = 4.2 × 10³ J/(kg·°C)'))
        sc = tut._scrub_cowrite_spoilers('此处正确答案 B，这道选择题中选 A 点作垂线', max_len=120)
        self.assertNotIn(' B', sc)
        self.assertIn('选 A 点', sc)
        self.assertIn('选择题', sc)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                cw = eng.cowrite
                q_w = cw._upsert_reviewed_candidate(
                    {'no': '第1题', 'subject': 'math', 'box': {'x': 0.04, 'y': 0.06, 'w': 0.42, 'h': 0.12}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第1题', 'topic': '分式方程', 'step_where': '检验', 'checkpoint': '核对增根', 'ocr_stem': '2/(x-2)=1求x'},
                    raw_sheet,
                )
                mid1 = cw.enroll_mistake(item_id=q_w['id'])['enrolled_ids'][0]
                mb_hit = next(m for m in eng.mistakes if m['id'] == mid1)
                mb_hit['stage'] = 2
                mb_hit['graduated'] = True
                mb_hit['retest_pass_count'] = 2
                mb_hit['steps'] = [
                    {'step': 1, 'title': 'Step 1', 'say': 'S1', 'board': 'B1', 'quiz': {'q': 'Q?', 'opts': ['A', 'B'], 'ans': 0}},
                ]
                eng._save_mistakes()

                # 2. Re-enrolling enrolled item is idempotent & preserves graduated + quiz steps
                re_enr = cw.enroll_mistake(item_id=q_w['id'])
                self.assertEqual(re_enr['enrolled_count'], 0)
                self.assertTrue(mb_hit['graduated'])
                self.assertTrue(mb_hit['steps'][0].get('quiz'))

                # 3. Clean diagnose_draft review updates steps while preserving earned stage/graduated
                res_dd = eng._enroll_or_refresh_mistake(
                    {
                        'id': 's_dd_r109', 'title': '伴写精讲', 'question': '2/(x-2)=1求x', 'mode': 'deep',
                        'teach_mode': 'diagnose_draft', 'pedagogy': 'junior', 'cowrite_item_id': q_w['id'],
                        'steps': [{'step': 1, 'title': '新拆解', 'say': 'S1', 'board': 'B1', 'quiz': {'q': 'Q?', 'opts': ['A', 'B'], 'ans': 0}}],
                    },
                    {'solved': True, 'peeked': False, 'weak_step': 0, 'solo_ace': True, 'first_try_count': 1, 'total_gates': 1},
                )
                self.assertTrue(res_dd['graduated'])
                self.assertEqual(res_dd['stage'], 2)

                # 4. Deleting linked mistake cleans mistake_id (even if enrolled_mistake=False) & HTTP returns cowrite
                q_u = cw._upsert_reviewed_candidate(
                    {'no': '第2题', 'subject': 'math', 'box': {'x': 0.52, 'y': 0.06, 'w': 0.42, 'h': 0.12}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第2题', 'topic': '勾股定理', 'step_where': '公式', 'checkpoint': '核对边长', 'ocr_stem': '直角三角形求斜边'},
                    raw_sheet,
                )
                q_u['enrolled_mistake'] = False
                q_u['mistake_id'] = mid1

                # 5. Stale/missing page_id returns 404 on /check_now, /check_crop & mobile cowrite_check even with another page & camera connected
                eng.camera.discover = lambda: {'connected': True, 'name': 'MockCam'}
                eng.phone_studio.save_page(raw_sheet, note='AlivePage')
                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    r_cn404 = await cli.post('/api/tutor/cowrite/check_now', json={'page_id': 'pg_missing_r109'}, headers=h)
                    self.assertEqual(r_cn404.status, 404)
                    r_cc404 = await cli.post('/api/tutor/cowrite/check_crop', json={'page_id': 'pg_missing_r109', 'crop': {'x': 0.1, 'y': 0.1, 'w': 0.4, 'h': 0.3}}, headers=h)
                    self.assertEqual(r_cc404.status, 404)
                    r_mob404 = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_check', 'page_id': 'pg_missing_r109'}, headers=hm)
                    self.assertEqual(r_mob404.status, 404)

                    r_del = await cli.post('/api/tutor/mistakes/delete', json={'id': mid1}, headers=h)
                    self.assertEqual(r_del.status, 200)
                    j_del = await r_del.json()
                    self.assertIn('cowrite', j_del)
                    self.assertFalse(q_w.get('enrolled_mistake'))
                    self.assertEqual(q_w.get('mistake_id'), '')
                    self.assertEqual(q_u.get('mistake_id'), '')

                mock_pause.assert_not_called()
                mock_tts.assert_not_called()

    async def test_38_r109_retry_sprint_solo_meta_phone_silent_retest_and_mobile_undo(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r109_tutor_sim2')

        h_idx = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for tok in ('solo_challenge: !!session.solo_challenge', 'session.sprint_meta', 'if (j.cowrite)', '黑板已通关'):
            self.assertIn(tok, h_idx)
        h_mob = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for tok in ('btnMobCwUnresolveOne', 'unresolveMobCwModalItem', '↩ 撤回待查', '黑板已通关'):
            self.assertIn(tok, h_mob)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng._post_chat_once = mock.AsyncMock(return_value=json.dumps({
                    'title': '特训变式', 'question': '新题：若x^2-5x+6=0求两根', 'subject': 'math',
                }, ensure_ascii=False))

                async def fake_pipe(sess_in, *a, **kw):
                    sess_in['steps'] = [{'step': 1, 'title': 'S1', 'speak': 'A', 'board': 'B', 'quiz': {'q': 'Q?', 'opts': ['A', 'B'], 'ans': 0}}]
                    sess_in['status'] = 'ready'

                eng._run_live_pipeline = fake_pipe
                cw = eng.cowrite
                q1 = cw._upsert_reviewed_candidate(
                    {'no': '第3题', 'subject': 'math', 'box': {'x': 0.04, 'y': 0.06, 'w': 0.42, 'h': 0.12}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第3题', 'topic': '一元二次', 'step_where': '求根', 'checkpoint': '核对判别式', 'ocr_stem': 'x^2-3x+2=0'},
                    raw_sheet,
                )
                cw.enabled = False
                mb_plain = {
                    'id': 'mb_plain_r109', 'orig_sid': 's_p', 'title': '普通题', 'question': '求解x^2-4=0',
                    'subject': 'math', 'error_tag': '第1关薄弱', 'stage': 0, 'graduated': False, 'next_review_at': 0,
                    'steps': [{'step': 1, 'title': 'S1', 'board': 'B1', 'quiz': {'q': 'Q?', 'opts': ['A', 'B'], 'ans': 0}}],
                }
                eng.mistakes.insert(0, mb_plain)
                eng._save_mistakes()

                # 1. Phone Co-Write used (enabled=False, items non-empty) keeps retest & replay silent=True
                rt = await eng.retest_mistake(mid='mb_plain_r109', open_ui=True)
                self.assertTrue(rt['session']['silent_start'])
                self.assertTrue(eng.public_session('m:mb_plain_r109')['silent_start'])

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 2. Sprint retry via /api/tutor/start forwards solo_challenge, mistake_id, sprint_meta & updates current_sid
                    r_sp = await cli.post('/api/tutor/mistakes/sprint_start', json={'count': 1, 'silent': True, 'solo_challenge': True, 'prefer_cowrite': True}, headers=h)
                    j_sp = await r_sp.json()
                    sp_id = j_sp['sprint_id']
                    sp_meta = j_sp['session']['sprint_meta']
                    sp_mid = j_sp['session']['mistake_id']

                    r_ret = await cli.post('/api/tutor/start', json={
                        'question': j_sp['session']['question'], 'mode': 'backup', 'pedagogy': 'junior',
                        'teach_mode': 'standard', 'source': 'sprint', 'solo_challenge': True, 'silent': True,
                        'mistake_id': sp_mid, 'sprint_meta': sp_meta,
                    }, headers=h)
                    j_ret = await r_ret.json()
                    s_ret = j_ret['session']
                    self.assertTrue(s_ret['solo_challenge'])
                    self.assertTrue(s_ret['silent_start'])
                    self.assertEqual(s_ret['mistake_id'], sp_mid)
                    self.assertEqual(s_ret['sprint_meta']['sprint_id'], sp_id)
                    self.assertEqual(eng.sprints[sp_id]['current_sid'], s_ret['id'])
                    await asyncio.sleep(0.02)
                    eng.update_progress(s_ret['id'], {'solved': True, 'peeked': False, 'weak_step': 0, 'solo_ace': True, 'first_try_count': 3, 'total_gates': 3})
                    r_adv = await cli.post('/api/tutor/mistakes/sprint_next', json={'sprint_id': sp_id}, headers=h)
                    j_adv = await r_adv.json()
                    self.assertTrue(j_adv.get('completed'))
                    self.assertEqual((j_adv.get('report') or {}).get('clean_count'), 1)

                    # 3. Parent mobile resolve_one -> undo resolve_one toggles warn_open_count 0 -> 1
                    r_res = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'resolve_one', 'id': q1['id'], 'resolved': True}, headers=hm)
                    j_res = await r_res.json()
                    self.assertEqual(j_res['cowrite']['warn_open_count'], 0)
                    r_un = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'resolve_one', 'id': q1['id'], 'resolved': False}, headers=hm)
                    j_un = await r_un.json()
                    self.assertFalse(j_un['resolved'])
                    self.assertEqual(j_un['cowrite']['warn_open_count'], 1)

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

    async def test_39_r110_scrubber_cross_photo_epoch_and_ever_graduated_lifecycle(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r110_tutor_sim1')

        # 1. R110 LaTeX \geqslant/\leqslant + un-copula numeric + english spelling + dictation quotes + negative preservation
        uni = tut._cowrite_latex_to_unicode(r'x \geqslant 2, y \leqslant -3')
        self.assertIn('≥', uni)
        self.assertIn('≤', uni)
        self.assertNotIn('\\', uni)

        sc_num = tut._scrub_cowrite_spoilers('核对单位，正确结果 16 J，请复查', max_len=120)
        self.assertNotIn('16', sc_num)

        sc_eng1 = tut._scrub_cowrite_spoilers('核对单词拼写，正确拼写是 knowledge', max_len=120)
        sc_eng2 = tut._scrub_cowrite_spoilers('核对单词，正确拼写 knowledge', max_len=120)
        self.assertNotIn('knowledge', sc_eng1.lower())
        self.assertNotIn('knowledge', sc_eng2.lower())

        sc_dict = tut._scrub_cowrite_spoilers('此处默写应为“长风破浪会有时”中的“沧”', max_len=120)
        self.assertNotIn('长风破浪', sc_dict)
        self.assertNotIn('沧', sc_dict)

        sc_neg = tut._scrub_cowrite_spoilers('请注意该词的正确拼写形式，分式方程增根需舍去，选 A 点作垂线', max_len=120)
        for keep in ('正确拼写形式', '增根', '选 A 点'):
            self.assertIn(keep, sc_neg)

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                cw = eng.cowrite

                # 2. Enroll -> graduate -> delete_mistake(clear_graduated=True) keeps ever_graduated=True & prevents resurrection
                q1 = cw._upsert_reviewed_candidate(
                    {'no': '第1题', 'subject': 'math', 'stage_page_id': 'pg_r110_1', 'box': {'x': 0.04, 'y': 0.06, 'w': 0.42, 'h': 0.14}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第1题', 'topic': '分式方程', 'step_where': '验根', 'checkpoint': '核对增根', 'ocr_stem': '解分式方程 2/(x-2)=1'},
                    raw_sheet,
                )
                mid1 = cw.enroll_mistake(item_id=q1['id'])['enrolled_ids'][0]
                mb1 = next(m for m in eng.mistakes if m['id'] == mid1)
                mb1['stage'] = 1
                eng._save_mistakes()
                res_grad = eng._enroll_or_refresh_mistake(
                    {'id': 's_r110_g2', 'question': '新变式', 'mistake_id': mid1, 'steps': [{'step': 1}]},
                    {'solved': True, 'peeked': False, 'weak_step': 0, 'solo_ace': True, 'first_try_count': 1, 'total_gates': 1},
                )
                self.assertTrue(res_grad and res_grad.get('graduated'))
                self.assertTrue(q1.get('ever_graduated'))

                eng.delete_mistake(clear_graduated=True)
                self.assertTrue(q1.get('ever_graduated'))
                self.assertFalse(q1.get('enrolled_mistake'))
                self.assertEqual(q1.get('mistake_id'), '')
                pub_after = cw.public_state()
                self.assertTrue(pub_after['items'][0]['mistake_graduated'])
                self.assertEqual(pub_after['digest']['unrecorded_warn_count'], 0)
                self.assertEqual(pub_after['digest']['sprintable_count'], 0)

                enr_all = cw.enroll_mistake(enroll_all_warns=True)
                self.assertEqual(enr_all['enrolled_count'], 0)
                enr_one = cw.enroll_mistake(item_id=q1['id'])
                self.assertEqual(enr_one['enrolled_count'], 0)
                self.assertTrue(enr_one.get('already_graduated'))

                res_ps = await eng.prepare_print_sheet(from_cowrite=True)
                self.assertTrue(res_ps.get('ok'))
                self.assertEqual(len(eng.mistakes), 0)
                self.assertFalse(any(str(it.get('tag') or '').startswith('伴写·') for it in (res_ps.get('items') or ())))

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}
                    r_sw = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_remote_ctl', 'sub_action': 'sprint_warns'}, headers=hm)
                    self.assertEqual(r_sw.status, 404)
                    self.assertEqual(len(eng.mistakes), 0)

                # 3. Cross-photo whole-page revision (without _force_id) preserves page_epoch=1 & resets ever_graduated on warn
                cw.resolve_action(new_page=True)
                self.assertEqual(cw.page_epoch, 2)
                q_re = cw._upsert_reviewed_candidate(
                    {'no': '第1题', 'subject': 'math', 'stage_page_id': 'pg_r110_2', 'box': {'x': 0.04, 'y': 0.06, 'w': 0.42, 'h': 0.14}},
                    {'verdict': 'warn', 'confidence': 0.96, 'no': '第1题', 'topic': '分式方程', 'step_where': '去分母', 'checkpoint': '常数项同乘', 'ocr_stem': '解分式方程 2/(x-2)=1'},
                    raw_sheet,
                )
                self.assertIs(q_re, q1)
                self.assertEqual(q_re['page_epoch'], 1)
                self.assertFalse(q_re['ever_graduated'])

                # 4. Cold _mistakes_loaded_path re-warn lazy-loads mistakes.json & un-graduates mb_hit; single/clear_all reset ever_graduated
                mid_rew = cw.enroll_mistake(item_id=q1['id'])['enrolled_ids'][0]
                mb_rew = next(m for m in eng.mistakes if m['id'] == mid_rew)
                mb_rew['graduated'] = True
                mb_rew['stage'] = 2
                eng._save_mistakes()
                eng.mistakes = []
                eng._mistakes_loaded_path = None
                cw._upsert_reviewed_candidate(
                    {'_force_id': q1['id'], 'no': '第1题', 'subject': 'math', 'stage_page_id': 'pg_r110_3', 'box': {'x': 0.04, 'y': 0.06, 'w': 0.42, 'h': 0.14}},
                    {'verdict': 'warn', 'confidence': 0.97, 'no': '第1题', 'topic': '分式方程', 'step_where': '验根', 'checkpoint': '补写检验', 'ocr_stem': '解分式方程 2/(x-2)=1'},
                    raw_sheet,
                )
                mb_loaded = next((m for m in eng.mistakes if m.get('id') == mid_rew), None)
                self.assertIsNotNone(mb_loaded)
                self.assertFalse(mb_loaded['graduated'])
                self.assertEqual(mb_loaded['stage'], 0)

                q1['ever_graduated'] = True
                eng.delete_mistake(mid=mid_rew)
                self.assertFalse(q1['ever_graduated'])
                q1['ever_graduated'] = True
                eng.delete_mistake(clear_all=True)
                self.assertFalse(q1['ever_graduated'])
                mock_pause.assert_not_called()
                mock_tts.assert_not_called()

    async def test_40_r110_api_tool_silence_and_mobile_retest_solo_cowrite_sync(self):
        from aiohttp.test_utils import TestClient, TestServer
        tut = _load_tutor('r110_tutor_sim2')

        raw_sheet = _make_worksheet_jpeg(1)
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            state_d = td_p / 'tutor_state'
            snap_d = state_d / 'snaps'
            state_d.mkdir(parents=True)
            with mock.patch.object(tut, 'STATE_DIR', state_d), \
                 mock.patch.object(tut, 'SNAP_DIR', snap_d), \
                 mock.patch.object(tut, 'HIST_PATH', state_d / 'history.json'), \
                 mock.patch.object(tut, 'PREFS_PATH', state_d / 'prefs.json'), \
                 mock.patch.object(tut, 'SHARE_DIR', _p('/usr/local/share/thinksmart-tutor')), \
                 mock.patch.object(tut.TutorEngine, 'pause_music_if_playing', new_callable=mock.AsyncMock) as mock_pause, \
                 mock.patch.object(tut.TutorEngine, 'synthesize_wav', new_callable=mock.AsyncMock) as mock_tts, \
                 mock.patch.dict(os.environ, {'TUTOR_OPEN_SIGNAL': str(td_p / 'open.json'), 'TUTOR_WARM_GOLDEN': '0'}):
                eng = tut.TutorEngine()
                eng.phone_studio.save_page(raw_sheet, note='p_tool')
                eng.camera.capture_jpeg = mock.AsyncMock(return_value=raw_sheet)

                async def fake_pipe(sess_in, *a, **kw):
                    sess_in['steps'] = [{'step': 1, 'title': 'S1', 'speak': 'A', 'board': 'B', 'quiz': {'q': 'Q?', 'opts': ['A', 'B'], 'ans': 0}}]
                    sess_in['status'] = 'ready'

                eng._run_live_pipeline = fake_pipe
                eng._post_chat_once = mock.AsyncMock(return_value=json.dumps({
                    'title': '复测变式', 'question': '解分式方程 3/(x-1)=2', 'subject': 'math',
                }, ensure_ascii=False))

                cw = eng.cowrite
                q1 = cw._upsert_reviewed_candidate(
                    {'no': '第1题', 'subject': 'math', 'box': {'x': 0.04, 'y': 0.06, 'w': 0.42, 'h': 0.14}},
                    {'verdict': 'warn', 'confidence': 0.95, 'no': '第1题', 'topic': '分式方程', 'step_where': '验根', 'checkpoint': '核对增根', 'ocr_stem': '2/(x-2)=1'},
                    raw_sheet,
                )
                cw.enabled = False

                app = tut.make_app(engine=eng)
                async with TestClient(TestServer(app)) as cli:
                    h = {'Host': f'127.0.0.1:{cli.server.port}'}
                    hm = {'Host': f'127.0.0.1:{cli.server.port}', 'X-TS-Internal': '1'}

                    # 1. api_tool phone_snap & camera_snap silence follows _is_cowrite_silent_active() (even enabled=False with items)
                    r_ps = await cli.post('/api/tutor/tool', json={'action': 'phone_snap', 'question': '解方程 x^2=9'}, headers=hm)
                    j_ps = await r_ps.json()
                    r_cs = await cli.post('/api/tutor/tool', json={'action': 'camera_snap', 'question': '求面积'}, headers=hm)
                    j_cs = await r_cs.json()
                    self.assertTrue(j_ps.get('silent'))
                    self.assertTrue(j_cs.get('silent'))
                    self.assertTrue(eng.sessions[j_ps['session_id']]['silent_start'])
                    self.assertTrue(eng.sessions[j_cs['session_id']]['silent_start'])

                    # 2. mobile trigger_mistake_retest forwards solo_challenge=True & returns cowrite brief; trigger_variant returns cowrite brief
                    mid1 = cw.enroll_mistake(item_id=q1['id'])['enrolled_ids'][0]
                    r_rt = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'trigger_mistake_retest', 'mistake_id': mid1, 'solo_challenge': True,
                    }, headers=hm)
                    j_rt = await r_rt.json()
                    self.assertTrue(j_rt['ok'] and j_rt['silent'])
                    self.assertIn('cowrite', j_rt)
                    rt_sid = j_rt['session']['id']
                    self.assertTrue(eng.sessions[rt_sid]['solo_challenge'])
                    self.assertTrue(eng.sessions[rt_sid]['silent_start'])

                    r_vr = await cli.post('/api/tutor/mobile/upload', json={
                        'action': 'trigger_variant', 'session_id': rt_sid, 'solo_challenge': True,
                    }, headers=hm)
                    j_vr = await r_vr.json()
                    self.assertTrue(j_vr['ok'] and j_vr['silent'])
                    self.assertIn('cowrite', j_vr)

                    # 3. cowrite_snap reports mistake_graduated=True even after clear_graduated=True via ever_graduated
                    next(m for m in eng.mistakes if m['id'] == mid1)['graduated'] = True
                    eng._save_mistakes()
                    eng.delete_mistake(clear_graduated=True)
                    r_sn = await cli.post('/api/tutor/mobile/upload', json={'action': 'cowrite_snap', 'id': q1['id']}, headers=hm)
                    j_sn = await r_sn.json()
                    self.assertTrue(j_sn['ok'])
                    self.assertTrue(j_sn['item']['mistake_graduated'])

                    mock_pause.assert_not_called()
                    mock_tts.assert_not_called()

                    # 4. When Co-Write inactive & empty, api_tool phone_snap & camera_snap are non-silent (silent=False)
                    cw.enabled = False
                    cw.items = []
                    cw._save_state()
                    r_ps_off = await cli.post('/api/tutor/tool', json={'action': 'phone_snap', 'question': '解方程 x^2=16'}, headers=hm)
                    j_ps_off = await r_ps_off.json()
                    self.assertFalse(j_ps_off.get('silent'))
                    self.assertFalse(eng.sessions[j_ps_off['session_id']]['silent_start'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
