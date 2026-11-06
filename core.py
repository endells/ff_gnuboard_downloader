"""그누보드 HTML → TXT. 사이트별 본문 지식이나 접근 제한 우회 기능은 없습니다."""
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import soupsieve
from bs4 import BeautifulSoup, Comment

from .organizer import (Engine as Store, OrganizerError, Cancelled, file_digest,
                        flag, normalized, now, safe_component, validate as validate_organizer)

Error = OrganizerError
DEFAULTS = {
    'board_url': '', 'download_path': '', 'target_path': '',
    'list_selector': '.list-body .item-subject, #bo_list .td_subject a',
    'title_selector': 'h1[itemprop="headline"], #bo_v_title .bo_v_tit',
    'body_selector': '.view-content[itemprop="description"], #bo_v_con',
    'page_delay': '15', 'request_delay': '2', 'timeout': '30',
    'basic_interval': '60', 'basic_auto_start': 'False', 'latest_pages': '2',
    'minimum': '3', 'bare_numbers': 'True', 'excluded_titles': '',
    'auto_organize': 'False', 'organize_auto': 'False', 'organize_interval': '60',
}


class Fatal(Error):
    """인증·차단·서버 장애는 연속 요청하지 않고 작업 전체를 중단합니다."""


class Paused(Cancelled):
    pass


def origin(url):
    p = urlsplit(url)
    return p.scheme.lower(), (p.hostname or '').lower(), p.port or (443 if p.scheme == 'https' else 80)


def board_url(value):
    value = str(value).strip()
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError()
        origin(value)
        if any(ord(c) < 32 for c in value):
            raise ValueError()
        query = parse_qs(p.query)
        if len(query.get('bo_table', [])) != 1 or not re.fullmatch(r'[\w-]+', query['bo_table'][0]):
            raise ValueError()
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k not in ('wr_id', 'page')]
        return urlunsplit((p.scheme, p.netloc, p.path or '/bbs/board.php', urlencode(pairs), ''))
    except (ValueError, TypeError):
        raise Error('bo_table이 포함된 http(s) 게시판 목록 주소를 입력하세요.')


def page_url(url, page):
    p = urlsplit(board_url(url))
    return urlunsplit((p.scheme, p.netloc, p.path, p.query + '&page=' + str(int(page)), ''))


def board_key(url):
    p = urlsplit(board_url(url))
    return json.dumps([origin(url), p.path, parse_qs(p.query)['bo_table'][0]], ensure_ascii=False)


def matching_link(base, href, article=False):
    candidate = urljoin(base, href)
    p, b = urlsplit(candidate), urlsplit(base)
    if origin(candidate) != origin(base) or p.path != b.path or p.username or p.password:
        return None
    q, bq = parse_qs(p.query), parse_qs(b.query)
    if q.get('bo_table') != bq.get('bo_table'):
        return None
    if article and (len(q.get('wr_id', [])) != 1 or not q['wr_id'][0].isdigit()):
        return None
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))


def validate(raw, require_url=False):
    if not isinstance(raw, dict):
        raise Error('설정 형식이 올바르지 않습니다.')
    result = dict(DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS})
    if str(result['board_url']).strip():
        result['board_url'] = board_url(result['board_url'])
    elif require_url or flag(result['basic_auto_start']):
        raise Error('설정에서 게시판 주소를 저장하세요.')
    for key in ('download_path', 'target_path'):
        value = str(result[key]).strip()
        if not value or not Path(value).is_absolute() or '\x00' in value:
            raise Error('다운로드·정리 경로는 FF 컨테이너 내부의 절대 경로로 입력하세요.')
        result[key] = os.path.normpath(value)
    for key, low, high in [('page_delay', 1, 3600), ('request_delay', 1, 300), ('timeout', 5, 120),
                           ('basic_interval', 1, 10080), ('latest_pages', 1, 100),
                           ('minimum', 1, 100000), ('organize_interval', 1, 10080)]:
        try:
            value = float(result[key]) if key in ('page_delay', 'request_delay') else int(str(result[key]))
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError()
            result[key] = value
        except (TypeError, ValueError):
            raise Error('%s 값은 %s~%s 범위로 입력하세요.' % (key, low, high))
    for key in ('basic_auto_start', 'bare_numbers', 'auto_organize', 'organize_auto'):
        result[key] = flag(result[key])
    for key in ('list_selector', 'title_selector', 'body_selector'):
        value = str(result[key]).strip()
        try:
            if not value or len(value) > 2000:
                raise ValueError()
            soupsieve.compile(value)
        except Exception:
            raise Error(key + ': CSS 선택자 형식이 올바르지 않습니다.')
        result[key] = value
    result['excluded_titles'] = str(result['excluded_titles'])
    organizer_config(result)
    return result


