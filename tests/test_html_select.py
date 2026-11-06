"""추가 CSS 패키지가 없는 실제 import 환경과 기본 선택자 동작을 검증합니다."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from ff_gnuboard_downloader import html_select as css
from ff_gnuboard_downloader.core import DEFAULTS, Error, parse_article, parse_list, validate


HTML = '''<section id="board"><ul class="list-body">
<li class="list-item"><a id="one" class="item-subject first" href="?bo_table=sample&amp;wr_id=1" data-label="a, b > c">기록 1편</a></li>
<li class="list-item"><a id="two" class="item-subject" href="?bo_table=sample&amp;wr_id=2">기록 2편</a></li>
<li class="bo_notice"><a id="notice" class="item-subject" href="?bo_table=sample&amp;wr_id=3">공지</a></li>
</ul></section><ul class="pagination"><li class="active">1</li><li><a href="?bo_table=sample&amp;page=20">끝</a></li></ul>'''
URL = 'https://board.example/bbs/board.php?bo_table=sample'


class BasicSelectors(unittest.TestCase):
    def setUp(self):
        self.soup = BeautifulSoup(HTML, 'html.parser')
        self.without = patch.object(css, '_soupsieve', None)
        self.without.start()
        self.addCleanup(self.without.stop)

    def ids(self, selector):
        return [node.get('id') for node in css.select(self.soup, selector)]

    def test_compound_descendant_child_and_attribute(self):
        self.assertEqual(self.ids('section#board > ul.list-body li a.item-subject[href]'), ['one', 'two', 'notice'])
        self.assertEqual(self.ids('section > a'), [])
        self.assertEqual(self.ids('[data-label="a, b > c"]'), ['one'])
        self.assertEqual(self.ids("[class~='first']"), ['one'])
        self.assertEqual(self.ids('[href$="wr_id=2"]'), ['two'])
        self.assertEqual(self.ids('[href*="sample"][href^="?"]'), ['one', 'two', 'notice', None])

    def test_group_document_order_no_duplicates_and_one(self):
        self.assertEqual(self.ids('#two, .first, #one'), ['one', 'two'])
        self.assertEqual(css.select_one(self.soup, '#two, #one')['id'], 'one')
        self.assertIsNone(css.select_one(self.soup, '#missing'))

    def test_attribute_exists_empty_and_dash_prefix(self):
        soup = BeautifulSoup('<div id="a" hidden data-v="" lang="ko-KR"></div><div id="b"></div>', 'html.parser')
        for selector in ('[hidden]', '[data-v=""]', '[lang|=ko]'):
            self.assertEqual([n['id'] for n in css.select(soup, selector)], ['a'])
        for selector in ('[missing=""]', '[data-v^=""]', '[data-v*=""]'):
            self.assertEqual(css.select(soup, selector), [])

    def test_scope_and_parent_backtracking(self):
        soup = BeautifulSoup('<main><section class="target"><div><section><a id="yes"></a></section></div></section></main><a id="outside"></a>', 'html.parser')
        self.assertEqual([n['id'] for n in css.select(soup.main, 'main > .target a')], ['yes'])
        self.assertEqual([n['id'] for n in css.select(soup.main, 'a')], ['yes'])

    def test_invalid_or_advanced_selectors_are_rejected(self):
        for selector in ('', ' ', '[bad', 'a >', '> a', 'a,,b', 'a,', 'a + b', 'a:not(.x)', 'a:nth-child(2)', '[x="unterminated]'):
            with self.subTest(selector=selector), self.assertRaises(css.SelectorError):
                css.validate_selector(selector)
        with self.assertRaisesRegex(Error, '고급 CSS'):
            validate(dict(DEFAULTS, download_path='/tmp/inbox', target_path='/tmp/sorted', list_selector='a:nth-child(2)'))

    def test_gnuboard_list_notice_pagination_and_article(self):
        result = parse_list(HTML, URL, DEFAULTS, 1)
        self.assertEqual([x['id'] for x in result['rows']], ['1', '2'])
        self.assertEqual(result['last_page'], 20)
        title, text = parse_article('<h1 itemprop="headline">기록 1편<span class="wr-icon">NEW</span></h1><div itemprop="description" class="view-content"><p>첫 <b>문장</b></p><p>다음<br>줄</p><span hidden>숨김</span><script>숨김</script></div>', DEFAULTS)
        self.assertEqual(title, '기록 1편')
        self.assertEqual(text, '첫 문장\n\n다음\n줄')


class OptionalDependency(unittest.TestCase):
    def test_fresh_import_and_default_parsing_without_soupsieve(self):
        # 이미 import된 bs4/SoupSieve가 누락 환경을 가리지 않도록 새 Python에서 검사합니다.
        parent = str(Path(__file__).resolve().parents[1].parent)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(p).absolute()) for p in sys.path if p] + [parent]))
        script = '''
import sys
sys.modules['soupsieve'] = None
from ff_gnuboard_downloader.core import DEFAULTS, parse_article, parse_list, validate
from ff_gnuboard_downloader.html_select import _soupsieve
assert _soupsieve is None
validate(dict(DEFAULTS, download_path='/tmp/inbox', target_path='/tmp/sorted'))
assert parse_article('<h1 itemprop="headline">Title</h1><div id="bo_v_con">Text</div>', DEFAULTS) == ('Title', 'Text')
result = parse_list('<div id="bo_list"><div class="td_subject"><a href="?bo_table=test&wr_id=1">Title</a></div></div>', 'https://board.example/bbs/board.php?bo_table=test', DEFAULTS, 1)
assert result['rows'][0]['id'] == '1'
'''
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipIf(css._soupsieve is None, '고급 선택 패키지가 없는 환경')
    def test_advanced_selectors_still_use_installed_package(self):
        soup = BeautifulSoup(HTML, 'html.parser')
        self.assertEqual(css.select_one(soup, 'li:nth-child(2) > a')['id'], 'two')


if __name__ == '__main__':
    unittest.main()
