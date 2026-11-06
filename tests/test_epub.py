import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ff_gnuboard_downloader.epub_export import build, folders, Engine
from ff_gnuboard_downloader.organizer import scan, copy_entry, title_parts, OrganizerError, Cancelled


class Epub(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / '별빛'
        self.work.mkdir()
        for n in (10, 2):
            (self.work / ('별빛 - %s편.txt' % n)).write_text('합성 시험 <내용> & 기호\x01\n\n다음 문단', encoding='utf-8')
        (self.work / '별빛 - 마지막편.txt').write_bytes('마지막 합성 시험'.encode('cp949'))

    def tearDown(self):
        self.temp.cleanup()

    def test_package_order_xml_and_update(self):
        before = {p.name: p.read_bytes() for p in self.work.iterdir()}
        result = build(self.root, '별빛')
        out = Path(result['path'])
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.infolist()[0].filename, 'mimetype')
            self.assertEqual(z.infolist()[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(z.read('mimetype'), b'application/epub+zip')
            for name in z.namelist():
                if name.endswith(('.xml', '.xhtml', '.opf')):
                    ET.fromstring(z.read(name))
            nav = ET.fromstring(z.read('OEBPS/nav.xhtml'))
            self.assertEqual([e.text for e in nav.iter('{http://www.w3.org/1999/xhtml}a')], ['별빛 - 2편', '별빛 - 10편', '별빛 - 11편 - 마지막편'])
            opf = ET.fromstring(z.read('OEBPS/content.opf'))
            for e in opf.iter('{http://www.idpf.org/2007/opf}item'):
                self.assertIn('OEBPS/' + e.attrib['href'], z.namelist())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.work.glob('*.txt')})
        stamp = out.stat().st_mtime_ns
        self.assertEqual(build(self.root, '별빛')['status'], 'skipped')
        self.assertEqual(stamp, out.stat().st_mtime_ns)
        (self.work / '별빛 - 12편.txt').write_bytes('추가'.encode('utf-16'))
        self.assertEqual(build(self.root, '별빛')['status'], 'created')
        self.assertFalse(list(self.work.glob('.epub-*')))

    def test_external_and_cancel_preserved(self):
        output = self.work / '별빛.epub'
        output.write_bytes(b'external')
        with self.assertRaises(OrganizerError):
            build(self.root, '별빛')
        self.assertEqual(output.read_bytes(), b'external')
        output.unlink()
        build(self.root, '별빛')
        previous = output.read_bytes()
        with self.assertRaises(Cancelled):
            build(self.root, '별빛', lambda: True)
        self.assertEqual(previous, output.read_bytes())
        (self.work / 'broken.txt').write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):
            build(self.root, '별빛')
        self.assertEqual(previous, output.read_bytes())
        self.assertFalse(list(self.work.glob('.epub-*')))

    def test_scope_selection_and_lock(self):
        (self.root / 'outside.txt').write_text('outside')
        (self.root / 'link').symlink_to(self.work)
        (self.work / 'link.txt').symlink_to(self.root / 'outside.txt')
        self.assertEqual(folders(self.root), [{'name': '별빛', 'count': 3}])
        engine = Engine(self.root / 'db' / 'epub.db')
        for names in ([], ['../escape'], ['missing']):
            with self.assertRaises(OrganizerError):
                engine.start(self.root, names)
        handle = engine.acquire()
        try:
            with self.assertRaises(OrganizerError):
                engine.start(self.root)
        finally:
            handle.close()
        engine.start(self.root, ['별빛'])
        engine.thread.join(5)
        self.assertEqual(engine.snapshot()['status'], 'completed')
        self.assertEqual(engine.snapshot()['done'], 1)
        for name in ('../escape', 'link'):
            with self.assertRaises(OrganizerError):
                build(self.root, name)

    def test_duplicate_old_terminal_copy_not_repeated(self):
        (self.work / '별빛 - 11편 - 마지막편.txt').write_bytes((self.work / '별빛 - 마지막편.txt').read_bytes())
        with zipfile.ZipFile(build(self.root, '별빛')['path']) as z:
            nav = ET.fromstring(z.read('OEBPS/nav.xhtml'))
            self.assertEqual(len(list(nav.iter('{http://www.w3.org/1999/xhtml}a'))), 3)

    def test_terminal_copy_uses_existing_max_and_stays_stable(self):
        source = self.root / 'source'
        source.mkdir()
        final = source / '별빛 - 마지막편.txt'
        final.write_text('합성 마지막', encoding='utf-8')
        config = {'source_path': str(source), 'target_path': str(self.root), 'minimum': 1}
        group = scan(config)['groups'][0]
        entry = group['files'][0]
        self.assertEqual(entry['output_name'], '별빛 - 11편 - 마지막편.txt')
        self.assertEqual(copy_entry(source, self.work, entry)['status'], 'copied')
        self.assertTrue(final.exists())
        self.assertEqual(scan(config)['groups'][0]['files'][0]['output_name'], entry['output_name'])
        self.assertEqual(title_parts('별빛 - 11편 - 마지막편')[:2], ('별빛', 'last'))
        (source / '새작품 - 마지막 편.txt').write_text('시험')
        new = next(g for g in scan(config)['groups'] if g['title'] == '새작품')
        self.assertNotIn('output_name', new['files'][0])
