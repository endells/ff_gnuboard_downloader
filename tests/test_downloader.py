"""대상 사이트에 접속하지 않는 합성 HTML·HTTP·파일 시험입니다."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from ff_gnuboard_downloader.core import (Client, DEFAULTS, Engine, Error, Fatal,
    board_key, board_url, fingerprint, organizer_config, page_url, parse_article,
    parse_list, save_txt, validate)
from ff_gnuboard_downloader.organizer import Engine as Organizer

URL = 'https://board.example/bbs/board.php?bo_table=stories'


def listing(page=1, ids=('101', '102'), last=3, active=True):
    rows = ''.join('<li class="list-item"><div class="wr-num">15</div><div class="wr-subject">'
                   '<a class="item-subject" href="/bbs/board.php?bo_table=stories&amp;wr_id=%s&amp;page=%s">'
                   '<span class="wr-icon">NEW</span>별빛 여행 - %s편</a></div></li>' % (pid, page, i + 1)
                   for i, pid in enumerate(ids))
    return ('<ul class="list-body">' + rows + '</ul><ul class="pagination">' +
            ('<li class="active"><a>%s</a></li>' % page if active else '') +
            '<li><a href="/bbs/board.php?bo_table=stories&amp;page=%s"><i class="fa fa-angle-double-right"></i></a></li></ul>' % last)


def article(pid='101'):
    return ('<h1 itemprop="headline">별빛 여행 - %s편</h1>'
            '<div itemprop="description" class="view-content"><p>첫 <b>문단</b>입니다.</p>'
            '<p>둘째 줄<br>이어지는 줄</p><script>SECRET</script></div><aside>OUTSIDE</aside>') % pid


class FakeClient:
    calls = []
    responses = {}
    hook = None

    def __init__(self, config, cookie='', check=None):
        self.check = check

    def get(self, url):
        self.check()
        q = parse_qs(urlsplit(url).query)
        key = 'id:' + q['wr_id'][0] if 'wr_id' in q else 'page:' + q['page'][0]
        type(self).calls.append(key)
        if type(self).hook:
            type(self).hook(key)
        if key in type(self).responses:
            value = type(self).responses[key]
            if isinstance(value, Exception):
                raise value
            return value
        if 'wr_id' in q:
            return article(q['wr_id'][0])
        page = int(q['page'][0])
        return listing(page, (str(page * 100 + 1), str(page * 100 + 2)), 3)

    def wait(self, seconds):
        self.check()

    def close(self):
        pass


class Parsers(unittest.TestCase):
    def test_supplied_structure_and_last_arrow(self):
        data = parse_list(listing(last=467), URL, DEFAULTS, 1)
        self.assertEqual([x['id'] for x in data['rows']], ['101', '102'])
        self.assertEqual(data['rows'][0]['title'], '별빛 여행 - 1편')
        self.assertEqual(data['last_page'], 467)

    def test_filter_other_host_board_and_duplicate(self):
        html = listing() + listing()
        html += '<a class="item-subject" href="https://other.example/bbs/board.php?bo_table=stories&wr_id=9">외부</a>'
        config = dict(DEFAULTS, list_selector='a.item-subject')
        self.assertEqual(len(parse_list(html, URL, config, 1)['rows']), 2)
        self.assertEqual(len(parse_list(listing().replace('bo_table=stories', 'bo_table=other') +
                                       '<div class="empty_table"></div>', URL, config, 1)['rows']), 0)

    def test_article_preserves_inline_and_blocks(self):
        title, body = parse_article(article(), DEFAULTS)
        self.assertEqual(title, '별빛 여행 - 101편')
        self.assertEqual(body, '첫 문단입니다.\n\n둘째 줄\n이어지는 줄')

    def test_empty_or_login_never_treated_as_success(self):
        for html in ('<html>로그인 필요</html>', '<html>차단 안내</html>'):
            with self.assertRaises(Fatal):
                parse_list(html, URL, DEFAULTS, 1)
            with self.assertRaises(Fatal):
                parse_article(html, DEFAULTS)
        with self.assertRaises(Error):
            parse_article('<h1 itemprop="headline">제목</h1><div class="view-content" itemprop="description"></div>', DEFAULTS)
        self.assertTrue(parse_list('<div class="empty_table">없음</div>', URL, DEFAULTS, 1)['empty'])

    def test_wrong_active_page_fails(self):
        with self.assertRaises(Fatal):
            parse_list(listing(page=1), URL, DEFAULTS, 2)

    def test_board_url_and_scope(self):
        self.assertEqual(board_url(URL + '&wr_id=99&page=33#foo'), URL)
        self.assertEqual(page_url(URL + '&page=4', 2), URL + '&page=2')
        self.assertEqual(board_key(URL), board_key(URL + '&page=4'))
        for value in ('file:///tmp/a', 'https://user:pass@board.example/bbs/board.php?bo_table=a', 'https://board.example/'):
            with self.assertRaises(Error):
                board_url(value)

    def test_basic_gnuboard_skin(self):
        html = '<div id="bo_list"><table><tr><td class="td_subject"><a href="?bo_table=stories&wr_id=99">기록 1화<span class="cnt_cmt">3</span></a></td></tr></table></div>'
        self.assertEqual(parse_list(html, URL, DEFAULTS, 1)['rows'][0]['title'], '기록 1화')
        title, body = parse_article('<div id="bo_v_title"><span class="bo_v_tit">기록 1화</span></div><div id="bo_v_con">합성 본문</div>', DEFAULTS)
        self.assertEqual((title, body), ('기록 1화', '합성 본문'))

    def test_encoding_and_hidden_content(self):
        html = '<meta charset="euc-kr"><h1 itemprop="headline">합성 기록</h1><div class="view-content" itemprop="description"><p>문서</p><span hidden>숨김</span><!--주석--><span style="display:none">감춤</span></div>'
        self.assertEqual(parse_article(html.encode('euc-kr'), DEFAULTS), ('합성 기록', '문서'))


class Work(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conf = validate(dict(DEFAULTS, board_url=URL, download_path=str(self.root / 'inbox'),
                                  target_path=str(self.root / 'sorted')))
        self.engine = Engine(self.root / 'history.db', FakeClient)
        FakeClient.calls, FakeClient.responses, FakeClient.hook = [], {}, None

    def tearDown(self):
        if self.engine.thread:
            self.engine.thread.join(5)
        self.temp.cleanup()

    def run_job(self, mode='range', start=1, end=1):
        self.engine.start(mode, self.conf, start, end)
        self.engine.thread.join(5)
        self.assertFalse(self.engine.thread.is_alive())
        return self.engine.snapshot()

    def test_analysis_does_not_fetch_detail_or_write_txt(self):
        self.assertEqual(self.run_job('analyze')['status'], 'completed')
        self.assertEqual(FakeClient.calls, ['page:1'])
        self.assertFalse((self.root / 'inbox').exists())

    def test_download_dedup_and_missing_file_repair(self):
        self.assertEqual(self.run_job()['downloaded'], 2)
        paths = list((self.root / 'inbox').glob('*.txt'))
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].read_text().startswith('별빛 여행'))
        FakeClient.calls = []
        state = self.run_job()
        self.assertEqual((state['downloaded'], state['skipped']), (0, 2))
        self.assertEqual(FakeClient.calls, ['page:1'])
        paths[0].unlink()
        self.assertEqual(self.run_job()['downloaded'], 1)

    def test_bulk_requires_actual_successful_page_test(self):
        self.run_job('analyze')
        with self.assertRaises(Error):
            self.engine.start('until', self.conf)
        self.run_job()
        state = self.run_job('until')
        self.assertEqual((state['downloaded'], state['skipped'], state['page']), (4, 2, 3))

    def test_range_and_interval_calls(self):
        self.run_job()
        FakeClient.calls = []
        state = self.run_job('range', 2, 3)
        self.assertEqual(state['downloaded'], 4)
        self.assertEqual([x for x in FakeClient.calls if x.startswith('page:')], ['page:2', 'page:3'])

    def test_page_repeat_without_current_marker_fails(self):
        self.run_job()
        FakeClient.responses = {'page:2': listing(1, active=False)}
        state = self.run_job('until')
        self.assertEqual(state['status'], 'failed')
        self.assertIn('반복', state['message'])

    def test_auth_failure_checkpoint_and_resume(self):
        FakeClient.responses['id:102'] = Fatal('합성 인증 실패')
        state = self.run_job()
        self.assertEqual((state['status'], state['downloaded'], state['can_resume']), ('failed', 1, True))
        FakeClient.responses = {}
        FakeClient.calls = []
        state = self.run_job('resume')
        self.assertEqual((state['downloaded'], state['skipped']), (1, 1))
        self.assertNotIn('id:101', FakeClient.calls)
        self.assertFalse(state['can_resume'])

    def test_pause_between_items_and_resume_after_restart(self):
        def hook(key):
            if key == 'id:102':
                self.engine.pause()
        FakeClient.hook = hook
        state = self.run_job()
        self.assertEqual((state['status'], state['downloaded']), ('paused', 1))
        other = Engine(self.root / 'history.db', FakeClient)
        FakeClient.hook = None
        other.start('resume', self.conf)
        other.thread.join(5)
        self.assertEqual(other.snapshot()['downloaded'], 1)

    def test_stop_discards_checkpoint_and_keeps_done_files(self):
        FakeClient.hook = lambda key: self.engine.stop_path.touch() if key == 'id:102' else None
        state = self.run_job()
        self.assertEqual(state['status'], 'cancelled')
        self.assertFalse(state['can_resume'])
        self.assertEqual(len(list((self.root / 'inbox').glob('*.txt'))), 1)

    def test_partial_failure_persists_then_rerun_repairs(self):
        FakeClient.responses['id:102'] = Error('HTTP 404')
        state = self.run_job()
        self.assertEqual(state['status'], 'completed_with_errors')
        self.assertEqual(len(self.engine.post_history(failed=True)['rows']), 1)
        FakeClient.responses = {}
        self.assertEqual(self.run_job()['downloaded'], 1)
        self.assertFalse(self.engine.post_history(failed=True)['rows'])

    def test_title_collision_keeps_existing_bytes(self):
        folder = self.root / 'inbox'
        a, _ = save_txt(folder, '같은 제목 1편', '1', b'first', 'scope')
        b, _ = save_txt(folder, '같은 제목 1편', '2', b'second', 'scope')
        self.assertNotEqual(a, b)
        self.assertEqual(Path(a).read_bytes(), b'first')
        self.assertEqual(save_txt(folder, '같은 제목 1편', '2', b'second', 'scope')[0], b)

    def test_conflicting_corrupt_file_not_overwritten(self):
        self.run_job()
        path = next((self.root / 'inbox').glob('*.txt'))
        path.write_bytes(b'externally modified')
        self.assertEqual(self.run_job()['downloaded'], 1)
        self.assertEqual(path.read_bytes(), b'externally modified')

    def test_shared_lock_blocks_organizer_during_download(self):
        organizer = Organizer(self.root / 'org.db')
        organizer.lock_path = self.engine.lock_path
        handle = self.engine.acquire()
        try:
            with self.assertRaises(Error):
                organizer.start('preview', organizer_config(self.conf))
        finally:
            handle.close()

    def test_scheduled_skips_paused_job(self):
        self.engine.put('checkpoint', {'mode': 'range'})
        with self.assertRaises(Error):
            self.engine.start('latest', self.conf)

    def test_scheduled_failure_retries_on_next_interval(self):
        FakeClient.responses['page:1'] = Fatal('합성 연결 실패')
        self.assertEqual(self.run_job('latest')['status'], 'failed')
        FakeClient.responses = {}
        self.assertEqual(self.run_job('latest')['downloaded'], 4)

    def test_config_validation_and_fingerprint(self):
        for delta in ({'request_delay': 'NaN'}, {'minimum': 0}, {'list_selector': '[bad'},
                      {'download_path': 'relative'}, {'target_path': self.conf['download_path']}):
            with self.assertRaises(Error):
                validate(dict(self.conf, **delta))
        self.assertEqual(fingerprint(self.conf), fingerprint(dict(self.conf, page_delay=90)))
        self.assertNotEqual(fingerprint(self.conf), fingerprint(dict(self.conf, body_selector='#new')))


class Http(unittest.TestCase):
    def config(self):
        return dict(DEFAULTS, board_url=URL, timeout=30, request_delay=1)

    def response(self, code=200, data=b'<html></html>', headers=None):
        r = Mock(status_code=code, headers=headers or {'Content-Type': 'text/html'})
        r.__enter__ = Mock(return_value=r)
        r.__exit__ = Mock(return_value=False)
        r.iter_content.return_value = [data]
        return r

    def test_no_cross_origin_cookie_redirect(self):
        client = Client(self.config(), cookie='session=secret')
        client.wait = Mock()
        client.session.get = Mock(return_value=self.response(302, headers={'Location': 'https://other.example/'}))
        with self.assertRaises(Fatal):
            client.get(URL)
        self.assertEqual(client.session.get.call_count, 1)
        client.close()

    def test_retry_after_and_bounded_timeout(self):
        client = Client(self.config())
        client.wait = Mock()
        client.session.get = Mock(side_effect=[self.response(429, headers={'Retry-After': '7'}), self.response()])
        self.assertEqual(client.get(URL), b'<html></html>')
        self.assertIn(unittest.mock.call(7), client.wait.call_args_list)
        client.session.get = Mock(side_effect=requests.Timeout())
        with self.assertRaises(Fatal):
            client.get(URL)
        self.assertEqual(client.session.get.call_count, 3)
        client.close()

    def test_block_response_stops_immediately(self):
        client = Client(self.config())
        client.wait = Mock()
        client.session.get = Mock(return_value=self.response(403))
        with self.assertRaises(Fatal):
            client.get(URL)
        self.assertEqual(client.session.get.call_count, 1)
        client.close()


if __name__ == '__main__':
    unittest.main()