def organizer_config(config):
    return validate_organizer({
        'source_path': config['download_path'], 'target_path': config['target_path'],
        'minimum': config['minimum'], 'bare_numbers': config['bare_numbers'],
        'excluded_titles': config['excluded_titles'],
        'basic_interval': config['organize_interval'], 'basic_auto_start': config['organize_auto'],
    })


def fingerprint(config):
    # 요청·파싱·저장 설정이 변경되면 이전 테스트 및 이어받기와 섞지 않습니다.
    keys = ('board_url', 'download_path', 'list_selector', 'title_selector', 'body_selector')
    return hashlib.sha256(json.dumps([config[k] for k in keys], ensure_ascii=False).encode()).hexdigest()


def cleaned_title(node):
    node = BeautifulSoup(str(node), 'html.parser')
    for unwanted in node.select('.wr-icon, .cnt_cmt, .sound_only, .sr-only, script, style, [hidden]'):
        unwanted.decompose()
    return normalized(node.get_text(' ', strip=True))


def parse_list(html, base, config, requested_page):
    soup = BeautifulSoup(html, 'html.parser')
    active = soup.select_one('.pagination .active, .pg_current')
    if active:
        digits = re.search(r'\d+', active.get_text(' ', strip=True))
        if digits and int(digits[0]) != requested_page:
            raise Fatal('요청한 페이지와 응답의 현재 페이지가 다릅니다. 반복 수집을 중단했습니다.')
    rows, seen = [], set()
    for node in soup.select(config['list_selector']):
        link = matching_link(base, node.get('href', ''), article=True)
        if not link:
            continue
        pid = parse_qs(urlsplit(link).query)['wr_id'][0]
        title = cleaned_title(node)
        if pid in seen or not title:
            continue
        seen.add(pid)
        # 공지글은 오래된 글이어도 모든 페이지에 반복되므로 수집 대상에서 제외합니다.
        parent = node.find_parent(['li', 'tr'])
        notice = bool(parent and (parent.select_one('.bo_notice, .notice_icon, .wr-notice') or
                      'bo_notice' in parent.get('class', []) or
                      any(x.get_text(strip=True) == '공지' for x in parent.select('.wr-num, .td_num'))))
        if not notice:
            rows.append({'id': pid, 'title': title, 'url': link})
    pages = [requested_page]
    for node in soup.select('.pagination a[href], .pg_wrap a[href], .list-page a[href]'):
        link = matching_link(base, node['href'])
        if link:
            page = parse_qs(urlsplit(link).query).get('page', [''])[0]
            if page.isdigit() and 1 <= int(page) <= 1000000:
                pages.append(int(page))
    # 빈 목록과 로그인/차단/선택자 불일치를 구별합니다.
    explicit_empty = bool(soup.select_one('.empty_table, .empty_list, .no-data, .list-none'))
    if not rows and not explicit_empty:
        raise Fatal('목록 글을 찾지 못했습니다. 로그인 상태 또는 목록 CSS 선택자를 확인하세요.')
    return {'rows': rows, 'last_page': max(pages), 'page': requested_page,
            'empty': explicit_empty and not rows}


def parse_article(html, config):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one(config['title_selector'])
    body = soup.select_one(config['body_selector'])
    if title_node is None or body is None:
        raise Fatal('제목/본문 영역을 찾지 못했습니다. 로그인 상태 또는 CSS 선택자를 확인하세요.')
    title = cleaned_title(title_node)
    for node in body.select('script, style, noscript, iframe, form, button, .sound_only, .sr-only, [hidden]'):
        node.decompose()
    for node in body.select('[style]'):
        if re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', node.get('style', ''), re.I):
            node.decompose()
    for node in body.find_all(string=lambda x: isinstance(x, Comment)):
        node.extract()
    for node in body.find_all('br'):
        node.replace_with('\n')
    for node in body.find_all(['p', 'div', 'section', 'blockquote', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'tr']):
        node.insert_before('\n')
        node.insert_after('\n\n')
    for node in body.find_all(['td', 'th']):
        node.insert_after('\t')
    text = body.get_text().replace('\xa0', ' ').replace('\r', '')
    text = '\n'.join(re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines())
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not title or not text:
        raise Error('제목 또는 본문이 비어 있어 TXT를 저장하지 않았습니다.')
    return title, text


