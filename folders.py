"""FF 프로세스에서 보이는 디렉터리만 탐색합니다. 파일은 읽거나 변경하지 않습니다."""
import os
from pathlib import Path


def list_folders(value, page=1, nearest=False):
    value = str(value or '/')
    if '\x00' in value or not Path(value).is_absolute():
        raise ValueError('FF에서 보이는 절대 경로를 입력하세요. 예: /data 또는 /books')
    path = Path(os.path.abspath(value))
    requested = str(path)
    if nearest:
        while not path.exists() and path != path.parent:
            path = path.parent
    if not path.is_dir():
        raise ValueError('폴더가 없습니다. NAS 폴더가 FF에 연결되어 있는지 확인하세요: ' + str(path))
    try:
        with os.scandir(str(path)) as entries:
            children = []
            for entry in entries:
                if entry.name.startswith('.'):
                    continue
                try:
                    if entry.is_dir():
                        children.append({'name': entry.name, 'path': str(path / entry.name)})
                except OSError:
                    continue
    except PermissionError:
        raise ValueError('이 폴더를 열 권한이 없습니다. NAS 공유 폴더 권한을 확인하세요: ' + str(path))
    children.sort(key=lambda row: (row['name'].casefold(), row['name']))
    page = max(1, int(page))
    size = 100
    page = min(page, max(1, (len(children) + size - 1) // size))
    return {'path': str(path), 'parent': str(path.parent), 'rows': children[(page-1)*size:page*size],
            'page': page, 'more': page*size < len(children), 'total': len(children),
            'notice': '지정 경로가 아직 없어 가장 가까운 상위 폴더를 표시합니다.' if requested != str(path) else ''}
