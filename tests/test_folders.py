import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ff_gnuboard_downloader.folders import list_folders

class Folders(unittest.TestCase):
    def test_pages_and_closest_existing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(105): (root / ('폴더%03d' % i)).mkdir()
            (root / '.hidden').mkdir()
            (root / 'file.txt').write_text('private')
            first = list_folders(tmp)
            second = list_folders(tmp, 2)
            self.assertEqual((len(first['rows']), first['more'], first['total']), (100, True, 105))
            self.assertEqual((len(second['rows']), second['more']), (5, False))
            self.assertEqual(list_folders(str(root/'new'/'sub'), nearest=True)['path'], tmp)
            self.assertEqual(list_folders('/')['parent'], '/')

    def test_permission_denied_and_invalid_path(self):
        with patch('os.scandir', side_effect=PermissionError()):
            with self.assertRaisesRegex(ValueError, '권한'): list_folders('/')
        for path in ('relative', '/bad\x00path'):
            with self.assertRaises(ValueError): list_folders(path)
