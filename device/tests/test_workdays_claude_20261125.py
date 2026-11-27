#!/opt/thinksmart-addons/venv/bin/python
"""Claude 2026-11-25：闹钟的「工作日」跟着国家放假安排（付总定的）。

放假不响，调休上班的周末照响；和主页提醒、温柔叫醒同一张表、同一个口径。
1. workdays.py 逐日和 lunar_python 的节假日表、主页的 _is_workday 对照；表读不出来就按周一到周五；换了新表不用重启。
2. 闹钟下一次、响过以后的下一次、放假表更新后挪日子；只要周一到周五的（weekly 1-5）不受影响。
3. 语音工具和页面上的说法。
不碰任何在跑的服务：只读文件，在临时目录里建闹钟。
"""
import datetime as dt
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

CLIENT = pathlib.Path(os.environ.get('XZ_CLIENT', '/opt/thinksmart/client/current'))
CLIENT_PY = '/opt/thinksmart/client/current/.venv/bin/python'
HOME_PY = os.environ.get('HOME_PY', '/usr/local/bin/thinksmart-home.py')
HOME_HTML = pathlib.Path(os.environ.get('HOME_HTML', '/usr/local/share/thinksmart-home/index.html'))
OVERLAY = pathlib.Path(os.environ.get('OVERLAY_JS', '/usr/local/share/thinksmart-monitor/home_overlay.js'))
VENDOR = pathlib.Path('/opt/thinksmart-addons/vendor')
TABLE = VENDOR / 'lunar_python/util/HolidayUtil.py'


