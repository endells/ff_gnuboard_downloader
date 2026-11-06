"""정리 결과의 작품 폴더만 EPUB 3로 묶습니다. 추가 패키지/네트워크 없음."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid
import zipfile
from html import escape

from .organizer import Engine as Store, OrganizerError, Cancelled, check_cancel, safe_component, signature, title_parts, preferred_style, styled_name

MARKER = 'META-INF/ff-gnuboard.json'


def folders(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise OrganizerError('정리 결과 폴더가 없거나 심볼릭 링크입니다. 먼저 TXT를 정리하세요.')
    rows = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if folder.name.startswith('.') or folder.is_symlink() or not folder.is_dir():
            continue
        count = len(txt_files(folder))
        if count:
            rows.append({'name': folder.name, 'count': count})
    return rows


def txt_files(folder):
    return sorted((p for p in folder.iterdir() if not p.name.startswith('.') and p.suffix.lower() == '.txt'
                   and not p.is_symlink() and p.is_file()),
                  key=lambda p: (title_parts(p.stem, True)[2],
                                 [int(x) if x.isdigit() else x.casefold() for x in re.split('(\\d+)', p.name)]))


def xml(value):
    value = ''.join(c for c in value if c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
    return escape(value, quote=True)


def decode(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('cp949')


def build(root, name, cancel=lambda: False):
    root = Path(root).resolve()
    if not isinstance(name, str) or name in ('.', '..') or Path(name).name != name or '/' in name or '\\' in name:
        raise OrganizerError('정리 폴더 바로 아래의 작품 폴더만 선택할 수 있습니다.')
    folder = root / name
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
        raise OrganizerError('유효한 작품 폴더가 아닙니다.')
    files = txt_files(folder)
    if not files:
        raise OrganizerError('TXT 파일이 없습니다.')
    stamps = {p.name: signature(p) for p in files}
    maximum = max((title_parts(p.stem, True)[2][1] for p in files if title_parts(p.stem, True)[2][0] == 0), default=0)
    style = preferred_style([p.stem for p in files])
    output = folder / (safe_component(name, 150) + '.epub')
    previous = None
    output_stamp = None
    if output.exists() or output.is_symlink():
        if output.is_symlink():
            raise OrganizerError('EPUB 저장 위치가 심볼릭 링크입니다.')
        output_stamp = signature(output)
        try:
            with zipfile.ZipFile(output) as z:
                previous = json.loads(z.read(MARKER))
            if previous.get('generator') != 'ff_gnuboard_downloader':
                raise ValueError()
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            raise OrganizerError('같은 이름의 외부 EPUB이 있어 덮어쓰지 않습니다: ' + output.name)
    fd, temp = tempfile.mkstemp(prefix='.epub-', dir=str(folder))
    os.close(fd)
    digest = hashlib.sha256(b"chapter-format-v2")
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            items, spine, links = [], [], []
            seen_terminal = set()
            for index, path in enumerate(files, 1):
                check_cancel(cancel)
                # O_NOFOLLOW로 교체된 심볼릭 링크를 따라가지 않습니다.
                source = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source, 'rb') as reader:
                    raw = reader.read()
                if signature(path) != stamps[path.name]:
                    raise OrganizerError('TXT가 변경되었습니다. 다시 생성하세요.')
                digest.update(json.dumps([path.name, hashlib.sha256(raw).hexdigest()], ensure_ascii=False).encode())
                text = decode(raw).replace('\r\n', '\n').replace('\r', '\n')
                chapter_title = Path(styled_name(path.name, path.stem, style, maximum)).stem
                terminal = title_parts(path.stem, True)[1] != 'unnumbered'
                terminal_key = (chapter_title, hashlib.sha256(raw).hexdigest())
                if terminal and terminal_key in seen_terminal:
                    continue
                if terminal:
                    seen_terminal.add(terminal_key)
                title = xml(chapter_title)
                body = ''.join('<p>' + xml(line) + '</p>' if line.strip() else '<p><br/></p>' for line in text.split('\n'))
                href = 'chapter-%05d.xhtml' % index
                z.writestr('OEBPS/' + href, '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ko"><head><title>' + title + '</title></head><body><h1>' + title + '</h1>' + body + '</body></html>')
                items.append('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (index, href))
                spine.append('<itemref idref="c%d"/>' % index)
                links.append('<li><a href="%s">%s</a></li>' % (href, title))
            uid = 'urn:uuid:' + str(uuid.uuid5(uuid.NAMESPACE_URL, str(folder)))
            modified = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            z.writestr('OEBPS/nav.xhtml', '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ko"><head><title>목차</title></head><body><nav epub:type="toc" id="toc"><h1>' + xml(name) + '</h1><ol>' + ''.join(links) + '</ol></nav></body></html>')
            z.writestr('OEBPS/content.opf', '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang="ko"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">' + uid + '</dc:identifier><dc:title>' + xml(name) + '</dc:title><dc:language>ko</dc:language><meta property="dcterms:modified">' + modified + '</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' + ''.join(items) + '</manifest><spine>' + ''.join(spine) + '</spine></package>')
            z.writestr(MARKER, json.dumps({'generator': 'ff_gnuboard_downloader', 'format': 1, 'digest': digest.hexdigest()}))
        check_cancel(cancel)
        if [p.name for p in txt_files(folder)] != [p.name for p in files] or any(signature(p) != stamps[p.name] for p in files):
            raise OrganizerError('생성 중 TXT 목록이 변경되었습니다. 다시 생성하세요.')
        if output_stamp is not None and (output.is_symlink() or signature(output) != output_stamp):
            raise OrganizerError('생성 중 기존 EPUB이 변경되었습니다. 다시 실행하세요.')
        if previous and previous.get('digest') == digest.hexdigest():
            with zipfile.ZipFile(output) as z:
                if z.testzip() is None:
                    return {'status': 'skipped', 'path': str(output)}
        with open(temp, 'rb') as stream:
            os.fsync(stream.fileno())
        if previous is None:
            os.link(temp, output)  # 경합으로 생긴 기존 파일은 덮어쓰지 않음
        else:
            os.replace(temp, output)
        return {'status': 'created', 'path': str(output)}
    finally:
        Path(temp).unlink(missing_ok=True)


class Engine(Store):
    def __init__(self, path):
        super().__init__(path)
        if self.get('state') is None:
            self.put('state', {'status': 'idle', 'message': '정리된 작품 폴더에서 EPUB을 생성합니다.', 'results': []})

    def start(self, root, selected=None):
        if selected is not None and (not isinstance(selected, list) or not selected or not all(isinstance(n, str) for n in selected)):
            raise OrganizerError('EPUB을 만들 작품 폴더를 선택하세요.')
        handle = self.acquire()
        if handle is None:
            raise OrganizerError('다운로드·정리·EPUB 작업이 실행 중입니다. 완료 후 다시 실행하세요.')
        try:
            available = [row['name'] for row in folders(root)]
            if selected is not None and not set(selected) <= set(available):
                raise OrganizerError('선택 폴더가 변경되었습니다. 목록을 새로고침하세요.')
            names = available if selected is None else [n for n in available if n in selected]
            if not names:
                raise OrganizerError('정리 결과에 TXT가 있는 작품 폴더가 없습니다.')
            self.stop_path.unlink(missing_ok=True)
            self.put('state', {'status': 'running', 'message': 'EPUB 생성 중', 'total': len(names), 'done': 0, 'results': []})
            self.thread = threading.Thread(target=self.work, args=(str(Path(root).resolve()), names, handle), daemon=True)
            self.thread.start()
        except Exception:
            handle.close()
            raise

    def work(self, root, names, handle):
        state = self.get('state')
        try:
            for name in names:
                check_cancel(self.stop_path.exists)
                try:
                    result = dict(build(root, name, self.stop_path.exists), name=name)
                except Cancelled:
                    raise
                except Exception as exc:
                    result = {'name': name, 'status': 'failed', 'error': str(exc)}
                state['results'].append(result)
                state['done'] += 1
                state['message'] = '%s / %s 폴더 처리' % (state['done'], state['total'])
                self.put('state', state)
            failed = sum(r['status'] == 'failed' for r in state['results'])
            state.update(status='completed_with_errors' if failed else 'completed', message='EPUB 처리 완료 · %s개 중 실패 %s개' % (len(names), failed))
        except Cancelled:
            state.update(status='cancelled', message='EPUB 생성을 중지했습니다. 완성된 EPUB은 유지됩니다.')
        except Exception as exc:
            state.update(status='failed', message=str(exc))
        finally:
            self.put('state', state)
            handle.close()
