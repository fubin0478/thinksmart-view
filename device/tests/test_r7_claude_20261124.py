#!/opt/thinksmart-addons/venv/bin/python
"""Claude 2026-11-24 round-7 (third deep check) regression tests — home page only.

1. 天气：详情页拿到比主页卡片新的数据时，卡片也刷新（原来只有强制刷新才刷，回到主页两边数字不一样）。
2. 竖屏新建 / 编辑倒数日：日期说明放不下时按钮换到第二行，农历不再被截断；横屏样式不变。
No service is touched: the page file is only read.
"""
import os
import pathlib
import re
import unittest

HOME_HTML = pathlib.Path(os.environ.get('HOME_HTML', '/usr/local/share/thinksmart-home/index.html'))


def _media_blocks(css, query):
    """Bodies of every `@media <query> { ... }` block (brace-matched)."""
    out, i = [], 0
    head = '@media ' + query
    while True:
        i = css.find(head, i)
        if i < 0:
            return out
        j = css.index('{', i) + 1
        depth, k = 1, j
        while depth:
            c = css[k]
            depth += (c == '{') - (c == '}')
            k += 1
        out.append(css[j:k - 1])
        i = k


class HomeWeatherSyncTests(unittest.TestCase):
    def setUp(self):
        self.s = HOME_HTML.read_text('utf-8')

    def test_detail_newer_than_card_refreshes_card(self):
        fn = self.s[self.s.index('async function loadWeatherDetail('):]
        fn = fn[:fn.index('\nfunction ')]
        self.assertIn("(Number(d.updated) || 0) > (Number(lastWeatherBase.updated) || 0)", fn)
        self.assertIn("city === homeCityName() && (force || !lastWeatherBase ||", fn)
        self.assertNotIn("if (force && city === homeCityName()) loadWeather(false);", fn)

    def test_card_keeps_base_for_comparison(self):
        self.assertIn('lastWeatherBase = w;', self.s)


class CountdownPreviewPortraitTests(unittest.TestCase):
    def setUp(self):
        self.s = HOME_HTML.read_text('utf-8')

    def test_portrait_preview_wraps(self):
        blocks = [b for b in _media_blocks(self.s, '(orientation: portrait)') if '.cde-foot' in b]
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertRegex(b, r'\.cde-prev \{[^}]*flex-wrap: wrap;[^}]*height: auto;')
        self.assertRegex(b, r'\.cde-prev-l \{[^}]*max-width: 100%;')
        self.assertRegex(b, r'\.cde-prev-r \{[^}]*margin-left: auto;')

    def test_landscape_preview_unchanged(self):
        self.assertIn('.cde-prev { height: 52px; padding: 0 16px;', self.s)
        self.assertIn('.cde-prev-l { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }', self.s)


if __name__ == '__main__':
    unittest.main(verbosity=1)
