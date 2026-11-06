"""site-packages와 인터넷 없이 동봉 파서로 TXT를 저장하는지 검사합니다."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

class BundledParser(unittest.TestCase):
    def test_wheel_integrity(self):
        for name, expected in {
            'beautifulsoup4-4.12.3-py3-none-any.whl': 'b80878c9f40111313e55da8ba20bdba06d8fa3969fc68304167741bbf9e082ed',
            'soupsieve-2.5-py3-none-any.whl': 'eaa337ff55a1579b6549dc679565eac1e3d000563bcb1c8ab0d0fefbc0c2cdc7',
        }.items():
            self.assertEqual(hashlib.sha256((ROOT / 'vendor' / name).read_bytes()).hexdigest(), expected)

    def test_clean_python_download_with_both_packages_absent(self):
        script = r'''
import importlib.util, sys, types, tempfile
from pathlib import Path
assert importlib.util.find_spec('bs4') is None
assert importlib.util.find_spec('soupsieve') is None
package = types.ModuleType('ff_gnuboard_downloader')
package.__path__ = [sys.argv[1]]
sys.modules['ff_gnuboard_downloader'] = package
# HTTP는 시험용 응답으로 대체합니다. 외부 네트워크나 pip를 사용하지 않습니다.
sys.modules['requests'] = types.ModuleType('requests')
from ff_gnuboard_downloader.core import DEFAULTS, Engine, validate
import bs4, soupsieve
assert '.whl/' in bs4.__file__, bs4.__file__
assert '.whl/' in soupsieve.__file__, soupsieve.__file__
assert bs4.__version__ == '4.12.3'
assert soupsieve.__version__ == '2.5'
class Client:
    def __init__(self, *args, **kwargs): pass
    def wait(self, *args): pass
    def close(self): pass
    def get(self, url):
        if 'wr_id=' in url:
            return '<h1 itemprop="headline">기록 1편</h1><div id="bo_v_con"><p>첫 문장</p><p>두 번째 문장</p></div>'
        return '<ul class="list-body"><li><a class="item-subject" href="?bo_table=test&wr_id=1">기록 1편</a></li></ul>'
with tempfile.TemporaryDirectory() as folder:
    config = validate(dict(DEFAULTS, board_url='https://board.example/bbs/board.php?bo_table=test', download_path=folder+'/inbox', target_path=folder+'/sorted'))
    engine = Engine(Path(folder)/'history.db', Client)
    engine.start('range', config, 1, 1)
    engine.thread.join(5)
    assert engine.snapshot()['downloaded'] == 1, engine.snapshot()
    files = list((Path(folder)/'inbox').glob('*.txt'))
    assert len(files) == 1
    assert '첫 문장\n\n두 번째 문장' in files[0].read_text()
print('PASS: bundled bs4 + soupsieve; TXT saved without site-packages')
'''
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, '-S', '-c', script, str(ROOT)], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__':
    unittest.main()
