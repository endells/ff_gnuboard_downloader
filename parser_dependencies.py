"""FF에 없는 파서만 동봉된 순수 Python wheel에서 불러옵니다. pip/통신 없음."""
import importlib.util
import sys
from pathlib import Path


def ensure_parser_dependencies():
    vendor = Path(__file__).resolve().parent / 'vendor'
    for module, filename in (
        ('soupsieve', 'soupsieve-2.5-py3-none-any.whl'),
        ('bs4', 'beautifulsoup4-4.12.3-py3-none-any.whl'),
    ):
        # 기존 FF 패키지와 이미 import된 모듈을 우선합니다.
        if module in sys.modules or importlib.util.find_spec(module) is not None:
            continue
        wheel = vendor / filename
        if not wheel.is_file():
            raise ImportError('동봉 파서 파일이 없습니다. 플러그인을 다시 업데이트하세요: ' + filename)
        if str(wheel) not in sys.path:
            sys.path.append(str(wheel))
    importlib.invalidate_caches()
