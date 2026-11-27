#!/opt/thinksmart-addons/venv/bin/python
"""Automated test suite for Family Countdowns / Birthdays / Anniversaries / Count-up (2026-11-13)."""
import datetime as dt
import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

if os.path.isdir('/opt/thinksmart-addons/vendor'):
    sys.path.insert(0, '/opt/thinksmart-addons/vendor')
if os.path.isdir('/usr/local/bin'):
    sys.path.insert(0, '/usr/local/bin')

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

REPO_DEVICE = pathlib.Path(__file__).resolve().parent.parent


def _asset(installed: str, repo_rel: str) -> pathlib.Path:
    p = pathlib.Path(installed)
    return p if p.exists() else (REPO_DEVICE / repo_rel)


def _load_mod(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


home_mod = _load_mod('ts_home_cd_test', _asset('/usr/local/bin/thinksmart-home.py', 'home/thinksmart-home.py'))
ic_mod = _load_mod('ts_ic_cd_test', _asset('/usr/local/bin/thinksmart-intercom.py', 'intercom/thinksmart-intercom.py'))
sc_mod = _load_mod('ts_sc_cd_test', _asset('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py', 'qqmusic/aec_bridge_sitecustomize.py'))

H8095 = {'Host': '127.0.0.1:8095'}
H_INT = {'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'}


class TestCountdownCoreAndMath(unittest.TestCase):
    def test_empty_default_no_public_holidays(self):
        with tempfile.TemporaryDirectory() as td:
            store = home_mod.CountdownStore(pathlib.Path(td) / 'countdowns.json')
            self.assertEqual(store.list_enriched(), [])
            summary = store.state_summary(dt.date(2026, 11, 13))
            self.assertEqual(summary['count'], 0)
            self.assertIsNone(summary['top'])

    def test_solar_lunar_leap_feb29_and_countup_math(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'countdowns.json'
            store = home_mod.CountdownStore(p)

            # 1. Once countdown
            e_exam = store.upsert({
                'title': '姐姐中考', 'kind': 'once', 'date': '2027-06-20', 'category': 'exam'
            })
            # 2. Yearly solar Feb 29 birthday with start_year
            e_feb29 = store.upsert({
                'title': '妹妹生日', 'kind': 'yearly_solar', 'month': 2, 'day': 29, 'start_year': 2020
            })
            # 3. Yearly lunar leap-month birthday (闰六月十五)
            e_lunar_leap = store.upsert({
                'title': '外婆生日', 'kind': 'yearly_lunar',
                'lunar_month': 6, 'lunar_day': 15, 'lunar_leap': True, 'start_year': 1960
            })
            # 4. Yearly lunar 30th day (clamps to 29 in small lunar months)
            e_lunar_30 = store.upsert({
                'title': '除夕团圆', 'kind': 'yearly_lunar',
                'lunar_month': 12, 'lunar_day': 30, 'lunar_leap': False
            })
            # 5. Countup
            e_cu = store.upsert({
                'title': '坚持晨读', 'kind': 'countup', 'date': '2025-11-01'
            })
            # 6. Expired once (should sink to bottom and never become top)
            e_past = store.upsert({
                'title': '期中考试', 'kind': 'once', 'date': '2026-10-10', 'pinned': True
            })

            # Check permissions 0600
            st_mode = stat.S_IMODE(os.lstat(p).st_mode)
            self.assertEqual(st_mode, 0o600)

            # Evaluate on 2026-11-13 (non-leap next year 2027)
            d_nov = dt.date(2026, 11, 13)
            items = {x['id']: x for x in store.list_enriched(d_nov)}

            self.assertEqual(items[e_exam['id']]['days'], (dt.date(2027, 6, 20) - d_nov).days)
            # 2027 is not leap -> Feb 28, 2027; 2027 - 2020 = 7周岁
            self.assertEqual(items[e_feb29['id']]['target_date'], '2027-02-28')
            self.assertEqual(items[e_feb29['id']]['anniv_label'], '7周岁')

            # Evaluate Feb 29 in 2028 (leap year) -> 2028-02-29
            items_2028 = {x['id']: x for x in store.list_enriched(dt.date(2028, 1, 15))}
            self.assertEqual(items_2028[e_feb29['id']]['target_date'], '2028-02-29')
            self.assertEqual(items_2028[e_feb29['id']]['anniv_label'], '8周岁')

            # Lunar leap 6th month in 2025 (2025 has leap 6th month -> 2025-08-08) vs 2026 (no leap 6th -> regular 6th month 2026-07-28)
            items_2025 = {x['id']: x for x in store.list_enriched(dt.date(2025, 8, 1))}
            self.assertEqual(items_2025[e_lunar_leap['id']]['target_date'], '2025-08-08')
            items_2026_summer = {x['id']: x for x in store.list_enriched(dt.date(2026, 7, 1))}
            self.assertEqual(items_2026_summer[e_lunar_leap['id']]['target_date'], '2026-07-28')

            # Lunar 12-30 never crashes even if 腊月 has 29 days
            self.assertGreaterEqual(items[e_lunar_30['id']]['days'], 0)

            # Countup elapsed days & 满1年
            self.assertEqual(items[e_cu['id']]['days'], (d_nov - dt.date(2025, 11, 1)).days)
            self.assertEqual(items[e_cu['id']]['anniv_label'], '满1年')

            # Expired once has negative days and is NOT top even though pinned=True
            self.assertLess(items[e_past['id']]['days'], 0)
            top = store.state_summary(d_nov)['top']
            self.assertIsNotNone(top)
            self.assertNotEqual(top['id'], e_past['id'])

            # Pinning an active item makes it #1 top immediately and unpins others
            store.set_pin(e_exam['id'], True)
            top2 = store.state_summary(d_nov)['top']
            self.assertEqual(top2['id'], e_exam['id'])
            self.assertTrue(top2['pinned'])

            # Reload from disk preserves everything
            store2 = home_mod.CountdownStore(p)
            self.assertEqual(len(store2.list_enriched(d_nov)), 6)
            self.assertEqual(store2.state_summary(d_nov)['top']['id'], e_exam['id'])

    def test_symlink_refused_and_parse_lunar_spec(self):
        self.assertEqual(home_mod.parse_lunar_spec('农历十月十二'), (10, 12, False))
        self.assertEqual(home_mod.parse_lunar_spec('闰六月十五'), (6, 15, True))
        self.assertEqual(home_mod.parse_lunar_spec('腊月廿八'), (12, 28, False))
        self.assertEqual(home_mod.parse_lunar_spec('正月三十'), (1, 30, False))
        with tempfile.TemporaryDirectory() as td:
            real_f = pathlib.Path(td) / 'real.json'
            real_f.write_text('[]', 'utf-8')
            link_f = pathlib.Path(td) / 'link.json'
            link_f.symlink_to(real_f)
            store = home_mod.CountdownStore(link_f)
            with self.assertRaises(OSError):
                store.upsert({'title': '测试', 'kind': 'once', 'date': '2027-01-01'})

    def test_r2_span_progress_today_first_and_lunar_dec_age(self):
        with tempfile.TemporaryDirectory() as td:
            store = home_mod.CountdownStore(pathlib.Path(td) / 'countdowns.json')
            today = dt.date(2026, 11, 13)
            e_pin_future = store.upsert({'title': '姐姐中考', 'kind': 'once', 'date': '2027-06-20', 'category': 'exam', 'pinned': True})
            e_today_bday = store.upsert({'title': '妈妈生日', 'kind': 'yearly_solar', 'month': 11, 'day': 13, 'start_year': 1990})
            e_cu_82 = store.upsert({'title': '背单词打卡', 'kind': 'countup', 'date': (today - dt.timedelta(days=82)).isoformat()})
            e_cu_100 = store.upsert({'title': '晨练百日', 'kind': 'countup', 'date': (today - dt.timedelta(days=100)).isoformat()})
            e_exp = store.upsert({'title': '旧模拟考', 'kind': 'once', 'date': '2026-10-01'})

            s = store.state_summary(today)
            self.assertEqual(s['count'], 5)
            self.assertEqual(s['active_count'], 4)
            self.assertEqual(s['top']['id'], e_pin_future['id'])
            self.assertEqual(len(s['today_items']), 1)
            self.assertEqual(s['today_items'][0]['id'], e_today_bday['id'])
            self.assertIsNotNone(s['next'])

            by_id = {x['id']: x for x in store.list_enriched(today)}
            self.assertIn('个月', by_id[e_pin_future['id']]['span_hint'])
            self.assertEqual(by_id[e_today_bday['id']]['span_hint'], '就是今天')
            self.assertEqual(by_id[e_cu_82['id']]['span_hint'], '距 100 天还差 18 天')
            self.assertEqual(by_id[e_cu_100['id']]['span_hint'], '已达成 100 天里程碑')
            self.assertEqual(by_id[e_cu_100['id']]['progress_pct'], 100)

            # Lunar Dec (腊月二十) in Nov 2026 falls in solar Jan 2027, but lunar year is still 2026 -> 36周岁
            conv = home_mod.convert_calendar_preview('lunar', 1990, 12, 20, False, today)
            self.assertEqual(conv['next_yearly_occ_year'], 2026)
            self.assertTrue(conv['next_yearly_solar'].startswith('2027-'))
            e_l12 = store.upsert({'title': '爸爸生日', 'kind': 'yearly_lunar', 'lunar_month': 12, 'lunar_day': 20, 'start_year': 1990})
            en_l12 = [x for x in store.list_enriched(today) if x['id'] == e_l12['id']][0]
            self.assertEqual(en_l12['anniv_num'], conv['next_yearly_occ_year'] - 1990)

            # Batch clear expired removes only expired once items
            cleared = store.clear_expired(today)
            self.assertEqual(cleared, 1)
            rem_ids = {x['id'] for x in store.list_enriched(today)}
            self.assertNotIn(e_exp['id'], rem_ids)
            self.assertEqual(len(rem_ids), 5)


class TestCountdownHttpAndVoiceAndRelay(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        tpath = pathlib.Path(self._td.name)
        self.inbox = home_mod.Inbox(tpath / 'messages.json')
        self.cd_store = home_mod.CountdownStore(tpath / 'countdowns.json')
        return home_mod.make_app(self.inbox, self.cd_store)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self._td.cleanup()

    async def test_crud_etag_calendar_marks_state_inbox_and_voice_tool(self):
        # 1. Initial GET + 304 ETag
        r0 = await self.client.get('/api/home/countdowns', headers=H8095)
        self.assertEqual(r0.status, 200)
        j0 = await r0.json()
        self.assertEqual(j0['items'], [])
        et0 = r0.headers['ETag']
        r304 = await self.client.get('/api/home/countdowns', headers={**H8095, 'If-None-Match': et0})
        self.assertEqual(r304.status, 304)

        # 2. Inbox ETag stability preserved
        ri1 = await self.client.get('/api/home/inbox', headers=H8095)
        self.assertEqual(ri1.status, 200)
        ji1 = await ri1.json()
        self.assertEqual(ji1['cd_open_seq'], 0)
        self.assertFalse(ji1['cd_open_fresh'])
        ri2 = await self.client.get('/api/home/inbox', headers={**H8095, 'If-None-Match': ri1.headers['ETag']})
        self.assertEqual(ri2.status, 304)

        # 3. Convert preview API
        rc = await self.client.get('/api/home/countdowns/convert?cal=lunar&y=2025&m=6&d=15&leap=1', headers=H8095)
        self.assertEqual(rc.status, 200)
        jc = await rc.json()
        self.assertEqual(jc['solar'], '2025-08-08')
        self.assertTrue(jc['lunar_leap'])

        # 4. Create via POST /api/home/countdowns
        rc1 = await self.client.post('/api/home/countdowns', headers=H8095, json={
            'title': '姐姐中考', 'kind': 'once', 'date': '2027-06-20', 'pinned': True
        })
        self.assertEqual(rc1.status, 200)
        item1 = (await rc1.json())['item']
        self.assertEqual(item1['category'], 'exam')
        self.assertTrue(item1['pinned'])

        # 5. Calendar marks on 2027-06 without mutating _MONTH_CACHE
        rcal = await self.client.get('/api/home/calendar?ym=2027-06', headers=H8095)
        self.assertEqual(rcal.status, 200)
        jcal = await rcal.json()
        day_hit = next(c for c in jcal['month']['days'] if c['date'] == '2027-06-20')
        self.assertEqual(day_hit['cd'][0]['title'], '姐姐中考')
        # Verify _MONTH_CACHE raw dict was NOT mutated
        for cached_grid in home_mod._MONTH_CACHE.values():
            for cell in cached_grid['days']:
                self.assertNotIn('cd', cell)

        # 6. Voice tool requires X-TS-Internal: 1
        r_forbid = await self.client.post('/api/home/countdowns/tool', headers=H8095, json={'action': 'list'})
        self.assertEqual(r_forbid.status, 403)

        # Voice add lunar birthday
        rv_add = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={
            'action': 'add', 'title': '妈妈生日', 'lunar_date': '十月十二', 'start_year': 1988
        })
        self.assertEqual(rv_add.status, 200)
        jv_add = await rv_add.json()
        self.assertTrue(jv_add['ok'])
        self.assertIn('妈妈生日', jv_add['text'])
        self.assertIn('周岁', jv_add['text'])

        # Voice query
        rv_q = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={
            'action': 'query', 'keyword': '中考'
        })
        jv_q = await rv_q.json()
        self.assertTrue(jv_q['ok'])
        self.assertIn('姐姐中考', jv_q['text'])

        # Voice open UI bumps cd_open_seq & cd_open_fresh
        rv_open = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={'action': 'open'})
        self.assertEqual(rv_open.status, 200)
        ri_after = await (await self.client.get('/api/home/inbox', headers=H8095)).json()
        self.assertEqual(ri_after['cd_open_seq'], 1)
        self.assertTrue(ri_after['cd_open_fresh'])
        st_after = await (await self.client.get('/api/home/state', headers=H8095)).json()
        self.assertEqual(st_after['countdowns']['open_seq'], 1)
        self.assertTrue(st_after['countdowns']['open_fresh'])
        rv_close = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={'action': 'close'})
        self.assertEqual(rv_close.status, 200)
        st_closed = await (await self.client.get('/api/home/state', headers=H8095)).json()
        self.assertEqual(st_closed['countdowns']['close_seq'], 1)
        self.assertTrue(st_closed['countdowns']['close_fresh'])

        # Voice delete
        rv_del = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={
            'action': 'delete', 'keyword': '妈妈生日'
        })
        self.assertTrue((await rv_del.json())['ok'])
        self.assertEqual(len(self.cd_store.list_enriched()), 1)

    async def test_r2_voice_relative_dates_update_and_clear_expired(self):
        today = dt.datetime.now(home_mod.LOCAL_TZ).date()
        r1 = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={
            'action': 'add', 'title': '英语等级考', 'date': '30天后', 'pinned': True
        })
        j1 = await r1.json()
        self.assertTrue(j1['ok'])
        self.assertFalse(j1['updated'])
        self.assertEqual(j1['item']['days'], 30)
        cid = j1['item']['id']

        r2 = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={
            'action': 'update', 'keyword': '英语等级', 'date': '明年六月二十号'
        })
        j2 = await r2.json()
        self.assertTrue(j2['ok'])
        self.assertTrue(j2['updated'])
        self.assertEqual(j2['item']['id'], cid)
        self.assertTrue(j2['item']['pinned'])
        self.assertEqual(j2['item']['target_date'], f'{today.year + 1}-06-20')
        self.assertEqual(len(self.cd_store.list_enriched()), 1)

        r3 = await self.client.post('/api/home/countdowns/tool', headers=H_INT, json={
            'action': 'add', 'title': '跳绳打卡', 'kind': 'countup', 'date': '10天前'
        })
        self.assertEqual((await r3.json())['item']['days'], 10)
        await self.client.post('/api/home/countdowns', headers=H8095, json={
            'title': '去年测试', 'kind': 'once', 'date': '2020-01-01'
        })
        self.assertEqual(len(self.cd_store.list_enriched()), 3)

        rc = await self.client.post('/api/home/countdowns/delete', headers=H8095, json={'clear_expired': True})
        jc = await rc.json()
        self.assertTrue(jc['ok'])
        self.assertEqual(jc['cleared'], 1)
        self.assertEqual(len(self.cd_store.list_enriched()), 2)

    async def test_intercom_relay_and_csrf_guard(self):
        # Point intercom HOME_API to our test server port & allow dynamic test Host
        orig_home_api = ic_mod.HOME_API
        dyn_host = f'127.0.0.1:{self.server.port}'
        home_mod.ALLOWED_HOSTS.add(dyn_host)
        ic_mod.HOME_API = f'http://{dyn_host}'
        try:
            ic_app = web.Application()
            import aiohttp
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                ic_app['study_session'] = aiohttp.ClientSession(trust_env=False)
            ic_app.router.add_get('/api/countdowns', ic_mod.handle_countdowns_get)
            ic_app.router.add_get('/api/countdown/convert', ic_mod.handle_countdown_convert)
            ic_app.router.add_post('/api/countdown', ic_mod.handle_countdown_save)
            ic_app.router.add_post('/api/countdown/pin', ic_mod.handle_countdown_pin)
            ic_app.router.add_post('/api/countdown/delete', ic_mod.handle_countdown_delete)
            async with TestServer(ic_app) as ic_srv, TestClient(ic_srv) as ic_cli:
                try:
                    # Missing X-Intercom-Msg header -> 403
                    r_bad = await ic_cli.get('/api/countdowns')
                    self.assertEqual(r_bad.status, 403)
                    r_bad_conv = await ic_cli.get('/api/countdown/convert?cal=lunar&y=2026&m=8&d=15')
                    self.assertEqual(r_bad_conv.status, 403)
                    r_ok_conv = await ic_cli.get('/api/countdown/convert?cal=lunar&y=1990&m=12&d=20', headers={'X-Intercom-Msg': '1'})
                    self.assertEqual(r_ok_conv.status, 200)
                    self.assertIn('next_yearly_occ_year', await r_ok_conv.json())

                    hdr = {'X-Intercom-Msg': '1'}
                    r_save = await ic_cli.post('/api/countdown', headers=hdr, json={
                        'title': '结婚纪念日', 'kind': 'yearly_solar', 'month': 5, 'day': 20, 'start_year': 2015, 'pinned': True
                    })
                    self.assertEqual(r_save.status, 200)
                    cid = (await r_save.json())['item']['id']

                    r_list = await ic_cli.get('/api/countdowns', headers=hdr)
                    self.assertEqual(r_list.status, 200)
                    items = (await r_list.json())['items']
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]['title'], '结婚纪念日')

                    r_pin = await ic_cli.post('/api/countdown/pin', headers=hdr, json={'id': cid, 'pinned': False})
                    self.assertEqual(r_pin.status, 200)
                    self.assertFalse((await r_pin.json())['item']['pinned'])

                    r_del = await ic_cli.post('/api/countdown/delete', headers=hdr, json={'id': cid})
                    self.assertEqual(r_del.status, 200)
                    self.assertEqual(len(self.cd_store.list_enriched()), 0)
                finally:
                    await ic_app['study_session'].close()
        finally:
            ic_mod.HOME_API = orig_home_api
            home_mod.ALLOWED_HOSTS.discard(dyn_host)


