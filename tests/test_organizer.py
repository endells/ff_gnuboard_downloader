import copy
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ff_gnuboard_downloader.organizer import (Cancelled, Engine, OrganizerError, copy_entry,
                                    safe_component, scan, signature, title_parts, validate)


class Titles(unittest.TestCase):
    def test_episode_patterns(self):
        for name, title, key in [
            ('별빛 여행 1화', '별빛 여행', 'n:1'),
            ('별빛 여행 - 제 002 편', '별빛 여행', 'n:2'),
            ('별빛 여행(3)', '별빛 여행', 'n:3'),
            ('별빛 여행 [004회]', '별빛 여행', 'n:4'),
            ('별빛 여행5화', '별빛 여행', 'n:5'),
            ('별빛 여행 (상)', '별빛 여행', 'p:상'),
            ('별빛 여행 -중', '별빛 여행', 'p:중'),
            ('별빛 여행 하편', '별빛 여행', 'p:하'),
            ('별빛 여행 마지막편', '별빛 여행', 'last'),
            ('별빛 여행 6화 (완결)', '별빛 여행', 'n:6'),
            ('별빛 여행 7화 - 완결', '별빛 여행', 'n:7'),
        ]:
            with self.subTest(name=name):
                self.assertEqual(title_parts(name)[:2], (title, key))

    def test_preserve_real_titles(self):
        for name in ['천하', '봄날의 구상', '산하', '1984', '프로젝트 2026', '별빛 1부 여행', '미완결', '제1차 탐험']:
            with self.subTest(name=name):
                self.assertEqual(title_parts(name)[0], name)

    def test_bare_number_opt_in(self):
        self.assertEqual(title_parts('산책 001', True)[:2], ('산책', 'n:1'))
        self.assertEqual(title_parts('산책 -002', True)[:2], ('산책', 'n:2'))
        self.assertEqual(title_parts('산책 001')[0], '산책 001')
        self.assertEqual(title_parts('1984', True)[0], '1984')

    def test_component_collision_and_length(self):
        self.assertNotEqual(safe_component('a:b'), safe_component('a?b'))
        for value in ['../..', 'a/b', 'a\\b', 'CON', '.', '긴제목' * 200]:
            result = safe_component(value)
            self.assertNotIn('/', result)
            self.assertNotIn('\\', result)
            self.assertLess(len(result.encode()), 255)


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.target = self.root / 'sorted'
        self.conf = {'source_path': str(self.source), 'target_path': str(self.target), 'minimum': 3}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, data=None):
        file = self.source / name
        file.write_bytes(data if data is not None else ('합성 시험 ' + name).encode())
        return file

    def examples(self):
        for name in ['별빛 1화.txt', '별빛 02화.txt', '별빛 (3).txt']:
            self.write(name)

    def engine(self):
        return Engine(self.root / 'db' / 'history.db')

    def run_job(self, engine, mode, keys=None):
        engine.start(mode, self.conf, keys)
        engine.thread.join(10)
        self.assertFalse(engine.thread.is_alive())
        return engine.snapshot()

    def test_threshold_distinct_episodes(self):
        for name in ['별빛 1화.txt', '별빛 01편.txt', '별빛 (2).txt']:
            self.write(name)
        group = scan(self.conf)['groups'][0]
        self.assertEqual(group['count'], 2)
        self.assertFalse(group['eligible'])
        self.write('별빛 3화.txt')
        self.assertTrue(scan(self.conf)['groups'][0]['eligible'])

    def test_top_level_only_and_hidden_symlink(self):
        self.examples()
        self.write('.숨김 1화.txt')
        self.write('ignore.epub')
        nested = self.source / 'nested'
        nested.mkdir()
        (nested / 'nested.txt').write_text('fake')
        (self.source / '링크.txt').symlink_to(self.source / '별빛 1화.txt')
        plan = scan(self.conf)
        self.assertEqual(plan['scanned'], 3)
        self.assertFalse(self.target.exists())

    def test_exclusions_and_nfc(self):
        import unicodedata
        self.write('산책 (상).txt')
        self.write(unicodedata.normalize('NFD', '산책 (중).txt'))
        self.write('산책 (하).txt')
        conf = dict(self.conf, excluded_titles='산책')
        group = scan(conf)['groups'][0]
        self.assertEqual(group['count'], 3)
        self.assertTrue(group['excluded'])
        self.assertFalse(group['eligible'])

    def test_copy_bytes_preserve_original_skip_on_rerun(self):
        self.examples()
        before = {f.name: f.read_bytes() for f in self.source.iterdir()}
        os.chmod(self.source / '별빛 1화.txt', 0o640)
        engine = self.engine()
        self.assertEqual(self.run_job(engine, 'preview')['status'], 'completed')
        self.assertFalse(self.target.exists())
        state = self.run_job(engine, 'copy', ['별빛'])
        self.assertEqual((state['copied'], state['failed']), (3, 0))
        self.assertEqual(before, {f.name: f.read_bytes() for f in self.source.iterdir()})
        self.assertEqual(sorted(before.values()), sorted(f.read_bytes() for f in (self.target / '별빛').iterdir()))
        self.assertEqual({f.name for f in (self.target / '별빛').iterdir()}, {'별빛 1화.txt', '별빛 2화.txt', '별빛 3화.txt'})
        self.assertEqual((self.target / '별빛' / '별빛 1화.txt').stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.run_job(engine, 'copy', ['별빛'])['skipped'], 3)

    def test_conflict_keeps_both_versions(self):
        self.examples()
        folder = self.target / '별빛'
        folder.mkdir(parents=True)
        conflict = folder / '별빛 1화.txt'
        conflict.write_text('기존 다른 내용')
        engine = self.engine()
        self.run_job(engine, 'preview')
        state = self.run_job(engine, 'copy', ['별빛'])
        self.assertEqual(state['copied'], 3)
        self.assertEqual(conflict.read_text(), '기존 다른 내용')
        self.assertEqual(len(list(folder.glob('*.txt'))), 4)
        self.assertEqual(self.run_job(engine, 'copy', ['별빛'])['skipped'], 3)

    def test_changed_after_preview_records_failure(self):
        self.examples()
        engine = self.engine()
        self.run_job(engine, 'preview')
        self.write('별빛 1화.txt', b'changed')
        state = self.run_job(engine, 'copy', ['별빛'])
        self.assertEqual((state['copied'], state['failed'], state['status']), (2, 1, 'completed_with_errors'))
        run = engine.history()['rows'][0]
        errors = [r for r in engine.details(run['id'])['rows'] if r['status'] == 'failed']
        self.assertIn('변경', errors[0]['error'])

    def test_reject_settings_changed_and_noneligible_selection(self):
        self.examples()
        engine = self.engine()
        self.run_job(engine, 'preview')
        with self.assertRaises(OrganizerError):
            engine.start('copy', dict(self.conf, minimum=2), ['별빛'])
        with self.assertRaises(OrganizerError):
            engine.start('copy', self.conf, ['없는제목'])
        with self.assertRaises(OrganizerError):
            engine.start('copy', self.conf, [])

    def test_schedule_scans_new_files_and_restores_missing_copy(self):
        self.examples()
        engine = self.engine()
        self.assertEqual(self.run_job(engine, 'scheduled')['copied'], 3)
        (self.target / '별빛' / '별빛 1화.txt').unlink()
        self.write('별빛 4화.txt')
        state = self.run_job(engine, 'scheduled')
        self.assertEqual((state['copied'], state['skipped']), (2, 2))

    def test_nested_target_not_rescanned(self):
        self.conf['target_path'] = str(self.source / '정리')
        self.examples()
        self.run_job(self.engine(), 'scheduled')
        self.assertEqual(scan(self.conf)['scanned'], 3)

    def test_symlink_destination_not_followed(self):
        self.examples()
        outside = self.root / 'outside'
        outside.mkdir()
        self.target.mkdir()
        (self.target / '별빛').symlink_to(outside, target_is_directory=True)
        state = self.run_job(self.engine(), 'scheduled')
        self.assertEqual(state['failed'], 3)
        self.assertFalse(list(outside.iterdir()))

    def test_cancel_copy_cleans_partial_and_keeps_source(self):
        source = self.write('단편.txt', b'x' * 3000000)
        entry = {'name': source.name, 'signature': signature(source)}
        calls = []
        def cancel():
            calls.append(1)
            return len(calls) > 1
        with self.assertRaises(Cancelled):
            copy_entry(self.source, self.target, entry, cancel)
        self.assertEqual(source.stat().st_size, 3000000)
        self.assertFalse(list(self.target.iterdir()))

    def test_changed_during_copy_does_not_commit(self):
        source = self.write('단편.txt', b'old')
        entry = {'name': source.name, 'signature': signature(source)}
        def mutate():
            source.write_bytes(b'new content')
            return False
        with self.assertRaises(OrganizerError):
            copy_entry(self.source, self.target, entry, mutate)
        self.assertFalse(list(self.target.iterdir()))

    def test_cross_instance_lock_and_stop(self):
        self.examples()
        engine, other = self.engine(), self.engine()
        entered, release = threading.Event(), threading.Event()
        original = scan
        def waiting(conf, cancel, progress):
            entered.set()
            release.wait(5)
            return original(conf, cancel, progress)
        with patch('ff_gnuboard_downloader.organizer.scan', waiting):
            engine.start('preview', self.conf)
            self.assertTrue(entered.wait(3))
            with self.assertRaises(OrganizerError):
                other.start('preview', self.conf)
            self.assertEqual(other.snapshot()['status'], 'running')
            other.stop()
            release.set()
            engine.thread.join(5)
        self.assertEqual(engine.snapshot()['status'], 'cancelled')

    def test_stale_running_state_and_history_marked(self):
        engine = self.engine()
        engine.put('state', {'status': 'running', 'message': 'old'})
        with engine.connect() as db:
            db.execute("INSERT INTO runs(status) VALUES ('running')")
        self.assertEqual(engine.snapshot()['status'], 'interrupted')
        self.assertEqual(engine.history()['rows'][0]['status'], 'interrupted')

    def test_history_plan_pagination_and_persistence(self):
        for i in range(55):
            self.write('제목%03d.txt' % i)
        engine = self.engine()
        self.run_job(engine, 'preview')
        other = self.engine()
        self.assertEqual(len(other.preview_page()['rows']), 50)
        self.assertTrue(other.preview_page()['more'])
        self.assertEqual(len(other.preview_page(2)['rows']), 5)
        self.assertFalse(other.preview_page(2)['more'])
        self.assertEqual(other.history()['rows'][0]['status'], 'completed')

    def test_validation(self):
        for patch_value in [{'source_path': 'relative'}, {'target_path': str(self.source)}, {'minimum': 0},
                            {'basic_interval': 0}, {'minimum': '2.5'}]:
            with self.subTest(patch_value=patch_value), self.assertRaises(OrganizerError):
                validate(dict(self.conf, **patch_value))