def _run(py, code, timeout=90, **env):
    r = subprocess.run([py, '-B', '-c', code], cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', **env})
    return r.returncode, r.stdout, r.stderr


def _workdays():
    spec = importlib.util.spec_from_file_location('workdays_t', CLIENT / 'thinksmart_client/workdays.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TableTests(unittest.TestCase):
    def test_every_day_like_the_others(self):
        w = _workdays()
        self.assertEqual(w.TABLE, TABLE)
        sys.path.insert(0, str(VENDOR))
        try:
            from lunar_python.util import HolidayUtil
        finally:
            sys.path.remove(str(VENDOR))
        d, n, odd = dt.date(2025, 1, 1), 0, 0
        while d < dt.date(2029, 1, 1):
            h = HolidayUtil.getHoliday(d.year, d.month, d.day)
            want = h.isWork() if h is not None else d.isoweekday() <= 5
            self.assertEqual(w.is_workday(d), want, d)
            odd += want != (d.isoweekday() <= 5)
            d += dt.timedelta(days=1)
            n += 1
        self.assertEqual(n, 1461)
        self.assertGreater(odd, 60)                                   # 放假的工作日和调休的周末，三年里有几十天

    def test_the_home_page_agrees(self):
        code = r'''
import ast, datetime as dt, os, sys
sys.path.insert(0, '/opt/thinksmart-addons/vendor')
from lunar_python.util import HolidayUtil
src = open(os.environ['HOME_PY'], encoding='utf-8').read()
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == '_is_workday')
ns = {'HolidayUtil': HolidayUtil, 'contextlib': __import__('contextlib'), 'dt': dt}
exec(compile(ast.Module([fn], []), 'home', 'exec'), ns)
import importlib.util
spec = importlib.util.spec_from_file_location('w', os.environ['W']); w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
d = dt.date(2026, 1, 1)
while d < dt.date(2028, 1, 1):
    assert bool(ns['_is_workday'](d)) == w.is_workday(d), d
    d += dt.timedelta(days=1)
print('AGREE')
'''
        rc, out, err = _run('/opt/thinksmart-addons/venv/bin/python', code, HOME_PY=HOME_PY, W=str(CLIENT / 'thinksmart_client/workdays.py'))
        self.assertEqual(rc, 0, err + out)
        self.assertIn('AGREE', out)

    def test_known_days(self):
        w = _workdays()
        for day, want in (('2027-01-01', False), ('2027-01-31', True), ('2027-02-08', False), ('2027-10-09', True), ('2027-10-01', False),
                          ('2026-11-25', True), ('2026-11-28', False), ('2031-05-05', True), ('2031-05-10', False)):
            self.assertEqual(w.is_workday(dt.date.fromisoformat(day)), want, day)

    def test_no_table_goes_by_the_week_and_comes_back(self):
        w = _workdays()
        w.log.disabled = True
        new_year, sunday = dt.date(2027, 1, 1), dt.date(2027, 1, 31)
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'HolidayUtil.py'
            t = w.Workdays(p)

            def look():
                t.next_look = 0.0
                return t(new_year), t(sunday)
            self.assertEqual(look(), (True, False))                   # 没有文件
            for bad in ('', 'x = 1', '__DATA = "2027010100"', '__DATA = "202701019920270101"', '__DATA = "2027130101202701010"', '__DATA = "abcdefgh01abcdefgh"'):
                p.write_text(bad, 'utf-8')
                os.utime(p, ns=(len(bad) + 1, len(bad) + 1))
                self.assertEqual(look(), (True, False), bad)
            p.write_text('    __DATA = "202701010120270101202701311020270206"\n', 'utf-8')
            self.assertEqual(look(), (False, True))                   # 换成好的，不用重启
            p.write_text('half', 'utf-8')
            self.assertEqual(look(), (False, True))                   # 坏了就留着上次读到的
            p.unlink()
            self.assertEqual(look(), (False, True))

    def test_looks_at_the_file_once_a_minute(self):
        w = _workdays()
        seen = []

        class P(type(pathlib.Path())):
            def stat(self, **kw):
                seen.append(1)
                return super().stat(**kw)
        t = w.Workdays()
        t.path = P(TABLE)
        for i in range(50):
            t(dt.date(2027, 1, 1) + dt.timedelta(days=i))
        self.assertEqual(len(seen), 1)


class AlarmTests(unittest.TestCase):
    CODE = r'''
import datetime as dt, json, os, pathlib, sys, tempfile
sys.path.insert(0, os.environ['XZ'])
os.environ['HOME'] = tempfile.mkdtemp()
from thinksmart_client import alarms, workdays
W = {'type': 'workdays'}
D = dt.datetime
def nxt(s, hm='07:00', rule=W): return alarms.next_time(rule, hm, D.fromisoformat(s)).strftime('%Y-%m-%d %H:%M')
# 和主页提醒的预期一样（test_r6_claude_20261124）
assert nxt('2026-12-31 07:00') == '2027-01-04 07:00'
assert nxt('2027-01-29 07:00') == '2027-01-31 07:00'            # 周日，春节调休上班
assert nxt('2027-02-04 06:50', '06:50') == '2027-02-14 06:50'   # 整个春节假
assert nxt('2027-02-14 07:00') == '2027-02-15 07:00'
assert nxt('2026-11-27 07:00') == '2026-11-30 07:00'
assert nxt('2027-09-30 07:00') == '2027-10-08 07:00'
assert nxt('2026-11-25 06:59') == '2026-11-25 07:00'
five = {'type': 'weekly', 'days': [1, 2, 3, 4, 5]}
assert nxt('2026-12-31 07:00', rule=five) == '2027-01-01 07:00'  # 只要周一到周五的不看放假
assert nxt('2027-01-29 07:00', rule=five) == '2027-02-01 07:00'
assert nxt('2026-12-31 07:00', rule={'type': 'daily'}) == '2027-01-01 07:00'
assert alarms.describe_repeat(W) == '工作日（放假不响，调休上班照响）'
ts = lambda s: D.fromisoformat(s).timestamp()
clock = [ts('2026-12-30 20:00')]
td = tempfile.mkdtemp()
a = alarms.Alarms(td, clock=lambda: clock[0])
it = a.create('alarm', '起床', '', {'type': 'workdays'}, time='07:00')
assert it['at'] == ts('2026-12-31 07:00') and a.view(it)['repeat'] == '工作日（放假不响，调休上班照响）'
# 响过以后的下一次：跳过元旦三天
clock[0] = ts('2026-12-31 07:00'); ring, notes = a.take_due(clock[0]); assert ring == [it] and not notes
a.finish(ring, clock[0], True, now=clock[0] + 40); assert it['at'] == ts('2027-01-04 07:00'), D.fromtimestamp(it['at'])
assert not a.refresh(clock[0] + 60)
# 钟往回拨一点，刚响过的不会再响一遍
assert not a.refresh(clock[0] - 120) and it['at'] == ts('2027-01-04 07:00')
# 老规则（周一到周五）定下的日子，按表挪过来，并且存盘
it['at'] = ts('2027-01-01 07:00')
clock[0] = ts('2026-12-31 20:00'); assert a.refresh() and it['at'] == ts('2027-01-04 07:00') and not a.refresh()
assert json.loads((pathlib.Path(td) / 'alarms.json').read_text())['items'][0]['at'] == ts('2027-01-04 07:00')
# 新表里多了一个调休上班日：提前到那天
saved = workdays.is_workday.days
try:
    workdays.is_workday.days = {**saved, dt.date(2027, 1, 2): True}
    workdays.is_workday.next_look = float('inf')
    assert a.refresh() and it['at'] == ts('2027-01-02 07:00')
finally:
    workdays.is_workday.days, workdays.is_workday.next_look = saved, 0.0
assert a.refresh() and it['at'] == ts('2027-01-04 07:00')
# 到点的、在睡的、别的循环、坏数据都不动
clock[0] = ts('2027-01-04 07:00'); assert not a.refresh() and [i['id'] for i in a.take_due(clock[0])[0]] == [it['id']]
a.finish([it], clock[0], True, now=clock[0] + 30)
sn = a.snooze(10, now=clock[0] + 60); assert sn and it['snooze'] == clock[0] + 660
keep = it['snooze']; assert not a.refresh(clock[0] + 90) and it['snooze'] == keep
d = a.create('alarm', '', '', {'type': 'daily'}, time='06:00'); before = d['at']; a.refresh(); assert d['at'] == before
a.items.append({'id': 99, 'kind': 'alarm', 'label': '', 'say': '', 'repeat': {'type': 'workdays'}, 'time': None, 'at': clock[0] + 1e6, 'snooze': None, 'rang': None, 'created': None, 'clip': None})
assert not a.refresh(clock[0] + 100)
print('ALARMS_OK')
'''

    def test_next_rings_and_moves(self):
        rc, out, err = _run(CLIENT_PY, self.CODE, XZ=str(CLIENT), TZ='Asia/Shanghai')
        self.assertEqual(rc, 0, err + out)
        self.assertIn('ALARMS_OK', out)

    def test_the_package_is_not_imported(self):
        code = "import os, sys; sys.path.insert(0, os.environ['XZ']); from thinksmart_client import alarms, workdays; import datetime as dt; " \
               "workdays.is_workday(dt.date(2027, 1, 1)); assert not [m for m in sys.modules if m.startswith('lunar')]; print('LIGHT_OK')"
        rc, out, err = _run(CLIENT_PY, code, XZ=str(CLIENT))
        self.assertEqual(rc, 0, err + out)
        self.assertIn('LIGHT_OK', out)

    def test_the_scheduler_asks(self):
        app = (CLIENT / 'thinksmart_client/app.py').read_text('utf-8')
        loop = app[app.index('async def _alarm_loop'):app.index('async def _alarm_tick')]
        self.assertIn('if self.alarms.refresh(now) or (', loop)
        self.assertLess(loop.index('if self.clock_ok and now - last_tidy > 60'), loop.index('self.alarms.refresh(now)'))   # 钟对准了才算


class WordingTests(unittest.TestCase):
    def test_voice_tools(self):
        for name in ('mcp.py', 'bridge.py'):
            s = (CLIENT / 'thinksmart_client' / name).read_text('utf-8')
            self.assertNotIn('工作日（周一到周五）', s, name)
            self.assertIn('按国家放假安排', s, name)
        self.assertIn('就用 weekly 配 weekdays [1,2,3,4,5]', (CLIENT / 'thinksmart_client/mcp.py').read_text('utf-8'))

    def test_pages(self):
        h = HOME_HTML.read_text('utf-8')
        self.assertEqual(h.count('<small>工作日响，放假不响</small>'), 2)
        self.assertIn("if (a.repeat) subParts.push(String(a.repeat).replace(/（.*?）/g, ''));", h)
        self.assertIn("toast(`已设好闹钟：${it.when || ''}${it.repeat ? '，' + it.repeat : ''}`);", h)
        self.assertNotIn('周一到周五', OVERLAY.read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
