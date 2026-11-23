#!/opt/thinksmart-addons/venv/bin/python
"""Automated test suite for AI Dynamic Blackboard Micro-Lesson Tutor R2 (:8096) & Dock/Overlay/Voice Integration (2026-11-16)."""
import asyncio
import base64
import importlib.util
import json
import os
import pathlib
import re
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer


STAGE_ROOT = pathlib.Path(os.environ.get('STAGE_ROOT', '/'))


def _p(abs_path: str) -> pathlib.Path:
    if str(STAGE_ROOT) != '/':
        cand = STAGE_ROOT / abs_path.lstrip('/')
        if cand.exists() or cand.parent.exists():
            return cand
    return pathlib.Path(abs_path)


if str(_p('/usr/local/bin')) not in sys.path:
    sys.path.insert(0, str(_p('/usr/local/bin')))

def _load_mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tutor_mod = _load_mod('ts_tutor_test', str(_p('/usr/local/bin/thinksmart-tutor.py')))
sc_mod = _load_mod('ts_sc_tutor_test', str(_p('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py')))


class TestTutorStaticDockAndR2Helpers(unittest.TestCase):
    def test_dock_order_overlay_voice_wireplumber_lua_and_typography(self):
        apps = json.loads(_p('/usr/local/share/thinksmart-home/apps.json').read_text('utf-8'))['apps']
        ids = [a['id'] for a in apps]
        self.assertIn('server', ids)
        self.assertIn('tutor', ids)
        self.assertEqual(ids.index('tutor'), ids.index('server') + 1, 'AI讲题 must sit immediately after 服务器')
        tutor_tile = apps[ids.index('tutor')]
        self.assertEqual(tutor_tile['title'], 'AI讲题')
        self.assertEqual(tutor_tile['action']['url'], 'http://127.0.0.1:8096/')

        home_html = _p('/usr/local/share/thinksmart-home/index.html').read_text('utf-8')
        self.assertIn('tutor:', home_html)
        self.assertIn('data-app="tutor"', home_html)

        ho_js = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn("'8096'", ho_js)
        self.assertIn('pollTutorBadge', ho_js)

        tool_names = [t['name'] for t in sc_mod.MUSIC_TOOLS]
        self.assertIn('self.tutor.explain', tool_names)
        self.assertIn('self.tutor.explain', sc_mod._MY_TOOLS)

        # Verify WirePlumber 0.4.17 Lua rule exists and legacy 0.5 conf dir is absent
        wp_lua = _p('/home/ubuntu/.config/wireplumber/main.lua.d/52-usb-doccam-no-mic.lua')
        self.assertTrue(wp_lua.exists())
        wp_txt = wp_lua.read_text('utf-8')
        self.assertIn('alsa_input.usb-*', wp_txt)
        self.assertIn('node.disabled', wp_txt)
        self.assertFalse(_p('/home/ubuntu/.config/wireplumber/wireplumber.conf.d').exists())

        # Verify 8 golden Socratic guided sample micro-lessons (Steps 1-3 have quiz + tip2, Step 3 uses num/chain + □ blank)
        self.assertEqual(len(tutor_mod.SAMPLE_PROBLEMS), 8)
        ans_indices = set()
        num_mode_count = 0
        chain_count = 0
        for sp in tutor_mod.SAMPLE_PROBLEMS:
            self.assertEqual(len(sp['golden_steps']), 4)
            for idx, st in enumerate(sp['golden_steps'], 1):
                ops = tutor_mod.sanitize_draw_ops(st['draw'])
                self.assertGreaterEqual(len(ops), 1)
                if idx in (1, 2, 3):
                    self.assertIn('quiz', st, f"{sp['id']} step {idx} missing quiz")
                    orig_correct = st['quiz']['opts'][st['quiz']['ans']]
                    sq = tutor_mod.sanitize_quiz(st['quiz'], idx, sp['id'])
                    self.assertIsNotNone(sq)
                    self.assertEqual(sq['opts'][sq['ans']], orig_correct)
                    self.assertTrue(bool(sq.get('tip2')), f"{sp['id']} step {idx} missing progressive tip2")
                    ans_indices.add(sq['ans'])
                    if idx in (1, 2):
                        self.assertEqual(len(sq['whys']), len(sq['opts']))
                        self.assertEqual(sq['whys'][sq['ans']], '')
                    if idx == 3:
                        self.assertIn('□', st['board'], f"{sp['id']} step 3 must leave □ blank")
                        if sq['mode'] == 'num':
                            num_mode_count += 1
                            self.assertTrue(sq['num_ans'].isdigit())
                        if len(sq.get('chain') or []) >= 2:
                            chain_count += 1
                else:
                    self.assertIsNone(st.get('quiz'))
        self.assertEqual(num_mode_count, 7, '7 numeric samples should use scratchpad fill-in-the-blank mode on Step 3')
        self.assertEqual(chain_count, 4, '4 multi-step samples should provide rung-by-rung chain sub-steps')
        self.assertGreaterEqual(len(ans_indices), 2, 'Quiz options should deterministically shuffle across A/B/C')

        # Verify R5/R6 auto-upgrade, formula-option cleanup & chain spoiler scrubber
        sq_formula_opts = tutor_mod.sanitize_quiz(
            {
                'q': '算出的面积是多少？',
                'num_ans': '288',
                'unit': '平方厘米',
                'chain': [
                    {'q': '先求宽', 'ans': '12', 'unit': '厘米'},
                    {'q': '最后求面积', 'ans': '288', 'unit': '平方厘米'},
                ],
                'opts': ['72÷（2×2＋2）×2平方厘米', '72÷6×24平方厘米', '72÷3平方厘米'],
                'ans': 1,
            },
            step_num=3,
            rng_seed='r6_seed',
        )
        self.assertEqual(sq_formula_opts['mode'], 'num')
        self.assertEqual(len(sq_formula_opts['chain']), 2)
        for o_clean in sq_formula_opts['opts']:
            self.assertNotIn('÷', o_clean)

        sq_live3 = tutor_mod.sanitize_quiz(
            {'q': '面积是多少平方厘米？', 'opts': ['576平方厘米', '288平方厘米', '144平方厘米'], 'ans': 1, 'tip': '长乘宽'},
            step_num=3,
            rng_seed='test_seed',
        )
        self.assertEqual(sq_live3['mode'], 'num')
        self.assertEqual(sq_live3['num_ans'], '288')
        self.assertEqual(sq_live3['unit'], '平方厘米')

        leaked_step = tutor_mod.scrub_step_spoilers({
            'step': 3,
            'title': '列式自算',
            'say': '面积等于288平方厘米，快算算吧！',
            'board': '① 宽 = 72 ÷ 6 = 12 (厘米)\n② 面积 = 24 × 12 = 288 (平方厘米)',
            'quiz': sq_formula_opts,
            'draw': tutor_mod.sanitize_draw_ops([
                {'op': 'clear'},
                {'op': 'rect', 'x': 170, 'y': 110, 'w': 360, 'h': 180, 'label': '面积=288平方厘米'},
            ]),
        })
        self.assertNotIn('= 12', leaked_step['board'])
        self.assertNotIn('288', leaked_step['board'])
        self.assertIn('□', leaked_step['board'])
        self.assertNotIn('288', leaked_step['say'])
        self.assertEqual(leaked_step['draw'][0]['op'], 'clear')
        self.assertIn('□', leaked_step['draw'][1]['label'])

        # R7-R10: Verify Step 1-3 across all 8 golden lessons have method tags, balanced options, and zero meta-giveaways
        for sp in tutor_mod.SAMPLE_PROBLEMS:
            for st in sp['golden_steps'][:3]:
                qz_sp = st['quiz']
                self.assertTrue(qz_sp.get('method'), f"Missing method tag on {sp['id']} step {st['step']}")
                for opt_txt in qz_sp.get('opts', []):
                    self.assertIsNone(
                        re.search(r'误当成|误用|误套|算错|漏除|横纵搞反|符号错', opt_txt),
                        f"Meta-giveaway leaked in {sp['id']} step {st['step']} opt: {opt_txt}",
                    )

        # R10: sanitize_quiz strips LLM meta-giveaways from opts and moves them to whys
        sq_meta = tutor_mod.sanitize_quiz({
            'q': '求斜边 AB？',
            'opts': ['3√5', '5（误套3-4-5）', '误当成 9'],
            'ans': 0,
            'whys': ['', '', ''],
            'no_shuffle': True,
        }, step_num=2)
        self.assertEqual(sq_meta['opts'], ['3√5', '5', '9'])
        self.assertIn('误套3-4-5', sq_meta['whys'][1])
        self.assertIn('误当成', sq_meta['whys'][2])

        # R10: Step 3 spoken say anti-spoiler scrubs dictated raw formulas
        leaked_s3_say = tutor_mod.scrub_step_spoilers({
            'step': 3,
            'title': '链式手算',
            'say': '现在先用140除以2.5求出乙速吧！',
            'board': '① 乙速：140 ÷ 2.5 = □',
            'quiz': {'mode': 'num', 'q': '求乙速', 'num_ans': '56', 'opts': ['56', '70'], 'ans': 0},
            'draw': [],
        })
        self.assertNotIn('140除以2.5', leaked_s3_say['say'])
        self.assertIn('严禁超纲设未知数 x/y', tutor_mod.resolve_pedagogy_hint('auto', ''))

        leaked_s2 = tutor_mod.scrub_step_spoilers({
            'step': 2,
            'title': '辨清份数',
            'say': '数数看一共几份？',
            'board': '一周共有 6 份，总份数 = 6',
            'quiz': {'mode': 'choice', 'q': '共几份？', 'opts': ['6份', '4份', '3份'], 'ans': 0},
            'draw': [],
        })
        self.assertNotIn('共有 6', leaked_s2['board'])
        self.assertNotIn('= 6', leaked_s2['board'])

        # R12: Step 0 Polya warmup mind-map + Step 1/2 spoken conclusion anti-spoiler
        w0 = tutor_mod.build_step0_warmup_draw('哥哥有 46 元，弟弟有 18 元，哥哥给弟弟多少元后，两人钱数一样多？')
        self.assertGreaterEqual(len(w0), 4)
        self.assertTrue(any(o['op'] == 'arrow' for o in w0))
        self.assertFalse(any('多少' in o.get('label', '') for o in w0 if o.get('label', '').startswith('已知')))
        self.assertTrue(any('哥哥给弟弟多少元后' in o.get('label', '') for o in w0 if o.get('label', '').startswith('目标：')))

        leaked_s1_say = tutor_mod.scrub_step_spoilers({
            'step': 1,
            'title': '找突破',
            'say': '我们可以先比较两人相差的钱数，那先求什么？',
            'board': '结论是两人相差的钱数',
            'quiz': {'mode': 'choice', 'q': '先比较什么？', 'opts': ['两人相差的钱数', '两人合起来的钱数', '哥哥原有的钱数'], 'ans': 0},
            'draw': [],
        })
        self.assertNotIn('两人相差的钱数', leaked_s1_say['say'])
        self.assertNotIn('两人相差的钱数', leaked_s1_say['board'])

        scrubbed_qa = tutor_mod.scrub_locked_spoilers_from_qa(
            {'say': '最终答案是50平方厘米哦', 'board': '面积 = 50 (cm²)', 'note': '得数是50'},
            [{'step': 3, 'quiz': {'num_ans': '50', 'chain': []}}, {'step': 4, 'board': 'S = 50 ✓'}],
        )
        self.assertNotIn('50', scrubbed_qa['say'])
        self.assertNotIn('50', scrubbed_qa['board'])
        self.assertNotIn('50', scrubbed_qa['note'])

        # R13: Scene-header merge in Step 0 mind-map + altitude right_angle auto-injection + tip2 anti-spoiler + veiled QA formula guard
        w0_rt = tutor_mod.build_step0_warmup_draw('在 Rt△ABC 中，∠C = 90°，AC = 6 厘米，BC = 8 厘米，求斜边 AB 的长以及斜边上的高 CD。')
        self.assertTrue(any('BC = 8 厘米' in o.get('label', '') for o in w0_rt))
        w0_num = tutor_mod.build_step0_warmup_draw('3. 【一行填空】甲车行驶 2.5 小时行了 150 千米，平均每小时行多少千米？')
        w0_num_lbls = [o.get('label', '') for o in w0_num if o.get('op') == 'text']
        self.assertFalse(any(lb == '已知1：3' for lb in w0_num_lbls), f'Leading item number leaked as 已知1: {w0_num_lbls}')
        self.assertTrue(any('2.5 小时' in lb for lb in w0_num_lbls), f'Decimal 2.5 was split on period: {w0_num_lbls}')

        enriched_s2 = tutor_mod.enrich_step_draw_scaffolds(
            [{'step': 1, 'draw': [
                {'op': 'polygon', 'pts': [[150, 100], [150, 300], [450, 300]]},
                {'op': 'right_angle', 'x': 150, 'y': 300, 'dx': 1, 'dy': -1, 'size': 16},
            ]}],
            {'step': 2, 'board': 'CD⊥AB，垂足为D', 'say': '过C作斜边上的高CD', 'draw': [
                {'op': 'aux_line', 'x1': 150, 'y1': 300, 'x2': 242.3, 'y2': 161.5, 'label': 'CD⊥AB'},
                {'op': 'point', 'x': 242.3, 'y': 161.5, 'label': 'D', 'pos': 'top'},
            ]},
        )
        self.assertTrue(any(o['op'] == 'right_angle' and abs(o['x'] - 242.3) < 2 and abs(o['y'] - 161.5) < 2 for o in enriched_s2['draw']))

        leaked_s3_tip = tutor_mod.scrub_step_spoilers({
            'step': 3,
            'title': '手算',
            'say': '请在草稿本算出得数',
            'board': '① 46 - 18 = □\n② 28 ÷ 2 = □',
            'quiz': {
                'mode': 'num',
                'q': '依次算出：',
                'num_ans': '14',
                'chain': [{'q': '差是多少？', 'ans': '28', 'tip': '46 - 18 = 28'}, {'q': '给多少？', 'ans': '14', 'tip': '平分'}],
                'opts': ['14', '28', '7'],
                'ans': 0,
                'tip': '先算差 = 28',
                'tip2': '① 差 = 28；② 给 = 14',
            },
            'draw': [],
        })
        self.assertNotIn('= 28', leaked_s3_tip['quiz']['tip2'])
        self.assertNotIn('= 14', leaked_s3_tip['quiz']['tip2'])
        self.assertNotIn('= 28', leaked_s3_tip['quiz']['chain'][0]['tip'])

        for sp in tutor_mod.SAMPLE_PROBLEMS:
            for gs in sp['golden_steps']:
                gqz = gs.get('quiz') or {}
                if gs['step'] == 3 and gqz.get('chain'):
                    r0_ans = str(gqz['chain'][0]['ans'])
                    self.assertNotRegex(gqz.get('tip2', ''), rf'[=＝]\s*{re.escape(r0_ans)}(?![0-9.])', f"Sample {sp['id']} tip2 leaks rung 0 answer!")

        scrubbed_qa_veil = tutor_mod.scrub_locked_spoilers_from_qa(
            {'say': '先用 46 减去 18 等于 28，最后答案是 14', 'board': '列式：46 - 18 = 28', 'note': '46 - 18'},
            [{'step': 3, 'quiz': {'mode': 'num', 'num_ans': '14', 'chain': [{'ans': '28'}, {'ans': '14'}]}}, {'step': 4, 'board': '答：给14元'}],
            formula_veiled=True,
        )
        self.assertNotIn('46 - 18', scrubbed_qa_veil['board'])
        self.assertNotIn('14', scrubbed_qa_veil['say'])

        # Verify Tutor UI strictly obeys 400/700 native font-weight, integer px, Socratic R7 UI, and vis===false audio stop
        ui_html = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('stopLessonPlayback()', ui_html)
        self.assertIn('cancelVoiceRecSilent()', ui_html)
        self.assertIn('drawCurlyBrace', ui_html)
        self.assertIn('braceSideForOp', ui_html)
        self.assertIn('labelAnchorForOp', ui_html)
        self.assertIn('formatQuestionHighlights', ui_html)
        self.assertIn('formatStepBoardHtml', ui_html)
        self.assertIn('diagnoseNumericError', ui_html)
        self.assertIn('pickRungRescueOpt', ui_html)
        self.assertIn('STAGE_QA_CHIPS', ui_html)
        self.assertIn('veilFormulaOnLine', ui_html)
        self.assertIn('unveilFormula', ui_html)
        self.assertIn('resolveDynamicCanvasLabel', ui_html)
        self.assertIn('togglePedagogy', ui_html)
        self.assertIn('submitFeynmanCheck', ui_html)
        self.assertIn('checkFractionSubmission', ui_html)
        self.assertIn('reshuffleChoiceStep', ui_html)
        self.assertIn('hasVeilableFormula', ui_html)
        self.assertIn('stepRungHasVeilableFormula', ui_html)
        self.assertIn('drawOrientedRightAngle', ui_html)
        self.assertIn('buildFormulaChoices', ui_html)
        self.assertIn('openFormulaBuilder', ui_html)
        self.assertIn('pickFormulaChoice', ui_html)
        self.assertIn('onStepCardClick', ui_html)
        self.assertIn('expandedStepSet', ui_html)
        self.assertIn('parseSpokenOptionIdx', ui_html)
        self.assertIn('toggleFeynmanText', ui_html)
        self.assertIn('submitFeynmanTextDraft', ui_html)
        self.assertIn('collectSolvedChainVars', ui_html)
        self.assertIn('inferImplicitRungExpr', ui_html)
        self.assertIn('resolveActiveTip2', ui_html)
        self.assertIn('explainFormulaChoiceError', ui_html)
        self.assertIn('toggleFeynmanPuzzle', ui_html)
        self.assertIn('buildFeynmanPuzzleLinks', ui_html)
        self.assertIn('pickFeynmanPuzzleOpt', ui_html)
        self.assertIn('computeCurrentWeakInfo', ui_html)
        self.assertIn('checkNumericPlausibility', ui_html)
        self.assertIn('substituteSolvedInExpr', ui_html)
        self.assertIn('cleanSolvedRungLabel', ui_html)
        self.assertIn('isFeynmanClosedBook', ui_html)
        self.assertIn('safeEvalSimpleMath', ui_html)
        self.assertIn('simulateFormulaMistakes', ui_html)
        self.assertIn('explainCanvasElement', ui_html)
        self.assertIn('onCanvasTap', ui_html)
        self.assertIn('canvasHitBoxes', ui_html)
        self.assertIn('strictSingleEntity', ui_html)
        self.assertIn('parseCnNumberToken', ui_html)
        self.assertIn('normalizeSpokenMathExpr', ui_html)
        self.assertIn('quizEverUsedHintSet', ui_html)
        self.assertIn('highlightStepVisualClue', ui_html)
        self.assertIn('feynmanPuzzleCausalDone', ui_html)
        self.assertIn('buildRungMentalScaffold', ui_html)
        self.assertIn('willReadFrequently', ui_html)
        self.assertIn('❓ 找中间桥', ui_html)

        # R17: Deduplicate Step 2 <-> Step 3 redundant sub-question rung + strip procedural slogan text pills
        self.assertEqual(tutor_mod.sanitize_draw_ops([{'op': 'text', 'x': 500, 'y': 230, 'label': '按份数依次计算'}]), [])
        dedup_s3 = tutor_mod.dedupe_step3_redundant_rungs(
            [{'step': 2, 'quiz': {'mode': 'choice', 'q': '多出的48棵相当于梨树的几倍？', 'opts': ['相当于梨树的2倍', '相当于梨树的3倍'], 'ans': 0}}],
            {
                'step': 3,
                'board': '① 倍数：3 − 1 = □（倍）\n② 梨树：48 ÷ ①的结果 = □（棵）\n③ 桃树：梨树 × 3 = □（棵）',
                'quiz': {
                    'mode': 'num',
                    'num_ans': '72',
                    'unit': '棵',
                    'chain': [
                        {'q': '多出的棵数相当于梨树的几倍？', 'ans': '2', 'unit': '倍'},
                        {'q': '梨树有多少棵？', 'ans': '24', 'unit': '棵'},
                        {'q': '桃树有多少棵？', 'ans': '72', 'unit': '棵'},
                    ],
                },
            },
        )
        self.assertEqual(len(dedup_s3['quiz']['chain']), 2)
        self.assertEqual(dedup_s3['quiz']['chain'][0]['ans'], '24')
        self.assertIn('3 − 1 = 2 ✓', dedup_s3['board'])
        self.assertIn('48 ÷ 2 = □', dedup_s3['board'])

        # R15: Expanded unit spoiler scrubbing + Step-0 conditional goal grouping + multi-row seg_bar right-side vertical brace normalization
        leaked_mei = tutor_mod.scrub_step_spoilers({
            'step': 1,
            'title': '抓住数量关系',
            'say': '拿走多出的12枚后，两盒还剩60枚徽章。',
            'board': '两盒还剩 60',
            'quiz': {'mode': 'choice', 'q': '两盒还剩多少枚？', 'opts': ['60枚', '72枚', '84枚'], 'ans': 0, 'tip': '还剩 60'},
            'draw': [
                {'op': 'seg_bar', 'x': 150, 'y': 150, 'w': 320, 'parts': 1, 'label': '红盒'},
                {'op': 'seg_bar', 'x': 150, 'y': 240, 'w': 213, 'parts': 1, 'label': '蓝盒'},
                {'op': 'brace', 'x1': 150, 'y1': 305, 'x2': 470, 'y2': 305, 'label': '两盒共72枚'},
            ],
        })
        self.assertNotIn('60枚', leaked_mei['say'])
        self.assertNotIn('60', leaked_mei['board'])
        self.assertNotIn('60', leaked_mei['quiz']['tip'])

        s0_cond = tutor_mod.build_step0_warmup_draw(
            '航天小组有72枚徽章，分装在红盒和蓝盒里，红盒比蓝盒多12枚。如果先从红盒中拿出多的12枚，剩下的徽章相当于几个蓝盒的数量？两盒原来各有多少枚徽章？'
        )
        s0_labels = [o.get('label', '') for o in s0_cond if o.get('op') == 'text']
        self.assertFalse(any('如果先从红盒' in lb and lb.startswith('已知') for lb in s0_labels), f'Conditional premise leaked into 已知: {s0_labels}')
        self.assertTrue(any(lb.startswith('目标：') and '如果先从红盒' in lb for lb in s0_labels), f'Conditional premise missing from 目标: {s0_labels}')

        norm_step = tutor_mod.enrich_step_draw_scaffolds([], leaked_mei)
        total_br = next(o for o in norm_step['draw'] if o.get('op') == 'brace' and '共72枚' in o.get('label', ''))
        self.assertEqual(total_br['x1'], total_br['x2'], 'Multi-row seg_bar total brace must be normalized to vertical!')
        self.assertGreaterEqual(total_br['x1'], 490.0, 'Multi-row seg_bar total brace must sit to the right of the bars!')
        self.assertLessEqual(total_br['y1'], 150.0)
        self.assertGreaterEqual(total_br['y2'], 240.0)

        tts_cir_ref = tutor_mod.clean_math_for_tts('① 先算平方；② AB＝√①，CD＝6×8÷②')
        self.assertIn('第一步，', tts_cir_ref)
        self.assertIn('根号下第一步的结果', tts_cir_ref)
        self.assertIn('除以第二步的结果', tts_cir_ref)
        self.assertIn('feyn-guide', ui_html)
        self.assertIn('tapQuestionClue', ui_html)
        self.assertIn('parseNumOrFrac', ui_html)
        intercom_src = _p('/usr/local/bin/thinksmart-intercom.py').read_text('utf-8')
        # 2026-11-18 Claude: 手机拍题单独一个页面（/tutor/），不放在远程可视（对讲）页面；学段改在拍题页 mobile.html 里选
        self.assertNotIn('phTutorPedagogy', intercom_src)
        self.assertIn('selPedagogy', _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8'))
        site_src = _p('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py').read_text('utf-8')
        self.assertIn("'pedagogy'", site_src)
        self.assertIn('renderOpPass', ui_html)
        self.assertIn('unlockAudioGesture', ui_html)
        self.assertIn('cycleSpeechRate', ui_html)
        self.assertIn('pickQuizOption', ui_html)
        self.assertIn('submitNumQuiz', ui_html)
        self.assertIn('shouldResetCanvasStage', ui_html)
        self.assertIn('startGuidedHold', ui_html)
        self.assertIn('toggleGuidedMode', ui_html)
        self.assertIn('activeQuizBox', ui_html)
        style_m = re.search(r'<style>(.*?)</style>', ui_html, re.S)
        self.assertIsNotNone(style_m)
        css = re.sub(r'/\*.*?\*/', '', style_m.group(1), flags=re.S)
        weights = set(re.findall(r'font-weight:\s*([^;}\s]+)', css))
        self.assertTrue(weights.issubset({'400', '700'}), f'Disallowed font-weights in tutor CSS: {weights}')
        frac_px = re.findall(r'\d+\.\d+px', css)
        self.assertEqual(frac_px, [], f'Fractional px in tutor CSS: {frac_px}')

    def test_uvc_camera_isolates_external_usb_from_internal_soc(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            sys_v4l = root / 'v4l'
            sys_v4l.mkdir()
            soc_real = root / 'devices/platform/soc@0/1b00020.camss/video4linux/video0'
            soc_real.mkdir(parents=True)
            (soc_real / 'index').write_text('0')
            (soc_real / 'name').write_text('msm_vfe0_pix')
            os.symlink(str(soc_real), str(sys_v4l / 'video0'))

            usb_v8 = root / 'devices/platform/soc@0/7000000.usb/xhci-hcd/usb1/1-1/video4linux/video8'
            usb_v8.mkdir(parents=True)
            (usb_v8 / 'index').write_text('0')
            (usb_v8 / 'name').write_text('HD 8MP Document Camera')
            os.symlink(str(usb_v8), str(sys_v4l / 'video8'))

            usb_v9 = root / 'devices/platform/soc@0/7000000.usb/xhci-hcd/usb1/1-1/video4linux/video9'
            usb_v9.mkdir(parents=True)
            (usb_v9 / 'index').write_text('1')
            (usb_v9 / 'name').write_text('HD 8MP Metadata')
            os.symlink(str(usb_v9), str(sys_v4l / 'video9'))

            orig_path = tutor_mod.Path

            def fake_path(p):
                if p == '/sys/class/video4linux':
                    return sys_v4l
                return orig_path(p)

            with mock.patch.object(tutor_mod, 'Path', side_effect=fake_path), \
                 mock.patch('os.path.exists', side_effect=lambda p: True if str(p).startswith('/dev/video') else os.path.exists(p)):
                cam = tutor_mod.UVCCameraManager().discover()
                self.assertTrue(cam['connected'])
                self.assertEqual(cam['dev'], '/dev/video8')
                self.assertEqual(cam['name'], 'HD 8MP Document Camera')

    def test_math_tts_normalizer_brace_extractor_and_r2_draw_primitives(self):
        spoken = tutor_mod.clean_math_for_tts('① 在 Rt△ABE 中，∠B=30°，S扇−S正=50cm²，AE=½AB，长∶宽=3/5')
        self.assertIn('第一步，', spoken)
        self.assertIn('直角三角形ABE', spoken)
        self.assertIn('角B', spoken)
        self.assertIn('30度', spoken)
        self.assertIn('扇形面积', spoken)
        self.assertIn('正方形面积', spoken)
        self.assertIn('50平方厘米', spoken)
        self.assertIn('二分之一AB', spoken)
        self.assertIn('长比宽', spoken)
        self.assertIn('5分之3', spoken)

        # R11: ASCII +, -, =, unary negative, and 3-4-5 TTS pronunciation + single-rung chain promotion + spaced text splitter
        spoken_ascii = tutor_mod.clean_math_for_tts('y = -2x + 6，28 - 20 = 8，勾股数 3-4-5')
        self.assertIn('y等于负2x加6', spoken_ascii.replace(' ', ''))
        self.assertIn('28减20等于8', spoken_ascii.replace(' ', ''))
        self.assertIn('3、4、5', spoken_ascii)

        sq_1rung = tutor_mod.sanitize_quiz({
            'q': '请在草稿本按步骤依次算出结果：',
            'chain': [{'q': '一共需要栽多少棵树苗？', 'ans': '7', 'unit': '棵', 'tip': '间隔段数加1'}],
            'opts': ['7棵', '6棵', '8棵'],
            'ans': 0,
        }, step_num=3)
        self.assertEqual(sq_1rung['q'], '一共需要栽多少棵树苗？')
        self.assertEqual(sq_1rung['num_ans'], '7')
        self.assertEqual(sq_1rung['chain'], [])

        split_ops = tutor_mod.sanitize_draw_ops([{'op': 'text', 'x': 170, 'y': 180, 'label': '车尾        车头'}])
        self.assertEqual(len(split_ops), 2)
        self.assertEqual([split_ops[0]['label'], split_ops[1]['label']], ['车尾', '车头'])

        self.assertIn('小学生', tutor_mod.resolve_pedagogy_hint('auto', '把一根长72厘米的铁丝围成长方形'))
        self.assertIn('初中生', tutor_mod.resolve_pedagogy_hint('auto', '已知一次函数 y=-2x+6 与 x轴交于A'))
        self.assertIn('小学生', tutor_mod.resolve_pedagogy_hint('elem', '已知一次函数'))
        self.assertIn('初中生', tutor_mod.resolve_pedagogy_hint('junior', '鸡兔同笼'))

        fenced_stream = (
            '```json\n[\n'
            '  {\n    "step": 1,\n    "title": "画图",\n    "say": "先画括号 {a+b}"\n  },\n'
            '  {\n    "step": 2,\n    "title": "作高",\n    "say": "再作垂线"\n  },\n'
            '  {"step": 3, "title": "未传完'
        )
        objs, rem = tutor_mod.extract_json_objects_from_buffer(fenced_stream)
        self.assertEqual(len(objs), 2)
        self.assertEqual(objs[0]['say'], '先画括号 {a+b}')
        self.assertEqual(objs[1]['step'], 2)
        self.assertTrue(rem.startswith('{"step": 3'))

        ops = tutor_mod.sanitize_draw_ops([
            {'op': 'aux_line', 'x1': -50, 'y1': 999, 'x2': 800, 'y2': 150, 'label': '高AE'},
            {'op': 'arrow', 'from': [80, 100], 'to': [300, 100], 'label': '甲行3小时'},
            {'op': 'polygon', 'vertices': [[100, 100], [300, 100], [200, 260]], 'label': '△ABC'},
            {'op': 'rect', 'x': 120, 'y': 90, 'width': 200, 'height': 140, 'label': '正方形'},
            {'op': 'text', 'x': 200, 'y': 60, 'text': '核心批注'},
            {'op': 'right_angle', 'x': 330, 'y': 300, 'dx': -1, 'dy': -1, 'size': 90},
            {'op': 'unknown_bad_op', 'x': 1},
        ])
        self.assertEqual([o['op'] for o in ops], ['aux_line', 'arrow', 'polygon', 'rect', 'text', 'right_angle'])
        self.assertEqual(ops[0]['x1'], 24.0)
        self.assertEqual(ops[0]['y1'], 420.0)
        self.assertEqual(ops[1]['x2'], 300.0)
        self.assertEqual(ops[2]['pts'][2], [200.0, 260.0])
        self.assertEqual(ops[3]['w'], 200.0)
        self.assertEqual(ops[4]['label'], '核心批注')
        self.assertEqual(ops[5]['size'], 24.0)

        # Verify R3 history deduplication & smart 3-tier model routing
        deduped = tutor_mod._dedupe_history_entries([
            {'id': 'a1', 'question': '把一根长72厘米的铁丝围成长方形？', 'mode': 'fast', 'image_url': ''},
            {'id': 'a2', 'question': '把一根长72厘米的铁丝围成长方形？', 'mode': 'fast', 'image_url': ''},
            {'id': 'a3', 'question': '把一根长72厘米的铁丝围成长方形？', 'mode': 'geo', 'image_url': ''},
        ])
        self.assertEqual([x['id'] for x in deduped], ['a1', 'a3'])

        with tempfile.TemporaryDirectory() as td:
            p_cfg = pathlib.Path(td) / 'gw.json'
            p_cfg.write_text(json.dumps({
                'LLM': {
                    'OpenAILLM': {'base_url': 'http://127.0.0.1:9999', 'api_key': 'k', 'model_name': 'gpt-6.1-terra'},
                    'GeminiAPI': {'base_url': 'http://127.0.0.1:9999', 'api_key': 'k', 'model_name': 'gemini-4-hydrogen'},
                    'DeepSeekLLM': {'url': 'http://127.0.0.1:9999', 'api_key': 'k', 'model_name': 'deepseek-v4.1-flash'},
                }
            }), encoding='utf-8')
            old_c, old_h, old_p = tutor_mod.HUB_CFG_PATH, tutor_mod.HIST_PATH, tutor_mod.PREFS_PATH
            try:
                tutor_mod.HUB_CFG_PATH = p_cfg
                tutor_mod.HIST_PATH = pathlib.Path(td) / 'hist.json'
                tutor_mod.PREFS_PATH = pathlib.Path(td) / 'prefs.json'
                e_tmp = tutor_mod.TutorEngine()
                # Primary default -> gpt-6.1-terra -> gpt-6.1-sol -> deepseek-v4.1-flash (zero Gemini!)
                c_fast = e_tmp._resolve_llm_chain('fast')[2]
                self.assertEqual([m for m, _ in c_fast], ['gpt-6.1-terra', 'gpt-6.1-sol', 'deepseek-v4.1-flash'])
                # Deep mode -> gpt-6.1-sol -> gpt-6.1-terra -> deepseek-v4.1-flash
                c_deep = e_tmp._resolve_llm_chain('deep')[2]
                self.assertEqual([m for m, _ in c_deep], ['gpt-6.1-sol', 'gpt-6.1-terra', 'deepseek-v4.1-flash'])
                # Manual backup mode -> deepseek-v4.1-flash -> gpt-6.1-terra -> gpt-6.1-sol
                c_bkp = e_tmp._resolve_llm_chain('backup')[2]
                self.assertEqual([m for m, _ in c_bkp], ['deepseek-v4.1-flash', 'gpt-6.1-terra', 'gpt-6.1-sol'])
            finally:
                tutor_mod.HUB_CFG_PATH, tutor_mod.HIST_PATH, tutor_mod.PREFS_PATH = old_c, old_h, old_p


class TestTutorHttpStreamingAndSecuritySilent(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        tp = pathlib.Path(self._td.name)
        self._orig_state = tutor_mod.STATE_DIR
        self._orig_snap = tutor_mod.SNAP_DIR
        self._orig_hist = tutor_mod.HIST_PATH
        self._orig_prefs = tutor_mod.PREFS_PATH
        self._orig_cfg = tutor_mod.HUB_CFG_PATH
        tutor_mod.STATE_DIR = tp / 'state'
        tutor_mod.SNAP_DIR = tutor_mod.STATE_DIR / 'snaps'
        tutor_mod.HIST_PATH = tutor_mod.STATE_DIR / 'history.json'
        tutor_mod.PREFS_PATH = tutor_mod.STATE_DIR / 'prefs.json'
        self._tts_fail_mode = False

        up_app = web.Application()

        async def mock_chat(req):
            body = await req.json()
            if body.get('stream'):
                resp = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
                await resp.prepare(req)
                # Deliberately stream multi-line JSON array inside markdown fence to verify R2 parser
                chunks = [
                    '```json\n[\n  {"step":1,"title":"画图审题",',
                    '"say":"先画线段图","board":"总和=30","draw":[{"op":"seg_bar","x":100,"y":200,"w":200,"parts":2,"label":"2份"}]},\n',
                    '  {"step":2,"title":"作对齐辅助线","say":"画红色虚线对齐","board":"1份=15","draw":[{"op":"aux_line","x1":200,"y1":180,"x2":200,"y2":260,"label":"对齐"}]}\n]\n```',
                ]
                for part in chunks:
                    chunk = {'choices': [{'delta': {'content': part}}]}
                    await resp.write(('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode('utf-8'))
                await resp.write(b'data: [DONE]\n\n')
                await resp.write_eof()
                return resp

            msg_str = json.dumps(body.get('messages') or [], ensure_ascii=False)
            if '费曼学习法' in msg_str:
                reply = '{"pass":true,"badge":"👑 金牌小讲师","say":"讲得太清楚啦，把周长6份这个关键点抓得稳稳的！","comment":"抓住关键份数桥梁，表达清晰！"}'
            elif '先批后讲' in msg_str:
                reply = '{"summary":"共识别2题：做对1题，待订正1题","items":[{"no":"第1题","status":"ok","box":[0.06,0.10,0.88,0.20],"note":"列式与结果全对"},{"no":"第2题","status":"wrong","box":[0.06,0.42,0.88,0.24],"note":"正确答案是288，72÷2=36需核对"}]}'
            elif '打印 A4 纸质清卷' in msg_str:
                reply = '{"items":[{"title":"换数第1题","question":"把80米篱笆围成长方形，长是宽的3倍，求面积？","ans_check":"300平方米"}]}'
            elif '手写思路点评' in msg_str:
                reply = '{"title":"👀 手写思路点评","say":"你画的垂线方向很准！不过最后得数是50，再看看底边分成了哪两段？","board":"✓ 垂线方向正确\n🧭 注意面积=50别算错","note":"注意底边分段"}'
            elif '前置基础概念' in msg_str:
                reply = '{"title":"基础概念微课","principle":"等高线越密集坡度越陡，最终答案是50。","example":"小山坡模型：每升高100米降温0.6度","apply_tip":"先看海拔差再算温差","draw":[{"op":"rect","x":180,"y":130,"w":300,"h":120,"label":"等高线模型"}]}'
            elif '考场满分卷面标准书写示范' in msg_str:
                reply = '{"badge":"📝 考场满分规范","title":"考场标准书写示范","script":"解：过点A作AE⊥BC于E。\\n∵ 在Rt△ABE中，∠B=30°，AB=10cm\\n∴ 高 AE = 5cm，S梯形 = (6+14)×5÷2 = 50(cm²)\\n答：梯形的面积是50平方厘米。","checkpoints":["先交代辅助线作法（过A作AE⊥BC）","梯形面积公式先算上下底之和务必加括号","末尾写完整答语及平方厘米单位"],"say":"上考场记得先写清辅助线作法，算式加好括号并带齐单位，步骤分一分不丢！"}'
            elif '母题变脸魔术' in msg_str:
                reply = '{"title":"母题变脸识破：和差倍同构","badge":"🔗 同核不同皮","core_bone":"抓住总和与份数比平分求1份","headers":["底层骨相","本题皮相","考场变身①","考场变身②"],"rows":[["总量","30","师徒共做30件","两管共注30升"],["单份","30÷2=15","工效和÷份数","流量和÷份数"]],"insight":"换汤不换药，认准总量与份数即可通杀！","say":"不管换成什么生活故事，底层骨相全是一模一样的份数关系！"}'
            elif '特例化降维破冰法' in msg_str:
                reply = '{"title":"🪄 波利亚降维试算","cases":["特例① (第1个图)：1 + 3×1 = 4根","特例② (第2个图)：1 + 3×2 = 7根","特例③ (第3个图)：1 + 3×3 = 10根"],"rule_q":"发现没？每多摆1个正方形就固定增加3根，而最终答案是50！","bridge":"用起始1根加上3乘序号就能算任意项！","draw":[{"op":"table","x":90,"y":90,"w":500,"headers":["序号","火柴根数"],"rows":[["1","4"],["2","7"],["3","10"]],"label":"规律表"}]}'
            elif '一题多解' in msg_str:
                reply = '{"title":"倒推份数验证法","badge":"逆向巧解法","steps":"把总和30按2份平分得1份=15，逆向相加15+15=30验证无误！","compare":"正推直观，倒推适合快速验算！","say":"我们还可以换个角度用逆向平分法，30平分两份也是15，两种方法完美印证！"}'
            elif '变式题' in msg_str:
                is_h = '进阶拔高' in msg_str
                reply = (
                    '{"check_calc":"(8+12)*6/2=60","title":"进阶练：梯形逆求高","question":"梯形面积60平方厘米，上底8厘米，下底12厘米，求高。"}'
                    if is_h else
                    '{"check_calc":"(8+12)*6/2=60","title":"变式练：梯形求面积","question":"梯形上底8厘米，下底12厘米，高6厘米，求面积。"}'
                )
            else:
                reply = '{"title":"辅助线原理","ocr":"测试识别题目","say":"因为作垂线才能构造直角三角形求高，最终答案是50。","board":"构造Rt△求高，S = 50","note":"见30°角作垂线"}'
            return web.json_response({'choices': [{'message': {'content': reply}}]})

        async def mock_tts(_req):
            if self._tts_fail_mode:
                return web.Response(status=503, text='busy')
            return web.Response(body=b'\x01\x00' * 4800, content_type='application/octet-stream')

        async def mock_asr(_req):
            return web.json_response({'text': '笼子里有鸡和兔共10只，腿共28条'})

        up_app.router.add_post('/v1/chat/completions', mock_chat)
        up_app.router.add_post('/v1/audio/transcriptions', mock_asr)
        up_app.router.add_post('/tts', mock_tts)
        self._up_srv = TestServer(up_app)
        await self._up_srv.start_server()
        up_base = f'http://127.0.0.1:{self._up_srv.port}'

        fake_cfg = tp / 'hub_cfg.yaml'
        fake_cfg.write_text(json.dumps({
            'LLM': {
                'GeminiAPI': {'base_url': up_base, 'api_key': 'test_k', 'model_name': 'mock-gemini'},
                'DeepSeekLLM': {'url': up_base, 'api_key': 'test_k', 'model_name': 'mock-deepseek'},
            },
            'TTS': {
                'IndexStreamTTS': {'api_url': f'{up_base}/tts', 'voice': 'xiaohe'},
            },
            'ASR': {
                'MacASR': {'base_url': up_base, 'api_key': 'none', 'model_name': 'Qwen3-ASR-1.7B'},
            },
        }), encoding='utf-8')
        tutor_mod.HUB_CFG_PATH = fake_cfg

        self.eng = tutor_mod.TutorEngine()
        self._pause_patch = mock.patch.object(self.eng, 'pause_music_if_playing', new=mock.AsyncMock())
        self._pause_patch.start()
        return tutor_mod.make_app(self.eng)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self._pause_patch.stop()
        await self._up_srv.close()
        tutor_mod.STATE_DIR = self._orig_state
        tutor_mod.SNAP_DIR = self._orig_snap
        tutor_mod.HIST_PATH = self._orig_hist
        tutor_mod.PREFS_PATH = self._orig_prefs
        tutor_mod.HUB_CFG_PATH = self._orig_cfg
        self._td.cleanup()

    async def test_r2_end_to_end_tts_retry_variant_voice_and_history_delete(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}

        # 1. Security guards
        r_bad_host = await self.client.get('/api/tutor/state', headers={'Host': 'evil.com'})
        self.assertEqual(r_bad_host.status, 403)
        r_bad_sec = await self.client.get('/api/tutor/state', headers={**H, 'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r_bad_sec.status, 403)
        r_bad_int = await self.client.post('/api/tutor/tool', headers=H, json={'action': 'open'})
        self.assertEqual(r_bad_int.status, 403)

        # 2. Index ETag & 304
        r_idx = await self.client.get('/', headers=H)
        self.assertEqual(r_idx.status, 200)
        r_304 = await self.client.get('/', headers={**H, 'If-None-Match': r_idx.headers['ETag']})
        self.assertEqual(r_304.status, 304)

        # 3. Verify transient TTS 503 does NOT poison tts_cache, and recovers once healthy
        self._tts_fail_mode = True
        wav_fb = await self.eng.synthesize_wav('测试临时超时不写入静音缓存')
        self.assertEqual(wav_fb[:4], b'RIFF')
        self.assertEqual(len(self.eng.tts_cache), 0, 'Silent fallback must NEVER poison tts_cache')
        self._tts_fail_mode = False
        wav_ok = await self.eng.synthesize_wav('测试临时超时不写入静音缓存')
        self.assertEqual(len(self.eng.tts_cache), 1)
        self.assertIn(b'\x01\x00', wav_ok[44:])

        # 4. Golden sample lesson + 24kHz WAV header verification
        r_gold = await self.client.post('/api/tutor/start', headers=H, json={'sample_id': 'geo_trapezoid_30', 'live': False})
        j_gold = await r_gold.json()
        self.assertTrue(j_gold['ok'])
        sid_g = j_gold['session']['id']
        self.assertEqual(len(j_gold['session']['steps']), 4)

        r_wav = await self.client.get(f'/api/tutor/tts?sid={sid_g}&idx=0', headers=H)
        self.assertEqual(r_wav.status, 200)
        wav_bytes = await r_wav.read()
        self.assertEqual(struct.unpack_from('<I', wav_bytes, 24)[0], 24000)

        # 5. Live Dual-Stage Streaming fenced multi-line JSON lesson (DeepSeek mode with image upload)
        fake_jpg = b'\xff\xd8\xff\xe0' + b'\x00' * 300 + b'\xff\xd9'
        r_ph = await self.client.post(
            '/api/tutor/phone_upload',
            headers={**H, 'X-TS-Internal': '1'},
            json={'image_b64': base64.b64encode(fake_jpg).decode(), 'mode': 'deep', 'pedagogy': 'junior'},
        )
        j_ph = await r_ph.json()
        self.assertTrue(j_ph['ok'])
        sid_live = j_ph['session']['id']
        self.assertTrue(self.eng.is_open_ui_fresh())

        r_tool_voice = await self.client.post(
            '/api/tutor/tool',
            headers={**H, 'X-TS-Internal': '1'},
            json={'action': 'open', 'sample_id': 'geo_trapezoid_30', 'silent_preview': True, 'preview_step': 0},
        )
        j_tv = await r_tool_voice.json()
        self.assertTrue(j_tv['ok'])
        pub_tv = self.eng.public_session(j_tv['session_id'])
        self.assertEqual(pub_tv['preview_step'], 0)

        # Re-run phone upload so sid_live remains the active streaming session
        r_ph = await self.client.post(
            '/api/tutor/phone_upload',
            headers={**H, 'X-TS-Internal': '1'},
            json={'image_b64': base64.b64encode(fake_jpg).decode(), 'mode': 'deep', 'pedagogy': 'elem'},
        )
        j_ph = await r_ph.json()
        sid_live = j_ph['session']['id']

        for _ in range(30):
            rs = await (await self.client.get(f'/api/tutor/session?id={sid_live}', headers=H)).json()
            if rs['session']['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        self.assertEqual(rs['session']['status'], 'ready')
        self.assertEqual(len(rs['session']['steps']), 3)
        self.assertEqual(rs['session']['question'], '测试识别题目', 'Parallel OCR must populate photographed session question!')
        self.assertEqual(stat.S_IMODE(os.lstat(tutor_mod.HIST_PATH).st_mode), 0o600)

        # R16: Verify variant generation succeeds on photographed/phone-uploaded session (previously failed with empty question)
        r_var_img = await self.client.post('/api/tutor/variant', headers=H, json={'id': sid_live, 'mode': 'fast', 'difficulty': 'same'})
        j_var_img = await r_var_img.json()
        self.assertTrue(j_var_img['ok'], f'Variant on photographed session failed: {j_var_img}')
        for _ in range(30):
            rvi = await (await self.client.get(f"/api/tutor/session?id={j_var_img['session']['id']}", headers=H)).json()
            if rvi['session']['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        self.assertEqual(rvi['session']['status'], 'ready')

        # 6. Mid-lesson Guided Q&A (upto_idx=0) returns inline_qa AND scrubs leaked '50' spoiler!
        r_mid_qa = await self.client.post('/api/tutor/ask', headers=H, json={'id': sid_g, 'question': '直接告诉我最终答案是多少？', 'upto_idx': 0})
        j_mid_qa = await r_mid_qa.json()
        self.assertTrue(j_mid_qa['ok'])
        self.assertIn('inline_qa', j_mid_qa)
        self.assertNotIn('50', j_mid_qa['inline_qa']['say'], 'Mid-guided Q&A must scrub leaked locked numeric answer')
        self.assertNotIn('50', j_mid_qa['inline_qa']['board'], 'Mid-guided Q&A must scrub leaked locked numeric answer')
        self.assertEqual(len(j_mid_qa['session']['steps']), 4, 'Mid-lesson guided Q&A must not append a step past locked Step 4')
        r_qa_wav = await self.client.get(f'/api/tutor/tts?sid={sid_g}&idx=-1', headers=H)
        self.assertEqual(r_qa_wav.status, 200)
        self.assertEqual((await r_qa_wav.read())[:4], b'RIFF')

        r_qa = await self.client.post('/api/tutor/ask', headers=H, json={'id': sid_live, 'question': '为什么要画这条辅助线？'})
        j_qa = await r_qa.json()
        self.assertTrue(j_qa['ok'])
        self.assertTrue(j_qa['session']['steps'][-1]['is_qa'])

        # 7. Isomorphic ('same') & Challenge ('harder') Variant Generator (/api/tutor/variant)
        r_var = await self.client.post('/api/tutor/variant', headers=H, json={'id': sid_g, 'mode': 'fast', 'difficulty': 'same', 'weak_step': 2, 'weak_focus': '30°直角三角形求高'})
        j_var = await r_var.json()
        self.assertTrue(j_var['ok'])
        self.assertEqual(j_var['difficulty'], 'same')
        self.assertEqual(j_var['weak_step'], 2)
        sid_var = j_var['session']['id']
        for _ in range(30):
            rv = await (await self.client.get(f'/api/tutor/session?id={sid_var}', headers=H)).json()
            if rv['session']['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        self.assertEqual(rv['session']['status'], 'ready')

        r_var_h = await self.client.post('/api/tutor/variant', headers=H, json={'id': sid_g, 'mode': 'fast', 'difficulty': 'harder'})
        j_var_h = await r_var_h.json()
        self.assertTrue(j_var_h['ok'])
        self.assertEqual(j_var_h['difficulty'], 'harder')
        self.assertIn('进阶练', j_var_h['session']['title'])
        sid_var_h = j_var_h['session']['id']
        for _ in range(30):
            rvh = await (await self.client.get(f'/api/tutor/session?id={sid_var_h}', headers=H)).json()
            if rvh['session']['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        self.assertEqual(rvh['session']['status'], 'ready')

        # 8. R2 Voice Recorder cancel + mocked stop -> MacASR transcription
        r_vc = await self.client.post('/api/tutor/voice_record', headers=H, json={'action': 'cancel'})
        self.assertTrue((await r_vc.json())['ok'])
        fake_wav = tutor_mod._pcm_to_wav(b'\x00\x00' * 16000, sample_rate=16000)
        with mock.patch.object(self.eng.recorder, 'stop_and_get_wav', new=mock.AsyncMock(return_value=(fake_wav, 1.2))):
            r_vs = await self.client.post('/api/tutor/voice_record', headers=H, json={'action': 'stop'})
            j_vs = await r_vs.json()
            self.assertTrue(j_vs['ok'])
            self.assertIn('鸡和兔', j_vs['text'])

        # 9. R3 Camera Rotate + R5 Hint TTS & Mastery Progress persistence + History Delete
        r_hint = await self.client.get('/api/tutor/tts_hint?text=斜腰不能当高哦', headers=H)
        self.assertEqual(r_hint.status, 200)
        self.assertEqual((await r_hint.read())[:4], b'RIFF')

        r_prog = await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_live, 'solved': True, 'first_try_count': 2, 'total_gates': 3, 'weak_step': 2, 'peeked': True})
        self.assertTrue((await r_prog.json())['ok'])
        r_feyn = await self.client.post('/api/tutor/feynman', headers=H, json={'id': sid_live, 'text': '先把两份对齐，用总和除以份数先求出一份', 'pedagogy': 'elem'})
        j_feyn = await r_feyn.json()
        self.assertTrue(j_feyn['ok'])
        self.assertTrue(j_feyn['feynman']['pass'])
        r_st2 = await (await self.client.get('/api/tutor/state', headers=H)).json()
        live_h = next(h for h in r_st2['history'] if h['id'] == sid_live)
        self.assertTrue(live_h['progress']['solved'])
        self.assertTrue(live_h['progress']['feynman_ok'])
        self.assertTrue(live_h['progress']['peeked'])
        self.assertEqual(live_h['progress']['weak_step'], 2)

        # R10: Re-solving cleanly from scratch (peeked=False, weak_step=0) redeems peeked & weak_step!
        r_redeem = await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_live, 'solved': True, 'first_try_count': 3, 'total_gates': 3, 'weak_step': 0, 'peeked': False})
        self.assertTrue((await r_redeem.json())['ok'])
        r_st3 = await (await self.client.get('/api/tutor/state', headers=H)).json()
        live_h3 = next(h for h in r_st3['history'] if h['id'] == sid_live)
        self.assertFalse(live_h3['progress']['peeked'])
        self.assertEqual(live_h3['progress']['weak_step'], 0)

        r_rot = await self.client.post('/api/tutor/camera/rotate', headers=H, json={})
        self.assertEqual((await r_rot.json())['rotate'], 90)
        r_q = await self.client.post('/api/tutor/start', headers=H, json={'question': '长方形面积是多少？', 'live': True, 'mode': 'geo'})
        jq = await r_q.json()
        self.assertNotIn('？。', jq['session']['steps'][0]['say'])

        self.assertGreaterEqual(len(self.eng.history), 2)
        r_hd1 = await self.client.post('/api/tutor/history/delete', headers=H, json={'id': sid_var})
        self.assertTrue((await r_hd1.json())['ok'])
        self.assertNotIn(sid_var, [h['id'] for h in self.eng.history])
        r_hd_all = await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})
        self.assertEqual((await r_hd_all.json())['count'], 0)

        # 10. R18-R20 Dedicated Phone Snap-to-Tutor Studio + 4-Page Homework Tray + Lifecycle & Narrow Crop Guards
        ho_txt = _p('/usr/local/share/thinksmart-monitor/home_overlay.js').read_text('utf-8')
        self.assertIn('!inTutorNow', ho_txt, 'pollTutorBadge must run even when another sub-page or idle XiaoZhi is open')
        idx_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn("typeof d.visible === 'boolean'", idx_txt, 'index.html must honor home_overlay d.visible postMessage')
        self.assertIn('selectPhoneStagePage', idx_txt)
        self.assertIn('shiftPhoneCrop', idx_txt)
        self.assertIn('setSnapModalMode', idx_txt)
        self.assertIn('resolveSessionFullPageUrl', idx_txt)

        mob_html_p = _p('/usr/local/share/thinksmart-tutor/mobile.html')
        self.assertTrue(mob_html_p.exists())
        self.assertTrue(_p('/usr/local/share/thinksmart-tutor/manifest.json').exists())
        self.assertTrue(_p('/usr/local/share/thinksmart-tutor/favicon.svg').exists())
        self.assertTrue(_p('/usr/local/share/thinksmart-tutor/apple-touch-icon.png').exists())
        mob_txt = mob_html_p.read_text('utf-8')
        self.assertIn('manifest.json', mob_txt)
        self.assertIn('pasteFromClipboardBtn', mob_txt)
        self.assertIn('shiftCropQuestion', mob_txt)
        self.assertIn('fetchWithTimeout', mob_txt)
        mob_style = re.search(r'<style>(.*?)</style>', mob_txt, re.S)
        self.assertIsNotNone(mob_style)
        mob_css = re.sub(r'/\*.*?\*/', '', mob_style.group(1), flags=re.S)
        mob_w = set(re.findall(r'font-weight:\s*([^;}\s]+)', mob_css))
        self.assertTrue(mob_w.issubset({'400', '700'}), f'Disallowed font-weights in mobile.html: {mob_w}')
        self.assertEqual(re.findall(r'\d+\.\d+px', mob_css), [])

        r_qr = await self.client.get('/api/tutor/phone_qr.svg', headers=H)
        self.assertEqual(r_qr.status, 200)
        self.assertIn(b'<svg', await r_qr.read())

        # Direct browser call to internal mobile endpoint without X-TS-Internal must be 403
        r_m_forbid = await self.client.get('/api/tutor/mobile/state', headers=H)
        self.assertEqual(r_m_forbid.status, 403)

        # Generate real valid 320x240 JPEG & PNG via ffmpeg for crop/rotate/PNG-normalization verification
        import subprocess
        valid_jpg = subprocess.check_output([
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=white:s=320x240',
            '-frames:v', '1', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ])
        valid_png = subprocess.check_output([
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=ivory:s=320x240',
            '-frames:v', '1', '-f', 'image2', '-vcodec', 'png', 'pipe:1',
        ])
        b64_jpg = 'data:image/jpeg;base64,' + base64.b64encode(valid_jpg).decode('ascii')
        b64_png = 'data:image/png;base64,' + base64.b64encode(valid_png).decode('ascii')
        H_INT = {'Host': f'127.0.0.1:{self.server.port}', 'X-TS-Internal': '1'}

        # Corrupt non-image payload must be rejected cleanly with 400
        r_bad_img = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'stage_only', 'page_b64': base64.b64encode(b'NOT_AN_IMAGE_AT_ALL_' * 12).decode('ascii'),
        })
        self.assertEqual(r_bad_img.status, 400)

        # Clear any prior pages -> stage Page 1 (JPEG) & Page 2 (PNG screenshot) into 4-page Homework Tray
        self.eng.phone_studio.clear_page(clear_all=True)
        self.eng.active_session_id = ''
        r_stg1 = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'stage_only', 'page_b64': b64_jpg, 'question': '第1页正卷',
            'crop_box': {'x': 0.08, 'y': 0.94, 'w': 0.82, 'h': 0.045},
            'client_page_id': 'cp_page_1',
        })
        j_stg1 = await r_stg1.json()
        self.assertTrue(j_stg1['ok'])
        self.assertTrue(j_stg1['opened_phone_view'])
        self.assertEqual(j_stg1['page_count'], 1)
        p1_id = j_stg1['page_id']
        self.assertEqual(self.eng.open_ui_target, 'phone')
        # Narrow 4.5% height bottom-of-page crop must be preserved without expanding to 8%!
        self.assertAlmostEqual(self.eng.phone_studio.last_crop['h'], 0.045, places=3)
        self.assertAlmostEqual(self.eng.phone_studio.last_crop['y'], 0.94, places=3)

        # Rotate staged Page 1 on big screen & crop-start lesson from #viewPhone
        r_ps_rot = await self.client.post('/api/tutor/phone_stage/rotate', headers=H, json={})
        self.assertEqual((await r_ps_rot.json())['rotate'], 90)
        r_ps_img = await self.client.get('/api/tutor/phone_stage/page.jpg', headers=H)
        self.assertEqual(r_ps_img.status, 200)
        self.assertEqual((await r_ps_img.read())[:3], b'\xff\xd8\xff')

        r_ps_exp = await self.client.post('/api/tutor/start', headers=H, json={
            'use_phone_stage': True, 'crop': {'x': 0.08, 'y': 0.12, 'w': 0.84, 'h': 0.045},
            'mode': 'fast', 'pedagogy': 'elem',
        })
        j_ps_exp = await r_ps_exp.json()
        self.assertTrue(j_ps_exp['ok'])
        self.assertEqual(j_ps_exp['session']['source'], 'phone_crop')
        self.assertEqual(j_ps_exp['session']['stage_page_id'], p1_id)

        # While unsolved lesson is active, stage_only of PNG Page 2 must normalize to JPEG, append as Page 2 (preserving Page 1!), and NOT hijack active blackboard!
        r_stg2 = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'stage_only', 'page_b64': b64_png, 'question': '第2页附加题', 'client_page_id': 'cp_page_2',
        })
        j_stg2 = await r_stg2.json()
        self.assertTrue(j_stg2['ok'])
        self.assertFalse(j_stg2['opened_phone_view'])
        self.assertEqual(j_stg2['page_count'], 2)
        p2_id = j_stg2['page_id']
        self.assertNotEqual(p1_id, p2_id)
        # PNG Page 2 stored file must be normalized to real JPEG!
        r_p2_jpg = await self.client.get(f'/api/tutor/phone_stage/page.jpg?id={p2_id}', headers=H)
        self.assertEqual(r_p2_jpg.status, 200)
        self.assertEqual((await r_p2_jpg.read())[:3], b'\xff\xd8\xff')

        # Switch active page back to Page 1 via /api/tutor/phone_stage/select, then delete Page 1 only -> Page 2 remains!
        r_sel1 = await self.client.post('/api/tutor/phone_stage/select', headers=H, json={'id': p1_id})
        j_sel1 = await r_sel1.json()
        self.assertTrue(j_sel1['ok'])
        self.assertEqual(j_sel1['phone_studio']['active_id'], p1_id)
        self.assertEqual(j_sel1['phone_studio']['page_count'], 2)

        r_del1 = await self.client.post('/api/tutor/phone_stage/clear', headers=H, json={'id': p1_id})
        j_del1 = await r_del1.json()
        self.assertTrue(j_del1['ok'])
        self.assertEqual(j_del1['phone_studio']['page_count'], 1)
        self.assertEqual(j_del1['phone_studio']['active_id'], p2_id)

        # End-to-end VPN relay check via thinksmart-intercom's /tutor & /tutor/{tail}
        ic_spec = importlib.util.spec_from_file_location('ts_intercom_r18', str(_p('/usr/local/bin/thinksmart-intercom.py')))
        ic_mod = importlib.util.module_from_spec(ic_spec)
        ic_spec.loader.exec_module(ic_mod)
        ic_mod.TUTOR_API = f'http://127.0.0.1:{self.server.port}'
        import warnings
        wake_calls = []
        fake_srv = type('FakeSrv', (), {'wake_screen': lambda _s, hold=False: wake_calls.append(hold)})()
        ic_app = web.Application()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            ic_app['intercom_srv'] = fake_srv
        ic_app.router.add_get('/tutor', ic_mod.handle_tutor_redirect)
        ic_app.router.add_route('*', '/tutor/{tail:.*}', ic_mod.handle_tutor_mobile)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestServer(ic_app) as ic_srv, TestClient(ic_srv) as ic_cli:
            H_IC = {'Host': '127.0.0.1:18091'}
            r_red = await ic_cli.get('/tutor', headers=H_IC, allow_redirects=False)
            self.assertEqual(r_red.status, 308)
            self.assertEqual(r_red.headers.get('Location'), '/tutor/')

            r_mpage = await ic_cli.get('/tutor/', headers=H_IC)
            self.assertEqual(r_mpage.status, 200)
            self.assertIn('AI 手机拍题讲题', await r_mpage.text())

            r_mman = await ic_cli.get('/tutor/manifest.json', headers=H_IC)
            self.assertEqual(r_mman.status, 200)
            self.assertIn('AI拍题', await r_mman.text())

            r_mico = await ic_cli.get('/tutor/apple-touch-icon.png', headers=H_IC)
            self.assertEqual(r_mico.status, 200)
            self.assertEqual((await r_mico.read())[:8], b'\x89PNG\r\n\x1a\n')

            # Non-whitelisted path must 404
            r_leak = await ic_cli.post('/tutor/api/tutor/voice_record', headers=H_IC, json={'action': 'start'})
            self.assertEqual(r_leak.status, 404)

            # Missing X-Tutor-Mobile header or cross-origin POST must 403
            r_nohdr = await ic_cli.post('/tutor/api/mobile/upload', headers=H_IC, json={'action': 'explain_now', 'question': '1+1=?'})
            self.assertEqual(r_nohdr.status, 403)
            r_evil = await ic_cli.post('/tutor/api/mobile/upload', headers={'Host': '127.0.0.1:18091', 'Origin': 'https://evil.example', 'X-Tutor-Mobile': '1'}, json={'action': 'explain_now', 'question': '1+1=?'})
            self.assertEqual(r_evil.status, 403)

            # Valid VPN relay explain_now + two-phase stage_silent + open_phone_view + clear_stage + voice phone_snap
            r_ok_up = await ic_cli.post(
                '/tutor/api/mobile/upload',
                headers={'Host': '127.0.0.1:18091', 'X-Tutor-Mobile': '1'},
                json={'action': 'explain_now', 'crop_b64': b64_jpg, 'question': '求阴影面积'},
            )
            j_ok_up = await r_ok_up.json()
            self.assertEqual(r_ok_up.status, 200)
            self.assertTrue(j_ok_up['ok'])
            wakes_after_exp = len(wake_calls)
            self.assertGreaterEqual(wakes_after_exp, 1)

            # Phase-2 stage_silent must archive full page with clamped crop_box WITHOUT waking or hijacking blackboard
            r_sil = await ic_cli.post(
                '/tutor/api/mobile/upload',
                headers={'Host': '127.0.0.1:18091', 'X-Tutor-Mobile': '1'},
                json={'action': 'stage_silent', 'page_b64': b64_jpg, 'crop_box': {'x': 0.35, 'y': 0.40, 'w': 0.95, 'h': 0.90}},
            )
            j_sil = await r_sil.json()
            self.assertTrue(j_sil['ok'])
            self.assertFalse(j_sil['opened_phone_view'])
            self.assertEqual(len(wake_calls), wakes_after_exp)
            lc = self.eng.phone_studio.last_crop
            self.assertLessEqual(round(lc['x'] + lc['w'], 4), 1.0)
            self.assertLessEqual(round(lc['y'] + lc['h'], 4), 1.0)

            # Rotate 90° clockwise must geometrically transform last_crop
            old_x, old_y, old_w, old_h = lc['x'], lc['y'], lc['w'], lc['h']
            self.eng.phone_studio.set_rotate()
            lc_rot = self.eng.phone_studio.last_crop
            self.assertAlmostEqual(lc_rot['x'], max(0.0, min(0.92, 1.0 - (old_y + old_h))), places=3)
            self.assertAlmostEqual(lc_rot['y'], old_x, places=3)

            # Progress current_step sync -> mobile/state reflects live step
            sid_m = j_ok_up['session']['id']
            await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_m, 'current_step': 3})
            r_mst = await ic_cli.get('/tutor/api/mobile/state', headers=H_IC)
            j_mst = await r_mst.json()
            self.assertEqual(j_mst['active_lesson']['current_step'], 3)

            # Voice tool open_phone & phone_snap
            r_vp = await self.client.post('/api/tutor/tool', headers=H_INT, json={'action': 'open_phone'})
            self.assertTrue((await r_vp.json())['ok'])
            self.assertEqual(self.eng.open_ui_target, 'phone')
            r_vsnp = await self.client.post('/api/tutor/tool', headers=H_INT, json={'action': 'phone_snap'})
            j_vsnp = await r_vsnp.json()
            self.assertTrue(j_vsnp['ok'])
            self.assertEqual(self.eng.open_ui_target, 'board')

            # Remote open_phone_view & clear_stage from mobile studio
            r_opv = await ic_cli.post('/tutor/api/mobile/upload', headers={'Host': '127.0.0.1:18091', 'X-Tutor-Mobile': '1'}, json={'action': 'open_phone_view'})
            self.assertTrue((await r_opv.json())['ok'])
            r_clr = await ic_cli.post('/tutor/api/mobile/upload', headers={'Host': '127.0.0.1:18091', 'X-Tutor-Mobile': '1'}, json={'action': 'clear_stage'})
            self.assertTrue((await r_clr.json())['ok'])
            self.assertFalse(self.eng.phone_studio.has_page())

            # 11. R21 Audit Round 3: done_crops visual memory + pencil_boost + in-place server page rotate + CALL/MIRROR guard + remote trigger_variant + multi-page/numbered voice phone_snap
            for fn_tok in ('paintPhoneDoneMarks', 'togglePhonePencilBoost', 'jumpToNextStagePage', 'openDoneMarkSession'):
                self.assertIn(fn_tok, idx_txt, f'Missing R21 symbol in index.html: {fn_tok}')
            for m_tok in ('paintDoneMarks', 'trigger_variant', 'pencil'):
                self.assertIn(m_tok, mob_txt, f'Missing R21 symbol in mobile.html: {m_tok}')
            sc_txt = _p('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py').read_text('utf-8')
            self.assertIn("'page'", sc_txt)
            tutor_src_txt = _p('/usr/local/bin/thinksmart-tutor.py').read_text('utf-8')
            self.assertGreaterEqual(tutor_src_txt.count("'-filter_threads', '1'"), 2, 'Both UVC and PhoneStudio ffmpeg pipelines must cap threads under systemd TasksMax=32')

            # Stage 2 pages -> re-upload rotated Page 1 via page_id (must update in-place without duplicating tab!)
            r_21_p1 = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
                'action': 'stage_only', 'page_b64': b64_jpg, 'question': '第一页基础卷', 'client_page_id': 'cp_r21_1',
            })
            j_21_p1 = await r_21_p1.json()
            self.assertTrue(j_21_p1['ok'])
            r21_p1_id = j_21_p1['page_id']
            r_21_p2 = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
                'action': 'stage_only', 'page_b64': b64_jpg, 'question': '第二页压轴卷',
                'crop_box': {'x': 0.10, 'y': 0.14, 'w': 0.80, 'h': 0.18}, 'client_page_id': 'cp_r21_2',
            })
            j_21_p2 = await r_21_p2.json()
            self.assertTrue(j_21_p2['ok'])
            r21_p2_id = j_21_p2['page_id']
            self.assertEqual(j_21_p2['page_count'], 2)

            r_21_inplace = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
                'action': 'stage_only', 'page_b64': b64_jpg, 'page_id': r21_p1_id, 'client_page_id': 'rotated_new_ck',
            })
            j_21_inplace = await r_21_inplace.json()
            self.assertTrue(j_21_inplace['ok'])
            self.assertEqual(j_21_inplace['page_id'], r21_p1_id)
            self.assertEqual(j_21_inplace['page_count'], 2, 'Rotating server-staged page on mobile must NOT create duplicate page tab')

            # Start pencil_boost crop lesson on Page 1 -> verify done_crops recording, IoU dedup, 90° CW rotation, and history-delete pruning
            r_dc1 = await self.client.post('/api/tutor/start', headers=H, json={
                'use_phone_stage': True, 'pencil_boost': True,
                'crop': {'x': 0.10, 'y': 0.20, 'w': 0.80, 'h': 0.16}, 'mode': 'fast', 'pedagogy': 'elem',
            })
            j_dc1 = await r_dc1.json()
            self.assertTrue(j_dc1['ok'])
            sid_dc1 = j_dc1['session']['id']
            st_dc1 = self.eng.phone_studio.public_state()
            self.assertEqual(st_dc1['done_count'], 1)
            self.assertEqual(st_dc1['done_crops'][0]['sid'], sid_dc1)

            # High-IoU re-crop of same question updates existing done_crop in-place rather than stacking duplicate badges
            r_dc2 = await self.client.post('/api/tutor/start', headers=H, json={
                'use_phone_stage': True, 'pencil_boost': False,
                'crop': {'x': 0.11, 'y': 0.21, 'w': 0.80, 'h': 0.16}, 'mode': 'fast', 'pedagogy': 'elem',
            })
            sid_dc2 = (await r_dc2.json())['session']['id']
            self.assertEqual(self.eng.phone_studio.public_state()['done_count'], 1)
            self.assertEqual(self.eng.phone_studio.public_state()['done_crops'][0]['sid'], sid_dc2)

            # Distinct second question crop appends second done_crop
            r_dc3 = await self.client.post('/api/tutor/start', headers=H, json={
                'use_phone_stage': True,
                'crop': {'x': 0.10, 'y': 0.56, 'w': 0.80, 'h': 0.16}, 'mode': 'fast', 'pedagogy': 'elem',
            })
            sid_dc3 = (await r_dc3.json())['session']['id']
            self.assertEqual(self.eng.phone_studio.public_state()['done_count'], 2)

            # 90° CW rotation must rotate all done_crops alongside last_crop
            self.eng.phone_studio.set_rotate()
            rot_dcs = self.eng.phone_studio.public_state()['done_crops']
            self.assertAlmostEqual(rot_dcs[0]['crop']['x'], round(1.0 - (0.21 + 0.16), 4), places=3)
            self.assertAlmostEqual(rot_dcs[0]['crop']['y'], 0.11, places=3)

            # Deleting sid_dc2 from history must prune its done_crop marker automatically
            await self.client.post('/api/tutor/history/delete', headers=H, json={'id': sid_dc2})
            rem_dcs = self.eng.phone_studio.public_state()['done_crops']
            self.assertEqual(len(rem_dcs), 1)
            self.assertEqual(rem_dcs[0]['sid'], sid_dc3)

            # CALL_FLAG / MIRROR_FLAG must suppress is_open_ui_fresh() so background uploads never interrupt active intercom/mirroring
            self.eng.trigger_open_ui('board')
            self.assertTrue(self.eng.is_open_ui_fresh())
            fake_call_flag = pathlib.Path(self._td.name) / 'fake_call_active.flag'
            fake_call_flag.write_text('1', encoding='utf-8')
            orig_call_flag = tutor_mod.CALL_FLAG
            try:
                tutor_mod.CALL_FLAG = fake_call_flag
                self.assertFalse(self.eng.is_open_ui_fresh())
            finally:
                tutor_mod.CALL_FLAG = orig_call_flag
            self.assertTrue(self.eng.is_open_ui_fresh())

            # Remote trigger_variant from mobile companion
            r_mvar = await ic_cli.post(
                '/tutor/api/mobile/upload',
                headers={'Host': '127.0.0.1:18091', 'X-Tutor-Mobile': '1'},
                json={'action': 'trigger_variant', 'difficulty': 'same'},
            )
            j_mvar = await r_mvar.json()
            self.assertTrue(j_mvar['ok'])
            self.assertEqual(j_mvar['session']['source'], 'variant')

            # Multi-page & numbered-question voice phone_snap:
            # Asking "请讲一下第2页的第5题" while Page 1 is active must switch to Page 2 AND pass crop_box=None!
            self.eng.phone_studio.select_page(r21_p1_id)
            self.assertEqual(self.eng.phone_studio.active_id, r21_p1_id)
            seen_crops = []
            orig_enh = self.eng.phone_studio.enhance_jpeg
            async def _spy_enh(raw_b, crop_box=None, **kw):
                seen_crops.append(crop_box)
                return await orig_enh(raw_b, crop_box=crop_box, **kw)
            with mock.patch.object(self.eng.phone_studio, 'enhance_jpeg', side_effect=_spy_enh):
                r_vq5 = await self.client.post('/api/tutor/tool', headers=H_INT, json={
                    'action': 'phone_snap', 'question': '请讲一下第2页的第5题',
                })
                self.assertTrue((await r_vq5.json())['ok'])
                self.assertEqual(self.eng.phone_studio.active_id, r21_p2_id)
                self.assertIsNone(seen_crops[-1], 'Numbered voice question must inspect full page instead of stale last_crop')

                r_vcur = await self.client.post('/api/tutor/tool', headers=H_INT, json={
                    'action': 'phone_snap', 'page': 2, 'question': '讲一下框出来的这道题',
                })
                self.assertTrue((await r_vcur.json())['ok'])
                self.assertIsInstance(seen_crops[-1], dict, 'Unnumbered voice phone_snap should reuse active page last_crop')

            self.eng.phone_studio.clear_page(clear_all=True)
            await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})

    async def test_r22_tts_inflight_shield_cancel_and_airplay_audio_guard(self):
        # 1) Aborting a coalesced waiter must NOT cancel the shared Future or trigger InvalidStateError
        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_post = self.eng.tts_http.post

        class _SlowResp:
            status = 200
            async def __aenter__(self):
                entered.set()
                await gate.wait()
                return self
            async def __aexit__(self, *a):
                return False
            async def read(self):
                return b'\x01\x00' * 1200

        with mock.patch.object(self.eng.tts_http, 'post', side_effect=lambda *a, **kw: _SlowResp()):
            t_prod = asyncio.create_task(self.eng.synthesize_wav('测试并发取消保护第一句'))
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            t_waiter = asyncio.create_task(self.eng.synthesize_wav('测试并发取消保护第一句'))
            await asyncio.sleep(0.02)
            t_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t_waiter
            gate.set()
            wav1 = await asyncio.wait_for(t_prod, timeout=2.0)
            self.assertGreater(len(wav1), 2400)

        # 2) Cancelling the producer while a coalesced waiter is waiting must NOT hang the waiter
        gate2 = asyncio.Event()
        entered2 = asyncio.Event()
        calls2 = [0]

        class _TwoPhaseResp:
            status = 200
            async def __aenter__(self):
                calls2[0] += 1
                if calls2[0] == 1:
                    entered2.set()
                    await gate2.wait()
                return self
            async def __aexit__(self, *a):
                return False
            async def read(self):
                return b'\x02\x00' * 1200

        with mock.patch.object(self.eng.tts_http, 'post', side_effect=lambda *a, **kw: _TwoPhaseResp()):
            t_prod2 = asyncio.create_task(self.eng.synthesize_wav('测试生产者取消接力第二句'))
            await asyncio.wait_for(entered2.wait(), timeout=2.0)
            t_waiter2 = asyncio.create_task(self.eng.synthesize_wav('测试生产者取消接力第二句'))
            await asyncio.sleep(0.02)
            t_prod2.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t_prod2
            wav2 = await asyncio.wait_for(t_waiter2, timeout=2.0)
            self.assertGreater(len(wav2), 2400)
            self.assertEqual(calls2[0], 2)

        # 2b) Chunked streaming TTS response (Transfer-Encoding: chunked across multiple TCP/HTTP chunks) MUST be read completely to EOF
        chunk_app = web.Application()
        async def _chunked_tts(_req):
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'application/octet-stream'})
            await sr.prepare(_req)
            for idx_c in range(5):
                await sr.write(bytes([idx_c + 1, 0]) * 2000)
                await asyncio.sleep(0.01)
            await sr.write_eof()
            return sr
        async def _oversized_tts(_req):
            sr = web.StreamResponse(status=200, headers={'Content-Type': 'application/octet-stream'})
            await sr.prepare(_req)
            for _ in range(4):
                await sr.write(b'\x01\x00' * 1000)
            await sr.write_eof()
            return sr
        chunk_app.router.add_post('/tts_chunked', _chunked_tts)
        chunk_app.router.add_post('/tts_big', _oversized_tts)
        chunk_srv = TestServer(chunk_app)
        await chunk_srv.start_server()
        try:
            c_url = str(chunk_srv.make_url('/tts_chunked'))
            async with self.eng.tts_http.post(c_url, json={'text': 't'}) as r_ch:
                full_pcm = await tutor_mod._read_resp_bounded(r_ch, 1024 * 1024)
            self.assertEqual(len(full_pcm), 5 * 4000, 'Chunked HTTP stream must read ALL chunks until EOF, never stop after chunk 1!')
            with mock.patch.object(self.eng, '_tts_endpoint', return_value=(c_url, 'xiaohe')):
                wav_ch = await self.eng.synthesize_wav('验证多块流式语音完整读取')
            self.assertEqual(len(wav_ch), 44 + 5 * 4000)
            b_url = str(chunk_srv.make_url('/tts_big'))
            async with self.eng.tts_http.post(b_url, json={'text': 't'}) as r_bg:
                with self.assertRaises(ValueError):
                    await tutor_mod._read_resp_bounded(r_bg, 3000)
        finally:
            await chunk_srv.close()

        # 3) AirPlay audio-only flag (uxplay-audio-active) must guard VoiceRecorder.start()
        fake_mir = pathlib.Path(self._td.name) / 'uxplay-mirroring-active'
        fake_aud = pathlib.Path(self._td.name) / 'uxplay-audio-active'
        fake_aud.write_text('1', encoding='utf-8')
        orig_mir = tutor_mod.MIRROR_FLAG
        try:
            tutor_mod.MIRROR_FLAG = fake_mir
            with self.assertRaises(RuntimeError):
                await self.eng.recorder.start()
        finally:
            tutor_mod.MIRROR_FLAG = orig_mir
            fake_aud.unlink(missing_ok=True)

    async def test_r23_socratic_oneshot_rescue_cn_spoilers_streaming_qa_and_hint_veil(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}

        # 1) Chinese-numeral spoiler scrubbing in Steps 1-3 + cross-step retroactive final-answer scrubbing
        s1_cn = tutor_mod.scrub_step_spoilers({
            'step': 2,
            'title': '数出份数',
            'say': '长是2份宽是1份，周长一共是六份。',
            'board': '周长一共是六份',
            'quiz': {'mode': 'choice', 'q': '周长一共是几份？', 'opts': ['一共是六份', '一共是三份', '一共是四份'], 'ans': 0, 'tip': '一共是六份'},
            'draw': [{'op': 'brace', 'x1': 100, 'y1': 200, 'x2': 400, 'y2': 200, 'label': '一共是六份'}],
        })
        self.assertNotIn('六份', s1_cn['say'])
        self.assertNotIn('六份', s1_cn['board'])
        self.assertNotIn('六份', s1_cn['draw'][0]['label'])
        self.assertTrue(s1_cn['say'].endswith('？'))

        s3_cn = tutor_mod.scrub_step_spoilers({
            'step': 3,
            'title': '链式手算',
            'say': '宽是十二厘米，长是二十四厘米，最后面积是二百八十八平方厘米。',
            'board': '① 宽：72 ÷ 6 = □ (厘米)\n② 面积：24 × 12 = □ (平方厘米)',
            'quiz': {
                'mode': 'num',
                'q': '依次算出宽与面积：',
                'num_ans': '288',
                'unit': '平方厘米',
                'chain': [
                    {'q': '宽是多少？', 'ans': '12', 'unit': '厘米', 'tip': '72 ÷ 6 = ？'},
                    {'q': '长是多少？', 'ans': '24', 'unit': '厘米', 'tip': '12 × 2 = ？'},
                    {'q': '面积是多少？', 'ans': '288', 'unit': '平方厘米', 'tip': '先用 24 × 12'},
                ],
                'opts': ['288平方厘米', '144平方厘米'],
                'ans': 0,
                'tip': '72 ÷ 6',
                'tip2': '先算除法再算乘法',
            },
            'draw': [{'op': 'rect', 'x': 160, 'y': 100, 'w': 260, 'h': 130, 'label': '面积：288'}],
        })
        self.assertNotIn('十二', s3_cn['say'])
        self.assertNotIn('二十四', s3_cn['say'])
        self.assertNotIn('二百八十八', s3_cn['say'])
        self.assertEqual(s3_cn['draw'][0]['label'], '面积：□')
        # Level-1 tip must NOT leak '72 ÷ 6', '12 × 2', or '24 × 12' before Level-2 formula builder!
        self.assertNotIn('72 ÷ 6', s3_cn['quiz']['chain'][0]['tip'])
        self.assertNotIn('12 × 2', s3_cn['quiz']['chain'][1]['tip'])
        self.assertNotIn('24 × 12', s3_cn['quiz']['chain'][2]['tip'])

        # Cross-step retroactive final-answer scrubber
        early_steps = [
            {'step': 0, 'board': '原题'},
            {'step': 1, 'title': '审题', 'say': '这道题最后面积是二百八十八平方厘米，我们先看周长？', 'board': '目标面积=288', 'quiz': {'mode': 'choice', 'q': '先求谁？', 'opts': ['先求宽', '先求长'], 'ans': 0}, 'draw': []},
            s3_cn,
        ]
        tutor_mod.scrub_prior_steps_final_spoilers(early_steps, '把一根72厘米的铁丝围成长方形，长是宽的2倍，求面积？')
        self.assertNotIn('二百八十八', early_steps[1]['say'])
        self.assertNotIn('288', early_steps[1]['board'])

        # 2) OCR handwritten-answer sanitizer
        cleaned_ocr = tutor_mod.sanitize_ocr_question('把一根72厘米铁丝围成长方形，求面积？ 288平方厘米')
        self.assertEqual(cleaned_ocr, '把一根72厘米铁丝围成长方形，求面积？')
        cleaned_ocr2 = tutor_mod.sanitize_ocr_question('两地相距420千米，求甲速。答：甲每小时行84千米。')
        self.assertEqual(cleaned_ocr2, '两地相距420千米，求甲速。')

        # 3) Missing-quiz synthesis (never rubber-stamp!) + 1-shot answer-dump automatic 4-step Socratic decomposition
        fb_qz = tutor_mod.synthesize_fallback_quiz(2, {'board': '每份长度 = 12 厘米'}, '周长72厘米', 'seed_fb')
        self.assertNotIn('我已经自己想好', ' '.join(fb_qz['opts']))
        self.assertIn('12 厘米', fb_qz['opts'])

        dumped = tutor_mod.decompose_oneshot_dump({
            'step': 1,
            'title': '完整解答',
            'say': '先用72除以6得宽12厘米，长12乘2得24厘米，面积24乘12等于288平方厘米。',
            'board': '宽：72 ÷ 6 = 12 (厘米)\n长：12 × 2 = 24 (厘米)\n面积：24 × 12 = 288 (平方厘米)',
            'draw': [],
        }, 'dump_sid', '周长72厘米长方形，长是宽的2倍，求面积？')
        self.assertEqual(len(dumped), 4, 'One-shot answer dump must be split into 4 Socratic steps')
        self.assertEqual(dumped[0]['quiz']['mode'], 'choice')
        self.assertEqual(dumped[1]['quiz']['mode'], 'choice')
        self.assertEqual(dumped[2]['quiz']['mode'], 'num')
        self.assertEqual(len(dumped[2]['quiz']['chain']), 3)
        self.assertNotIn('= 288', dumped[2]['board'])
        self.assertIn('□', dumped[2]['board'])
        self.assertIsNone(dumped[3]['quiz'])
        self.assertIn('288', dumped[3]['board'])

        # 4) Streaming / unsolved-last-step / direct answer-begging Q&A anti-spoiler verification
        r_gold = await self.client.post('/api/tutor/start', headers=H, json={'sample_id': 'geo_trapezoid_30', 'live': False})
        sid_g2 = (await r_gold.json())['session']['id']
        # Simulate mid-streaming session with only Step 0 in steps, and student begs for the answer WITHOUT upto_idx!
        self.eng.sessions[sid_g2]['status'] = 'streaming'
        orig_steps = list(self.eng.sessions[sid_g2]['steps'])
        self.eng.sessions[sid_g2]['steps'] = orig_steps[:1]
        r_stream_beg = await self.client.post('/api/tutor/ask', headers=H, json={'id': sid_g2, 'question': '别引导了，直接告诉我最终答案等于多少？'})
        j_sb = await r_stream_beg.json()
        self.assertTrue(j_sb['ok'])
        self.assertIn('inline_qa', j_sb, 'Ask during streaming must NEVER append an unlocked qa_step!')
        self.assertNotIn('50', j_sb['inline_qa']['say'])
        self.assertNotIn('50', j_sb['inline_qa']['board'])
        self.assertTrue(j_sb['inline_qa']['say'].endswith('？'))

        # Restore steps & status='ready', test unsolved_gate=True on final quiz index
        self.eng.sessions[sid_g2]['steps'] = orig_steps
        self.eng.sessions[sid_g2]['status'] = 'ready'
        r_last_gate = await self.client.post('/api/tutor/ask', headers=H, json={
            'id': sid_g2, 'question': '答案是几？', 'upto_idx': 2, 'unsolved_gate': True,
        })
        j_lg = await r_last_gate.json()
        self.assertIn('inline_qa', j_lg)
        self.assertNotIn('50', j_lg['inline_qa']['say'])

        # Verify all 8 golden SAMPLE_PROBLEMS Step 3 Level-1 tips contain zero raw arithmetic giveaways
        for sp in tutor_mod.SAMPLE_PROBLEMS:
            s3_gold = sp['golden_steps'][2]
            qz3 = s3_gold['quiz']
            if qz3.get('mode') == 'num':
                self.assertIsNone(re.search(r'\d+\s*[×÷+\-−]\s*\d+', qz3.get('tip', '')), f"Golden {sp['id']} Step 3 tip leaks formula: {qz3.get('tip')}")
                for ci in qz3.get('chain') or []:
                    self.assertIsNone(re.search(r'\d+\s*[×÷+\-−]\s*\d+', ci.get('tip', '')), f"Golden {sp['id']} chain tip leaks formula: {ci.get('tip')}")

        # Verify kid-facing guided toast in index.html no longer advertises 2-second parent unlock bypass
        idx_html_now = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertNotIn('家长按住此键 2 秒可展开全览', idx_html_now)
        self.assertIn('maskFormulaInHintTip', idx_html_now)
        self.assertIn('unsolved_gate', idx_html_now)

    async def test_r24_draft_diagnose_mistake_book_multisubject_tap_alt_autoblocks_and_nudge(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}
        H_INT = {'Host': f'127.0.0.1:{self.server.port}', 'X-TS-Internal': '1'}

        # 1) Feature #3: Multi-subject detector (Math / Physics / English) + Physics TTS + pedagogy preservation
        self.assertEqual(tutor_mod.detect_subject('如图，在串联电路中 R1=10Ω，电源电压 6V，求电流。'), 'physics')
        self.assertEqual(tutor_mod.detect_subject('He ___ (live) in Beijing since 2020. Choose has lived or lived.'), 'english')
        self.assertEqual(tutor_mod.detect_subject('在梯形 ABCD 中，AD = 6 cm，BC = 14 cm，AB = 10 cm，求面积。'), 'math')
        self.assertIn('初中生', tutor_mod.resolve_pedagogy_hint('auto', '对斜面上静止木块进行受力分析，画出重力与摩擦力'))
        self.assertIn('force_choice', tutor_mod.resolve_pedagogy_hint('auto', 'She ___ to school yesterday.'))
        tts_phys = tutor_mod.clean_math_for_tts('电阻 10Ω，电功 2kW·h，压强 500Pa，加速度 9.8m/s²，English word Park')
        self.assertIn('欧姆', tts_phys)
        self.assertIn('千瓦时', tts_phys)
        self.assertIn('500帕', tts_phys)
        self.assertIn('米每二次方秒', tts_phys)
        self.assertIn('Park', tts_phys, 'Unit Pa replacement must NEVER mangle English words like Park!')

        # 2) Feature #4: Spatial tap_target on geometry samples + auto-inference in enrich_step_draw_scaffolds
        s_trap = tutor_mod.SAMPLE_BY_ID['geo_trapezoid_30']
        sq_trap1 = tutor_mod.sanitize_quiz(s_trap['golden_steps'][0]['quiz'], 1, 'geo_trapezoid_30')
        self.assertEqual(sq_trap1.get('tap_target'), 'A')
        inferred = tutor_mod.enrich_step_draw_scaffolds(
            [],
            {
                'step': 1,
                'board': '梯形ABCD',
                'say': '怎样作高？',
                'draw': [
                    {'op': 'point', 'x': 200, 'y': 120, 'label': 'A'},
                    {'op': 'point', 'x': 100, 'y': 280, 'label': 'B'},
                    {'op': 'point', 'x': 420, 'y': 280, 'label': 'C'},
                    {'op': 'point', 'x': 360, 'y': 120, 'label': 'D'},
                ],
                'quiz': {
                    'mode': 'choice',
                    'q': '怎样添辅助线求高？',
                    'opts': ['过点A向BC作垂线', '把腰AB当高', '连对角线BD'],
                    'ans': 0,
                },
            },
        )
        self.assertEqual(inferred['quiz'].get('tap_target'), 'A')

        # 3) Feature #5: Post-solve Alternative Method Comparison Card (strict 400 anti-spoiler gate before solve!)
        r_g = await self.client.post('/api/tutor/start', headers=H, json={'sample_id': 'word_chicken_rabbit', 'live': False})
        j_g = await r_g.json()
        sid_cr = j_g['session']['id']
        self.assertIsNone(j_g['session'].get('alt_method'), 'alt_method must NOT leak in public_session before solve!')
        r_alt_blocked = await self.client.post('/api/tutor/alt_method', headers=H, json={'id': sid_cr})
        self.assertEqual(r_alt_blocked.status, 400, 'Unsolved session must reject /api/tutor/alt_method with 400!')

        # Complete session with weak_step=2 -> unlocks alt_method AND enrolls into Mistake Book!
        r_pr = await self.client.post('/api/tutor/progress', headers=H, json={
            'id': sid_cr, 'solved': True, 'current_step': 4, 'first_try_count': 2, 'total_gates': 3, 'weak_step': 2,
        })
        j_pr = await r_pr.json()
        self.assertTrue(j_pr['ok'])
        self.assertEqual(j_pr['mistake_event']['type'], 'enrolled')
        mid_cr = j_pr['mistake_event']['id']

        r_alt_ok = await self.client.post('/api/tutor/alt_method', headers=H, json={'id': sid_cr})
        j_alt_ok = await r_alt_ok.json()
        self.assertEqual(r_alt_ok.status, 200)
        self.assertTrue(j_alt_ok['ok'])
        self.assertIn('吹哨抬腿', j_alt_ok['alt_method']['title'])

        # 4) Feature #2: Ebbinghaus Mistake Book persistence across history clear + 2-stage retest graduation!
        await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})
        r_mb = await self.client.get('/api/tutor/mistakes', headers=H)
        j_mb = await r_mb.json()
        self.assertEqual(j_mb['summary']['active_count'], 1, 'Mistake Book must survive history clear_all!')
        # Evicted session still replays cleanly via m:<mid>
        r_m_rep = await self.client.get(f'/api/tutor/session?id=m:{mid_cr}', headers=H)
        self.assertEqual(r_m_rep.status, 200)
        self.assertEqual((await r_m_rep.json())['session']['source'], 'mistake_replay')

        # Retest Stage 0 -> clean pass -> advances to Stage 1 (+3d, not graduated yet)
        r_rt1 = await self.client.post('/api/tutor/mistakes/retest', headers=H, json={'id': mid_cr})
        j_rt1 = await r_rt1.json()
        self.assertTrue(j_rt1['ok'])
        sid_rt1 = j_rt1['session']['id']
        for _ in range(40):
            if self.eng.sessions[sid_rt1]['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        r_p1 = await self.client.post('/api/tutor/progress', headers=H, json={
            'id': sid_rt1, 'solved': True, 'first_try_count': 3, 'total_gates': 3, 'weak_step': 0, 'peeked': False,
        })
        ev1 = (await r_p1.json())['mistake_event']
        self.assertEqual(ev1['type'], 'stage1_pass')
        self.assertFalse(ev1['graduated'])

        # Retest Stage 1 -> 2nd clean pass -> Graduated ('荣誉销号')!
        r_rt2 = await self.client.post('/api/tutor/mistakes/retest', headers=H, json={'id': mid_cr})
        sid_rt2 = (await r_rt2.json())['session']['id']
        for _ in range(40):
            if self.eng.sessions[sid_rt2]['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        r_p2 = await self.client.post('/api/tutor/progress', headers=H, json={
            'id': sid_rt2, 'solved': True, 'first_try_count': 3, 'total_gates': 3, 'weak_step': 0, 'peeked': False,
        })
        ev2 = (await r_p2.json())['mistake_event']
        self.assertEqual(ev2['type'], 'graduated')
        self.assertTrue(ev2['graduated'])
        self.assertEqual(self.eng.public_mistake_summary()['active_count'], 0)
        self.assertEqual(self.eng.public_mistake_summary()['graduated_count'], 1)

        # 5) Feature #1 & #8: Draft-Diagnosis pipeline ('diagnose_draft') + Parent Nudge & Mobile Mistake Retest
        import subprocess
        draft_jpg = subprocess.check_output([
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=white:s=320x240',
            '-frames:v', '1', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ])
        draft_b64 = 'data:image/jpeg;base64,' + base64.b64encode(draft_jpg).decode('ascii')
        r_dr = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'explain_now',
            'teach_mode': 'diagnose_draft',
            'crop_b64': draft_b64,
            'question': '看看孩子草稿哪步算错了',
        })
        j_dr = await r_dr.json()
        self.assertTrue(j_dr['ok'])
        sid_dr = j_dr['session']['id']
        self.assertEqual(self.eng.sessions[sid_dr]['teach_mode'], 'diagnose_draft')
        for _ in range(40):
            if self.eng.sessions[sid_dr]['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        # Draft diagnosis auto-enrolls into Mistake Book so child can retest tomorrow!
        self.assertGreaterEqual(self.eng.public_mistake_summary()['active_count'], 1)

        # Progress with step_attempts & step_title -> visible in mobile/state + parent_nudge round-trip
        await self.client.post('/api/tutor/progress', headers=H, json={
            'id': sid_dr, 'current_step': 2, 'step_attempts': 2, 'step_title': '红笔标出错因',
        })
        r_nudge = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'parent_nudge', 'text': '别急，先盯住红色虚线看份数哦！',
        })
        j_nudge = await r_nudge.json()
        self.assertTrue(j_nudge['ok'])
        self.assertEqual(j_nudge['nudge']['text'], '别急，先盯住红色虚线看份数哦！')

        r_ms24 = await self.client.get('/api/tutor/mobile/state', headers=H_INT)
        j_ms24 = await r_ms24.json()
        self.assertEqual(j_ms24['active_lesson']['step_attempts'], 2)
        self.assertEqual(j_ms24['active_lesson']['step_title'], '红笔标出错因')
        self.assertEqual(j_ms24['active_lesson']['teach_mode'], 'diagnose_draft')
        self.assertGreaterEqual(j_ms24['mistake_summary']['active_count'], 1)

        r_st24 = await self.client.get('/api/tutor/state', headers=H)
        j_st24 = await r_st24.json()
        self.assertEqual(j_st24['parent_nudge']['text'], '别急，先盯住红色虚线看份数哦！')

        # Remote mistake retest from mobile companion + voice tool mistake_retest
        r_mob_mr = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={'action': 'trigger_mistake_retest'})
        self.assertTrue((await r_mob_mr.json())['ok'])
        r_v_mr = await self.client.post('/api/tutor/tool', headers=H_INT, json={'action': 'mistake_retest'})
        self.assertTrue((await r_v_mr.json())['ok'])

        # 6) Feature #6 & UI symbol checks on index.html and mobile.html
        idx_r24 = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r24 = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for sym in ('detectWorksheetQuestionBlocks', 'startMistakeRetest', 'requestAltMethod', 'toggleDraftDiagnoseMode', 'dismissParentNudge'):
            self.assertIn(sym, idx_r24, f'Missing R24 symbol in index.html: {sym}')
        for msym in ('detectWorksheetQuestionBlocks', 'sendParentNudge', 'diagnose_draft', 'trigger_mistake_retest'):
            self.assertIn(msym, mob_r24, f'Missing R24 symbol in mobile.html: {msym}')

        await self.client.post('/api/tutor/mistakes/delete', headers=H, json={'clear_all': True})
        await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})

    async def test_r25_page_check_scratch_aux_traps_chinese_chem_sprint_and_print(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}
        H_INT = {'Host': f'127.0.0.1:{self.server.port}', 'X-TS-Internal': '1'}

        # 1) Feature #4 & #5: 5-subject detection (Chinese Reading + Junior Chemistry) & Chemistry TTS
        self.assertEqual(tutor_mod.detect_subject('请赏析第3自然段加点词“攥紧”运用了什么描写手法，表达了怎样的人物心理？'), 'chinese')
        self.assertEqual(tutor_mod.detect_subject('实验室用过氧化氢和 MnO2 制取 O2，若用万能集气瓶排水法收集应长进短出还是短进长出？'), 'chemistry')
        self.assertIn('寻证导图', tutor_mod.resolve_pedagogy_hint('auto', '阅读理解：赏析句子修辞手法与思想感情'))
        self.assertIn('化学', tutor_mod.resolve_pedagogy_hint('auto', '配平化学方程式 Fe2O3 + CO -> Fe + CO2'))
        tts_chem = tutor_mod.clean_math_for_tts('CaCO3 与稀 HCl 反应生成 CO2↑ 和 H2O，催化剂 MnO2')
        self.assertIn('碳酸钙', tts_chem)
        self.assertIn('盐酸', tts_chem)
        self.assertIn('二氧化碳', tts_chem)
        self.assertIn('水', tts_chem)
        self.assertIn('二氧化锰', tts_chem)

        # 2) Feature #3: Careless Reading Trap Radar (unit mismatch, diameter vs radius, both sides, reverse question)
        tr_u = tutor_mod.detect_reading_traps('一根木料长 3 米，锯下 40 厘米后还剩多少厘米？')
        self.assertTrue(any(t['kind'] == 'unit_mismatch' for t in tr_u))
        tr_d = tutor_mod.detect_reading_traps('一个圆形花坛的直径是 10 米，求它的占地面积。')
        self.assertTrue(any(t['kind'] == 'diameter_radius' for t in tr_d))
        tr_b = tutor_mod.detect_reading_traps('在长 100 米的马路两旁栽树，每隔 5 米栽一棵，共栽多少棵？')
        self.assertTrue(any(t['kind'] == 'both_sides' for t in tr_b))

        # 3) Feature #1: Full-page 'Check First, Teach Mistakes Only' + strict anti-spoiler scrubbing on wrong notes!
        import subprocess
        ws_jpg = subprocess.check_output([
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=white:s=320x240',
            '-frames:v', '1', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ])
        ws_b64 = 'data:image/jpeg;base64,' + base64.b64encode(ws_jpg).decode('ascii')
        r_chk = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'check_page', 'page_b64': ws_b64,
        })
        j_chk = await r_chk.json()
        self.assertTrue(j_chk['ok'])
        self.assertEqual(j_chk['ok_count'], 1)
        self.assertEqual(j_chk['fix_count'], 1)
        wrong_mark = next(m for m in j_chk['check_marks'] if m['status'] == 'wrong')
        # Mock returned '正确答案是288，72÷2=36需核对' -> anti-spoiler gate MUST scrub '288' and '36' and '72÷2'!
        self.assertNotIn('288', wrong_mark['note'])
        self.assertNotIn('36', wrong_mark['note'])
        self.assertNotIn('72÷2', wrong_mark['note'])
        # Rotating page 90° CW must also rotate check_marks boxes!
        old_cm_y = wrong_mark['box']['y']
        await self.client.post('/api/tutor/phone_stage/rotate', headers=H, json={})
        ps_rot = self.eng.phone_studio.public_state()
        self.assertAlmostEqual(ps_rot['check_marks'][1]['box']['y'], 0.06, places=2)
        self.assertNotAlmostEqual(ps_rot['check_marks'][1]['box']['y'], old_cm_y, places=2)

        # 4) Feature #6 & #7: 3-Question Exam Sprint + Mobile Printable A4 Sheet (original & desensitized)
        r_g1 = await self.client.post('/api/tutor/start', headers=H, json={'sample_id': 'geo_trapezoid_30', 'live': False})
        sid_g1 = (await r_g1.json())['session']['id']
        await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_g1, 'solved': True, 'first_try_count': 1, 'total_gates': 3, 'weak_step': 1})

        r_sp1 = await self.client.post('/api/tutor/mistakes/sprint_start', headers=H, json={'count': 2})
        j_sp1 = await r_sp1.json()
        self.assertTrue(j_sp1['ok'])
        self.assertEqual(j_sp1['idx'], 1)
        self.assertEqual(j_sp1['total'], 2)
        sp_id = j_sp1['sprint_id']
        await self.client.post('/api/tutor/progress', headers=H, json={
            'id': j_sp1['session']['id'], 'solved': True, 'first_try_count': 3, 'total_gates': 3, 'weak_step': 0,
        })
        r_sp2 = await self.client.post('/api/tutor/mistakes/sprint_next', headers=H, json={'sprint_id': sp_id})
        j_sp2 = await r_sp2.json()
        self.assertTrue(j_sp2['ok'])
        self.assertFalse(j_sp2['completed'])
        self.assertEqual(j_sp2['idx'], 2)
        await self.client.post('/api/tutor/progress', headers=H, json={
            'id': j_sp2['session']['id'], 'solved': True, 'first_try_count': 3, 'total_gates': 3, 'weak_step': 0,
        })
        r_sp_fin = await self.client.post('/api/tutor/mistakes/sprint_next', headers=H, json={'sprint_id': sp_id})
        j_sp_fin = await r_sp_fin.json()
        self.assertTrue(j_sp_fin['ok'])
        self.assertTrue(j_sp_fin['completed'])
        self.assertEqual(j_sp_fin['report']['clean_count'], 2)

        r_prt_orig = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={'action': 'prepare_print_sheet', 'sheet_mode': 'original'})
        j_prt_orig = await r_prt_orig.json()
        self.assertTrue(j_prt_orig['ok'])
        self.assertGreaterEqual(len(j_prt_orig['items']), 1)

        r_prt_des = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={'action': 'prepare_print_sheet', 'sheet_mode': 'desensitized'})
        j_prt_des = await r_prt_des.json()
        self.assertTrue(j_prt_des['ok'])
        self.assertIn('80米', j_prt_des['items'][0]['question'])

        # 5) Feature #2 & UI symbols check on index.html and mobile.html
        idx_r25 = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r25 = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        for sym in ('scratchCanvas', 'toggleScratchPen', 'checkAuxStrokeSolve', 'runPhoneStagePageCheck', 'startMistakeSprint', 'ackReadingRadar'):
            self.assertIn(sym, idx_r25, f'Missing R25 symbol in index.html: {sym}')
        for msym in ('generateA4PrintSheet', 'prepare_print_sheet', 'check_page', 'printModal'):
            self.assertIn(msym, mob_r25, f'Missing R25 symbol in mobile.html: {msym}')

        self.eng.phone_studio.clear_page(clear_all=True)
        await self.client.post('/api/tutor/mistakes/delete', headers=H, json={'clear_all': True})
        await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})

    async def test_r26_motion_sim_scratch_check_foundation_subquestions_clever_calc_and_9subjects(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}

        # 1) Feature #7: Full 9-subject detection (biology, geography, history, civics) & pedagogy hints
        self.assertEqual(tutor_mod.detect_subject('植物叶肉细胞中进行光合作用的场所是叶绿体还是线粒体？'), 'biology')
        self.assertEqual(tutor_mod.detect_subject('读某地等高线地形图，判断甲处陡崖和乙处鞍部的地形特征。'), 'geography')
        self.assertEqual(tutor_mod.detect_subject('材料一：洋务运动与辛亥革命的历史意义比较，请结合史料分析原因。'), 'history')
        self.assertEqual(tutor_mod.detect_subject('【初中道法】结合未成年人保护法与公民基本义务，评析材料中人物行为。'), 'civics')
        self.assertIn('force_choice', tutor_mod.resolve_pedagogy_hint('auto', '鸦片战争的历史意义是什么？'))
        self.assertIn('force_choice', tutor_mod.resolve_pedagogy_hint('auto', '【道法】结合诚实守信与社会责任分析材料'))

        # 2) Feature #1: Motion Timeline Simulator detection (meet, chase, bridge, moving_point)
        ms_meet = tutor_mod.detect_motion_sim('甲、乙两车同时从相距 420 千米的两地相对开出，3 小时后相遇。')
        self.assertEqual(ms_meet['kind'], 'meet')
        ms_br = tutor_mod.detect_motion_sim('一列火车长 180 米，通过一座长 720 米的大桥需要多少秒？')
        self.assertEqual(ms_br['kind'], 'bridge')
        ms_mp = tutor_mod.detect_motion_sim('如图，在矩形ABCD中，动点 P 从点 A 出发沿 AB 向终点 B 每秒 2 厘米运动。')
        self.assertEqual(ms_mp['kind'], 'moving_point')

        # 3) Feature #6: Clever Calc & Number-Sense detector (25x36, 125x24, diff of squares, distributive)
        cc_25 = tutor_mod.detect_clever_calc_tip('总个数：25 × 36 = □')
        self.assertIsNotNone(cc_25)
        self.assertIn('4 × 9', cc_25['tip'])
        self.assertNotIn('900', cc_25['tip'])
        cc_sq = tutor_mod.detect_clever_calc_tip('直角边：13² - 12² = □')
        self.assertIsNotNone(cc_sq)
        self.assertIn('平方差', cc_sq['badge'])
        self.assertNotIn('25', cc_sq['tip'])

        # 4) Feature #3 & #2: 60s Foundation Micro-Capsule + Blackboard Scratch AI Check (strict anti-spoiler!)
        r_g = await self.client.post('/api/tutor/start', headers=H, json={'sample_id': 'geo_trapezoid_30', 'live': False})
        j_g = await r_g.json()
        sid_g = j_g['session']['id']

        r_fd = await self.client.post('/api/tutor/foundation', headers=H, json={'id': sid_g})
        j_fd = await r_fd.json()
        self.assertTrue(j_fd['ok'])
        self.assertIn('梯形', j_fd['capsule']['title'])
        self.assertGreaterEqual(len(j_fd['capsule']['draw']), 2)
        self.assertNotIn('50', j_fd['capsule']['principle'])

        import subprocess
        scratch_jpg = subprocess.check_output([
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=black:s=320x200',
            '-frames:v', '1', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ])
        sc_b64 = 'data:image/jpeg;base64,' + base64.b64encode(scratch_jpg).decode('ascii')
        r_sc = await self.client.post('/api/tutor/check_scratch', headers=H, json={
            'id': sid_g, 'step_idx': 0, 'image_b64': sc_b64,
        })
        j_sc = await r_sc.json()
        self.assertTrue(j_sc['ok'])
        # Mock returned '最后得数是50' -> anti-spoiler gate MUST scrub '50'!
        self.assertNotIn('50', j_sc['inline_qa']['say'])
        self.assertNotIn('50', j_sc['inline_qa']['board'])

        # 5) Feature #5: Multi-part Subquestion Progressive Relay ((1) -> (2) carrying proven conclusion)
        mp_q = '甲、乙两车同时从相距 300 千米的两地相向开出，2 小时相遇，甲每小时行 80 千米。(1) 求乙车每小时行多少千米？(2) 相遇时甲车比乙车多行了多少千米？'
        sp_parsed = tutor_mod.split_multipart_subquestions(mp_q)
        self.assertIsNotNone(sp_parsed)
        self.assertEqual(len(sp_parsed['parts']), 2)

        r_mp1 = await self.client.post('/api/tutor/start', headers=H, json={'question': mp_q, 'live': True})
        j_mp1 = await r_mp1.json()
        self.assertTrue(j_mp1['ok'])
        self.assertEqual(j_mp1['session']['subq_meta']['cur_idx'], 1)
        self.assertEqual(j_mp1['session']['subq_meta']['total'], 2)
        self.assertEqual(j_mp1['session']['motion_sim']['kind'], 'meet')
        sid_mp1 = j_mp1['session']['id']
        for _ in range(30):
            s_cur = (await (await self.client.get(f'/api/tutor/session?id={sid_mp1}', headers=H)).json())['session']
            if s_cur['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_mp1, 'solved': True, 'first_try_count': 3, 'total_gates': 3})

        r_mp2 = await self.client.post('/api/tutor/subquestion/next', headers=H, json={'id': sid_mp1})
        j_mp2 = await r_mp2.json()
        self.assertTrue(j_mp2['ok'])
        self.assertEqual(j_mp2['subq_meta']['cur_idx'], 2)
        self.assertGreaterEqual(len(j_mp2['subq_meta']['proven']), 1)

        # 6) Verify R26 frontend symbols in index.html
        idx_r26 = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for sym in ('motionSimBar', 'drawMotionSimOverlay', 'checkScratchWithTeacher', 'openFoundationCapsule', 'advanceNextSubquestion', 'detectCleverCalcTipClient'):
            self.assertIn(sym, idx_r26, f'Missing R26 symbol in index.html: {sym}')

        await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})

    async def test_r27_primitives_3d_table_curve_exam_rubric_writing_polya_and_snap_guard(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}

        # 1) Features #1, #2, #3: Sanitize new draw primitives (curve, chart, cube3d, cylinder, table) + table cell anti-spoiler!
        ops27 = tutor_mod.sanitize_draw_ops([
            {'op': 'curve', 'pts': [[120, 100], [240, 280], [360, 100]], 'label': 'y=x²'},
            {'op': 'chart', 'kind': 'bar', 'x': 100, 'y': 90, 'w': 440, 'h': 220, 'cats': ['甲', '乙'], 'vals': [12, 18], 'label': '统计'},
            {'op': 'cube3d', 'x': 180, 'y': 140, 'w': 200, 'h': 140, 'cut': 'x', 'label': '长方体'},
            {'op': 'cylinder', 'x': 220, 'y': 110, 'w': 160, 'h': 180, 'label': '圆柱'},
            {'op': 'table', 'x': 90, 'y': 90, 'w': 500, 'headers': ['区间', '费用'], 'rows': [['3km内', '8元'], ['超3km', '288元']], 'label': '计费表'},
        ])
        self.assertEqual([o['op'] for o in ops27], ['curve', 'chart', 'cube3d', 'cylinder', 'table'])
        self.assertEqual(ops27[2]['cut'], 'x')
        scrubbed_tb = tutor_mod.scrub_step_spoilers({
            'step': 3, 'title': '算总费', 'say': '自己算一算？', 'board': '总费 = □',
            'quiz': {'mode': 'num', 'q': '总费是多少？', 'num_ans': '288', 'unit': '元', 'opts': ['288元', '144元'], 'ans': 0},
            'draw': [ops27[4]],
        })
        self.assertEqual(scrubbed_tb['draw'][0]['rows'][1][1], '待求？')

        # 2) Feature #5: Chinese & English Essay Socratic Scaffolds (strictly anti-ghostwriting!)
        self.assertEqual(tutor_mod.detect_writing_task('请以《那一刻，我长大了》为题写一篇不少于500字的记叙文。'), 'chinese_essay')
        self.assertEqual(tutor_mod.detect_writing_task('【英语书面表达】Write a short passage about saving water (不少于80词).'), 'english_essay')
        self.assertEqual(tutor_mod.detect_subject('请以《那一刻，我长大了》为题写一篇不少于500字的记叙文。'), 'chinese')
        self.assertIn('严禁替孩子代写整篇现成范文', tutor_mod.resolve_pedagogy_hint('auto', '请以《那一刻》为题写一篇不少于500字的记叙文'))
        self.assertIn('force_choice', tutor_mod.resolve_pedagogy_hint('auto', '【英语书面表达】写一封保护环境的英文信'))

        # 3) Feature #6 & #4: Polya Reduction Sandbox (zero spoiler!) & Post-Solve Exam Standard Writing Rubric (gated before solved!)
        self.assertTrue(tutor_mod.detect_polya_reduction_candidate('按规律摆火柴棒，第 2026 个图形需要多少根火柴？'))
        r_g = await self.client.post('/api/tutor/start', headers=H, json={'sample_id': 'geo_trapezoid_30', 'live': False})
        sid_g = (await r_g.json())['session']['id']

        # Before solving, exam_rubric MUST return HTTP 400 (anti-spoiler gate!)
        r_rub_early = await self.client.post('/api/tutor/exam_rubric', headers=H, json={'id': sid_g})
        self.assertEqual(r_rub_early.status, 400)

        # Polya simplify works mid-lesson and scrubs leaked '50' from mock response!
        r_pol = await self.client.post('/api/tutor/polya_simplify', headers=H, json={'id': sid_g})
        j_pol = await r_pol.json()
        self.assertTrue(j_pol['ok'])
        self.assertNotIn('50', j_pol['sandbox']['rule_q'])
        self.assertEqual(j_pol['sandbox']['draw'][0]['op'], 'table')

        # After solving, exam_rubric unlocks HTTP 200 with formatted exam script & 3 anti-deduction checkpoints!
        await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_g, 'solved': True, 'first_try_count': 3, 'total_gates': 3})
        r_rub_ok = await self.client.post('/api/tutor/exam_rubric', headers=H, json={'id': sid_g})
        self.assertEqual(r_rub_ok.status, 200)
        j_rub = await r_rub_ok.json()
        self.assertTrue(j_rub['ok'])
        self.assertIn('答：', j_rub['exam_rubric']['script'])
        self.assertEqual(len(j_rub['exam_rubric']['checkpoints']), 3)

        # 3D Foundation capsule check
        r_3d = await self.client.post('/api/tutor/start', headers=H, json={'question': '把一个正方体木块锯成两段长方体，表面积增加多少？', 'live': True})
        sid_3d = (await r_3d.json())['session']['id']
        r_fd3d = await self.client.post('/api/tutor/foundation', headers=H, json={'id': sid_3d})
        j_fd3d = await r_fd3d.json()
        self.assertTrue(j_fd3d['ok'])
        self.assertEqual(j_fd3d['capsule']['draw'][0]['op'], 'cube3d')

        # 4) Feature #7 & UI symbols check on index.html
        idx_r27 = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for sym in ('drawSmoothCurve', 'cube3d', 'requestExamRubric', 'openPolyaSimplify', 'snapGuessWarnedStepSet'):
            self.assertIn(sym, idx_r27, f'Missing R27 symbol in index.html: {sym}')

        await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})

    async def test_r28_solo_challenge_archetype_map_geo_models_essay_critique_vcalc_and_read_aloud(self):
        H = {'Host': f'127.0.0.1:{self.server.port}'}

        # 1) Feature #5: vcalc vertical-arithmetic primitive + anti-spoiler scrubbing + decimal & equation capsules!
        vc_ops = tutor_mod.sanitize_draw_ops([
            {'op': 'vcalc', 'x': 200, 'y': 90, 'a': '12.5', 'op_sym': '*', 'b': '3.75', 'res': '16.25', 'rule': '小数点对齐', 'label': '竖式'},
        ])
        self.assertEqual(len(vc_ops), 1)
        self.assertEqual(vc_ops[0]['op'], 'vcalc')
        self.assertEqual(vc_ops[0]['op_sym'], '×')
        scrub_vc = tutor_mod.scrub_step_spoilers({
            'step': 3, 'title': '竖式计算', 'say': '列竖式算一算？', 'board': '12.5 + 3.75 = □',
            'quiz': {'mode': 'num', 'q': '和是多少？', 'num_ans': '16.25', 'unit': '元', 'opts': ['16.25元', '15.25元'], 'ans': 0},
            'draw': [{'op': 'vcalc', 'x': 200, 'y': 90, 'a': '12.50', 'op_sym': '+', 'b': '3.75', 'res': '16.25', 'rule': '小数点对齐'}],
        })
        self.assertEqual(scrub_vc['draw'][0]['res'], '□')
        self.assertEqual(tutor_mod.detect_calc_microscope('妈妈买苹果花了 12.5 元，买草莓花了 3.75 元，一共多少元？')['kind'], 'decimal_vcalc')
        self.assertEqual(tutor_mod.detect_calc_microscope('解方程：3x + 5 = 20')['kind'], 'eq_transpose')

        r_dec = await self.client.post('/api/tutor/start', headers=H, json={'question': '列竖式计算：12.5 + 3.75 等于多少？', 'live': True})
        sid_dec = (await r_dec.json())['session']['id']
        j_fdec = await (await self.client.post('/api/tutor/foundation', headers=H, json={'id': sid_dec})).json()
        self.assertEqual(j_fdec['capsule']['draw'][0]['op'], 'vcalc')
        self.assertEqual(j_fdec['capsule']['draw'][0]['res'], '？')

        r_eq = await self.client.post('/api/tutor/start', headers=H, json={'question': '解方程：2x + 7 = 19', 'live': True})
        sid_eq = (await r_eq.json())['session']['id']
        j_feq = await (await self.client.post('/api/tutor/foundation', headers=H, json={'id': sid_eq})).json()
        self.assertIn('变号', j_feq['capsule']['title'])

        # 2) Feature #3: Classic Geometry Model Recognition & Zero-Spoiler Skeleton Atlas
        gm_trap = tutor_mod.detect_geometry_model('在梯形 ABCD 中，AD∥BC，AB=10，∠B=30°，求面积。')
        self.assertEqual(gm_trap['id'], 'special_angle_alt')
        self.assertNotIn('10', json.dumps(gm_trap['draw'], ensure_ascii=False))
        self.assertEqual(tutor_mod.detect_geometry_model('在凹四边形 ABCD 中，∠A=35°，求凹角 ∠BDC。')['id'], 'dart_concave')
        self.assertEqual(tutor_mod.detect_geometry_model('已知定点A、B在直线l同侧，求 PA + PB 最小值（将军饮马）。')['id'], 'general_drink_horse')
        self.assertEqual(tutor_mod.detect_geometry_model('已知 AB ∥ CD，点 E 为平行线间拐点，求折角 ∠BED。')['id'], 'parallel_pig_foot')

        # 3) Feature #4: Handwritten / Draft Essay Critique & Sentence Upgrade Mode
        self.assertTrue(tutor_mod.is_essay_critique_request('【作文升格精批】请帮我批改这段作文草稿：“今天运动会我很紧张，后来跑完了我很开心。”'))
        self.assertTrue(tutor_mod.is_essay_critique_request('今天阳光明媚，妈妈带我去公园放风筝，风筝飞得很高，我玩得真开心啊。', teach_mode='diagnose_draft'))
        r_es = await self.client.post('/api/tutor/start', headers=H, json={
            'question': '【作文升格精批】帮我看看我写的段落：运动会开始了，我跑得很快，最后得了第一名，我很开心。',
            'live': True,
        })
        j_es = await r_es.json()
        self.assertTrue(j_es['ok'])
        self.assertTrue(j_es['session']['essay_critique'])
        self.assertEqual(j_es['session']['teach_mode'], 'diagnose_draft')

        # 4) Feature #1 & #2: Solo Challenge Mode + Post-Solve Archetype Skin-vs-Bone Mapping Table (with anti-spoiler gate!)
        r_cr = await self.client.post('/api/tutor/start', headers=H, json={
            'sample_id': 'word_chicken_rabbit', 'live': False, 'solo_challenge': True,
        })
        j_cr = await r_cr.json()
        sid_cr = j_cr['session']['id']
        self.assertTrue(j_cr['session']['solo_challenge'])
        self.assertIsNone(j_cr['session']['archetype_map'])

        # Anti-spoiler gate: archetype_map MUST reject with HTTP 400 before solved!
        r_am_early = await self.client.post('/api/tutor/archetype_map', headers=H, json={'id': sid_cr})
        self.assertEqual(r_am_early.status, 400)

        # Simulate Solo Ace completion -> unlocks archetype_map & persists solo_ace!
        await self.client.post('/api/tutor/progress', headers=H, json={
            'id': sid_cr, 'solved': True, 'solo_ace': True, 'solo_sec': 19, 'first_try_count': 3, 'total_gates': 3, 'weak_step': 0,
        })
        pub_after = (await (await self.client.get(f'/api/tutor/session?id={sid_cr}', headers=H)).json())['session']
        self.assertFalse(pub_after['solo_challenge'])
        self.assertTrue(pub_after['progress']['solo_ace'])
        self.assertEqual(pub_after['progress']['solo_sec'], 19)

        r_am_ok = await self.client.post('/api/tutor/archetype_map', headers=H, json={'id': sid_cr})
        self.assertEqual(r_am_ok.status, 200)
        j_am = await r_am_ok.json()
        self.assertTrue(j_am['ok'])
        self.assertEqual(len(j_am['archetype_map']['headers']), 4)
        self.assertIn('船', json.dumps(j_am['archetype_map']['rows'], ensure_ascii=False))

        # Live session archetype_map via LLM
        await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_eq, 'solved': True, 'first_try_count': 3, 'total_gates': 3})
        r_am_live = await self.client.post('/api/tutor/archetype_map', headers=H, json={'id': sid_eq})
        self.assertEqual(r_am_live.status, 200)
        self.assertGreaterEqual(len((await r_am_live.json())['archetype_map']['rows']), 2)

        # 5) Feature #6: English Sentence & Classical Chinese Read-Aloud Coach (strictly gated until solved!)
        en_q = '【英语语法】Tom ___ (live) in Shanghai since 2021. 请判断横线处应填 lives、lived 还是 has lived？'
        r_en = await self.client.post('/api/tutor/start', headers=H, json={'question': en_q, 'live': True})
        sid_en = (await r_en.json())['session']['id']
        for _ in range(30):
            s_en = (await (await self.client.get(f'/api/tutor/session?id={sid_en}', headers=H)).json())['session']
            if s_en['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        # Inject realistic English Step 3 choice quiz to verify blank-filling reconstruction AND pre-solve anti-spoiler gate!
        self.eng.sessions[sid_en]['steps'][2]['quiz'] = {
            'mode': 'choice', 'q': '横线处填什么？', 'opts': ['lives', 'has lived', 'lived'], 'ans': 1,
        }
        pub_en_unsolved = self.eng.public_session(sid_en)
        self.assertIsNone(pub_en_unsolved['read_aloud_target'], 'Must NOT leak completed English sentence before solved!')
        r_ra_early = await self.client.post('/api/tutor/read_aloud', headers=H, json={'id': sid_en, 'text': 'Tom has lived in Shanghai since 2021'})
        self.assertEqual(r_ra_early.status, 400)

        await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_en, 'solved': True, 'first_try_count': 3, 'total_gates': 3})
        pub_en_solved = self.eng.public_session(sid_en)
        self.assertEqual(pub_en_solved['read_aloud_target']['text'], 'Tom has lived in Shanghai since 2021.')
        r_ra_pass = await self.client.post('/api/tutor/read_aloud', headers=H, json={'id': sid_en, 'text': 'Tom has lived in Shanghai since 2021.'})
        j_ra_pass = await r_ra_pass.json()
        self.assertTrue(j_ra_pass['ok'])
        self.assertTrue(j_ra_pass['read_aloud']['pass'])
        self.assertTrue(j_ra_pass['session']['progress']['read_aloud_ok'])

        # Classical Chinese 2/2/3 pause guide check
        cn_ra = tutor_mod.extract_read_aloud_target('【古诗赏析】赏析“春风又绿江南岸，明月何时照我还”中的“绿”字。')
        self.assertEqual(cn_ra['kind'], 'classical_cn')
        self.assertIn('春风 / 又绿 / 江南岸', cn_ra['display'])

        # 6) Verify R28 & R29 Deep-Audit Polish regressions (TTS English safety, essay MistakeBook isolation, post-restart hydration, mobile state sync, TDZ & IIFE exports)
        self.assertEqual(tutor_mod.clean_math_for_tts('She put a bookmark in her twenty-five-page notebook.'), 'She put a bookmark in her twenty-five-page notebook.')
        self.assertIn('厘米', tutor_mod.clean_math_for_tts('长12cm，x - y = 5'))
        self.assertIn('减', tutor_mod.clean_math_for_tts('长12cm，x - y = 5'))

        # Essay critique must NEVER enroll into Mistake Retest Book!
        mb_list_after_essay = (await (await self.client.get('/api/tutor/mistakes', headers=H)).json())['mistakes']
        self.assertFalse(any('运动会' in (m.get('question') or '') for m in mb_list_after_essay), 'Essay critique must not pollute Mistake Book!')

        # Post-restart session hydration: clear in-memory self.eng.sessions and verify exam_rubric & archetype_map still hydrate & persist to history!
        self.eng.sessions.clear()
        r_rub_hyd = await self.client.post('/api/tutor/exam_rubric', headers=H, json={'id': sid_eq})
        self.assertEqual(r_rub_hyd.status, 200)
        self.assertIn(sid_eq, self.eng.sessions)
        hist_eq = next((h for h in self.eng.history if h.get('id') == sid_eq), None)
        self.assertIsNotNone(hist_eq)
        self.assertIsInstance(hist_eq.get('exam_rubric'), dict)

        # Mobile state must expose R28 solo_ace & essay_critique fields
        self.eng.active_session_id = sid_cr
        r_ms = await self.client.get('/api/tutor/mobile/state', headers={**H, 'X-TS-Internal': '1'})
        j_ms = await r_ms.json()
        self.assertTrue(j_ms['active_lesson']['solo_ace'])

        idx_r28 = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        for sym in ('soloChallengeBox', 'submitSoloAnswer', 'exitSoloToGuided', 'requestArchetypeMap', 'openGeoModelAtlas', 'playReadAloudDemo', "op === 'vcalc'", 'window.speakHintText', 'activeSumPanel'):
            self.assertIn(sym, idx_r28, f'Missing R28/R29 symbol in index.html: {sym}')
        # Verify isFeynmanClosedBook is declared BEFORE showMs in renderBoardUi (no TDZ!)
        self.assertLess(idx_r28.index('const isFeynmanClosedBook ='), idx_r28.index('const showMs ='))

        # R30 assertions: pre-solve clever_calc anti-spoiler gate, contraction-tolerant English read-aloud, & essay rubric style
        r_cc = await self.client.post('/api/tutor/start', headers=H, json={'question': '每人做 25 个风车，36 人一共做多少个？', 'live': True})
        sid_cc = (await r_cc.json())['session']['id']
        for _ in range(30):
            s_cc = (await (await self.client.get(f'/api/tutor/session?id={sid_cc}', headers=H)).json())['session']
            if s_cc['status'] == 'ready':
                break
            await asyncio.sleep(0.05)
        self.eng.sessions[sid_cc]['steps'][-1]['board'] = '核对正确：25 × 36 = 900 (个) ✓'
        self.assertIsNone(self.eng.public_session(sid_cc)['clever_calc'], 'clever_calc must NOT leak hidden Step 3/4 formula before solved!')
        await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_cc, 'solved': True, 'first_try_count': 3, 'total_gates': 3})
        self.assertIsNotNone(self.eng.public_session(sid_cc)['clever_calc'])

        # English parenthetical blank + contraction tolerance ("hasn't" vs "has not")
        en_ra2 = tutor_mod.extract_read_aloud_target(
            '【英语语法】He (   ) finished his homework yet. 请选择正确选项。',
            [{'step': 3, 'quiz': {'opts': ["A. hasn't（尚未）", 'B. did not'], 'ans': 0}}],
        )
        self.assertEqual(en_ra2['text'], "He hasn't finished his homework yet.")
        hyd_en = self.eng._get_or_hydrate_session(sid_en)
        hyd_en['question'] = '【英语语法】He (   ) finished his homework yet. 请选择正确选项。'
        hyd_en['steps'][2]['quiz'] = {'mode': 'choice', 'q': '选哪个？', 'opts': ["hasn't", 'did not'], 'ans': 0}
        r_ra_contr = await self.client.post('/api/tutor/read_aloud', headers=H, json={'id': sid_en, 'text': 'He has not finished his homework yet.'})
        self.assertTrue((await r_ra_contr.json())['read_aloud']['pass'], 'Expanded contraction must pass English read-aloud!')

        # R31 Pass-3 assertions:
        # 1) Writing tasks & draft-diagnose must NEVER enter solo_challenge even if solo_challenge=True is requested
        r_wt_solo = await self.client.post('/api/tutor/start', headers=H, json={
            'question': '【语文作文】请以《那一刻，我读懂了___》为题，完成半命题作文审题选材与三段式细节构思（不少于500字）。',
            'live': True,
            'solo_challenge': True,
        })
        j_wt_solo = await r_wt_solo.json()
        self.assertTrue(j_wt_solo['ok'])
        self.assertTrue(bool(j_wt_solo['session']['writing_task']))
        self.assertFalse(j_wt_solo['session']['solo_challenge'], 'Writing task must never enter solo_challenge!')

        # 2) Two-phase mobile upload: crop-only Phase 1 on a brand-new Page 2 must NOT bind to old Page 1 before stage_silent arrives!
        import subprocess as _subp
        tiny_jpg = _subp.check_output([
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=white:s=240x180',
            '-frames:v', '1', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ])
        import base64 as _b64
        b64_img = _b64.b64encode(tiny_jpg).decode('ascii')
        H_INT = {**H, 'X-TS-Internal': '1'}
        r_p1 = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'stage_only', 'page_b64': b64_img, 'client_page_id': 'cp_page1',
        })
        p1_id = (await r_p1.json())['page_id']
        r_p2_crop = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'explain_now', 'crop_b64': b64_img, 'client_page_id': 'cp_page2', 'page_id': '',
        })
        sid_p2 = (await r_p2_crop.json())['session']['id']
        self.assertNotEqual(self.eng.sessions[sid_p2].get('stage_page_id', ''), p1_id, 'New crop-only upload must not falsely bind to old Page 1!')
        r_p2_stage = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={
            'action': 'stage_silent', 'session_id': sid_p2, 'page_b64': b64_img, 'client_page_id': 'cp_page2',
            'crop_box': {'x': 0.1, 'y': 0.1, 'w': 0.8, 'h': 0.3},
        })
        p2_id = (await r_p2_stage.json())['page_id']
        self.assertNotEqual(p1_id, p2_id)
        self.assertEqual(self.eng.sessions[sid_p2].get('stage_page_id'), p2_id)

        # 3) #foundationCanvas isolation guard & .snap-thumb-btn z-index check in index.html
        self.assertIn('if (!lbl || renderingAuxCanvas || !session', idx_r28)
        self.assertIn('if (!session || !guidedMode || soloChallengeActive || !startPt || !endPt)', idx_r28)
        self.assertIn('position: absolute; top: 8px; right: 10px; z-index: 4;', idx_r28)

        # R32 Pass-4 assertions:
        # 1) Unicode minus '−' (−) in detect_clever_calc_tip (diff-of-squares & distributive subtraction)
        cc_sq = tutor_mod.detect_clever_calc_tip('102² − 98² = □')
        self.assertIsNotNone(cc_sq, 'detect_clever_calc_tip must recognize Unicode minus in difference of squares!')
        self.assertIn('平方差', cc_sq['badge'])
        cc_dist = tutor_mod.detect_clever_calc_tip('37 × 18 − 37 × 8 = □')
        self.assertIsNotNone(cc_dist, 'detect_clever_calc_tip must recognize Unicode minus in distributive subtraction!')
        self.assertIn('公因数', cc_dist['badge'])

        # 2) cube3d top-edge clamp (y + dy >= 32) & multi-subject _scrub_page_check_spoilers
        c3d = tutor_mod.sanitize_draw_ops([{'op': 'cube3d', 'x': 120, 'y': 85, 'w': 180, 'h': 120, 'dx': 60, 'dy': -95, 'label': '长方体'}])[0]
        self.assertGreaterEqual(c3d['y'] + c3d['dy'], 32.0)
        scrub_en = tutor_mod.TutorEngine._scrub_page_check_spoilers('时态不对，应填 has lived，正确选项为 B')
        self.assertNotIn('has lived', scrub_en)
        self.assertNotIn('为 B', scrub_en)

        # 3) Verify computeVcalcLayout fixes vcalc label NaN & closedBookCanvas suppresses motion_sim overlay in index.html
        self.assertIn('function computeVcalcLayout(d, resTxt)', idx_r28)
        self.assertIn('!soloChallengeActive && !closedBookCanvas', idx_r28)

        # R33 Pass-5 assertions:
        # 1) clean_math_for_tts preserves English number-hyphen compounds ('12-year-old', '5-minute') while converting '5 - x' & 'm³'
        tts_en_num = tutor_mod.clean_math_for_tts('She is a 12-year-old girl taking a 5-minute walk.')
        self.assertIn('12-year-old', tts_en_num)
        self.assertIn('5-minute', tts_en_num)
        self.assertNotIn('减', tts_en_num)
        self.assertIn('减', tutor_mod.clean_math_for_tts('5 - x = 2'))
        self.assertIn('立方米', tutor_mod.clean_math_for_tts('体积为 12m³'))

        # 2) _extract_subq_conclusion extracts Step 3 final answer ('70千米/时') rather than Step 4 first intermediate '✓' line
        subq_concl = self.eng._extract_subq_conclusion({
            'steps': [
                {'step': 3, 'quiz': {'mode': 'num', 'num_ans': '70', 'unit': '千米/时', 'chain': [{'q': '速度和', 'ans': '150', 'unit': '千米/时'}, {'q': '乙车速度', 'ans': '70', 'unit': '千米/时'}]}},
                {'step': 4, 'quiz': None, 'board': '① 速度和：450 ÷ 3 = 150(千米/时) ✓\n② 乙车速度：150 − 80 = 70(千米/时) ✓'},
            ],
        }, '第(1)问')
        self.assertIn('70千米/时', subq_concl)
        self.assertNotIn('150', subq_concl)

        # 3) Writing compositions must NOT enroll into mistake_book; m:<id> mistake replay gates before solve & unlocks rubric/archetype/alt after solve
        self.eng.update_progress(j_wt_solo['session']['id'], {'solved': True, 'weak_step': 1, 'first_try_count': 1, 'total_gates': 3})
        self.assertFalse(any(tutor_mod.detect_writing_task(m.get('question') or '') for m in self.eng.mistakes), 'Writing task must never enroll into mistake_book!')
        r_g_m = await self.client.post('/api/tutor/start', headers=H, json={'sample_id': 'word_chicken_rabbit', 'live': False})
        sid_gm = (await r_g_m.json())['session']['id']
        self.eng.update_progress(sid_gm, {'solved': True, 'weak_step': 2, 'first_try_count': 1, 'total_gates': 3})
        mb_id = self.eng.mistakes[0]['id']
        m_ref = f'm:{mb_id}'
        self.eng.sessions.clear()
        r_m_alt_early = await self.client.post('/api/tutor/alt_method', headers=H, json={'id': m_ref})
        self.assertEqual(r_m_alt_early.status, 400, 'm:<id> replay must gate alt_method before solve!')
        await self.client.post('/api/tutor/progress', headers=H, json={'id': m_ref, 'solved': True, 'first_try_count': 3, 'total_gates': 3, 'weak_step': 0})
        r_m_rub_ok = await self.client.post('/api/tutor/exam_rubric', headers=H, json={'id': m_ref})
        self.assertEqual(r_m_rub_ok.status, 200, 'm:<id> replay must unlock exam_rubric after solve!')
        r_m_arch_ok = await self.client.post('/api/tutor/archetype_map', headers=H, json={'id': m_ref})
        self.assertEqual(r_m_arch_ok.status, 200, 'm:<id> replay must unlock archetype_map after solve!')

        # 4) Verify 220-char speakHintText, camera/phone snap isolation & streaming solo-ace guard in index.html
        self.assertIn('encodeURIComponent(String(txt).slice(0, 220))', idx_r28)
        self.assertIn('const zeroHintAce = !soloUsedScratchHint;', idx_r28)

        # R34 Pass-6 assertions:
        # 1) extract_read_aloud_target fills inline no-underscore '(live)' verb prompt & strips leading item number
        ra_inline = tutor_mod.extract_read_aloud_target(
            '【英语语法】1. Tom (live) in Shanghai since 2021. 请用括号内动词适当形式填空。',
            [{'step': 3, 'quiz': {'mode': 'choice', 'q': '应填？', 'opts': ['lives', 'has lived', 'lived'], 'ans': 1}}],
        )
        self.assertIsNotNone(ra_inline)
        self.assertEqual(ra_inline['text'], 'Tom has lived in Shanghai since 2021.')
        self.assertEqual(ra_inline['focus'], 'has lived')

        # 2) detect_clever_calc_tip rejects decimal suffix '2.7 × 6 + 7 × 8' & precedence trap '3.14 × 10² − 5²', while catching '3.14 × 64 + 3.14 × 36'
        self.assertIsNone(tutor_mod.detect_clever_calc_tip('2.7 × 6 + 7 × 8 = □'))
        self.assertIsNone(tutor_mod.detect_clever_calc_tip('3.14 × 10² − 5² = □'))
        cc_pi = tutor_mod.detect_clever_calc_tip('3.14 × 64 + 3.14 × 36 = □')
        self.assertIsNotNone(cc_pi)
        self.assertIn('3.14', cc_pi['title'])

        # 3) sanitize_draw_ops clamps oversized right/bottom coordinates inside 700x440 across cube3d, table, chart, cylinder, circle
        clamped_ops = tutor_mod.sanitize_draw_ops([
            {'op': 'cube3d', 'x': 470, 'y': 295, 'w': 320, 'h': 220, 'dx': 100, 'dy': -50, 'label': '长方体'},
            {'op': 'table', 'x': 320, 'y': 255, 'w': 560, 'headers': ['A', 'B'], 'rows': [['1', '2'], ['3', '4'], ['5', '6'], ['7', '8']]},
            {'op': 'chart', 'x': 350, 'y': 215, 'w': 520, 'h': 260, 'cats': ['甲', '乙'], 'vals': [12, 18]},
            {'op': 'cylinder', 'x': 490, 'y': 255, 'w': 280, 'h': 240},
            {'op': 'circle', 'cx': 635, 'cy': 375, 'r': 150},
        ])
        self.assertEqual(len(clamped_ops), 5)
        self.assertLessEqual(clamped_ops[0]['x'] + clamped_ops[0]['w'] + clamped_ops[0]['dx'], 676.1)
        self.assertLessEqual(clamped_ops[0]['y'] + clamped_ops[0]['h'], 416.1)
        self.assertLessEqual(clamped_ops[1]['x'] + clamped_ops[1]['w'], 676.1)
        self.assertLessEqual(clamped_ops[1]['y'] + 5 * 36, 420.1)
        self.assertLessEqual(clamped_ops[2]['x'] + clamped_ops[2]['w'], 676.1)
        self.assertLessEqual(clamped_ops[2]['y'] + clamped_ops[2]['h'], 396.1)
        self.assertLessEqual(clamped_ops[3]['x'] + clamped_ops[3]['w'], 676.1)
        self.assertLessEqual(clamped_ops[3]['y'] + clamped_ops[3]['h'], 416.1)
        self.assertLessEqual(clamped_ops[4]['cx'] + clamped_ops[4]['r'], 676.1)
        self.assertLessEqual(clamped_ops[4]['cy'] + clamped_ops[4]['r'], 412.1)

        # 4) Spoken/typed math draft check auto-promotes teach_mode='diagnose_draft' & essay Step 0 uses writing scaffold
        self.assertIn('找闪光亮点', json.dumps(j_es['session']['steps'][0]['draw'], ensure_ascii=False))
        r_m_draft = await self.client.post('/api/tutor/start', headers=H, json={
            'question': '长方形周长 72 厘米，长是宽的 2 倍，我算 72 ÷ 3 = 24，哪一步算错了？',
            'live': True, 'solo_challenge': True,
        })
        j_m_draft = await r_m_draft.json()
        self.assertEqual(j_m_draft['session']['teach_mode'], 'diagnose_draft')
        self.assertFalse(j_m_draft['session']['solo_challenge'])
        for _ in range(30):
            s_md = (await (await self.client.get(f"/api/tutor/session?id={j_m_draft['session']['id']}", headers=H)).json())['session']
            if s_md['status'] in ('ready', 'error'):
                break
            await asyncio.sleep(0.05)

        # 5) prepare_print_sheet original mode extracts final chain/choice answer & save_page rot_steps rotates done_crops/check_marks
        self.eng.mistakes.insert(0, {
            'id': 'mb_r34_chain',
            'title': '两车相遇',
            'question': '甲乙相距450千米，3小时相遇，甲速80，求乙速？',
            'subject': 'math',
            'error_tag': '第3关薄弱',
            'graduated': False,
            'steps': [
                {'step': 3, 'quiz': {'mode': 'num', 'num_ans': '70', 'unit': '千米/时', 'chain': [{'q': '速度和', 'ans': '150'}, {'q': '乙速', 'ans': '70', 'unit': '千米/时'}]}},
                {'step': 4, 'quiz': None, 'board': '① 速度和 = 150 ✓\n② 乙速 = 70 ✓'},
            ],
        })
        ps_orig = await self.eng.prepare_print_sheet(ids=['mb_r34_chain'], sheet_mode='original')
        self.assertIn('70千米/时', ps_orig['items'][0]['ans_check'])
        self.assertNotIn('150', ps_orig['items'][0]['ans_check'])

        pid_rot = self.eng.phone_studio.save_page(tiny_jpg, crop_box={'x': 0.1, 'y': 0.1, 'w': 0.8, 'h': 0.2}, client_key='ck_rot_test')
        self.eng.phone_studio.record_done_crop(pid_rot, 'sid_rot_1', {'x': 0.1, 'y': 0.1, 'w': 0.8, 'h': 0.2})
        self.eng.phone_studio.set_check_marks(pid_rot, [{'no': '第1题', 'status': 'wrong', 'box': {'x': 0.1, 'y': 0.1, 'w': 0.8, 'h': 0.2}, 'note': '核对份数'}], summary='共1题')
        self.eng.phone_studio.save_page(tiny_jpg, page_id=pid_rot, rot_steps=1)
        pg_after_rot = next(p for p in self.eng.phone_studio.pages if p['id'] == pid_rot)
        self.assertEqual(len(pg_after_rot['check_marks']), 1, 'Rotating staged page from mobile must rotate & preserve check_marks!')
        self.assertAlmostEqual(pg_after_rot['done_crops'][0]['crop']['x'], 0.7, places=2)

        # 6) Verify index.html solo/feynman canvas tap guards & choice-only straightness aux-stroke guard
        self.assertIn('if (soloChallengeActive) return;', idx_r28)
        self.assertIn("curSt.quiz.mode !== 'choice'", idx_r28)
        self.assertIn('if (pathLen > strokeLen * 1.45) return false;', idx_r28)

        # ---- R35 Pass-7 Audit Regression Checks ----
        # 7a) build_step0_scaffold preserves dedicated writing scaffold on photographed essay/composition OCR
        sc_ec = tutor_mod.build_step0_scaffold('【作文升格精批】今天运动会我很累但坚持跑完了。', teach_mode='diagnose_draft', essay_critique=True, from_ocr=True)
        self.assertIn('找闪光亮点', json.dumps(sc_ec['draw'], ensure_ascii=False))
        sc_wt = tutor_mod.build_step0_scaffold('请以《那一刻我长大了》为题写一篇记叙文（不少于500字）', teach_mode='standard', essay_critique=False, from_ocr=True)
        self.assertIn('审题抓题眼', json.dumps(sc_wt['draw'], ensure_ascii=False))
        sc_en = tutor_mod.build_step0_scaffold('【英语语法】Tom ___ (live) here since 2021.', teach_mode='standard', essay_critique=False, from_ocr=False)
        self.assertIn('找语法标志词', json.dumps(sc_en['draw'], ensure_ascii=False))

        # 7b) evaluate_read_aloud English number-word <-> digit equivalence ('twelve' <-> '12', '2021' kept)
        en_num_sess = self.eng._new_session_record('英语朗读数字核验', '【英语】She has kept twelve books since 2021.', 'fast', 'custom')
        en_num_sess['status'] = 'ready'
        en_num_sess['progress'] = {'solved': True}
        en_num_sess['steps'] = [
            {'step': 4, 'title': '原句朗读', 'say': '完成', 'board': 'She has kept twelve books since 2021. ✓', 'quiz': None, 'draw': []},
        ]
        ra_num_res = await self.eng.evaluate_read_aloud(en_num_sess['id'], 'She has kept 12 books since 2021')
        self.assertTrue(ra_num_res['read_aloud']['pass'], f"Number word <-> digit should match in read_aloud: {ra_num_res}")
        self.assertEqual(ra_num_res['read_aloud']['score'], 100)

        # 7c) Essay critique variant preserves essay_critique=True & stays excluded from math mistake_book
        r_es_r35 = await self.client.post('/api/tutor/start', headers=H, json={
            'question': '【作文升格精批】请帮我批改习作片段：“今天我去公园玩，看到很多花，我很开心。”',
            'live': True,
        })
        sid_es_r35 = (await r_es_r35.json())['session']['id']
        for _ in range(30):
            s_ec35 = (await (await self.client.get(f'/api/tutor/session?id={sid_es_r35}', headers=H)).json())['session']
            if s_ec35['status'] in ('ready', 'error'):
                break
            await asyncio.sleep(0.05)
        var_ec_res = await self.eng.generate_variant_problem(sid_es_r35, mode='fast')
        self.assertTrue(var_ec_res['session']['essay_critique'], 'Variant of essay critique must keep essay_critique=True!')
        self.assertFalse(var_ec_res['session']['solo_challenge'])
        for _ in range(30):
            s_vec = (await (await self.client.get(f"/api/tutor/session?id={var_ec_res['session']['id']}", headers=H)).json())['session']
            if s_vec['status'] in ('ready', 'error'):
                break
            await asyncio.sleep(0.05)
        self.assertFalse(any(m.get('essay_critique') for m in self.eng.mistakes), 'Essay critique must remain excluded from mistake_book!')
        self.eng.mistakes.insert(0, {'id': 'mb_r35_ec_legacy', 'title': '旧习作回看', 'question': '【作文升格精批】我很开心。', 'teach_mode': 'diagnose_draft', 'essay_critique': True, 'steps': []})
        pub_m_ec = self.eng.public_session('m:mb_r35_ec_legacy')
        self.assertTrue(pub_m_ec['essay_critique'])

        # 7d) api_mobile_upload updates staged page last_crop even when re-cropping without re-uploading page_b64
        pid_mc = self.eng.phone_studio.save_page(tiny_jpg, crop_box={'x': 0.06, 'y': 0.10, 'w': 0.88, 'h': 0.25}, client_key='ck_mc_r35')
        r_mc = await self.client.post(
            '/api/tutor/mobile/upload',
            headers={**H, 'X-TS-Internal': '1'},
            json={
                'action': 'explain_now',
                'page_id': pid_mc,
                'crop_box': {'x': 0.08, 'y': 0.52, 'w': 0.84, 'h': 0.28},
                'crop_b64': base64.b64encode(tiny_jpg).decode('ascii'),
                'question': '第2题',
            },
        )
        self.assertEqual(r_mc.status, 200)
        j_mc = await r_mc.json()
        pg_mc_after = next(p for p in self.eng.phone_studio.pages if p['id'] == pid_mc)
        self.assertAlmostEqual(pg_mc_after['last_crop']['y'], 0.52, places=2)
        for _ in range(30):
            s_mc = (await (await self.client.get(f"/api/tutor/session?id={j_mc['session']['id']}", headers=H)).json())['session']
            if s_mc['status'] in ('ready', 'error'):
                break
            await asyncio.sleep(0.05)

        # 7e) Verify index.html R35 subject-adaptive QA chips, contraction-aware spoken match & solo word-problem anti-spoiler
        self.assertIn('function resolveStageQaChips(', idx_r28)
        self.assertIn('function scoreSpokenChoiceMatch(', idx_r28)
        self.assertIn('② 独立找等量关系', idx_r28)

        # ---- R36 Pass-8 Audit Regression Checks ----
        # 8a) detect_reading_traps zero false positives on English "I'm", algebra 'm', grain '大米'/'玉米', verb '克服', and multi-char saw_cuts kw
        self.assertEqual(tutor_mod.detect_reading_traps("I'm 12 years old and my desk is 80 cm wide."), [])
        self.assertEqual(tutor_mod.detect_reading_traps('已知长方形宽为 m，周长为 24cm，求长。'), [])
        self.assertEqual(tutor_mod.detect_reading_traps('妈妈买大米 5 千克，米袋长 40 厘米。'), [])
        self.assertEqual(tutor_mod.detect_reading_traps('物体质量为 5 千克，克服摩擦力做功 100J。'), [])
        tr_real_m = tutor_mod.detect_reading_traps('一根彩带长 2m，剪去 50cm，还剩多少厘米？')
        self.assertTrue(any(t['kind'] == 'unit_mismatch' and t['kw'] == '50cm' for t in tr_real_m), f'Expected 50cm kw in {tr_real_m}')
        tr_real_mass = tutor_mod.detect_reading_traps('一袋面粉重 2 千克 500 克，合多少克？')
        self.assertTrue(any(t['kind'] == 'mass_unit' for t in tr_real_mass))
        tr_saw = tutor_mod.detect_reading_traps('把一根长 20 分米的长方体木料横截成相等的 3 段，表面积增加 36 平方分米。')
        saw_hit = next((t for t in tr_saw if t['kind'] == 'saw_cuts'), None)
        self.assertIsNotNone(saw_hit)
        self.assertEqual(saw_hit['kw'], '截成相等的 3 段')

        # 8b) extract_read_aloud_target classical poem fallback from solved step board when question only quotes 1-char “绿”
        cn_short_q = '【古诗赏析】王安石《泊船瓜洲》名句中“绿”字好在哪里？'
        cn_ra_board = tutor_mod.extract_read_aloud_target(cn_short_q, [
            {'step': 1, 'board': '原句：“春风又绿江南岸，明月何时照我还。”'},
            {'step': 4, 'board': '炼字赏析：化静为动写出春回大地生机 ✓'},
        ])
        self.assertIsNotNone(cn_ra_board)
        self.assertEqual(cn_ra_board['kind'], 'classical_cn')
        self.assertIn('春风 / 又绿 / 江南岸', cn_ra_board['display'])

        # 8c) PhoneStudioManager.public_state top-level page_url seq matches per-page seq across select_page
        self.eng.phone_studio.clear_page(clear_all=True)
        pid_a = self.eng.phone_studio.save_page(tiny_jpg, client_key='ck_r36_a')
        pid_b = self.eng.phone_studio.save_page(tiny_jpg, client_key='ck_r36_b')
        self.eng.phone_studio.select_page(pid_a)
        ps_r36 = self.eng.phone_studio.public_state()
        pg_a_entry = next(p for p in ps_r36['pages'] if p['id'] == pid_a)
        self.assertEqual(ps_r36['page_url'], pg_a_entry['page_url'], 'Top-level page_url must match active page entry page_url!')
        self.assertNotEqual(pid_a, pid_b)

        # 8d) Writing/essay critique alt_method & archetype_map return writing-appropriate structures
        await self.client.post('/api/tutor/progress', headers=H, json={'id': sid_es_r35, 'solved': True, 'first_try_count': 3, 'total_gates': 3})
        am_ec_res = await self.eng.get_archetype_mapping(sid_es_r35)
        self.assertTrue(am_ec_res['ok'])
        self.assertGreaterEqual(len(am_ec_res['archetype_map']['headers']), 3)
        alt_ec_res = await self.eng.generate_alt_method(sid_es_r35)
        self.assertTrue(alt_ec_res['ok'])
        self.assertTrue(bool(alt_ec_res['alt_method']['steps']))

        # 8e) Verify index.html R36 multi-subject labels, Step-0 canvas guide & progress flag preservation
        for sym36 in ('一材多用', '换种写法', '找语法标志词', '寻证析角度', 'read_aloud_ok: prevRaOk'):
            self.assertIn(sym36, idx_r28, f'Missing R36 symbol in index.html: {sym36}')

        # ---- R37 Pass-9 Audit Regression Checks ----
        # 9a) clean_math_for_tts must read 2H2O2 as 过氧化氢 (never '2水2') and Fe2O3 as 氧化铁
        tts_h2o2 = tutor_mod.clean_math_for_tts('实验：2H2O2 = 2H2O + O2↑，Fe2O3 与 Ca(OH)2')
        self.assertIn('过氧化氢', tts_h2o2)
        self.assertIn('水', tts_h2o2)
        self.assertNotIn('2水2', tts_h2o2)
        self.assertIn('氧化铁', tts_h2o2)
        self.assertIn('氢氧化钙', tts_h2o2)

        # 9b) scrub_step_spoilers on Step 3 choice mode must NOT append '草稿本列式自算：结果 = □' or '算出得数吗？', and must scrub leaked choice answer
        s3_en_choice = tutor_mod.scrub_step_spoilers({
            'step': 3,
            'title': '选出正确时态',
            'say': '看到 since 2021 标志词，横线处应该填 has lived。',
            'board': '主语 Tom 为第三人称单数\n结论是 has lived',
            'quiz': {
                'mode': 'choice',
                'q': '横线处应填哪一项？',
                'opts': ['lives', 'has lived', 'lived'],
                'ans': 1,
                'whys': ['一般现在时不对', '', '一般过去时不对'],
                'tip': '注意 since + 过去时间点搭配现在完成时',
                'tip2': '',
            },
            'draw': [],
        })
        self.assertNotIn('结果 = □', s3_en_choice['board'])
        self.assertNotIn('算出得数吗', s3_en_choice['say'])
        self.assertNotIn('has lived', s3_en_choice['board'])
        self.assertNotIn('has lived', s3_en_choice['say'])
        self.assertIn('选哪一项呢？', s3_en_choice['say'])

        # 9c) synthesize_fallback_quiz on writing & humanities produces non-math Socratic checkpoints
        fb_wr = tutor_mod.synthesize_fallback_quiz(3, {'board': '', 'title': '慢镜头升格'}, '【作文升格精批】我很开心。', 'seed_wr')
        self.assertEqual(fb_wr['mode'], 'choice')
        self.assertIn('五感', json.dumps(fb_wr['opts'], ensure_ascii=False))
        fb_en = tutor_mod.synthesize_fallback_quiz(1, {'board': '', 'title': '寻找标志词'}, '【英语语法】Tom ___ here since 2021.', 'seed_en')
        self.assertNotIn('数字相加', json.dumps(fb_en['opts'], ensure_ascii=False))

        # 9d) extract_read_aloud_target strips inner emphasis quotes & supports unquoted 5/7-char couplets on board/say, & api_mobile_state exposes writing_task/subject
        cn_inner_q = tutor_mod.extract_read_aloud_target(
            '【古诗赏析】王安石《泊船瓜洲》名句中“绿”字好在哪里？',
            [{'step': 1, 'board': '名句赏析：“春风又“绿”江南岸，明月何时照我还。”'}],
        )
        self.assertIsNotNone(cn_inner_q)
        self.assertIn('春风 / 又绿 / 江南岸', cn_inner_q['display'])
        cn_unq = tutor_mod.extract_read_aloud_target(
            '【古诗赏析】王安石《泊船瓜洲》名句中“绿”字好在哪里？',
            [{'step': 1, 'board': '颔联：“春风又（绿）江南岸”与尾句“明月何时照我还。”'}],
        )
        self.assertIsNotNone(cn_unq)
        self.assertIn('春风 / 又绿 / 江南岸', cn_unq['display'])
        cn_7line = tutor_mod.extract_read_aloud_target(
            '【古诗赏析】王安石《泊船瓜洲》名句中“绿”字好在哪里？',
            [{'step': 1, 'board': '春风又绿江南岸\n关注字词怎样描绘景物'}],
        )
        self.assertIsNotNone(cn_7line)
        self.assertIn('春风 / 又绿 / 江南岸', cn_7line['display'])
        sc_poem0 = tutor_mod.build_step0_scaffold('【古诗赏析】王安石《泊船瓜洲》名句中“绿”字好在哪里？')
        self.assertNotIn('古诗赏析', sc_poem0['say'])
        self.assertIn('抓线索与设问', sc_poem0['title'])
        self.assertNotIn('【古诗赏析】', json.dumps(sc_poem0['draw'], ensure_ascii=False))

        wt_sess_r37 = self.eng._new_session_record('作文立意', '请以《那一刻我长大了》为题写一篇记叙文（不少于500字）', 'fast', 'custom')
        wt_sess_r37['status'] = 'ready'
        self.eng.active_session_id = wt_sess_r37['id']
        r_ms37 = await self.client.get('/api/tutor/mobile/state', headers={**H, 'X-TS-Internal': '1'})
        j_ms37 = await r_ms37.json()
        self.assertEqual(j_ms37['active_lesson']['writing_task'], 'chinese_essay')
        self.assertEqual(j_ms37['active_lesson']['subject'], 'chinese')
        mob_r37 = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('已完成构思升格', mob_r37)
        self.assertIn('思路梳理与规范作答区：', mob_r37)

        # 9e) prepare_print_sheet tags non-calculation desensitized items with '同考新编·' and excludes active writing sessions
        self.eng.mistakes.clear()
        self.eng.active_session_id = wt_sess_r37['id']
        ps_excl = await self.eng.prepare_print_sheet(sheet_mode='original')
        self.assertTrue(all('那一刻我长大了' not in it['question'] for it in ps_excl['items']))
        self.eng.mistakes.insert(0, {
            'id': 'mb_r37_en_tag',
            'title': '现在完成时辨析',
            'question': '【英语语法】Tom ___ here since 2021.',
            'subject': 'english',
            'error_tag': '时态混淆',
            'graduated': False,
            'steps': [{'step': 3, 'quiz': {'mode': 'choice', 'opts': ['lives', 'has lived'], 'ans': 1}}, {'step': 4, 'board': 'has lived ✓'}],
        })
        ps_en37 = await self.eng.prepare_print_sheet(ids=['mb_r37_en_tag'], sheet_mode='desensitized')
        self.assertTrue(ps_en37['items'][0]['tag'].startswith('同考新编·'), f"Expected 同考新编· tag, got {ps_en37['items'][0]['tag']}")

        # ---- R38 Pass-10 Audit Regression Checks ----
        # 10a) clean_math_for_tts must NOT corrupt subscript 'H₂O' -> '氢气O', 'SO₂' -> 'S氧气', or 'NO₂' -> 'N氧气'
        tts_sub_chem = tutor_mod.clean_math_for_tts('反应：2H₂O₂ = 2H₂O + O₂↑，尾气含 SO₂ 与 NO₂ 及 CaCl₂')
        self.assertIn('过氧化氢', tts_sub_chem)
        self.assertIn('水', tts_sub_chem)
        self.assertIn('氧气', tts_sub_chem)
        self.assertIn('二氧化硫', tts_sub_chem)
        self.assertIn('二氧化氮', tts_sub_chem)
        self.assertIn('氯化钙', tts_sub_chem)
        self.assertNotIn('氢气O', tts_sub_chem)
        self.assertNotIn('S氧气', tts_sub_chem)
        self.assertNotIn('N氧气', tts_sub_chem)

        # 10b) detect_polya_reduction_candidate & detect_calc_microscope zero false positives on '(2025·中考)', '...', & '3.1 如图'
        self.assertFalse(tutor_mod.detect_polya_reduction_candidate('（2025·北京中考）如图，在 △ABC 中，AB = AC，∠A = 40°，求 ∠B。'))
        self.assertFalse(tutor_mod.detect_polya_reduction_candidate('【英语完形】Tom looked at the map... and asked for help.'))
        self.assertTrue(tutor_mod.detect_polya_reduction_candidate('用火柴棒按规律摆图形：第 1 个用 4 根，第 2 个用 7 根……照这样摆下去，第 2026 个图形要几根？'))
        self.assertTrue(tutor_mod.detect_polya_reduction_candidate('计算：1 + 2 + 4 + …… + 2026'))
        self.assertIsNone(tutor_mod.detect_calc_microscope('3.1 如图，在 △ABC 中，∠C = 90°，求斜边长。'))
        self.assertIsNone(tutor_mod.detect_calc_microscope('长方形花园长 12.5 米，宽 8 米，求面积是多少平方米？'))
        self.assertIsNotNone(tutor_mod.detect_calc_microscope('妈妈买苹果花了 12.5 元，买草莓花了 3.75 元，一共花了多少元？'))
        self.assertIsNotNone(tutor_mod.detect_calc_microscope('列竖式计算：15.6 - 4.82 ='))

        # 10c) POST /api/tutor/progress with solved=True immediately returns read_aloud_target & clever_calc for live Step-4 UI unlock!
        en_live_s38 = self.eng._new_session_record('完成时微课', '【英语语法】Tom ___ (live) here since 2021.', 'fast', 'custom')
        en_live_s38['status'] = 'ready'
        en_live_s38['steps'] = [
            {'step': 3, 'title': '选词形', 'say': '选哪项？', 'board': 'Tom + has lived', 'quiz': {'mode': 'choice', 'q': '填哪项？', 'opts': ['lives', 'has lived', 'lived'], 'ans': 1}, 'draw': []},
            {'step': 4, 'title': '复盘', 'say': '完成', 'board': 'Tom has lived here since 2021. ✓\n巧算验证：25 × 36 = 900', 'quiz': None, 'draw': []},
        ]
        r_pr38 = await self.client.post('/api/tutor/progress', headers=H, json={'id': en_live_s38['id'], 'solved': True, 'first_try_count': 3, 'total_gates': 3})
        j_pr38 = await r_pr38.json()
        self.assertIn('read_aloud_target', j_pr38)
        self.assertEqual(j_pr38['read_aloud_target']['kind'], 'english')
        self.assertIn('has lived', j_pr38['read_aloud_target']['text'])
        self.assertIn('clever_calc', j_pr38)

        # 10d) get_exam_rubric on biology & geography uses dedicated science rubric (not elementary arithmetic!)
        bio_s38 = self.eng._new_session_record('叶片光合作用', '【初中生物】绿色植物进行光合作用的主要场所是叶绿体还是线粒体？', 'fast', 'custom')
        bio_s38['status'] = 'ready'
        bio_s38['progress'] = {'solved': True}
        bio_s38['steps'] = [{'step': 4, 'title': '复盘', 'say': '完成', 'board': '叶绿体 ✓', 'quiz': None, 'draw': []}]
        rub_bio38 = await self.eng.get_exam_rubric(bio_s38['id'])
        self.assertTrue(rub_bio38['ok'])
        idx_r38_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('res.read_aloud_target', idx_r38_txt)
        self.assertIn('① 圈出题干标志词', idx_r38_txt)

        # ---- R39 Pass-11 Audit Regression Checks ----
        # 11a) scrub_locked_spoilers_from_qa: short 2~3 char choice answers scrubbed in giveaway phrases without polluting stem mentions, & ensure_question_end=False keeps principle clean
        unsolved_r39 = [{
            'step': 2,
            'board': '等高线向高处凸出',
            'quiz': {'mode': 'choice', 'q': '判断是山脊还是山谷？', 'opts': ['A. 山脊', 'B. 山谷'], 'ans': 1},
        }]
        qa_r39 = tutor_mod.scrub_locked_spoilers_from_qa({
            'title': '读图点拨',
            'say': '判断是山脊还是山谷要先看凸向，所以此处答案是山谷。',
            'board': '此处应该选山谷',
            'note': '结论是山谷',
        }, unsolved_r39)
        self.assertIn('判断是山脊还是山谷', qa_r39['say'])
        self.assertNotIn('答案是山谷', qa_r39['say'])
        self.assertNotIn('应该选山谷', qa_r39['board'])
        self.assertNotIn('结论是山谷', qa_r39['note'])
        self.assertTrue(qa_r39['say'].endswith('？'))
        cap_scrub_r39 = tutor_mod.scrub_locked_spoilers_from_qa({
            'title': '概念微课',
            'say': '凸高为谷、凸低为脊是等高线地形图的核心判读法则。',
            'board': '小例子',
            'note': '回原题看弯曲方向',
        }, unsolved_r39, formula_veiled=True, strict_no_result=True, ensure_question_end=False)
        self.assertNotIn('顺着这个线索你来试着推下一步好吗', cap_scrub_r39['say'])
        self.assertTrue(tutor_mod._is_answer_begging('这道英语题横线处填什么？'))

        # 11b) evaluate_feynman_explanation gates unsolved & unpeeked sessions; reset_run=True resets active session without wiping history.json
        unsolved_feyn_s39 = self.eng._new_session_record('未闯关会话', '把长 24 厘米铁丝围成正方形，面积是多少？', 'fast', 'custom')
        unsolved_feyn_s39['status'] = 'ready'
        unsolved_feyn_s39['steps'] = [{'step': 1, 'title': '审题', 'say': '看题', 'board': '周长24', 'quiz': {'mode': 'num', 'q': '边长？', 'num_ans': '6'}, 'draw': []}]
        r_feyn_gate39 = await self.client.post('/api/tutor/feynman', headers=H, json={'id': unsolved_feyn_s39['id'], 'text': '边长是6面积是36'})
        self.assertEqual(r_feyn_gate39.status, 400)
        self.assertFalse((self.eng.public_session(unsolved_feyn_s39['id'])['progress'] or {}).get('solved'))

        # Activate via GET /api/tutor/session?activate=1 & reset_run=True via POST /api/tutor/progress
        r_act39 = await self.client.get(f"/api/tutor/session?id={en_live_s38['id']}&activate=1", headers=H)
        self.assertEqual(r_act39.status, 200)
        self.assertEqual(self.eng.active_session_id, en_live_s38['id'])
        r_rst39 = await self.client.post('/api/tutor/progress', headers=H, json={'id': en_live_s38['id'], 'reset_run': True, 'current_step': 1})
        self.assertEqual((await r_rst39.json())['ok'], True)
        self.assertFalse(self.eng.public_session(en_live_s38['id'])['progress']['solved'])
        hist_saved38 = next((h for h in self.eng.history if h.get('id') == en_live_s38['id']), None)
        if hist_saved38:
            self.assertTrue(hist_saved38['progress']['solved'])

        # 11c) prepare_print_sheet _extract_check_key ignores Step-4 boilerplate '核对无误 ✓' & error_tag
        self.eng.mistakes.insert(0, {
            'id': 'mb_r39_boiler',
            'title': '无答案缓存错题',
            'question': '测试原题：求长方形周长。',
            'subject': 'math',
            'error_tag': '曾看家长全览',
            'stage': 0,
            'graduated': False,
            'next_review_at': 0,
            'steps': [{'step': 4, 'board': '复盘核验：代回原题条件核对单位与等量关系 ✓'}],
        })
        ps_orig39 = await self.eng.prepare_print_sheet(ids=['mb_r39_boiler'], sheet_mode='original')
        self.assertEqual(ps_orig39['items'][0]['ans_check'], '请结合原题独立验算核对')
        idx_r39_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('reset_run: true', idx_r39_txt)
        self.assertIn('!isFeynmanClosedBook && activeSumPanel ===', idx_r39_txt)

        # ---- R40 Pass-12 Audit Regression Checks ----
        # 12a) Cross-subject false-positive guards: reading_traps, motion_sim, split_multipart_subquestions, calc_microscope, geo_model
        self.assertEqual(tutor_mod.detect_reading_traps('【作文升格精批】请批改习作：妈妈在病房走廊来回踱步了1小时20分钟。'), [])
        self.assertEqual(tutor_mod.detect_reading_traps('【语文阅读理解】母亲在院子里来回忙碌了2小时15分钟，表现了怎样的情感？'), [])
        rev_h_traps = tutor_mod.detect_reading_traps('【初中历史】下列关于洋务运动的表述不正确的一项是？')
        self.assertEqual(len(rev_h_traps), 1)
        self.assertEqual(rev_h_traps[0]['kind'], 'reverse_ask')
        self.assertIsNone(tutor_mod.detect_motion_sim('【语文阅读理解】赏析句子：“火车驶过长江大桥，春风迎面吹来。”'))
        self.assertIsNotNone(tutor_mod.detect_motion_sim('一列火车长 200 米，以每秒 25 米的速度通过一座长 800 米的大桥，需要多少秒？'))
        self.assertIsNone(tutor_mod.split_multipart_subquestions('【语文作文】请以《成长的足迹》为题写一篇记叙文：(1) 写出你如何克服困难的过程；(2) 不少于500字。'))

        # 12b) clean_math_for_tts fixes dm²/km² ordering, ℃/°C, science units (6V, 32g, 50mL, 10N/kg), & strips decorative UI emojis
        tts_r40 = tutor_mod.clean_math_for_tts('🤔 面积 36dm²，陆地 9.6km²，气温从 24°C 下降 0.6℃，电压 6V，电流 0.5A，生成 32g 氧气，量取 50mL 水，g=10N/kg')
        self.assertNotIn('🤔', tts_r40)
        self.assertIn('36平方分米', tts_r40)
        self.assertNotIn('d平方米', tts_r40)
        self.assertIn('9.6平方千米', tts_r40)
        self.assertNotIn('k平方米', tts_r40)
        self.assertIn('24摄氏度', tts_r40)
        self.assertNotIn('度C', tts_r40)
        self.assertIn('0.6摄氏度', tts_r40)
        self.assertIn('6伏', tts_r40)
        self.assertIn('0.5安', tts_r40)
        self.assertIn('32克', tts_r40)
        self.assertIn('50毫升', tts_r40)
        self.assertIn('10牛每千克', tts_r40)

        # 12c) sanitize_quiz strips redundant A./B./C. option prefixes before shuffling while preserving 'A useful book' & 'A:B = 2:3'
        sq_pref40 = tutor_mod.sanitize_quiz({
            'q': '等高线向高处凸出是什么地形？',
            'opts': ['A. 山脊', 'B. 山谷', 'C. 鞍部'],
            'ans': 1,
        }, step_num=2, rng_seed='r40_seed')
        self.assertEqual(set(sq_pref40['opts']), {'山脊', '山谷', '鞍部'})
        self.assertEqual(sq_pref40['opts'][sq_pref40['ans']], '山谷')
        sq_art40 = tutor_mod.sanitize_quiz({
            'q': 'Choose the right phrase:',
            'opts': ['A useful book', 'An useful book', 'The useful'],
            'ans': 0,
            'no_shuffle': True,
        }, step_num=2)
        self.assertEqual(sq_art40['opts'][0], 'A useful book')
        sq_ratio40 = tutor_mod.sanitize_quiz({
            'q': '求甲乙之比：',
            'opts': ['A:B = 2:3', 'A:B = 3:2', 'A:B = 1:2'],
            'ans': 0,
            'no_shuffle': True,
        }, step_num=2)
        self.assertEqual(sq_ratio40['opts'][0], 'A:B = 2:3')

        # 12d) enrich_step_draw_scaffolds infers tap_target on 2-point chemistry/geography diagrams; Step-0 splits declarative humanities goal
        chem_s2_40 = tutor_mod.enrich_step_draw_scaffolds(
            [{'step': 1, 'draw': [
                {'op': 'rect', 'x': 240, 'y': 120, 'w': 180, 'h': 200, 'label': '装满水的集气瓶'},
                {'op': 'point', 'x': 290, 'y': 110, 'label': 'A(长管)'},
                {'op': 'point', 'x': 370, 'y': 110, 'label': 'B(短管)'},
            ]}],
            {
                'step': 2,
                'board': '排水集气看密度',
                'say': '氧气应从哪端通入？',
                'draw': [{'op': 'line', 'x1': 290, 'y1': 90, 'x2': 290, 'y2': 290}],
                'quiz': {'mode': 'choice', 'q': '氧气应从哪端通入？', 'opts': ['从长导管点A通入', '从短导管点B通入', '两端都行'], 'ans': 1},
            },
        )
        self.assertEqual(chem_s2_40['quiz'].get('tap_target'), 'B')
        w0_en40 = tutor_mod.build_step0_warmup_draw('【英语语法】Tom ___ (live) in Shanghai since 2021，请判断横线处应填哪一项，并讲清原因。')
        w0_en_goal = next((o['label'] for o in w0_en40 if o.get('label', '').startswith('目标：')), '')
        self.assertIn('请判断横线处', w0_en_goal)
        w0_geo40 = tutor_mod.build_step0_warmup_draw('【初中地理】读某地等高线图，海拔每升高100米气温下降0.6℃，若山脚A处气温为24℃，求山峰B处的气温？')
        w0_geo_goal = next((o['label'] for o in w0_geo40 if o.get('label', '').startswith('目标：')), '')
        self.assertIn('求山峰B处', w0_geo_goal)

        # 12e) scrub_locked_spoilers_from_qa scrubs draw labels/rows & advance_subquestion preserves teach_mode
        qa_dr40 = tutor_mod.scrub_locked_spoilers_from_qa(
            {
                'title': '概念微课',
                'say': '先看基本公式。',
                'board': '小例子',
                'note': '回原题',
                'draw': [{'op': 'table', 'x': 100, 'y': 100, 'w': 400, 'headers': ['项', '值'], 'rows': [['n = 1', '4'], ['原题面积', '288']], 'label': '面积=288'}],
            },
            [{'step': 3, 'quiz': {'mode': 'num', 'num_ans': '288'}}],
            strict_no_result=True,
            ensure_question_end=False,
        )
        self.assertIn('n = 1', qa_dr40['draw'][0]['rows'][0][0])
        self.assertNotIn('288', json.dumps(qa_dr40['draw'], ensure_ascii=False))
        sq_dm_sess = self.eng._new_session_record('草稿多问', '甲乙两车同时从相距 450 千米两地相对开出，3 小时相遇，甲每小时行 80 千米：(1) 求乙车速度；(2) 求相遇时甲比乙多行多少千米？', 'fast', 'phone_crop')
        sq_dm_sess['teach_mode'] = 'diagnose_draft'
        sq_dm_sess['subq_meta'] = {'full_question': sq_dm_sess['question'], 'stem': '甲乙两车相遇', 'parts': [{'idx': 1, 'label': '第(1)问', 'text': '求乙车速度'}, {'idx': 2, 'label': '第(2)问', 'text': '求甲比乙多行多少'}], 'cur_idx': 1, 'total': 2, 'proven': []}
        adv_dm = await self.eng.advance_subquestion(sq_dm_sess['id'], solo_challenge=True)
        self.assertEqual(adv_dm['session']['teach_mode'], 'diagnose_draft')
        self.assertFalse(adv_dm['session']['solo_challenge'])
        idx_r40_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('isAsciiOpt', idx_r40_txt)

        # ---- R41 Pass-13 Audit Regression Checks ----
        # 13a) get_foundation_capsule isolates math interval/shape templates from essays/humanities & scopes physics/chemistry capsules
        essay_tree_s41 = self.eng._new_session_record('植树节习作', '【语文作文】植树节那天我们在学校路灯旁栽树，请写一篇不少于500字的记叙文。', 'fast', 'custom')
        essay_tree_s41['status'] = 'ready'
        cap_essay41 = await self.eng.get_foundation_capsule(essay_tree_s41['id'])
        self.assertTrue(cap_essay41['ok'])
        self.assertNotIn('间隔段数', cap_essay41['capsule']['title'] + cap_essay41['capsule']['example'])

        phys_mech_s41 = self.eng._new_session_record('浮力阿基米德', '【初中物理】将一个体积为 100cm³ 的铁块浸没在水中，求铁块受到的浮力 F浮 是多少牛？', 'fast', 'custom')
        phys_mech_s41['status'] = 'ready'
        cap_mech41 = await self.eng.get_foundation_capsule(phys_mech_s41['id'])
        self.assertTrue(cap_mech41['ok'])
        self.assertNotIn('欧姆定律', cap_mech41['capsule']['title'])

        chem_bottle_s41 = self.eng._new_session_record('排水法集气', '【初中化学】若用装满水的洗气瓶（长导管A、短导管B）排水法收集氧气，气体应从哪端通入？', 'fast', 'custom')
        chem_bottle_s41['status'] = 'ready'
        chem_bottle_s41['steps'] = [{'step': 2, 'board': '排水集气', 'quiz': {'mode': 'choice', 'q': '哪端通入？', 'opts': ['长导管A', '短导管B'], 'ans': 1}}]
        cap_chem41 = await self.eng.get_foundation_capsule(chem_bottle_s41['id'])
        self.assertTrue(cap_chem41['ok'])
        self.assertNotIn('短进长出', cap_chem41['capsule']['principle'])

        # 13b) Parent Full-View (peeked=True) unlocks exam_rubric / alt_method / archetype_map & mobile state 'peeked', while fresh unsolved stays 400
        peek_s41 = self.eng._new_session_record('全览测试', '【英语语法】She ___ (keep) this book since 2022.', 'fast', 'custom')
        peek_s41['status'] = 'ready'
        peek_s41['steps'] = [
            {'step': 3, 'title': '选时态', 'say': '选哪项？', 'board': 'has kept', 'quiz': {'mode': 'choice', 'q': '填哪项？', 'opts': ['keeps', 'has kept'], 'ans': 1}, 'draw': []},
            {'step': 4, 'title': '复盘', 'say': '完成', 'board': 'She has kept this book since 2022. ✓', 'quiz': None, 'draw': []},
        ]
        r_pre_peek = await self.client.post('/api/tutor/exam_rubric', headers=H, json={'id': peek_s41['id']})
        self.assertEqual(r_pre_peek.status, 400)
        r_pk_prog = await self.client.post('/api/tutor/progress', headers=H, json={'id': peek_s41['id'], 'peeked': True})
        j_pk_prog = await r_pk_prog.json()
        self.assertIn('read_aloud_target', j_pk_prog)
        r_post_peek_rub = await self.client.post('/api/tutor/exam_rubric', headers=H, json={'id': peek_s41['id']})
        self.assertEqual(r_post_peek_rub.status, 200)
        r_post_peek_alt = await self.client.post('/api/tutor/alt_method', headers=H, json={'id': peek_s41['id']})
        self.assertEqual(r_post_peek_alt.status, 200)
        r_post_peek_arch = await self.client.post('/api/tutor/archetype_map', headers=H, json={'id': peek_s41['id']})
        self.assertEqual(r_post_peek_arch.status, 200)
        self.eng.active_session_id = peek_s41['id']
        r_ms41 = await self.client.get('/api/tutor/mobile/state', headers={**H, 'X-TS-Internal': '1'})
        self.assertTrue((await r_ms41.json())['active_lesson']['peeked'])

        # 13c) solo_ace with total_gates=0 is never enrolled in mistake_book & counts as clean_pass in sprint
        solo_s41 = self.eng._new_session_record('秒杀零关卡', '把长 36 厘米铁丝围成正方形，边长是多少厘米？', 'fast', 'custom')
        solo_s41['status'] = 'ready'
        solo_s41['steps'] = [{'step': 4, 'title': '复盘', 'say': '9厘米', 'board': '36 ÷ 4 = 9 ✓', 'quiz': None, 'draw': []}]
        r_solo_pr41 = await self.client.post('/api/tutor/progress', headers=H, json={'id': solo_s41['id'], 'solved': True, 'solo_ace': True, 'solo_sec': 8, 'first_try_count': 0, 'total_gates': 0, 'weak_step': 0, 'peeked': False})
        j_solo_pr41 = await r_solo_pr41.json()
        self.assertNotIn('mistake_event', j_solo_pr41)

        # 13d) diagnose_draft text multi-part auto-splits on start_live_problem & frontend/mobile R41 guards present
        dm_mp41 = self.eng.start_live_problem(
            question='甲、乙两车从相距 360 千米两地同时相向开出，3 小时相遇，甲每小时行 70 千米：(1) 求乙车速度；(2) 相遇时甲比乙多行多少千米？',
            mode='fast',
            teach_mode='diagnose_draft',
        )
        self.assertIsNotNone(dm_mp41.get('subq_meta'))
        self.assertEqual(dm_mp41['teach_mode'], 'diagnose_draft')
        self.eng._cancel_previous_session_work()
        idx_r41_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r41_txt = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('isSciTap', idx_r41_txt)
        self.assertIn('prevRawLbl', idx_r41_txt)
        self.assertIn('activeServerPageId', mob_r41_txt)

        # ---- R42 Pass-14 Audit Regression Checks ----
        # 14a) is_essay_critique_request in diagnose_draft must NOT misclassify science/geography/reading/English-blank/geometry questions as essays
        self.assertFalse(tutor_mod.is_essay_critique_request(
            '读某地等高线地形图，图中甲处等高线向高处凸出，请判断甲处是什么地形部位，并说明理由。',
            teach_mode='diagnose_draft',
        ))
        self.assertFalse(tutor_mod.is_essay_critique_request(
            '12. Look! The children ___ (sing) happily in the classroom, and their teacher is smiling.',
            teach_mode='diagnose_draft',
        ))
        self.assertFalse(tutor_mod.is_essay_critique_request(
            '如图所示，闭合开关后滑动变阻器滑片向右移动，电流表和电压表的示数分别如何变化？',
            teach_mode='diagnose_draft',
        ))
        self.assertFalse(tutor_mod.is_essay_critique_request(
            '阅读短文第3自然段，“小草偷偷地从土里钻出来”，请赏析加点字“钻”的表达效果。',
            teach_mode='diagnose_draft',
        ))
        self.assertTrue(tutor_mod.is_essay_critique_request(
            'Last Sunday I met a well-known teacher at the park, and we talked happily all afternoon.',
            teach_mode='diagnose_draft',
        ))
        self.assertIsNone(tutor_mod.extract_read_aloud_target(
            '【作文升格精批】帮我润色这段英文短文：Last Sunday I go to the park and play football.',
        ))

        # 14b) clean_math_for_tts reads 2N2/2N₂ as 2氮气 (never 2牛2), kPa as 千帕, Cl2 as 氯气, and preserves 2V2/2A1 & '5 A boy'
        tts_r42 = tutor_mod.clean_math_for_tts('合成氨：N2 + 3H2 = 2NH3，空气含 2N₂ 与 Cl₂，气压 101kPa，U1=2V2，表 2A1，6V 与 0.5A，Page 5 A boy')
        self.assertIn('2氮气', tts_r42)
        self.assertNotIn('2牛', tts_r42)
        self.assertIn('氯气', tts_r42)
        self.assertIn('101千帕', tts_r42)
        self.assertIn('2V2', tts_r42)
        self.assertNotIn('2伏2', tts_r42)
        self.assertIn('2A1', tts_r42)
        self.assertNotIn('2安1', tts_r42)
        self.assertIn('6伏', tts_r42)
        self.assertIn('0.5安', tts_r42)
        self.assertIn('Page 5 A boy', tts_r42)

        # 14c) detect_calc_microscope excludes chemistry equations & pure function graphs while keeping math equations
        self.assertIsNone(tutor_mod.detect_calc_microscope('【初中化学】写出过氧化氢分解的化学方程式并解释原因：2H2O2 = 2H2O + O2↑'))
        self.assertIsNone(tutor_mod.detect_calc_microscope('已知二次函数 y = x² - 4x + 3 的图象与 x 轴交于 A、B 两点，求顶点坐标。'))
        self.assertEqual(tutor_mod.detect_calc_microscope('解方程：2x - 6 = 10')['kind'], 'eq_transpose')

        # 14d) enrich_step_draw_scaffolds honors {'op': 'clear'} so cleared Step-1 points never leak tap_target onto redrawn Step-2
        cleared_s2_42 = tutor_mod.enrich_step_draw_scaffolds(
            [{'step': 1, 'draw': [{'op': 'point', 'x': 200, 'y': 120, 'label': 'A'}, {'op': 'point', 'x': 400, 'y': 120, 'label': 'B'}]}],
            {
                'step': 2,
                'board': '切换线段图',
                'say': '看新图',
                'draw': [{'op': 'clear'}, {'op': 'seg_bar', 'x': 120, 'y': 200, 'w': 300, 'parts': 3, 'label': '共3份'}],
                'quiz': {'mode': 'choice', 'q': '应过哪点？', 'opts': ['过点A分析', '选B', '选C'], 'ans': 0},
            },
        )
        self.assertNotIn('tap_target', cleared_s2_42['quiz'])

        # 14e) Parent Full-View (peeked=True) unlocks evaluate_read_aloud, preserves unsolved state on Feynman check, & public_session exposes pedagogy
        peek_s41['pedagogy'] = 'elem'
        pub_pk42 = self.eng.public_session(peek_s41['id'])
        self.assertEqual(pub_pk42['pedagogy'], 'elem')
        r_ra_pk42 = await self.client.post('/api/tutor/read_aloud', headers=H, json={'id': peek_s41['id'], 'text': 'She has kept this book since 2022.'})
        self.assertEqual(r_ra_pk42.status, 200)
        self.assertTrue((await r_ra_pk42.json())['read_aloud']['pass'])
        r_fy_pk42 = await self.client.post('/api/tutor/feynman', headers=H, json={'id': peek_s41['id'], 'text': '因为句中有 since 2022 表示从过去持续到现在，所以用现在完成时 has kept'})
        self.assertEqual(r_fy_pk42.status, 200)
        self.assertFalse(self.eng.public_session(peek_s41['id'])['progress'].get('solved'), 'Peeked Feynman check must not falsely mark unsolved steps as solved!')

        idx_r42_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r42_txt = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('curStRelock', idx_r42_txt)
        self.assertIn('wasFeynRec', idx_r42_txt)
        self.assertIn('已完成浏览（曾开启家长全览', mob_r42_txt)

        # ---- R43 Pass-15 Audit Regression Checks ----
        # 15a) _build_rescue_opts preserves 2-decimal precision & reduced fraction distractors without visual format giveaways
        dec2_opts = tutor_mod._build_rescue_opts('3.75', '元')
        self.assertEqual(len(dec2_opts), 3)
        self.assertTrue(all(re.match(r'^\d+\.\d{2} 元$', x) for x in dec2_opts), f'Expected 2-decimal uniformity: {dec2_opts}')
        sub1_opts = tutor_mod._build_rescue_opts('0.25', '米')
        self.assertTrue(all(re.match(r'^\d+\.\d{2} 米$', x) for x in sub1_opts), f'Expected 2-decimal sub-1 uniformity: {sub1_opts}')
        frac_opts = tutor_mod._build_rescue_opts('3/5', '')
        self.assertEqual(len(frac_opts), 3)
        self.assertTrue(all(re.match(r'^\d+/[2-9]\d*$', x) for x in frac_opts), f'Expected reduced fraction distractors: {frac_opts}')
        half_opts = tutor_mod._build_rescue_opts('1/2', '')
        self.assertTrue(all(re.match(r'^\d+/[2-9]\d*$', x) for x in half_opts), f'Expected reduced fraction distractors for 1/2: {half_opts}')

        # 15b) sanitize_quiz replaces ASCII formula-stuffed options on numeric checkpoints with clean rescue options
        qz_ascii_f = tutor_mod.sanitize_quiz(
            {'q': '算出总价', 'num_ans': '3.75', 'unit': '元', 'opts': ['2.50 + 1.25', '5.00 - 1.25', '2.50 * 2']},
            step_num=3, rng_seed='r43',
        )
        self.assertTrue(all('+' not in o and '*' not in o for o in qz_ascii_f['opts']), qz_ascii_f['opts'])

        # 15c) api_tool voice text-only essay critique with '作文' + '批改/草稿' routes to diagnose_draft & essay_critique=True
        r_vt43 = await self.client.post(
            '/api/tutor/tool',
            headers=H_INT,
            json={'action': 'solve', 'question': '帮我批改润色这篇半命题作文草稿：那天放学下着大雨，我背起书包往家跑，妈妈撑着伞站在校门口等我。'},
        )
        self.assertEqual(r_vt43.status, 200)
        sid_vt43 = (await r_vt43.json())['session_id']
        pub_vt43 = self.eng.public_session(sid_vt43)
        self.assertEqual(pub_vt43['teach_mode'], 'diagnose_draft')
        self.assertTrue(pub_vt43['essay_critique'])

        # 15d) Frontend & Mobile R43 static guards (temperature plausibility exclusion, interval off-by-one scope, 600 maxlength, idle reset)
        self.assertIn('isTempUnit', idx_r42_txt)
        self.assertIn('isIntervalQ', idx_r42_txt)
        self.assertIn('maxlength="600"', idx_r42_txt)
        self.assertIn('maxlength="600"', mob_r42_txt)
        self.assertIn('大屏当前空闲，拍一道题或输入文字即可在家里电子黑板开启 4 步启发式引导。', mob_r42_txt)

        # ---- R45 Full-Scenario Simulation Audit Regression Checks ----
        # 16a) _asr_endpoint strips trailing /audio/transcriptions so full MacASR URLs never double-append into 404
        with mock.patch.object(
            self.eng, '_load_gateway_cfg',
            return_value={'ASR': {'MacASR': {'base_url': 'http://127.0.0.1:19000/abc/v1/audio/transcriptions', 'api_key': 'k', 'model_name': 'Qwen3-ASR-1.7B'}}},
        ):
            v1_asr, _, m_asr = self.eng._asr_endpoint()
            self.assertEqual(v1_asr, 'http://127.0.0.1:19000/abc/v1')
            self.assertEqual(m_asr, 'Qwen3-ASR-1.7B')

        # 16b) Priority TTS slot wakes priority=True user requests ahead of queued priority=False background prefetch!
        order_log = []
        hold_ev = asyncio.Event()

        async def _worker_slot(name: str, prio: bool, wait_hold: bool = False):
            async with self.eng._acquire_tts_slot(priority=prio):
                order_log.append(name)
                if wait_hold:
                    await hold_ev.wait()

        t_hold = asyncio.create_task(_worker_slot('step0', False, wait_hold=True))
        await asyncio.sleep(0.01)
        t_lo = asyncio.create_task(_worker_slot('prefetch_step2', False))
        await asyncio.sleep(0.01)
        t_hi = asyncio.create_task(_worker_slot('live_hint', True))
        await asyncio.sleep(0.01)
        hold_ev.set()
        await asyncio.gather(t_hold, t_lo, t_hi)
        self.assertEqual(order_log, ['step0', 'live_hint', 'prefetch_step2'], f'Priority TTS slot failed: {order_log}')

        idx_r45_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        self.assertIn('if (!quizGateShownAt) quizGateShownAt = performance.now();', idx_r45_txt)
        self.assertIn('if (audioEl.src === SILENT_WAV_URI) audioEl.pause();', idx_r45_txt)

        # ---- R46 Real-Scenario Multi-Pass Audit Regression Checks ----
        # 17a) sanitize_quiz normalizes Unicode minus & glued units ('68g', '−1.8℃') and demotes non-keypad √/π answers to choice mode
        qz_glued = tutor_mod.sanitize_quiz({'q': '需多少克过氧化氢？', 'num_ans': '68g', 'unit': ''}, 3, 'r46_g')
        self.assertEqual(qz_glued['mode'], 'num')
        self.assertEqual(qz_glued['num_ans'], '68')
        self.assertEqual(qz_glued['unit'], 'g')

        qz_uminus = tutor_mod.sanitize_quiz({'q': '山顶气温是多少？', 'num_ans': '−1.8℃', 'unit': ''}, 3, 'r46_t')
        self.assertEqual(qz_uminus['mode'], 'num')
        self.assertEqual(qz_uminus['num_ans'], '-1.8')
        self.assertEqual(qz_uminus['unit'], '℃')

        qz_rad = tutor_mod.sanitize_quiz({'q': '高 h 等于多少？', 'num_ans': '2√3', 'unit': 'cm'}, 3, 'r46_r')
        self.assertEqual(qz_rad['mode'], 'choice')
        self.assertEqual(qz_rad['num_ans'], '')
        self.assertGreaterEqual(len(qz_rad['opts']), 2)
        self.assertIn('2√3', qz_rad['opts'][qz_rad['ans']])

        # 17b) scrub_step_spoilers scrubs trailing-zero decimal equivalents ('6.60' <-> '6.6') & Unicode minus / '零下' temperatures
        st_dec60 = tutor_mod.scrub_step_spoilers({
            'step': 3,
            'title': '竖式计算',
            'say': '对齐小数点相加，得数是六点六，也就是等于 6.6。',
            'board': '3.75 + 2.85 = 6.6\n化简 = 6.60',
            'draw': [{'op': 'vcalc', 'a': '3.75', 'b': '2.85', 'op_sym': '+', 'res': '6.6'}],
            'quiz': {'mode': 'num', 'q': '算出结果：', 'num_ans': '6.60', 'unit': '', 'opts': ['6.60', '5.60', '6.50'], 'ans': 0},
        })
        self.assertNotIn('= 6.6', st_dec60['board'])
        self.assertNotIn('六点六', st_dec60['say'])
        self.assertEqual(st_dec60['draw'][0]['res'], '□')

        st_neg_t = tutor_mod.scrub_step_spoilers({
            'step': 3,
            'title': '求山顶气温',
            'say': '3减去4.8，结果是零下一点八摄氏度。',
            'board': '山顶气温 = −1.8℃',
            'draw': [],
            'quiz': {'mode': 'num', 'q': '山顶气温？', 'num_ans': '-1.8', 'unit': '℃', 'opts': ['-1.8℃', '1.8℃', '-0.8℃'], 'ans': 0},
        })
        self.assertNotIn('−1.8', st_neg_t['board'])
        self.assertNotIn('零下一点八', st_neg_t['say'])

        # 17c) api_mobile_upload returns pending=True before 15s proxy cutoff if an LLM task is slow
        async def _slow_var(*_a, **_kw):
            await asyncio.sleep(0.05)
            return {'ok': True, 'session': {'id': 's_slow', 'title': '慢变式', 'status': 'streaming'}}

        with mock.patch.object(self.eng, 'generate_variant_problem', side_effect=_slow_var):
            with mock.patch.object(tutor_mod.asyncio, 'wait', new=mock.AsyncMock(return_value=(set(), set()))):
                r_pend = await self.client.post('/api/tutor/mobile/upload', headers=H_INT, json={'action': 'trigger_variant', 'session_id': 'x'})
                self.assertEqual(r_pend.status, 200)
                j_pend = await r_pend.json()
                self.assertTrue(j_pend['ok'])
                self.assertTrue(j_pend.get('pending'))
        await asyncio.sleep(0.08)

        idx_r46_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r46_txt = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('function finalizeGuidedSolve()', idx_r46_txt)
        self.assertIn('function isSpokenArithmeticExpr(', idx_r46_txt)
        self.assertIn('(?<!正)(?:负的?|零下)', idx_r46_txt)
        self.assertIn('pendingCheckPageId', mob_r46_txt)

        # ---- R47 Real-Scenario Multi-Pass Audit Regression Checks ----
        # 18a) clean_math_for_tts: unary Unicode minus -> '负' (never '减'), binary minus -> '减', '___' -> '横线处', 7/100 -> '100分之7'
        tts_neg = tutor_mod.clean_math_for_tts('山峰气温 = −1.8℃，点 A(−3, 0)，21 − 1 = 20，Tom ___ in Shanghai，占 7/100，Rt△AHO ∽ Rt△ABO，AC = ±4√3')
        self.assertIn('等于 负1.8摄氏度', tts_neg)
        self.assertIn('A(负3, 0)', tts_neg)
        self.assertIn('21 减 1', tts_neg)
        self.assertNotIn('等于 减1.8', tts_neg)
        self.assertIn('横线处', tts_neg)
        self.assertNotIn('___', tts_neg)
        self.assertIn('100分之7', tts_neg)
        self.assertIn('相似于', tts_neg)
        self.assertIn('正负4根号3', tts_neg)

        # 18b) _coerce_multiline_str & sanitize_draw_ops coordinate aliases (live diagnose_draft list-board & point/seg_bar coords)
        coerced_bd = tutor_mod._coerce_multiline_str(['原题条件：长100米', '草稿：100 ÷ 5 = 20'], 380)
        self.assertEqual(coerced_bd, '原题条件：长100米\n草稿：100 ÷ 5 = 20')
        coerced_repr = tutor_mod._coerce_multiline_str("['间隔数：100 ÷ 5 = □（个）', 'note：两端都栽加一']", 380, strip_note_prefix=True)
        self.assertNotIn("['", coerced_repr)
        self.assertIn('间隔数：100 ÷ 5 = □（个）\n口诀：两端都栽加一', coerced_repr)

        aliased_ops = tutor_mod.sanitize_draw_ops([
            {'op': 'seg_bar', 'x1': 110, 'y1': 190, 'x2': 470, 'y2': 190, 'parts': 3, 'label': '全长'},
            {'op': 'point', 'pt': [470, 190], 'label': '终点'},
        ])
        self.assertEqual(aliased_ops[0]['x'], 110.0)
        self.assertEqual(aliased_ops[0]['w'], 360.0)
        self.assertEqual(aliased_ops[1]['x'], 470.0)
        self.assertEqual(aliased_ops[1]['y'], 190.0)

        idx_r47_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r47_txt = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('function canonSpokenMathChoice(', idx_r47_txt)
        self.assertIn('function parseBoardBlankLineMeta(', idx_r47_txt)
        self.assertIn('请独立算出最终目标「', idx_r47_txt)
        self.assertIn('pendingPrevSummary', mob_r47_txt)

        # ---- R48 Real-Scenario Multi-Pass Audit Regression Checks ----
        # 19a) get_polya_reduction & scrub_locked_spoilers_from_qa: preserve n=1,2,3 toy arithmetic & given numbers (20℃) while scrubbing unsolved answers (-4, 10/3)!
        polya_sb_r48 = tutor_mod.scrub_locked_spoilers_from_qa(
            {
                'title': '🪄 波利亚降维试算',
                'say': '对比上面3个小例子，海拔每升高1000米气温怎样变化？最终答案是负四℃！',
                'board': '特例①：升高1000米，气温是20−6=14℃\n特例②：升高2000米，气温是20−12=8℃\n特例③：升高3000米，气温是20−18=2℃',
                'note': '山脚基准气温是20℃，抓准变化规律再算山顶！',
                'draw': [{'op': 'table', 'x': 90, 'y': 85, 'w': 520, 'headers': ['序号', '气温'], 'rows': [['第1个', '14℃'], ['山顶', '-4℃']], 'label': '规律表'}],
            },
            [
                {'step': 3, 'quiz': {'mode': 'num', 'num_ans': '-4', 'chain': [{'ans': '4'}, {'ans': '24'}, {'ans': '-4'}]}},
                {'step': 4, 'board': '③ 20 − 24 = −4（℃）✓\n验算：−4 + 24 = 20（℃）'},
            ],
            formula_veiled=False,
            strict_no_result=False,
            question_txt='已知海拔每升高 1000 米，气温约下降 6℃。若某山脚气温为 20℃，求海拔 4000 米山顶的气温是多少℃？',
        )
        self.assertIn('20−6=14℃', polya_sb_r48['board'])
        self.assertIn('20−12=8℃', polya_sb_r48['board'])
        self.assertIn('20℃', polya_sb_r48['note'])
        self.assertNotIn('负四', polya_sb_r48['say'])
        self.assertNotIn('-4', json.dumps(polya_sb_r48['draw'], ensure_ascii=False))

        # Fraction spoiler scrubbing ('10/3' -> '10/3', '3分之10', '三分之十')
        frac_qa_r48 = tutor_mod.scrub_locked_spoilers_from_qa(
            {'say': '最终答案等于三分之十，也就是 x = 10/3', 'board': 'x = 10/3', 'note': '结果是3分之10'},
            [{'step': 3, 'quiz': {'mode': 'num', 'num_ans': '10/3'}}],
            question_txt='解方程：(2x - 1)/3 - (x + 2)/6 = 1',
        )
        self.assertNotIn('三分之十', frac_qa_r48['say'])
        self.assertNotIn('10/3', frac_qa_r48['board'])
        self.assertNotIn('3分之10', frac_qa_r48['note'])

        # 19b) sanitize_draw_ops filters bare placeholder '升格口诀' / '防错口诀' note labels while keeping real mantras
        self.assertEqual(tutor_mod.sanitize_draw_ops([{'op': 'note', 'x': 350, 'y': 398, 'label': '升格口诀'}]), [])
        self.assertEqual(len(tutor_mod.sanitize_draw_ops([{'op': 'note', 'x': 350, 'y': 398, 'label': '慢放连续微动作，首尾回扣题眼词'}])), 1)

        # 19c) clean_math_for_tts: parenthesized algebraic fraction '(2x - 1)/3', center-dot '6·(2x−1)', subscripts 'R₁/U₂', & checkmarks
        tts_r48 = tutor_mod.clean_math_for_tts('方程 (2x - 1)/3 = 1，两边乘 6·(2x−1)，电阻 R₁ 两端电压 U₁ = 6V ✓，对比 2V₂')
        self.assertIn('3分之(2x减1)', tts_r48)
        self.assertNotIn('/3', tts_r48)
        self.assertIn('6乘(2x减1)', tts_r48)
        self.assertIn('R1', tts_r48)
        self.assertIn('U1 等于 6伏', tts_r48)
        self.assertIn('2V2', tts_r48)
        self.assertNotIn('✓', tts_r48)

        # 19d) detect_clever_calc_tip: operator-precedence guards reject '100 ÷ 25 × 8', '25 × 4²', '6 × 3 + 6 × 8 ÷ 2', '35 ÷ 5'
        self.assertIsNone(tutor_mod.detect_clever_calc_tip('100 ÷ 25 × 8 = □'))
        self.assertIsNone(tutor_mod.detect_clever_calc_tip('25 × 4² = □'))
        self.assertIsNone(tutor_mod.detect_clever_calc_tip('6 × 3 + 6 × 8 ÷ 2 = □'))
        self.assertIsNone(tutor_mod.detect_clever_calc_tip('35 ÷ 5 = □'))
        self.assertIsNotNone(tutor_mod.detect_clever_calc_tip('135 ÷ 5 = □'))

        # 19e) extract_read_aloud_target: 8-line Lvshi (>34 chars) narrows cleanly to key couplet
        lvshi_ra = tutor_mod.extract_read_aloud_target(
            '【古诗赏析】阅读杜甫《春望》：“国破山河在，城春草木深。感时花溅泪，恨别鸟惊心。烽火连三月，家书抵万金。白头搔更短，浑欲不胜簪。”请赏析“感时花溅泪”中“溅”字的妙处。'
        )
        self.assertIsNotNone(lvshi_ra)
        self.assertIn('感时花溅泪', lvshi_ra['text'])
        self.assertLessEqual(len(lvshi_ra['text']), 24)

        idx_r48_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r48_txt = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn('function extractRungTargetNoun(', idx_r48_txt)
        self.assertIn("vars['S扇'] = val", idx_r48_txt)
        self.assertIn('pendingPrevCheckSeq', mob_r48_txt)

        # ---- R49 Real-Scenario Multi-Pass Audit Regression Checks ----
        # 20a) clean_math_for_tts: chemical reaction conditions ('==MnO₂==', '==△=='), relative atomic mass ('H-1，O-16'), & ⑤/⑥ refs
        tts_chem_r49 = tutor_mod.clean_math_for_tts(
            '实验室制氧气（2H₂O₂ ==MnO₂== 2H₂O + O₂↑；2KMnO₄ ==△== K₂MnO₄ + MnO₂ + O₂↑），相对原子质量：H-1，O-16，Ca-40，编码 UTF-8，⑤ × 2 = ⑥'
        )
        self.assertIn('在二氧化锰条件下生成', tts_chem_r49)
        self.assertIn('在加热条件下生成', tts_chem_r49)
        self.assertNotIn('等于等于', tts_chem_r49)
        self.assertNotIn('三角形', tts_chem_r49)
        self.assertIn('H为1', tts_chem_r49)
        self.assertIn('O为16', tts_chem_r49)
        self.assertIn('Ca为40', tts_chem_r49)
        self.assertNotIn('H减1', tts_chem_r49)
        self.assertIn('UTF-8', tts_chem_r49)
        self.assertIn('第五步的结果', tts_chem_r49)

        # 20b) sanitize_quiz: full English sentence options up to 88 chars (no ':36' mid-word cut) & 6-rung chain preservation
        qz_en_r49 = tutor_mod.sanitize_quiz({
            'force_choice': True,
            'q': '哪一句时态订正最准确？',
            'opts': [
                'Last Sunday I went to the park with my best friend Tom.',
                'Last Sunday I will go to the park with my best friend Tom.',
                'We ran after each other and had a lot of fun on the grass.',
            ],
            'ans': 0,
            'no_shuffle': True,
        }, step_num=2, rng_seed='r49_en')
        self.assertIn('Last Sunday I went to the park with my best friend Tom.', qz_en_r49['opts'])
        self.assertIn('We ran after each other and had a lot of fun on the grass.', qz_en_r49['opts'])

        qz_6r_r49 = tutor_mod.sanitize_quiz({
            'q': '请依次算出等腰三角形面积各步：',
            'num_ans': '60',
            'unit': 'cm²',
            'chain': [
                {'q': '① 底边一半是多少？', 'ans': '5', 'unit': 'cm'},
                {'q': '② 腰长的平方是多少？', 'ans': '169', 'unit': 'cm²'},
                {'q': '③ 半底的平方是多少？', 'ans': '25', 'unit': 'cm²'},
                {'q': '④ 高的平方是多少？', 'ans': '144', 'unit': 'cm²'},
                {'q': '⑤ 底边上的高是多少？', 'ans': '12', 'unit': 'cm'},
                {'q': '⑥ 三角形面积是多少？', 'ans': '60', 'unit': 'cm²'},
            ],
        }, step_num=3, rng_seed='r49_6r')
        self.assertEqual(len(qz_6r_r49['chain']), 6)
        self.assertEqual(qz_6r_r49['chain'][-1]['ans'], '60')

        # 20c) detect_writing_task vs essay_critique non-collision & photographed Chinese draft detect_subject
        en_crit_q_r49 = '【英语作文批改】Last Sunday I go to the park with my best friend. We have a good time.'
        self.assertEqual(tutor_mod.detect_writing_task(en_crit_q_r49), '')
        self.assertTrue(tutor_mod.is_essay_critique_request(en_crit_q_r49))
        self.assertEqual(tutor_mod.detect_subject(en_crit_q_r49), 'english')
        cn_photo_draft_r49 = '今天放学路上突然下起了瓢泼大雨。妈妈撑着伞跑来接我，把伞大半边都倾向我这边，她自己的肩膀却湿透了。'
        self.assertEqual(tutor_mod.detect_subject(cn_photo_draft_r49, teach_mode='diagnose_draft'), 'chinese')

        # 20d) build_step0_warmup_draw on multi-subquestion relay & English blank cloze + _extract_subq_conclusion skipping verification lines
        sq_ops_r49 = tutor_mod.build_step0_warmup_draw('已知客车每小时行 70 千米，货车每小时行 50 千米，相距 360 千米。第(1)问：两车经过几小时相遇？')
        sq_goal_lbl = next((d['label'] for d in sq_ops_r49 if str(d.get('label', '')).startswith('目标：')), '')
        self.assertIn('第(1)问', sq_goal_lbl)
        en_cloze_ops_r49 = tutor_mod.build_step0_warmup_draw('Look! My brother ______ (play) basketball on the playground, and he plays very well.')
        en_goal_lbl = next((d['label'] for d in en_cloze_ops_r49 if str(d.get('label', '')).startswith('目标：')), '')
        self.assertNotIn('and he', en_goal_lbl)

        concl_r49 = self.eng._extract_subq_conclusion({
            'steps': [{'step': 4, 'board': '梯形面积 = 50 cm² ✓\n✅ 逆向验算：50 × 2 ÷ 5 = 20 ✓'}],
        }, '第(1)问')
        self.assertIn('50 cm²', concl_r49)
        self.assertNotIn('逆向验算', concl_r49)

        idx_r49_txt = _p('/usr/local/share/thinksmart-tutor/index.html').read_text('utf-8')
        mob_r49_txt = _p('/usr/local/share/thinksmart-tutor/mobile.html').read_text('utf-8')
        self.assertIn("['①', '②', '③', '④', '⑤', '⑥']", idx_r49_txt)
        self.assertIn('Math.max(ai.length, bi.length, ri.length, 1)', idx_r49_txt)
        self.assertNotIn('>= 0.42', idx_r49_txt)
        self.assertNotIn('>= 0.42', mob_r49_txt)

        # ---- R50 Real-Scenario Multi-Pass Audit Regression Checks ----
        # 21a) sanitize_quiz: π inside unit ('12' + 'π平方厘米'), variable prefix ('x = 10/3'), fractional radical ('2√3/3'), ± & ratio
        qz_pi_u_r50 = tutor_mod.sanitize_quiz({
            'q': '请在草稿本按步骤依次算出结果：',
            'num_ans': '12',
            'unit': 'π平方厘米',
            'chain': [
                {'q': '第①步：圆的面积是多少？', 'ans': '36π', 'unit': '平方厘米'},
                {'q': '第②步：扇形的面积是多少平方厘米（结果保留π）？', 'ans': '12π', 'unit': '平方厘米'},
            ],
        }, step_num=3, rng_seed='r50_pi')
        self.assertEqual(qz_pi_u_r50['mode'], 'choice')
        self.assertEqual(qz_pi_u_r50['chain'], [])
        self.assertEqual(qz_pi_u_r50['unit'], '平方厘米')
        self.assertIn('12π平方厘米', qz_pi_u_r50['opts'][qz_pi_u_r50['ans']])
        self.assertNotIn('依次算出', qz_pi_u_r50['q'])

        qz_pi_hyb_r50 = tutor_mod.sanitize_quiz({
            'q': '请在草稿本按步骤依次算出结果：',
            'num_ans': '12',
            'unit': '',
            'opts': ['24π平方厘米', '36π平方厘米', '12π平方厘米'],
            'ans': 2,
            'chain': [
                {'q': '整圆面积中π的系数是多少？', 'ans': '36', 'unit': ''},
                {'q': '扇形面积中π的系数是多少？', 'ans': '12', 'unit': ''},
            ],
        }, step_num=3, rng_seed='r50_pihyb')
        self.assertEqual(qz_pi_hyb_r50['mode'], 'choice')
        self.assertEqual(qz_pi_hyb_r50['chain'], [])
        self.assertIn('12π平方厘米', qz_pi_hyb_r50['opts'][qz_pi_hyb_r50['ans']])
        self.assertNotIn('依次算出', qz_pi_hyb_r50['q'])

        qz_pi_fopts_r50 = tutor_mod.sanitize_quiz({
            'q': '请依次手算，并选择等价的结果：',
            'opts': ['6²π÷6平方厘米', '6²π÷3平方厘米', '6²π÷2平方厘米'],
            'ans': 1,
            'unit': '平方厘米',
        }, step_num=3, rng_seed='r50_pif')
        self.assertEqual(qz_pi_fopts_r50['mode'], 'choice')
        self.assertEqual(qz_pi_fopts_r50['opts'][qz_pi_fopts_r50['ans']], '12π平方厘米')
        self.assertFalse(any('÷' in o or '²' in o for o in qz_pi_fopts_r50['opts']))

        qz_xeq_r50 = tutor_mod.sanitize_quiz({'q': '解得 x 等于多少？', 'num_ans': 'x = 10 / 3', 'unit': ''}, step_num=3, rng_seed='r50_x')
        self.assertEqual(qz_xeq_r50['mode'], 'num')
        self.assertEqual(qz_xeq_r50['num_ans'], '10/3')

        qz_frad_r50 = tutor_mod.sanitize_quiz({'q': 'tan 30° 等于多少？', 'num_ans': '√3/3', 'unit': ''}, step_num=3, rng_seed='r50_fr')
        self.assertEqual(qz_frad_r50['mode'], 'choice')
        self.assertEqual(qz_frad_r50['opts'][qz_frad_r50['ans']], '√3/3')
        self.assertFalse(any('×' in o for o in qz_frad_r50['opts']))

        qz_rat_r50 = tutor_mod.sanitize_quiz({'q': '甲乙速度比是多少？', 'num_ans': '3:2', 'unit': ''}, step_num=3, rng_seed='r50_rat')
        self.assertEqual(qz_rat_r50['mode'], 'choice')
        self.assertEqual(qz_rat_r50['opts'][qz_rat_r50['ans']], '3:2')

        # 21b) detect_calc_microscope fractional equation with parenthesized numerator & _mask_formula_in_level1_tip parenthesized/power masking
        cm_feq_r50 = tutor_mod.detect_calc_microscope('解方程：(2x - 1)/3 - (x + 2)/6 = 1')
        self.assertIsNotNone(cm_feq_r50)
        self.assertEqual(cm_feq_r50['badge'], '🔬 方程移项去分母显微镜')
        masked_tr_r50 = tutor_mod._mask_formula_in_level1_tip('先用 (6 + 14) × 5 ÷ 2 算出梯形面积')
        self.assertNotIn('× 5 ÷ 2', masked_tr_r50)
        masked_py_r50 = tutor_mod._mask_formula_in_level1_tip('根据勾股定理算 13² − 5²')
        self.assertNotIn('13²', masked_py_r50)

        # 21c) build_step0_warmup_draw strips orphaned closing quotes & evaluate_read_aloud classical homophones / couplets / '、'
        poem_ops_r50 = tutor_mod.build_step0_warmup_draw('阅读王安石《泊船瓜洲》：“京口瓜洲一水间，钟山只隔数重山。春风又绿江南岸，明月何时照我还。”请赏析“绿”字妙处。')
        poem_goal_r50 = next((d['label'] for d in poem_ops_r50 if str(d.get('label', '')).startswith('目标：')), '')
        self.assertFalse(poem_goal_r50.startswith('目标：”'))
        self.assertIn('请赏析', poem_goal_r50)

        s_poem_r50 = self.eng.start_live_problem(
            question='阅读王安石《泊船瓜洲》：“京口瓜洲一水间，钟山只隔数重山。春风又绿江南岸，明月何时照我还。”请赏析“绿”字妙处。',
            mode='fast',
            source='test',
        )
        sid_poem_r50 = s_poem_r50['id']
        self.eng.sessions[sid_poem_r50]['status'] = 'ready'
        self.eng.sessions[sid_poem_r50]['steps'] = [
            {'step': 1, 'title': '炼字寻景', 'say': '看春风吹绿江南岸？', 'board': '春风又绿江南岸', 'quiz': {'mode': 'choice', 'q': '“绿”是什么用法？', 'opts': ['形容词作动词', '名词', '副词'], 'ans': 0, 'whys': ['', '', ''], 'num_ans': '', 'unit': '', 'chain': [], 'tip': '', 'tip2': ''}, 'draw': []},
            {'step': 4, 'title': '全诗复盘', 'say': '诵读体会意境。', 'board': '全诗收束于思乡之情 ✓', 'quiz': None, 'draw': []},
        ]
        self.eng.update_progress(sid_poem_r50, {'solved': True})
        ra_coup_r50 = await self.eng.evaluate_read_aloud(sid_poem_r50, '春风又绿江南岸，明月何时照我还。')
        self.assertFalse(ra_coup_r50['read_aloud']['pass'])
        self.assertEqual(ra_coup_r50['read_aloud']['badge'], '🎙️ 请连读整段诗句')
        self.assertNotIn('京口瓜」', ra_coup_r50['read_aloud']['comment'])

        ra_homo_r50 = await self.eng.evaluate_read_aloud(sid_poem_r50, '京口瓜州一水间，钟山知隔数崇山。春风又绿江南岸，明月何时照我环。')
        self.assertTrue(ra_homo_r50['read_aloud']['pass'])

        ra_miss_r50 = await self.eng.evaluate_read_aloud(sid_poem_r50, '一水间，钟山只隔数重山，明月何时照我还。')
        self.assertFalse(ra_miss_r50['read_aloud']['pass'])
        self.assertIn('、', ra_miss_r50['read_aloud']['comment'])

        # 21d) send_parent_nudge session isolation + multi-ask original print sheet check key
        self.eng.active_session_id = sid_poem_r50
        self.eng.send_parent_nudge('注意体会炼字画面感哦！', '妈妈')
        self.assertIsNotNone(self.eng.public_session(sid_poem_r50)['parent_nudge'])
        s_other_r50 = self.eng.start_golden_sample(tutor_mod.SAMPLE_PROBLEMS[0]['id'])
        self.assertIsNone(self.eng.public_session(s_other_r50['id'])['parent_nudge'])

        self.eng._ensure_mistakes_loaded()
        self.eng.mistakes.insert(0, {
            'id': 'mk_r50_multi',
            'title': '长方形宽与面积',
            'question': '一个长方形的周长是 24 厘米，长是 8 厘米。这个长方形的宽是多少厘米？面积是多少平方厘米？',
            'subject': 'math',
            'error_tag': '周长漏除以2',
            'graduated': False,
            'steps': [{
                'step': 3,
                'title': '分步求解',
                'board': '① 长+宽 = 24 ÷ 2 = □\n② 宽 = 12 − 8 = □\n③ 面积 = 8 × 4 = □',
                'quiz': {
                    'mode': 'num',
                    'q': '请依次算出：',
                    'num_ans': '32',
                    'unit': '平方厘米',
                    'chain': [
                        {'q': '① 一条长加一条宽的和是多少厘米？', 'ans': '12', 'unit': '厘米'},
                        {'q': '② 长方形的宽是多少厘米？', 'ans': '4', 'unit': '厘米'},
                        {'q': '③ 长方形的面积是多少平方厘米？', 'ans': '32', 'unit': '平方厘米'},
                    ],
                },
            }],
        })
        ps_orig_r50 = await self.eng.prepare_print_sheet(ids=['mk_r50_multi'], sheet_mode='original')
        ck_r50 = ps_orig_r50['items'][0]['ans_check']
        self.assertIn('宽4厘米', ck_r50)
        self.assertIn('面积32平方厘米', ck_r50)
        self.assertNotIn('12厘米', ck_r50)

        # ---- R51 Seventh Real-Scenario Multi-Pass Audit Regression Checks ----
        # 22a) Cross-provider emergency failover preserves locked _resolve_llm_chain while appending DeepSeekLLM/GeminiAPI when non-mock
        with tempfile.TemporaryDirectory() as td_r51:
            cfg_r51 = pathlib.Path(td_r51) / 'gw51.json'
            cfg_r51.write_text(json.dumps({
                'LLM': {
                    'OpenAILLM': {'base_url': 'http://127.0.0.1:9998', 'api_key': 'k1', 'model_name': 'gpt-6.1-terra'},
                    'DeepSeekLLM': {'url': 'http://127.0.0.1:9997', 'api_key': 'k2', 'model_name': 'deepseek-v4.1-flash'},
                }
            }), encoding='utf-8')
            old_c51 = tutor_mod.HUB_CFG_PATH
            try:
                tutor_mod.HUB_CFG_PATH = cfg_r51
                e51 = tutor_mod.TutorEngine()
                self.assertEqual([m for m, _ in e51._resolve_llm_chain('fast')[2]], ['gpt-6.1-terra', 'gpt-6.1-sol', 'deepseek-v4.1-flash'])
                t51 = e51._iter_llm_targets('fast')
                self.assertEqual([x[2] for x in t51], ['gpt-6.1-terra', 'gpt-6.1-sol', 'deepseek-v4.1-flash'])
                e51._mark_llm_target_cooldown('http://127.0.0.1:9998/v1', 'gpt-6.1-terra', 429)
                e51._mark_llm_target_cooldown('http://127.0.0.1:9998/v1', 'gpt-6.1-sol', 400)
                self.assertEqual([x[2] for x in e51._iter_llm_targets('fast')], ['deepseek-v4.1-flash'])
            finally:
                tutor_mod.HUB_CFG_PATH = old_c51

        # 22b) History subject detection on Treaty of Nanjing & sanitize_quiz symbolic q replacement when q copied chain[0]['q']
        his_q_r51 = '阅读材料：“自今以后，大皇帝恩准英国人民带同所属家眷，寄居大清沿海之广州、福州、厦门、宁波、上海等五处港口。”请结合所学历史知识回答：该条款出自近代哪一部不平等条约？'
        self.assertEqual(tutor_mod.detect_subject(his_q_r51), 'history')
        qz_rad_q_r51 = tutor_mod.sanitize_quiz({
            'q': '两条直角边的平方和是多少？',
            'chain': [
                {'q': '两条直角边的平方和是多少？', 'ans': '20', 'unit': '平方厘米'},
                {'q': '斜边 AB 的长是多少厘米？', 'ans': '2√5', 'unit': '厘米'},
            ],
        }, step_num=3, rng_seed='r51_radq')
        self.assertEqual(qz_rad_q_r51['mode'], 'choice')
        self.assertEqual(qz_rad_q_r51['q'], '斜边 AB 的长是多少厘米？')
        self.assertIn('2√5', qz_rad_q_r51['opts'][qz_rad_q_r51['ans']])

        # 22c) TTS inline circled numbers ('按①、②两步'), Step 0 '= ？' TTS, semicolon rung splitting, & spoiler hint stripping
        tts_cir_r51 = tutor_mod.clean_math_for_tts('请在草稿本上按①、②两步算完，再按①②③核对。')
        self.assertNotIn('第一步，、', tts_cir_r51)
        self.assertNotIn('第二步，两步', tts_cir_r51)
        sc0_vc_r51 = tutor_mod.build_step0_scaffold('列竖式计算：12.4 - 5.75 = ？')
        self.assertIn('等于多少', sc0_vc_r51['say'])
        self.assertNotIn('等于。', sc0_vc_r51['say'])
        bd_semi_r51 = tutor_mod._coerce_multiline_str('① EC = 6；② DE = 8；③ CD = 10', 380, strip_note_prefix=True)
        self.assertEqual(len(bd_semi_r51.splitlines()), 3)
        q_strip_hint_r51 = tutor_mod.sanitize_ocr_question('一块正方形手帕的周长是96厘米，面积是多少平方厘米？提示：先求出一条边再算面积。')
        self.assertNotIn('提示：', q_strip_hint_r51)

        # 22d) Error-session hygiene: is_lesson_in_progress returns False on status=='error', & sprint_start invalid count guard
        s_err_r51 = self.eng.start_golden_sample(tutor_mod.SAMPLE_PROBLEMS[0]['id'])
        self.eng.sessions[s_err_r51['id']]['status'] = 'error'
        self.eng.sessions[s_err_r51['id']]['error'] = 'AI 老师当前通道请求繁忙'
        self.assertFalse(self.eng.is_lesson_in_progress())
        r_sp_bad_r51 = await self.client.post('/api/tutor/mistakes/sprint_start', headers=H, json={'count': 'bad_int'})
        self.assertEqual(r_sp_bad_r51.status, 200)

        # ---- R52 Eighth Real-Scenario Multi-Pass Audit Regression Checks ----
        # 23a) Spoiler gate catches '最后的答案是负三' / '解得x等于负三' & unsolved ask_followup defaults upto_idx
        qa_leak_r52 = {'say': '最后的答案是负三。去分母时两边同乘6。', 'board': '解得 x = -3'}
        scrubbed_r52 = tutor_mod.scrub_locked_spoilers_from_qa(
            qa_leak_r52,
            [{'step': 3, 'board': 'x = -3', 'quiz': {'mode': 'num', 'num_ans': '-3', 'chain': [{'q': '求x', 'ans': '-3'}]}}],
            question_txt='解方程：(2x + 1)/3 - (5x - 1)/6 = 1',
        )
        self.assertNotIn('负三', scrubbed_r52['say'])
        self.assertNotIn('-3', scrubbed_r52['board'])

        # 23b) Primary/middle-school '单元习作 / 习作要求 / 不少于400字' writing_task detection & subquestion '请问：' cleanup
        comp_q_r52 = '五年级语文·单元习作题目\n题目：《那一刻，我长大了》\n习作要求：把动作和心理变化写具体，结构完整，感情真挚，不少于400字。'
        self.assertEqual(tutor_mod.detect_writing_task(comp_q_r52), 'chinese_essay')
        self.assertEqual(tutor_mod.detect_subject(comp_q_r52), 'chinese')
        sq_bio_r52 = tutor_mod.split_multipart_subquestions(
            '【初中生物】某同学将一株天竺葵放在黑暗处一昼夜后，移到阳光下照射几小时，摘下叶片脱色后滴加碘液。请问：（1）暗处理一昼夜的目的是什么？（2）见光部分呈什么颜色？'
        )
        self.assertIsNotNone(sq_bio_r52)
        self.assertFalse(sq_bio_r52['stem'].endswith('请问'))

        # 23c) TTS remainder '……', clock '8:40', scientific '10³ kg/m³', density 'ρ水' dedup, & 'ZnSO₄'
        tts_rem_r52 = tutor_mod.clean_math_for_tts('47 ÷ 6 = 7（条）……5（人）')
        self.assertIn('余5', tts_rem_r52)
        tts_clk_r52 = tutor_mod.clean_math_for_tts('周六上午小明 8:40 开始写作业，10:05 全部写完')
        self.assertIn('8点40分', tts_clk_r52)
        self.assertIn('10点零5分', tts_clk_r52)
        tts_phy_r52 = tutor_mod.clean_math_for_tts('水的密度 ρ水 = 1.0×10³kg/m³，受到的浮力 F浮')
        self.assertIn('10的3次方千克每立方米', tts_phy_r52)
        self.assertNotIn('浮力 浮力', tts_phy_r52)
        self.assertNotIn('水的密度 水的密度', tts_phy_r52)
        tts_chm_r52 = tutor_mod.clean_math_for_tts('Zn + H₂SO₄ = ZnSO₄ + H₂↑')
        self.assertIn('硫酸锌', tts_chm_r52)
        self.assertNotIn('ZnSO4', tts_chm_r52)

        # 23d) Classical Chinese 1/3 pause guide ('山 / 不在高，水 / 不在深') & Step 0 clean clause cut
        pause_ccn_r52 = tutor_mod.compute_classical_pause_guide('山不在高，有仙则名。水不在深，有龙则灵。')
        self.assertIn('山 / 不在高', pause_ccn_r52)
        self.assertIn('水 / 不在深', pause_ccn_r52)
        sc0_phy_r52 = tutor_mod.build_step0_scaffold(
            '【八年级物理】把一个金属块挂在弹簧测力计下，在空气中称得重力 G = 7.8N；把它全部浸没在水中时，弹簧测力计的示数 F拉 = 6.8N（g取10N/kg，水的密度 ρ水 = 1.0×10³kg/m³）。求金属块受到的浮力 F浮 和金属块的体积 V（单位：cm³）。'
        )
        self.assertNotIn('浮力 和。', sc0_phy_r52['say'])

        # ---- R53 Ninth Real-Scenario Multi-Pass Audit Regression Checks ----
        # 24a) System of equations with ① / ② must NOT raise ValueError('invalid literal for int()...: ①') or false-split
        sys_q_r53 = '【七年级数学】解二元一次方程组：2x + y = 7 ①，x - y = 2 ②，求 x 和 y 的值。'
        self.assertIsNone(tutor_mod.split_multipart_subquestions(sys_q_r53))
        cir_subq_r53 = tutor_mod.split_multipart_subquestions(
            '【八年级物理】工人用滑轮组在 10s 内将重 400N 的货物匀速提升 3m，所用拉力 F = 250N（动滑轮由 2 段绳子承担物重，s = 2h）。求：① 有用功 W有 是多少焦？② 拉力做的总功 W总 和机械效率 η 是多少？'
        )
        self.assertIsNotNone(cir_subq_r53)
        self.assertFalse(cir_subq_r53['stem'].endswith('求'))

        # 24b) TTS: CJK minus ('x的平方 - 5x'), redundant chemical parentheses ('氢氧化钠（NaOH）', '稀盐酸（HCl）'), & '10s / 3m / W有 / W总'
        tts_quad_r53 = tutor_mod.clean_math_for_tts('解一元二次方程：x² - 5x + 6 = 0')
        self.assertIn('x的平方减5x', tts_quad_r53.replace(' ', ''))
        self.assertNotIn('-', tts_quad_r53)
        tts_ph_r53 = tutor_mod.clean_math_for_tts('向盛有氢氧化钠（NaOH）溶液的烧杯中逐滴加入稀盐酸（HCl）')
        self.assertNotIn('氢氧化钠（氢氧化钠）', tts_ph_r53)
        self.assertNotIn('稀盐酸（盐酸）', tts_ph_r53)
        tts_pul_r53 = tutor_mod.clean_math_for_tts('在 10s 内将重 400N 的货物匀速提升 3m，求有用功 W有 和总功 W总')
        self.assertIn('10秒', tts_pul_r53)
        self.assertIn('3米', tts_pul_r53)
        self.assertNotIn('W有', tts_pul_r53)
        self.assertNotIn('W总', tts_pul_r53)
        self.assertNotIn('有用功 有用功', tts_pul_r53)

        # 24c) Both-ends planting reading trap ('两端都要安装') & Mistake Book clean solve / 3-strike struggle
        traps_tree_r53 = tutor_mod.detect_reading_traps('在一条长 1.2 千米的街道两旁安装路灯（两端都要安装），每隔 6 米安装一盏，一共需要多少盏？')
        self.assertTrue(any(t['kind'] == 'both_ends' for t in traps_tree_r53))
        s_clean_r53 = self.eng.start_golden_sample(tutor_mod.SAMPLE_PROBLEMS[0]['id'])
        self.eng.update_progress(s_clean_r53['id'], {'solved': True, 'weak_step': 0})
        self.assertFalse(any(m.get('orig_sid') == s_clean_r53['id'] for m in self.eng.mistakes))
        s_strug_r53 = self.eng.start_golden_sample(tutor_mod.SAMPLE_PROBLEMS[1]['id'])
        self.eng.update_progress(s_strug_r53['id'], {'current_step': 3, 'step_attempts': 3, 'solved': False})
        self.assertTrue(any(m.get('orig_sid') == s_strug_r53['id'] for m in self.eng.mistakes))

        # ---- R54 Tenth Milestone Real-Scenario Multi-Pass Audit Regression Checks ----
        # 25a) Physics electric power/energy kW·h subject & trap detection + natural essay critique routing
        elec_q_r54 = '【九年级物理】小明家有一台额定功率为 1500W 的电热水壶，正常工作 20min，消耗的电能是多少 kW·h？'
        self.assertEqual(tutor_mod.detect_subject(elec_q_r54), 'physics')
        traps_elec_r54 = tutor_mod.detect_reading_traps(elec_q_r54)
        self.assertTrue(any(t['kind'] == 'elec_unit' for t in traps_elec_r54))
        self.assertTrue(tutor_mod.is_essay_critique_request('请帮我批改升格这篇拔河比赛作文草稿', 'diagnose_draft'))

        # 25b) TTS: algebra equation labels ('2x + y = 7 ①', '代入②', '①式'), inequalities ('>', '＜'), & metric ton/kW ('125t', '1.5kW')
        tts_sys_r54 = tutor_mod.clean_math_for_tts('解二元一次方程组：2x + y = 7 ①，x - y = 2 ②，把 x 代入②与①式核对。')
        self.assertIn('第1式', tts_sys_r54)
        self.assertIn('第2式', tts_sys_r54)
        self.assertIn('代入第2式', tts_sys_r54)
        self.assertNotIn('第1步式', tts_sys_r54)
        tts_ineq_r54 = tutor_mod.clean_math_for_tts('解一元一次不等式：-2x + 6 > 10，得 x ＜ -2')
        self.assertIn('大于', tts_ineq_r54)
        self.assertIn('小于', tts_ineq_r54)
        self.assertNotIn('>', tts_ineq_r54)
        tts_ton_r54 = tutor_mod.clean_math_for_tts('高温煅烧含碳酸钙80%的石灰石 125t，额定功率 1.5kW')
        self.assertIn('125吨', tts_ton_r54)
        self.assertIn('1.5千瓦', tts_ton_r54)

        # 25c) sanitize_quiz: inequality sign-flip preserves choice mode & simplified compound-pi ('（4π－8）cm²') preserves clean distractors
        qz_ineq_r54 = tutor_mod.sanitize_quiz({
            'q': '请在草稿本按步骤依次算出结果：',
            'num_ans': '-2',
            'chain': [
                {'q': '第①步右边得到多少？', 'ans': '4', 'unit': ''},
                {'q': '第②步右边得到多少？', 'ans': '-2', 'unit': ''},
            ],
            'opts': ['x＞−2', 'x＜2', 'x＜−2'],
            'ans': 2,
        }, step_num=3, rng_seed='r54_ineq')
        self.assertEqual(qz_ineq_r54['mode'], 'choice')
        self.assertEqual(qz_ineq_r54['opts'][qz_ineq_r54['ans']], 'x＜−2')

        qz_ineq_bare_r54 = tutor_mod.sanitize_quiz({
            'q': '两边同除以 -2 后解集是？',
            'chain': [
                {'q': '10 - 6 等于多少？', 'ans': '4'},
                {'q': '不等式的解集是？', 'ans': 'x < -2'},
            ],
        }, step_num=3, rng_seed='r54_ineq_bare')
        self.assertEqual(qz_ineq_bare_r54['mode'], 'choice')
        self.assertEqual(qz_ineq_bare_r54['opts'][qz_ineq_bare_r54['ans']], 'x < -2')
        self.assertIn('x > -2', qz_ineq_bare_r54['opts'])

        qz_pi_binom_r54 = tutor_mod.sanitize_quiz({
            'q': '弓形阴影面积是多少？',
            'unit': 'cm²',
            'opts': ['（4π－8）cm²', '（4π＋8）cm²', '（16π－8）cm²'],
            'ans': 0,
        }, step_num=3, rng_seed='r54_pi_binom')
        self.assertEqual(qz_pi_binom_r54['mode'], 'choice')
        self.assertEqual(qz_pi_binom_r54['opts'][qz_pi_binom_r54['ans']], '（4π－8）cm²')
        self.assertTrue(any('16π' in o for o in qz_pi_binom_r54['opts']))

        # ---- R55 USB Document Camera Hotplug, Format Isolation & Capture Fallback Regression Checks ----
        # 26a) probe_resolutions isolates MJPG sizes from trailing YUYV sizes & supports YUYV-only cameras
        uvc_mgr = tutor_mod.UVCCameraManager()
        v4l_mixed_out = (
            "ioctl: VIDIOC_ENUM_FMT\n"
            "\t[0]: 'MJPG' (Motion-JPEG, compressed)\n"
            "\t\tSize: Discrete 1280x720\n"
            "\t\tSize: Discrete 1920x1080\n"
            "\t[1]: 'YUYV' (YUYV 4:2:2)\n"
            "\t\tSize: Discrete 3264x2448\n"
        ).encode('utf-8')

        class _FakeV4lProc:
            def __init__(self, payload, rc=0):
                self._payload = payload
                self.returncode = rc

            async def communicate(self):
                return self._payload, b''

            def kill(self):
                pass

            async def wait(self):
                return self.returncode

        with mock.patch('asyncio.create_subprocess_exec', mock.AsyncMock(return_value=_FakeV4lProc(v4l_mixed_out))):
            info_mixed = await uvc_mgr.probe_resolutions('/dev/video8', 'Mixed DocCam')
            self.assertEqual(info_mixed['input_fmt'], 'mjpeg')
            self.assertEqual(info_mixed['hi_res'], '1920x1080')
            self.assertEqual(info_mixed['preview_res'], '1280x720')

        v4l_yuyv_out = (
            "ioctl: VIDIOC_ENUM_FMT\n"
            "\t[0]: 'YUYV' (YUYV 4:2:2)\n"
            "\t\tSize: Discrete 640x480\n"
            "\t\tSize: Discrete 2592x1944\n"
        ).encode('utf-8')
        with mock.patch('asyncio.create_subprocess_exec', mock.AsyncMock(return_value=_FakeV4lProc(v4l_yuyv_out))):
            info_yuyv = await uvc_mgr.probe_resolutions('/dev/video9', 'YUYV Only DocCam')
            self.assertEqual(info_yuyv['input_fmt'], '')
            self.assertEqual(info_yuyv['hi_res'], '2592x1944')
            self.assertEqual(info_yuyv['preview_res'], '640x480')

        # 26b) capture_jpeg automatically retries without -input_format / -video_size if primary ffmpeg call fails
        seen_cmds = []
        fake_jpg = b'\xff\xd8\xff\xe0' + (b'\x00' * 400) + b'\xff\xd9'

        async def _fake_ffmpeg_exec(*cmd_args, **kwargs):
            seen_cmds.append(cmd_args)
            if len(seen_cmds) == 1:
                return _FakeV4lProc(b'', rc=1)
            return _FakeV4lProc(fake_jpg, rc=0)

        uvc_mgr._res_cache[('/dev/fake_video_mock', 'Mixed DocCam')] = {
            'preview_res': '1280x720', 'hi_res': '1920x1080', 'input_fmt': 'mjpeg', 'probed': True,
        }
        with mock.patch.object(uvc_mgr, 'discover', return_value={
            'connected': True, 'dev': '/dev/fake_video_mock', 'name': 'Mixed DocCam',
            'preview_res': '1280x720', 'hi_res': '1920x1080', 'rotate': 0,
        }), mock.patch('asyncio.create_subprocess_exec', side_effect=_fake_ffmpeg_exec):
            out_jpg = await uvc_mgr.capture_jpeg(high_res=True)
            self.assertEqual(out_jpg, fake_jpg)
            self.assertEqual(len(seen_cmds), 2)
            self.assertIn('-input_format', seen_cmds[0])
            self.assertNotIn('-input_format', seen_cmds[1])

        # 26c) Hotplug auto-open via /api/tutor/badge & /api/tutor/state (opens camera when idle, respects active lesson)
        self.eng.active_session_id = ''
        self.eng._cam_was_connected = False
        prev_oseq = self.eng.open_ui_seq
        with mock.patch.object(self.eng.camera, 'discover', return_value={
            'connected': True, 'dev': '/dev/video8', 'name': 'HD 8MP DocCam',
            'preview_res': '1280x720', 'hi_res': '1920x1080', 'rotate': 0,
        }):
            rb_plug = await self.client.get('/api/tutor/badge')
            jb_plug = await rb_plug.json()
            self.assertTrue(jb_plug['camera_connected'])
            self.assertGreater(jb_plug['open_ui_seq'], prev_oseq)
            self.assertEqual(self.eng.open_ui_target, 'camera')

        # Unplug resets _cam_was_connected and clears frame cache
        self.eng.camera._last_frame = b'cached'
        with mock.patch.object(self.eng.camera, 'discover', return_value={
            'connected': False, 'dev': '', 'name': '',
            'preview_res': '1280x720', 'hi_res': '1920x1080', 'rotate': 0,
        }):
            rb_unplug = await self.client.get('/api/tutor/badge')
            jb_unplug = await rb_unplug.json()
            self.assertFalse(jb_unplug['camera_connected'])
            self.assertIsNone(self.eng.camera._last_frame)

        # Plugging in during an active unsolved lesson must NOT hijack open_ui_target
        s_act = self.eng.start_golden_sample(tutor_mod.SAMPLE_PROBLEMS[0]['id'], open_ui=False)
        self.eng.open_ui_target = 'board'
        seq_before_busy_plug = self.eng.open_ui_seq
        with mock.patch.object(self.eng.camera, 'discover', return_value={
            'connected': True, 'dev': '/dev/video8', 'name': 'HD 8MP DocCam',
            'preview_res': '1280x720', 'hi_res': '1920x1080', 'rotate': 0,
        }):
            await self.client.get('/api/tutor/state')
            self.assertEqual(self.eng.open_ui_seq, seq_before_busy_plug)
            self.assertEqual(self.eng.open_ui_target, 'board')
        self.eng._cam_was_connected = False

        # 26e) 4:3 full-sensor MJPG preference (2592x1944 / 1600x1200) & /api/tutor/camera/refocus + syncCameraCropLayer
        v4l_fic_out = (
            "ioctl: VIDIOC_ENUM_FMT\n"
            "\t[0]: 'MJPG' (Motion-JPEG, compressed)\n"
            "\t\tSize: Discrete 1280x720\n"
            "\t\tSize: Discrete 1600x1200\n"
            "\t\tSize: Discrete 2592x1944\n"
            "\t\tSize: Discrete 3840x2160\n"
            "\t\tSize: Discrete 4160x3120\n"
        ).encode('utf-8')
        with mock.patch('asyncio.create_subprocess_exec', mock.AsyncMock(return_value=_FakeV4lProc(v4l_fic_out))):
            info_fic = await uvc_mgr.probe_resolutions('/dev/video10', 'Fic760xUsbCamera')
            self.assertEqual(info_fic['hi_res'], '2592x1944')
            self.assertEqual(info_fic['preview_res'], '1600x1200')

        with mock.patch.object(self.eng.camera, 'discover', return_value={
            'connected': True, 'dev': '/dev/fake_video8', 'name': 'Fic760xUsbCamera',
            'preview_res': '1600x1200', 'hi_res': '2592x1944', 'rotate': 270,
        }), mock.patch.object(self.eng.camera, '_run_v4l2_ctrl', mock.AsyncMock()):
            r_rf = await self.client.post('/api/tutor/camera/refocus', headers=H, json={})
            self.assertEqual(r_rf.status, 200)
            self.assertTrue((await r_rf.json())['ok'])

        idx_html_now = _p('/usr/local/share/thinksmart-tutor/index.html').read_text(encoding='utf-8')
        self.assertIn('function syncCameraCropLayer(', idx_html_now)
        self.assertIn('refocusCameraView()', idx_html_now)
        self.assertNotIn('2000px', idx_html_now)
        self.assertIn('/api/tutor/camera/frame.jpg?raw=1&wait=1&after=', idx_html_now)
        self.assertIn('stageCameraFullPage(', idx_html_now)
        self.assertIn('toggleCameraFreeze()', idx_html_now)
        self.assertIn('cycleCameraZoom()', idx_html_now)
        self.assertIn('pickCamNote(', idx_html_now)

        # 26g) R57: Camera zoom (1.0/1.5/2.0), bright_boost, freeze/unfreeze, & stage_page -> Homework Tray
        if tutor_mod._HAS_PIL:
            im_z = tutor_mod._PILImage.new('RGB', (320, 240), (242, 242, 242))
            for xx in range(120, 200):
                for yy in range(90, 150):
                    im_z.putpixel((xx, yy), (18, 22, 28))
            bz = tutor_mod.io.BytesIO()
            im_z.save(bz, format='JPEG', quality=92)
            raw_z = bz.getvalue()
            with mock.patch.object(self.eng.camera, 'discover', side_effect=lambda: {
                'connected': True, 'dev': '/dev/fake_r57_cam', 'name': 'Fic760xUsbCamera',
                'preview_res': '1600x1200', 'hi_res': '2592x1944',
                'rotate': self.eng.camera.rotate, 'zoom': self.eng.camera.zoom,
                'bright_boost': self.eng.camera.bright_boost, 'frozen': self.eng.camera._frozen_raw_jpg is not None,
            }), mock.patch.object(self.eng.camera, '_is_real_chardev', return_value=True),                mock.patch.object(self.eng.camera, '_ensure_stream', mock.AsyncMock(return_value=True)),                mock.patch.object(self.eng.camera, '_apply_exposure_ctrls', mock.AsyncMock()):
                self.eng.camera._raw_stream_jpg = raw_z
                self.eng.camera._raw_stream_seq = 77
                # Zoom 1.5x & bright_boost
                r_ct1 = await self.client.post('/api/tutor/camera/ctrl', headers=H, json={'zoom': 1.5, 'bright_boost': True, 'freeze': True})
                j_ct1 = await r_ct1.json()
                self.assertTrue(j_ct1['ok'])
                self.assertEqual(j_ct1['camera']['zoom'], 1.5)
                self.assertTrue(j_ct1['camera']['bright_boost'])
                self.assertTrue(j_ct1['camera']['frozen'])
                # Frozen preview serves zoomed frame, then 204 on same seq
                r_fp1 = await self.client.get('/api/tutor/camera/frame.jpg?raw=1&wait=1&after=-1')
                self.assertEqual(r_fp1.status, 200)
                f_seq = r_fp1.headers.get('X-Cam-Seq')
                r_fp2 = await self.client.get(f'/api/tutor/camera/frame.jpg?raw=1&wait=1&after={f_seq}')
                self.assertEqual(r_fp2.status, 204)
                # Stage full page into Homework Tray (auto-unfreezes camera!)
                r_stg = await self.client.post('/api/tutor/camera/stage_page', headers=H, json={'pencil_boost': True, 'note': '只讲第2小问'})
                j_stg = await r_stg.json()
                self.assertTrue(j_stg['ok'])
                self.assertTrue(j_stg['phone_studio']['has_page'])
                self.assertFalse(self.eng.camera._frozen_raw_jpg is not None)
                # Reset camera zoom & bright_boost back to clean defaults
                await self.client.post('/api/tutor/camera/ctrl', headers=H, json={'zoom': 1.0, 'bright_boost': False, 'freeze': False})
        self.assertIn('window.__tsTutorUiVer', idx_html_now)
        # 26g) Round 58: Portrait 800x1280 UI + coordinate transform + adaptive UVC stream resolution + esc() guard
        for sym58 in ('portrait-ui', 'toggleTutorOrientation', 'getLocalMetrics', 'eventToLocalPx'):
            self.assertIn(sym58, idx_html_now, f'Missing R58 symbol in index.html: {sym58}')
        self.assertNotIn('escapeHtml(', idx_html_now)
        self.eng.camera.set_zoom(1.0)
        self.assertEqual(self.eng.camera._desired_stream_res({'preview_res': '1600x1200', 'hi_res': '2592x1944'}), '1600x1200')
        self.eng.camera.set_zoom(1.5)
        self.assertEqual(self.eng.camera._desired_stream_res({'preview_res': '1600x1200', 'hi_res': '2592x1944'}), '2592x1944')
        self.eng.camera.set_zoom(1.0)

        # 26f) Verify PIL crop-before-rotate pixel equivalence & raw=1 + 204 dedupe endpoint
        if tutor_mod._HAS_PIL:
            im_t = tutor_mod._PILImage.new('RGB', (160, 120), (240, 244, 248))
            for xx in range(20, 90):
                for yy in range(30, 80):
                    im_t.putpixel((xx, yy), (20, 40, 180))
            b_src = tutor_mod.io.BytesIO()
            im_t.save(b_src, format='JPEG', quality=95)
            raw_t = b_src.getvalue()
            for r_deg in (0, 90, 180, 270):
                out_b = tutor_mod.UVCCameraManager._pil_transform_sync(
                    raw_t, r_deg, {'x': 0.12, 'y': 0.18, 'w': 0.55, 'h': 0.42}, high_res=True,
                )
                self.assertTrue(out_b.startswith(b'\xff\xd8') and out_b.endswith(b'\xff\xd9'))

            with mock.patch.object(self.eng.camera, 'discover', return_value={
                'connected': True, 'dev': '/dev/fake_raw_cam', 'name': 'Fic760xUsbCamera',
                'preview_res': '1600x1200', 'hi_res': '2592x1944', 'rotate': 270,
            }), mock.patch.object(self.eng.camera, '_is_real_chardev', return_value=True),                mock.patch.object(self.eng.camera, '_ensure_stream', mock.AsyncMock(return_value=True)):
                self.eng.camera._raw_stream_jpg = raw_t
                self.eng.camera._raw_stream_seq = 42
                r_raw1 = await self.client.get('/api/tutor/camera/frame.jpg?raw=1&after=-1')
                self.assertEqual(r_raw1.status, 200)
                self.assertEqual(r_raw1.headers.get('X-Cam-Seq'), '42')
                self.assertGreater(len(await r_raw1.read()), 256)
                r_raw2 = await self.client.get('/api/tutor/camera/frame.jpg?raw=1&after=42')
                self.assertEqual(r_raw2.status, 204)

        # 26d) camera_snap LookupError opens 'camera' guide view
        with mock.patch.object(self.eng.camera, 'discover', return_value={
            'connected': False, 'dev': '', 'name': '', 'preview_res': '1280x720', 'hi_res': '1920x1080', 'rotate': 0,
        }):
            r_csnap_err = await self.client.post('/api/tutor/tool', headers=H_INT, json={'action': 'camera_snap', 'question': '讲一下这道题'})
            j_csnap_err = await r_csnap_err.json()
            self.assertFalse(j_csnap_err['ok'])
            self.assertIn('5V', j_csnap_err['text'])
            self.assertEqual(self.eng.open_ui_target, 'camera')

        await self.client.post('/api/tutor/phone_stage/clear', headers=H, json={'clear_all': True})
        await self.client.post('/api/tutor/history/delete', headers=H, json={'clear_all': True})
        await self.client.post('/api/tutor/mistakes/delete', headers=H, json={'clear_all': True})


if __name__ == '__main__':
    unittest.main(verbosity=2)
