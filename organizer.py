"""네트워크 연결 없이 로컬 파일명만 분석하고 원본 바이트를 복사합니다."""
import copy
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import unicodedata
from pathlib import Path


class OrganizerError(Exception):
    pass


class Cancelled(OrganizerError):
    pass


KST = datetime.timezone(datetime.timedelta(hours=9))
DEFAULTS = {
    'source_path': '', 'target_path': '', 'minimum': '3',
    'bare_numbers': 'False', 'excluded_titles': '',
    'basic_interval': '60', 'basic_auto_start': 'False',
}


def now():
    return datetime.datetime.now(KST).isoformat(timespec='seconds')


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', value)).strip()


def flag(value):
    return value in (True, 'True', 'true', '1', 1)


def validate(raw):
    result = dict(DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS})
    for key in ('source_path', 'target_path'):
        path = str(result[key]).strip()
        if not path or '\x00' in path or not Path(path).is_absolute():
            raise OrganizerError('원본·정리 경로는 FF 컨테이너 내부의 절대 경로로 입력하세요.')
        result[key] = os.path.normpath(path)
    source, target = Path(result['source_path']), Path(result['target_path'])
    if source.resolve() == target.resolve():
        raise OrganizerError('원본 폴더와 정리 폴더를 서로 다르게 지정하세요.')
    try:
        result['minimum'] = int(str(result['minimum']))
        result['basic_interval'] = int(str(result['basic_interval']))
    except (ValueError, TypeError):
        raise OrganizerError('최소 편수와 실행 간격은 정수로 입력하세요.')
    if not 1 <= result['minimum'] <= 100000:
        raise OrganizerError('최소 편수는 1~100000편 범위입니다.')
    if not 1 <= result['basic_interval'] <= 10080:
        raise OrganizerError('실행 간격은 1~10080분 범위입니다.')
    result['bare_numbers'] = flag(result['bare_numbers'])
    result['basic_auto_start'] = flag(result['basic_auto_start'])
    result['excluded_titles'] = str(result['excluded_titles']).strip()
    return result


def title_parts(stem, bare_numbers=False):
    """경계가 명확한 끝 회차만 제거. '천하', '1984' 같은 본제목은 보존."""
    title = normalized(stem)
    # 이미 번호를 붙인 마지막편도 같은 작품의 마지막 회차로 인식합니다.
    terminal = re.fullmatch(r'(.+?)\s*[-_:–—]?\s*(?:제\s*)?\d+\s*(?:화|회|편|부|장)\s*[-_:–—]\s*마지막\s*(?:편|부|화|회|장)', title)
    if terminal:
        return terminal[1].strip(), 'last', [2, 0]
    title = re.sub(r' \[(?:글-\d+(?:-[0-9a-f]+)?|[0-9a-f]{10})\]$', '', title)
    # 완결 표시는 회차 판별 전에 제거하되, 분명한 구분자가 있을 때만 적용합니다.
    title = re.sub(r'(?:\s*[\[(](?:완결|완|끝)[\])]|\s+[-–—]?\s*(?:완결|마침))$', '', title).strip()
    label = r'(?:제\s*\d+\s*(?:화|회|편|부|장)|\d+\s*(?:화|회|편|부|장)|[상중하](?:편)?|마지막\s*(?:편|부|화|회|장))'
    patterns = [
        r'^(?P<title>.+?)\s*[\[(]\s*(?P<episode>' + label + r'|\d+)\s*[\])]$',
        r'^(?P<title>.+?)\s*[-_:–—]?\s*(?P<episode>(?:제\s*)?\d+\s*(?:화|회|편|부|장))$',
        r'^(?P<title>.+?)(?:\s+|\s*[-_:–—]\s*)(?P<episode>[상중하](?:편)?|마지막\s*(?:편|부|화|회|장))$',
    ]
    if bare_numbers:
        patterns.append(r'^(?P<title>.+?)(?:\s+|\s*[-_:–—]\s*)(?P<episode>\d+)$')
    for pattern in patterns:
        match = re.fullmatch(pattern, title)
        if not match:
            continue
        base = match['title'].rstrip(' -_:–—').strip()
        if not base:
            continue
        episode = re.sub(r'\s+', '', match['episode'])
        digits = re.search(r'\d+', episode)
        if digits:
            number = int(digits.group())
            episode_key, order = 'n:' + str(number), [0, number]
        elif episode.startswith(('상', '중', '하')):
            part = episode[0]
            episode_key, order = 'p:' + part, [1, {'상': 1, '중': 2, '하': 3}[part]]
        else:
            episode_key, order = 'last', [2, 0]
        return base, episode_key, order
    return title or normalized(stem), 'unnumbered', [3, 0]