class Client:
    def __init__(self, config, cookie='', check=None):
        self.config, self.check = config, check or (lambda: None)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'FF-Gnuboard-TXT/0.1.0', 'Accept': 'text/html'})
        if cookie:
            self.session.headers['Cookie'] = cookie
        self.last_request = 0.0

    def close(self):
        self.session.close()

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(.2, max(0, end - time.monotonic())))
        self.check()

    def get(self, url):
        if origin(url) != origin(self.config['board_url']):
            raise Fatal('설정된 사이트와 다른 호스트에는 요청하지 않습니다.')
        current = url
        redirects = 0
        for attempt in range(3):
            self.wait(max(0, self.last_request + self.config['request_delay'] - time.monotonic()))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(current, timeout=(10, self.config['timeout']),
                                            allow_redirects=False, stream=True)
                with response:
                    if 300 <= response.status_code < 400:
                        redirects += 1
                        target = urljoin(current, response.headers.get('Location', ''))
                        if redirects > 2 or not matching_link(self.config['board_url'], target):
                            raise Fatal('로그인 또는 다른 주소로 이동했습니다. 주소·로그인 상태를 확인하세요.')
                        expected = parse_qs(urlsplit(url).query)
                        actual = parse_qs(urlsplit(target).query)
                        if any(actual.get(k) != expected.get(k) for k in ('wr_id', 'page')):
                            raise Fatal('리다이렉트로 게시글 또는 페이지가 변경되어 중단했습니다.')
                        current = target
                        continue
                    if response.status_code in (401, 403):
                        raise Fatal('HTTP %s: 접근이 거부되었습니다. 로그인 상태를 확인하세요.' % response.status_code)
                    if response.status_code in (429, 500, 502, 503, 504):
                        retry = response.headers.get('Retry-After', '')
                        wait = int(retry) if retry.isdigit() else 5 * (attempt + 1)
                        if attempt == 2 or wait > 300:
                            raise Fatal('HTTP %s: 서버 제한/오류로 중단했습니다. 나중에 이어받으세요.' % response.status_code)
                        response.close()
                        self.wait(max(5, wait))
                        continue
                    if response.status_code >= 400:
                        raise Error('HTTP %s: 게시글을 가져오지 못했습니다.' % response.status_code)
                    content_type = response.headers.get('Content-Type', '').lower()
                    if content_type and 'html' not in content_type:
                        raise Fatal('HTML 응답이 아니어서 중단했습니다.')
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        self.check()
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            raise Fatal('한 페이지의 HTML이 8MB를 초과했습니다.')
                        chunks.append(chunk)
                    data = b''.join(chunks)
                    # 메타 charset과 BOM을 BeautifulSoup이 해석하게 바이트를 전달합니다.
                    return data
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise Fatal('연결/응답 시간이 초과되었습니다. 완료된 파일은 유지됩니다.') from None
                self.wait(5 * (attempt + 1))
            except requests.RequestException:
                raise Fatal('HTTP 요청 실패. 주소와 네트워크 설정을 확인하세요.') from None
        raise Fatal('주소 이동이 반복되어 중단했습니다.')


def save_txt(folder, title, pid, data, scope):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise Error('다운로드 경로에는 심볼릭 링크 대신 실제 경로를 지정하세요.')
    checksum = hashlib.sha256(data).hexdigest()
    stem = safe_component(title, 170)
    names = [stem + '.txt', stem + ' [글-' + pid + '-' + scope + '].txt',
             stem + ' [글-' + pid + '-' + checksum[:12] + '].txt']
    fd, tmp = tempfile.mkstemp(prefix='.gnuboard-', dir=str(folder))
    try:
        with os.fdopen(fd, 'wb') as writer:
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
            os.fchmod(writer.fileno(), 0o644)
        for name in names:
            target = folder / name
            try:
                os.link(tmp, str(target))
                return str(target), checksum
            except FileExistsError:
                if target.is_symlink():
                    raise Error('같은 이름의 심볼릭 링크가 있어 저장하지 않았습니다.')
                if target.is_file() and file_digest(target) == checksum:
                    return str(target), checksum
        raise Error('같은 이름의 다른 파일이 있어 저장하지 않았습니다.')
    finally:
        Path(tmp).unlink(missing_ok=True)