if __name__ == '__main__':
    unittest.main()


class Styles(unittest.TestCase):
    setUp = Files.setUp
    tearDown = Files.tearDown
    write = Files.write
    engine = Files.engine
    run_job = Files.run_job
    def test_majority_spacing_unit_and_final(self):
        for n in [1, 2, 3]:
            self.write('나는 관사병이었다 -%s부.txt' % n)
        self.write('나는 관사병이었다 - 4 편.txt')
        self.write('나는 관사병이었다 - 마지막편.txt')
        engine = self.engine()
        self.run_job(engine, 'scheduled')
        folder = self.target / '나는 관사병이었다'
        self.assertEqual({p.name for p in folder.iterdir()}, {
            '나는 관사병이었다 -1부.txt', '나는 관사병이었다 -2부.txt',
            '나는 관사병이었다 -3부.txt', '나는 관사병이었다 -4부.txt',
            '나는 관사병이었다 -5부 -마지막부.txt'})
        self.assertEqual(self.run_job(engine, 'scheduled')['skipped'], 5)
        self.assertEqual(title_parts('나는 관사병이었다 -5부 -마지막부')[:2], ('나는 관사병이었다', 'last'))
        self.assertTrue((self.source / '나는 관사병이었다 - 마지막편.txt').exists())

    def test_existing_identical_alias_normalized_without_losing_other_content(self):
        for n in [1, 2, 3]:
            self.write('여행 - %s 부.txt' % n)
        self.write('여행 -4편.txt', b'same')
        folder = self.target / '여행'
        folder.mkdir(parents=True)
        (folder / '여행 -4편.txt').write_bytes(b'same')
        (folder / '여행 - 4편.txt').write_bytes(b'different')
        self.run_job(self.engine(), 'scheduled')
        self.assertFalse((folder / '여행 -4편.txt').exists())
        self.assertEqual((folder / '여행 - 4 부.txt').read_bytes(), b'same')
        self.assertEqual((folder / '여행 - 4편.txt').read_bytes(), b'different')

    def test_style_votes_do_not_double_count_copies(self):
        from ff_gnuboard_downloader.organizer import preferred_style, styled_name
        self.assertEqual(preferred_style(['여행 -1부', '여행 -2부', '여행 - 3편'] * 2), (' -', '부', 0))
        for before, after in [(' -', '편'), (' - ', '편'), (' -', '부'), (' -', ' 부'), (' ', '화')]:
            style = preferred_style(['여행' + before + '21' + after])
            name = styled_name('여행 마지막부.txt', '여행 마지막부', style, 21)
            self.assertTrue(name.startswith('여행' + before + '22' + after))
            self.assertEqual(title_parts(Path(name).stem)[:2], ('여행', 'last'))
        self.assertEqual(preferred_style(['여행 -2편', '여행 - 1부']), (' - ', '부', 0))


    def test_original_folder_cannot_be_used_as_work_destination(self):
        self.write('여행 -1부.txt')
        entry = scan(dict(self.conf, minimum=1))['groups'][0]['files'][0]
        with self.assertRaises(OrganizerError):
            copy_entry(self.source, self.source, entry)
        self.assertTrue((self.source / '여행 -1부.txt').exists())