def preferred_style(titles):
    """숫자 회차별 한 표. 동률이면 가장 이른 회차의 표기를 선택합니다."""
    votes = {}
    for title in titles:
        base, episode, order = title_parts(title, True)
        if order[0] != 0:
            continue
        match = re.fullmatch(re.escape(base) + r'(?P<before>\s*[-_:–—]?\s*(?:제\s*)?)(?P<number>\d+)(?P<after>\s*[화회편부장])', title)
        if not match:
            continue
        number = match['number']
        style = (match['before'], match['after'], len(number) if number.startswith('0') and len(number) > 1 else 0)
        votes.setdefault(style, set()).add(order[1])
    if not votes:
        return None
    return min(votes, key=lambda style: (-len(votes[style]), min(votes[style]), style))


def styled_name(name, title, style, maximum):
    base, episode, order = title_parts(title, True)
    if episode == 'last':
        if not maximum:
            return name
        style = style or (' - ', '편', 0)
        number = maximum + 1
        separator = re.sub(r'제\s*$', '', style[0])
        separator = separator if re.search(r'[-_:–—]', separator) else ' - '
        ending = separator + '마지막' + style[1]  # 단위 앞 띄어쓰기도 작품 표기와 맞춤
    elif order[0] == 0 and style:
        number, ending = order[1], ''
    else:
        return name
    before, after, width = style
    return base + before + str(number).zfill(width) + after + ending + Path(name).suffix


def last_episode_name(name, maximum):
    if title_parts(Path(name).stem, True)[1] != 'last':
        return name
    return styled_name(name, Path(name).stem, None, maximum)


def safe_component(value, limit=180):
    original = unicodedata.normalize('NFC', value)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', original).strip(' .')
    if not cleaned:
        cleaned = '제목없음'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', cleaned, re.I):
        cleaned = '_' + cleaned
    truncated = len(cleaned.encode('utf-8')) > limit
    if truncated:
        cleaned = cleaned.encode('utf-8')[:limit].decode('utf-8', errors='ignore').rstrip(' .')
    if cleaned != original or truncated:
        cleaned += ' [' + hashlib.sha256(original.encode()).hexdigest()[:10] + ']'
    return cleaned


def signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OrganizerError('일반 파일이 아닙니다: ' + path.name)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def check_cancel(cancel):
    if cancel and cancel():
        raise Cancelled('중지했습니다. 완료된 복사본과 원본은 유지합니다.')