class Engine(Store):
    def __init__(self, db_path, client_factory=Client):
        super().__init__(db_path)
        self.pause_path = self.db_path.with_suffix('.pause')
        self.client_factory = client_factory
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS posts (
                    board TEXT, post_id TEXT, title TEXT, path TEXT, sha TEXT, status TEXT,
                    error TEXT, updated TEXT, PRIMARY KEY(board, post_id));
            ''')

    def check(self):
        if self.stop_path.exists():
            raise Cancelled('중지했습니다. 완료된 TXT는 유지됩니다.')
        if self.pause_path.exists():
            raise Paused('일시정지했습니다. 이어받기로 현재 페이지부터 계속할 수 있습니다.')

    def snapshot(self):
        state = self.get('state', {'status': 'idle', 'message': '페이지 분석부터 시작하세요.'})
        handle = self.acquire()
        if handle is not None:
            try:
                state = self.get('state', state)
                if state['status'] == 'running':
                    state.update(status='interrupted', message='이전 작업이 중단되었습니다. 이어받을 수 있습니다.')
                    self.put('state', state)
                    with self.connect() as db:
                        db.execute("UPDATE runs SET status='interrupted', finished=? WHERE status='running'", (now(),))
            finally:
                handle.close()
        state['can_resume'] = bool(self.get('checkpoint')) and state['status'] != 'running'
        return state

    def pause(self):
        self.pause_path.touch()
        return '일시정지를 요청했습니다. 진행 중인 응답은 최대 설정된 시간까지 기다릴 수 있습니다.'

    def stop(self):
        self.stop_path.touch()
        # 실행 중이면 worker가 종료 시 체크포인트를 제거합니다.
        handle = self.acquire()
        if handle is not None:
            try:
                self.put('checkpoint', None)
                state = self.get('state', {})
                state.update(status='cancelled', message='작업을 중지했습니다. 완료된 TXT는 유지됩니다.')
                self.put('state', state)
            finally:
                handle.close()
        return '중지를 요청했습니다. 완료된 TXT는 유지됩니다.'

    def start(self, mode, config, start=1, end=1, cookie='', on_finished=None):
        config = validate(config, require_url=True)
        if mode not in ('analyze', 'range', 'until', 'latest', 'resume'):
            raise Error('알 수 없는 작업입니다.')
        start, end = int(start), int(end)
        if not 1 <= start <= 1000000 or not 1 <= end <= 1000000:
            raise Error('페이지 번호는 1~1000000 범위입니다.')
        handle = self.acquire()
        if handle is None:
            raise Error('다운로드 또는 정리 작업이 실행 중입니다.')
        try:
            saved = self.get('checkpoint')
            if mode == 'latest' and saved:
                previous_state = self.get('state', {})
                if saved.get('mode') == 'latest' and previous_state.get('status') in ('failed', 'interrupted'):
                    # 예약 확인의 일시적인 실패는 다음 예약 때 첫 페이지부터 재시도합니다.
                    saved = None
                    self.put('checkpoint', None)
                else:
                    raise Error('이어받을 작업이 있어 이번 예약 수집을 건너뜁니다.')
            if mode == 'resume':
                if not saved or saved['fingerprint'] != fingerprint(config):
                    raise Error('이어받을 작업이 없거나 주소·저장 경로·선택자가 변경되었습니다.')
                mode, start, end = saved['mode'], saved['next_page'], saved['end']
            else:
                if saved:
                    raise Error('이어받을 작업이 있습니다. 이어받기 또는 중지 후 새 작업을 시작하세요.')
                if mode == 'latest':
                    start, end = 1, config['latest_pages']
                if mode == 'until' or (mode == 'range' and end > start):
                    if self.get('tested') != fingerprint(config):
                        raise Error('먼저 한 페이지 받기로 TXT 저장을 확인하세요.')
                if mode in ('range', 'latest') and end < start:
                    raise Error('끝 페이지는 시작 페이지 이상이어야 합니다.')
            self.stop_path.unlink(missing_ok=True)
            self.pause_path.unlink(missing_ok=True)
            checkpoint = {'mode': mode, 'next_page': start, 'end': end, 'fingerprint': fingerprint(config)}
            if mode != 'analyze':
                self.put('checkpoint', checkpoint)
            else:
                self.put('analysis', None)
            self.put('state', {'status': 'running', 'mode': mode, 'message': '작업을 시작합니다.',
                               'page': start, 'downloaded': 0, 'skipped': 0, 'failed': 0})
            self.thread = threading.Thread(target=self._work,
                args=(mode, config, checkpoint, cookie, handle, on_finished), daemon=True)
            self.thread.start()
        except Exception:
            handle.close()
            raise

    def known(self, scope, pid, folder):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM posts WHERE board=? AND post_id=?', (scope, pid)).fetchone()
        if not row or row['status'] != 'completed':
            return False
        path = Path(row['path'])
        try:
            return (not path.is_symlink() and path.parent.resolve() == Path(folder).resolve()
                    and path.is_file() and file_digest(path, self.stop_path.exists) == row['sha'])
        except OSError:
            return False

    def record(self, scope, item, title, path, sha, status, error=''):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?)',
                       (scope, item['id'], title, path, sha, status, error, now()))

    def _work(self, mode, config, checkpoint, cookie, handle, callback):
        state = self.get('state')
        run_id, client, success = None, None, False
        try:
            with self.connect() as db:
                run_id = db.execute('INSERT INTO runs(started,mode,status) VALUES (?,?,?)',
                                    (now(), mode, 'running')).lastrowid
            client = self.client_factory(config, cookie=cookie, check=self.check)
            scope, seen_pages = board_key(config['board_url']), set()
            scope_short = hashlib.sha256(scope.encode()).hexdigest()[:8]
            page = checkpoint['next_page']
            while page <= 1000000:
                self.check()
                state.update(page=page, message='%s페이지 목록을 확인합니다.' % page)
                self.put('state', state)
                result = parse_list(client.get(page_url(config['board_url'], page)), config['board_url'], config, page)
                if mode == 'analyze':
                    self.put('analysis', result)
                    state['message'] = '%s페이지 · 글 %s개 · 확인된 마지막 페이지 %s' % (page, len(result['rows']), result['last_page'])
                    break
                if result['empty']:
                    state['message'] = '빈 목록을 확인해 종료했습니다.'
                    break
                ids = tuple(sorted(x['id'] for x in result['rows']))
                if ids in seen_pages:
                    raise Fatal('다른 페이지에서 같은 글 목록이 반복되어 중단했습니다.')
                seen_pages.add(ids)
                state['page_total'], state['page_done'] = len(result['rows']), 0
                for item in result['rows']:
                    self.check()
                    state['message'] = '%s페이지 · %s' % (page, item['title'])
                    self.put('state', state)
                    try:
                        if self.known(scope, item['id'], config['download_path']):
                            state['skipped'] += 1
                        else:
                            title, body = parse_article(client.get(item['url']), config)
                            self.check()
                            data = (title + '\n\n' + body + '\n').encode('utf-8')
                            path, sha = save_txt(config['download_path'], title, item['id'], data, scope_short)
                            self.record(scope, item, title, path, sha, 'completed')
                            self.add_result(run_id, title, item['id'], 'downloaded', path, '')
                            state['downloaded'] += 1
                    except Cancelled:
                        raise
                    except (Error, OSError) as exc:
                        state['failed'] += 1
                        self.record(scope, item, item['title'], '', '', 'failed', str(exc))
                        self.add_result(run_id, item['title'], item['id'], 'failed', '', str(exc))
                        if isinstance(exc, Fatal):
                            raise
                    state['page_done'] += 1
                    self.put('state', state)
                limit = result['last_page'] if mode == 'until' else min(checkpoint['end'], result['last_page'])
                if page >= limit:
                    break
                page += 1
                checkpoint['next_page'] = page
                self.put('checkpoint', checkpoint)
                state['message'] = '다음 페이지까지 %s초 대기합니다.' % config['page_delay']
                self.put('state', state)
                client.wait(config['page_delay'])
            if mode != 'analyze':
                if state['downloaded'] + state['skipped'] and not state['failed']:
                    self.put('tested', fingerprint(config))
                self.put('checkpoint', None)
                state['message'] = '저장 %s개 · 기존 %s개 · 실패 %s개' % (state['downloaded'], state['skipped'], state['failed'])
            state['status'] = 'completed_with_errors' if state['failed'] else 'completed'
            success = mode != 'analyze'
        except Paused as exc:
            state.update(status='paused', message=str(exc))
        except Cancelled as exc:
            self.put('checkpoint', None)
            state.update(status='cancelled', message=str(exc))
        except Exception as exc:
            state.update(status='failed', message=str(exc))
        finally:
            try:
                if client:
                    client.close()
                self.put('state', state)
                if run_id is not None:
                    with self.connect() as db:
                        db.execute('UPDATE runs SET finished=?,status=?,summary=? WHERE id=?',
                                   (now(), state['status'], json.dumps(state, ensure_ascii=False), run_id))
            finally:
                handle.close()
            if success and callback:
                callback(config)

    def post_history(self, page=1, failed=False):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            where = "WHERE status='failed'" if failed else ''
            rows = db.execute('SELECT * FROM posts ' + where + ' ORDER BY updated DESC LIMIT 51 OFFSET ?',
                              ((page - 1) * 50,)).fetchall()
        return {'rows': [dict(r) for r in rows[:50]], 'page': page, 'more': len(rows) > 50}