class TestStaticAndBridgeInvariants(unittest.TestCase):
    def test_mcp_tool_registered_and_dock_unchanged(self):
        names = [t['name'] for t in sc_mod.MUSIC_TOOLS]
        self.assertIn('self.home.countdown', names)
        self.assertIn('self.home.countdown', sc_mod._MY_TOOLS)

        apps = json.loads(_asset('/usr/local/share/thinksmart-home/apps.json', 'home/apps.json').read_text('utf-8'))['apps']
        self.assertEqual(len(apps), 9)  # 6 + 3 dock (AI讲题 added right after 服务器)

        idx = _asset('/usr/local/share/thinksmart-home/index.html', 'home/index.html').read_text('utf-8')
        cd_tool = next(t for t in sc_mod.MUSIC_TOOLS if t['name'] == 'self.home.countdown')
        self.assertIn('update', cd_tool['inputSchema']['properties']['action']['enum'])
        self.assertIn('close', cd_tool['inputSchema']['properties']['action']['enum'])

        for token in ('id="calCdPill"', 'id="calHint"', 'id="cpage"', 'openCountdownPage', 'st-cd',
                      'id="cpCleanBtn"', 'cp-tpl-grid', 'id="cdeGridPane"', "life: '生活'",
                      "m.classList.remove('amb-lab', 'msg-page', 'cd-edit')"):
            self.assertIn(token, idx)

        cd_css = idx[idx.find('/* ===== 倒数日'):idx.find('</style>')]
        self.assertNotIn('font-weight: 800', cd_css)
        self.assertNotIn('font-weight: 600', cd_css)
        self.assertIn('transform: none', cd_css)

        ho = _asset('/usr/local/share/thinksmart-monitor/home_overlay.js', 'monitor/home_overlay.js').read_text('utf-8')
        for tok in ('cd_open_seq', 'lastCdOpenSeq', 'openCountdown', 'triggerInboxPoll',
                    '#tsVoicePill', ':not(#tsVoicePill)', 'syncVoicePill', 'voicePill.onclick'):
            self.assertIn(tok, ho)

    def test_intercom_phone_countdown_js_fixes(self):
        ic = _asset('/usr/local/bin/thinksmart-intercom.py', 'intercom/thinksmart-intercom.py').read_text('utf-8')
        for tok in (
            'async function previewPhoneCd() {\n  phConv = null;',
            "const sol = phCdMode === 'yearly' ? (phConv.next_yearly_solar || phConv.solar) : phConv.solar;",
            'if (Number(it.month) === 2 && Number(it.day) === 29',
            '起始年份需在 1920–2099 之间',
            '请检查农历日期是否有效',
        ):
            self.assertIn(tok, ic)

    def test_night_wake_brightness_and_tunnel_proxy_guard(self):
        sw = _load_mod('ts_sw_test', _asset('/usr/local/bin/screen-wake-daemon.py', 'system/screen-wake-daemon.py'))
        px = _load_mod('ts_px_test', _asset('/usr/local/bin/vcnl4200-proximity-daemon.py', 'system/vcnl4200-proximity-daemon.py'))
        night_ts = dt.datetime(2026, 11, 14, 3, 15, 0).timestamp()
        day_ts = dt.datetime(2026, 11, 14, 13, 30, 0).timestamp()
        for mod in (sw, px):
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                mod.HINT_PATH = root / 'hint.json'
                mod.MODE_PATH = root / 'mode.json'
                mod.USER_BRIGHTNESS_PATH = root / 'ub'
                mod.LEGACY_USER_BRIGHTNESS_PATH = root / 'ub_legacy'
                mod.NIGHT_VIEW_PATH = root / 'night_view'
                mod.SUNRISE_PATH = root / 'sunrise.json'
                mod.HINT_PATH.write_text(json.dumps({'brightness': 80}), 'utf-8')
                mod.USER_BRIGHTNESS_PATH.write_text('3800\n', 'utf-8')
                # Default NIGHT_TEST=False with isolated HINT_PATH keeps daytime semantics even at 03:15
                mod.NIGHT_TEST = False
                self.assertGreater(mod.read_wake_brightness(night_ts), 3000)
                # Production night rules enabled: 03:15 wakes at NIGHT_LEVEL (1400), 13:30 wakes at user level
                mod.NIGHT_TEST = True
                self.assertEqual(mod.read_wake_brightness(night_ts), 1400)
                self.assertGreater(mod.read_wake_brightness(day_ts), 3000)
                # Daytime with active sleep night-view wakes at NIGHT_LEVEL (1400)
                mod.NIGHT_VIEW_PATH.write_text(json.dumps({'until': day_ts + 600}), 'utf-8')
                self.assertEqual(mod.read_wake_brightness(day_ts), 1400)
                # Active un-stopped gentle sunrise yields to normal wake brightness instead of forcing 1400
                mod.SUNRISE_PATH.write_text(json.dumps({'start': night_ts - 60, 'ramp': 600, 'hold': 300}), 'utf-8')
                self.assertGreater(mod.read_wake_brightness(night_ts), 3000)
                mod.NIGHT_TEST = False

        tun = _asset('/usr/local/bin/thinksmart-intercom-tunnel.sh', 'intercom/thinksmart-intercom-tunnel.sh').read_text('utf-8')
        self.assertNotIn('ProxyCommand="exec ', tun)
        self.assertIn('ProxyCommand="nc -w 45 -X 5 -x ', tun)
        self.assertIn('-o IdentitiesOnly=yes', tun)
        self.assertIn('-o BatchMode=yes', tun)
        self.assertIn('Both paths failed in', tun)


if __name__ == '__main__':
    unittest.main(verbosity=2)