def scan(config, cancel=None, progress=None, title_lookup=None):
    config = validate(config)
    source = Path(config['source_path'])
    if not source.is_dir():
        raise OrganizerError('원본 폴더를 찾을 수 없습니다. 볼륨 연결과 경로를 확인하세요.')
    excluded = {normalized(x).casefold() for x in config['excluded_titles'].splitlines() if x.strip()}
    groups, errors, scanned = {}, [], 0
    # 출력 폴더가 원본 하위에 있어도 다시 읽지 않도록 바로 아래 파일만 검사합니다.
    with os.scandir(source) as entries:
        for entry in entries:
            check_cancel(cancel)
            if entry.name.startswith('.') or not entry.name.lower().endswith('.txt'):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                fingerprint = signature(Path(entry.path))
                original_title = (title_lookup or {}).get(entry.name, Path(entry.name).stem)
                title, episode, order = title_parts(original_title, config['bare_numbers'])
                key = normalized(title).casefold()
                group = groups.setdefault(key, {'key': key, 'title': normalized(title), 'files': []})
                # NFC와 대소문자만 다른 파일명도 항상 동일한 폴더명을 사용합니다.
                if title < group['title']:
                    group['title'] = title
                group['files'].append({'name': entry.name, 'original_title': original_title, 'episode': episode, 'order': order, 'signature': fingerprint})
                scanned += 1
                if progress and scanned % 250 == 0:
                    progress(scanned)
            except OSError as exc:
                errors.append({'file': entry.name, 'error': str(exc)})
    rows = []
    folder_keys = {}
    for key in sorted(groups):
        group = groups[key]
        group['files'].sort(key=lambda x: (x['order'], normalized(x['name'])))
        group['count'] = len({x['episode'] for x in group['files']})
        group['excluded'] = key in excluded
        group['eligible'] = group['count'] >= config['minimum'] and not group['excluded']
        group['folder'] = safe_component(group['title'])
        # 다른 제목이 파일시스템의 폴더명으로 충돌하는 경우 명시적인 구분자를 추가합니다.
        folded = group['folder'].casefold()
        if folded in folder_keys and folder_keys[folded] != key:
            group['folder'] += ' [' + hashlib.sha256(key.encode()).hexdigest()[:12] + ']'
        folder_keys[group['folder'].casefold()] = key
        maximum = max((f['order'][1] for f in group['files'] if f['order'][0] == 0), default=0)
        style_titles = [f['original_title'] for f in group['files']]
        source_episodes = {f['episode'] for f in group['files']}
        existing = Path(config['target_path']) / group['folder']
        if existing.is_dir() and not existing.is_symlink():
            for child in existing.iterdir():
                if child.suffix.lower() == '.txt' and child.is_file() and not child.is_symlink():
                    base, existing_episode, order = title_parts(child.stem, True)
                    if normalized(base).casefold() == key and order[0] == 0:
                        maximum = max(maximum, order[1])
                        if existing_episode not in source_episodes:
                            style_titles.append(child.stem)
        style = preferred_style(style_titles)
        for entry in group['files']:
            output = styled_name(entry['name'], entry['original_title'], style, maximum)
            if output != entry['name'] or (style and entry['order'][0] == 0):
                entry['output_name'] = output
        rows.append(group)
    rows.sort(key=lambda x: (not x['eligible'], x['title']))
    return {'config': config, 'created': now(), 'source_real': str(source.resolve()),
            'target_real': str(Path(config['target_path']).resolve()), 'scanned': scanned,
            'groups': rows, 'errors': errors}


def file_digest(path, cancel=None):
    digest = hashlib.sha256()
    # 심볼릭 링크를 따라가지 않고 기존 파일을 읽습니다.
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OrganizerError('일반 파일이 아닙니다: ' + path.name)
        while True:
            check_cancel(cancel)
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def clean_identical_aliases(folder, destination, entry, checksum, cancel):
    if 'output_name' not in entry:
        return
    wanted = title_parts(entry.get('original_title', Path(entry['name']).stem), True)[:2]
    for old in folder.iterdir():
        if old == destination or old.is_symlink() or not old.is_file() or old.suffix.lower() != '.txt':
            continue
        if title_parts(old.stem, True)[:2] != wanted:
            continue
        before = signature(old)
        if file_digest(old, cancel) == checksum and signature(old) == before:
            check_cancel(cancel)
            old.unlink()


def copy_entry(source, folder, entry, cancel=None):
    """완성된 임시 파일만 공개하며 기존 목적지는 절대 덮어쓰지 않습니다."""
    if source.resolve() == folder.resolve():
        raise OrganizerError('작품 정리 폴더가 원본 폴더와 같습니다. 다른 정리 경로를 지정하세요.')
    source_file = source / entry['name']
    if signature(source_file) != entry['signature']:
        raise OrganizerError('분석 후 원본이 변경되었습니다. 다시 미리보기 하세요.')
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise OrganizerError('정리 폴더가 일반 디렉터리가 아닙니다.')
    temp_fd, temp_name = tempfile.mkstemp(prefix='.txt-organizer-', dir=str(folder))
    temp_path = Path(temp_name)
    try:
        digest = hashlib.sha256()
        source_fd = os.open(str(source_file), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(source_fd, 'rb') as reader, os.fdopen(temp_fd, 'wb') as writer:
            temp_fd = None
            before = os.fstat(reader.fileno())
            if [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns] != entry['signature']:
                raise OrganizerError('분석 후 원본이 변경되었습니다.')
            while True:
                check_cancel(cancel)
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            os.fchmod(writer.fileno(), stat.S_IMODE(before.st_mode) & 0o666)
        if signature(source_file) != entry['signature']:
            raise OrganizerError('복사 도중 원본이 변경되어 이번 복사를 취소했습니다.')
        checksum = digest.hexdigest()
        name = safe_component(Path(entry.get('output_name', entry['name'])).stem, 160) + '.txt'
        destination = folder / name
        for attempt in range(3):
            check_cancel(cancel)
            try:
                # 같은 파일시스템에서의 하드링크로 원자적·덮어쓰기 없는 최종화를 수행합니다.
                os.link(str(temp_path), str(destination))
                clean_identical_aliases(folder, destination, entry, checksum, cancel)
                return {'status': 'copied', 'destination': str(destination)}
            except FileExistsError:
                if destination.is_symlink():
                    raise OrganizerError('목적지에 심볼릭 링크가 있어 복사하지 않았습니다.')
                if destination.is_file() and file_digest(destination, cancel) == checksum:
                    clean_identical_aliases(folder, destination, entry, checksum, cancel)
                    return {'status': 'skipped', 'destination': str(destination)}
                if attempt == 0:
                    destination = folder / (Path(name).stem + ' [다른내용-' + checksum[:12] + '].txt')
                elif attempt == 1:
                    destination = folder / (Path(name).stem + ' [' + checksum + '].txt')
                else:
                    raise OrganizerError('같은 이름의 다른 파일이 있어 복사하지 않았습니다.')
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        temp_path.unlink(missing_ok=True)


class Engine:
    def __init__(self, db_path):
        self.title_lookup = None
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.db_path.with_suffix('.lock')
        self.stop_path = self.db_path.with_suffix('.stop')
        self.thread = None
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started TEXT,
                    finished TEXT, mode TEXT, status TEXT, summary TEXT);
                CREATE TABLE IF NOT EXISTS results (id INTEGER PRIMARY KEY, run_id INTEGER,
                    filename TEXT, title TEXT, status TEXT, destination TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS result_run ON results(run_id, id);
            ''')

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT body FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def acquire(self):
        handle = open(self.lock_path, 'a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            handle.close()
            return None

    def snapshot(self):
        state = self.get('state', {'status': 'idle', 'message': '미리보기로 정리할 파일을 확인하세요.'})
        if state['status'] in ('running', 'stopping'):
            handle = self.acquire()
            if handle is not None:
                try:
                    # 실행 잠금 안에서 다시 읽어 완료 직후 상태를 덮어쓰지 않습니다.
                    state = self.get('state', state)
                    if state['status'] in ('running', 'stopping'):
                        state.update(status='interrupted', message='이전 작업이 중단되었습니다. 다시 미리보기 후 실행하세요.')
                        self.put('state', state)
                        with self.connect() as db:
                            db.execute("UPDATE runs SET status='interrupted', finished=? WHERE status='running'", (now(),))
                finally:
                    handle.close()
        return state

    def stop(self):
        self.stop_path.touch()
        return '중지를 요청했습니다. 진행 중인 파일 처리가 취소됩니다.'

    def start(self, mode, config, keys=None):
        config = validate(config)
        if mode not in ('preview', 'copy', 'scheduled'):
            raise OrganizerError('알 수 없는 작업입니다.')
        handle = self.acquire()
        if handle is None:
            raise OrganizerError('다른 정리 작업이 진행 중입니다.')
        try:
            self.stop_path.unlink(missing_ok=True)
            plan = None
            if mode == 'copy':
                plan = self.get('plan')
                if not plan or plan['config'] != config:
                    raise OrganizerError('설정에 맞는 미리보기를 먼저 실행하세요.')
                if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
                    raise OrganizerError('복사할 제목을 선택하세요.')
                eligible = {g['key'] for g in plan['groups'] if g['eligible']}
                if not set(keys) <= eligible:
                    raise OrganizerError('미리보기에서 기준을 충족한 제목만 선택할 수 있습니다.')
            self.put('state', {'status': 'running', 'mode': mode, 'message': '작업을 시작합니다.',
                               'processed': 0, 'total': 0, 'copied': 0, 'skipped': 0, 'failed': 0})
            self.thread = threading.Thread(target=self._work, args=(mode, config, keys, plan, handle), daemon=True)
            self.thread.start()
        except Exception:
            handle.close()
            raise

    def _work(self, mode, config, keys, plan, handle):
        state = {'status': 'running', 'mode': mode, 'message': 'TXT 파일명을 분석하고 있습니다.',
                 'processed': 0, 'total': 0, 'copied': 0, 'skipped': 0, 'failed': 0}
        run_id = None
        try:
            with self.connect() as db:
                run_id = db.execute('INSERT INTO runs(started,mode,status) VALUES (?,?,?)',
                                    (now(), mode, 'running')).lastrowid
            self.put('state', state)
            cancel = self.stop_path.exists
            if mode != 'copy':
                # 새 분석 실패 시 예전 미리보기를 잘못 실행하지 않도록 무효화합니다.
                self.put('plan', None)
                def progress(count):
                    state['message'] = '%s개 파일명을 확인했습니다.' % count
                    self.put('state', state)
                if self.title_lookup:
                    plan = scan(config, cancel, progress, self.title_lookup(config['source_path']))
                else:
                    plan = scan(config, cancel, progress)
                self.put('plan', plan)
            for issue in plan['errors']:
                state['failed'] += 1
                self.add_result(run_id, issue['file'], '', 'failed', '', issue['error'])
            if mode == 'preview':
                state.update(status='completed', message='%s개 TXT, %s개 정리 대상 제목을 찾았습니다.' %
                             (plan['scanned'], sum(g['eligible'] for g in plan['groups'])))
            else:
                source, target = Path(config['source_path']), Path(config['target_path'])
                if str(source.resolve()) != plan['source_real'] or str(target.resolve()) != plan['target_real']:
                    raise OrganizerError('분석 후 폴더 경로가 변경되었습니다. 다시 미리보기 하세요.')
                if target.is_symlink():
                    raise OrganizerError('정리 경로에는 심볼릭 링크 대신 실제 경로를 입력하세요.')
                selected = [g for g in plan['groups'] if g['eligible'] and (keys is None or g['key'] in keys)]
                state['total'] = sum(len(g['files']) for g in selected)
                for group in selected:
                    for entry in group['files']:
                        check_cancel(cancel)
                        state['message'] = group['title'] + ' / ' + entry['name']
                        try:
                            folder = target / group['folder']
                            if folder.resolve().parent != target.resolve():
                                raise OrganizerError('정리 폴더가 지정 경로를 벗어납니다.')
                            result = copy_entry(source, folder, entry, cancel)
                            state[result['status']] += 1
                            self.add_result(run_id, entry['name'], group['title'], result['status'], result['destination'], '')
                        except Cancelled:
                            raise
                        except (OSError, OrganizerError) as exc:
                            state['failed'] += 1
                            self.add_result(run_id, entry['name'], group['title'], 'failed', '', str(exc))
                        state['processed'] += 1
                        self.put('state', state)
                state.update(status='completed', message='복사 %s개 · 동일 파일 %s개 · 실패 %s개' %
                             (state['copied'], state['skipped'], state['failed']))
            if state['failed']:
                state['status'] = 'completed_with_errors'
                state['message'] += ' — 실행 이력에서 오류를 확인하세요.'
        except Cancelled as exc:
            state.update(status='cancelled', message=str(exc))
        except Exception as exc:
            state.update(status='failed', message=str(exc))
        finally:
            try:
                self.put('state', state)
                if run_id is not None:
                    with self.connect() as db:
                        db.execute('UPDATE runs SET finished=?,status=?,summary=? WHERE id=?',
                                   (now(), state['status'], json.dumps(state, ensure_ascii=False), run_id))
            finally:
                handle.close()

    def add_result(self, run_id, filename, title, status, destination, error):
        with self.connect() as db:
            db.execute('INSERT INTO results(run_id,filename,title,status,destination,error) VALUES (?,?,?,?,?,?)',
                       (run_id, filename, title, status, destination, error))

    def preview_page(self, page=1):
        plan = self.get('plan')
        if not plan:
            return {'rows': [], 'page': page, 'more': False, 'ready': False}
        start = (page - 1) * 50
        rows = []
        for group in plan['groups'][start:start + 50]:
            item = {k: copy.deepcopy(v) for k, v in group.items() if k != 'files'}
            item['file_count'] = len(group['files'])
            item['sample'] = [f.get('output_name', f['name']) for f in group['files'][:5]]
            rows.append(item)
        return {'rows': rows, 'page': page, 'more': start + 50 < len(plan['groups']), 'ready': True,
                'created': plan['created'], 'scanned': plan['scanned'], 'total_groups': len(plan['groups']),
                'eligible_groups': sum(g['eligible'] for g in plan['groups'])}

    def history(self, page=1):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 51 OFFSET ?', ((page - 1) * 50,)).fetchall()
        return {'rows': [dict(r) for r in rows[:50]], 'more': len(rows) > 50, 'page': page}

    def details(self, run_id, page=1):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM results WHERE run_id=? ORDER BY id LIMIT 51 OFFSET ?',
                              (run_id, (page - 1) * 50)).fetchall()
        return {'rows': [dict(r) for r in rows[:50]], 'more': len(rows) > 50, 'page': page}
