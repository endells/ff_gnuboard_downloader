"""FF 연동부를 Flask 테스트 앱과 모의 FF 스케줄러로 검증합니다."""
import importlib
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, has_app_context, request
from jinja2 import ChoiceLoader, DictLoader

from ff_gnuboard_downloader.core import DEFAULTS


def make_app(root):
    root = Path(root)
    templates = Path(__file__).resolve().parents[1] / 'templates'
    app = Flask('organizer_test', template_folder=str(templates))
    app.secret_key = 'synthetic-tests-only'
    app.jinja_loader = ChoiceLoader([app.jinja_loader, DictLoader({
        'base.html': '<!doctype html><html lang="ko"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>TXT 정리 시험</title></head><body>{% block content %}{% endblock %}</body></html>'
    })])

    class Settings:
        values = dict(DEFAULTS)

        @classmethod
        def get(cls, key):
            assert has_app_context(), '설정 조회에는 Flask 컨텍스트가 필요합니다.'
            return cls.values.get(key)

        @classmethod
        def set(cls, key, value):
            assert has_app_context()
            cls.values[key] = value

    class Scheduler:
        def __init__(self):
            self.jobs, self.added, self.removed = {}, [], []

        def is_include(self, key):
            return key in self.jobs

        def add_job_instance(self, job, run=True):
            if job.job_id not in self.jobs:
                self.jobs[job.job_id] = job
                self.added.append((job, run))

        def remove_job(self, key):
            self.jobs.pop(key, None)
            self.removed.append(key)

    class Job:
        def __init__(self, package, job_id, interval, target, description):
            self.job_id, self.interval, self.target = job_id, interval, target

    class Base:
        def __init__(self, P, first_menu, name, scheduler_desc):
            self.P, self.name, self.desc = P, name, scheduler_desc

        def get_scheduler_name(self):
            return self.P.package_name + '_' + self.name

        def get_scheduler_desc(self):
            return self.desc

    frame = types.SimpleNamespace(app=app, config={'path_data': str(root)}, scheduler=Scheduler())
    fake_framework = types.ModuleType('framework')
    fake_framework.F, fake_framework.Job = frame, Job
    fake_plugin = types.ModuleType('plugin')
    fake_plugin.PluginModuleBase = Base
    with patch.dict(sys.modules, {'framework': fake_framework, 'plugin': fake_plugin}):
        adapter = importlib.import_module('ff_gnuboard_downloader.mod_basic')
        adapter = importlib.reload(adapter)
    instance = adapter.ModuleBasic(types.SimpleNamespace(
        package_name='ff_gnuboard_downloader', ModelSetting=Settings, logger=logging.getLogger('test')))

    @app.route('/ff_gnuboard_downloader/basic/<page>')
    def menu(page):
        return instance.process_menu(page, request)

    @app.route('/ff_gnuboard_downloader/ajax/basic/command', methods=['POST', 'GET'])
    def command():
        return instance.process_command(request.form.get('command'), request.form.get('arg1'),
                                        request.form.get('arg2'), request.form.get('arg3'), request)

    return app, instance, frame, Settings


class Adapter(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app, self.module, self.frame, self.settings = make_app(self.root)
        self.client = self.app.test_client()
        self.client.get('/ff_gnuboard_downloader/basic/setting')
        with self.client.session_transaction() as session:
            self.token = session['gnuboard_csrf']
        self.source = self.root / 'inbox'
        self.source.mkdir()
        self.conf = dict(DEFAULTS, board_url='https://board.example/bbs/board.php?bo_table=stories',
                         download_path=str(self.source), target_path=str(self.root / 'sorted'))

    def tearDown(self):
        for engine in (self.module.engine, self.module.organizer):
            if engine.thread:
                engine.thread.join(5)
        self.temp.cleanup()

    def post(self, command, data=None, arg2=None):
        args = {'command': command, 'csrf_token': self.token}
        if data is not None:
            args['arg1'] = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
        if arg2 is not None:
            args['arg2'] = str(arg2)
        return self.client.post('/ff_gnuboard_downloader/ajax/basic/command', data=args)

    def test_all_tabs_initial_empty_config(self):
        with self.app.app_context():
            self.module.plugin_load()
        for page in ('download', 'organize', 'setting', 'history'):
            response = self.client.get('/ff_gnuboard_downloader/basic/' + page)
            self.assertEqual(response.status_code, 200)
            self.assertIn('그누보드 TXT 다운로더'.encode(), response.data)

    def test_folder_browse_and_save_download_path(self):
        self.post('save', self.conf)
        (self.source / '한글 폴더').mkdir()
        (self.source / '문서.txt').write_text('본문')
        result = self.post('folders', str(self.source)).json
        self.assertEqual(result['ret'], 'success')
        self.assertEqual([r['name'] for r in result['rows']], ['한글 폴더'])
        before = dict(self.settings.values)
        destination = str(self.source / '한글 폴더')
        result = self.post('save_download_path', destination).json
        self.assertEqual(result['ret'], 'success')
        self.assertEqual(self.settings.values['download_path'], destination)
        self.assertEqual(self.settings.values, dict(before, download_path=destination))
        self.assertTrue((self.source / '문서.txt').is_file())
        self.assertIsNone(self.module.engine.thread)
        self.assertIn(destination.encode(), self.client.get('/ff_gnuboard_downloader/basic/download').data)

    def test_invalid_folder_path_does_not_change_settings(self):
        before = dict(self.settings.values)
        self.assertEqual(self.post('save_download_path', 'relative').json['ret'], 'error')
        self.assertEqual(before, self.settings.values)
        self.assertEqual(self.post('folders', '/not-a-real-folder-qa').json['ret'], 'error')
        response = self.client.post('/ff_gnuboard_downloader/ajax/basic/command', data={'command': 'folders', 'arg1': '/'})
        self.assertEqual(response.status_code, 403)

    def test_csrf_required(self):
        response = self.client.post('/ff_gnuboard_downloader/ajax/basic/command', data={'command': 'save'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/ff_gnuboard_downloader/ajax/basic/command').status_code, 403)

    def test_save_without_running_or_enabling_jobs(self):
        self.assertEqual(self.post('save', self.conf).json['ret'], 'success')
        self.assertIsNone(self.module.engine.thread)
        self.assertIsNone(self.module.organizer.thread)
        self.assertFalse(self.frame.scheduler.jobs)

    def test_independent_schedules_preserve_next_run(self):
        conf = dict(self.conf, basic_auto_start=True, organize_auto=True)
        self.assertEqual(self.post('save', conf).json['ret'], 'success')
        self.assertEqual(len(self.frame.scheduler.added), 2)
        self.assertTrue(all(run is False for _, run in self.frame.scheduler.added))
        self.post('save', dict(conf, minimum=5))
        self.assertEqual(len(self.frame.scheduler.added), 2)
        self.post('save', dict(conf, basic_interval=120))
        self.assertEqual(len(self.frame.scheduler.added), 3)
        self.post('save', self.conf)
        self.assertFalse(self.frame.scheduler.jobs)

    def test_scheduler_context_and_client_config(self):
        self.post('save', dict(self.conf, basic_auto_start=True))
        with patch.object(self.module.engine, 'start') as start:
            self.module.scheduler_function()
            self.assertEqual(start.call_args[0][0], 'latest')
        self.post('save', dict(self.conf, organize_auto=True))
        with patch.object(self.module.organizer, 'start') as start:
            self.module.organizer_schedule()
            self.assertEqual(start.call_args[0][0], 'scheduled')

    def test_preview_copy_http_and_status(self):
        self.post('save', self.conf)
        for i in range(1, 4):
            (self.source / ('산책 %s화.txt' % i)).write_text('합성 문서')
        self.assertEqual(self.post('preview').json['ret'], 'success')
        self.module.organizer.thread.join(5)
        plan = self.post('plan').json
        self.assertEqual(plan['rows'][0]['count'], 3)
        self.assertEqual(self.post('copy', ['산책']).json['ret'], 'success')
        self.module.organizer.thread.join(5)
        self.assertEqual(self.post('status').json['organize']['copied'], 3)

    def test_cookie_not_returned_and_scoped_to_board(self):
        self.assertEqual(self.post('save', dict(self.conf, cookie='sid=secret123')).json['ret'], 'success')
        self.assertEqual(self.module.cookie_path.stat().st_mode & 0o777, 0o600)
        with self.app.app_context():
            self.assertEqual(self.module.read_cookie(self.module.config()), 'sid=secret123')
        response = self.client.get('/ff_gnuboard_downloader/basic/setting')
        self.assertNotIn(b'secret123', response.data)
        self.post('save', dict(self.conf, board_url='https://other.example/bbs/board.php?bo_table=stories'))
        with self.app.app_context():
            self.assertEqual(self.module.read_cookie(self.module.config()), '')
        self.post('save', dict(self.conf, clear_cookie=True))
        self.assertFalse(self.module.cookie_path.exists())

    def test_invalid_save_changes_nothing(self):
        before = dict(self.settings.values)
        self.assertEqual(self.post('save', dict(self.conf, minimum=0)).json['ret'], 'error')
        self.assertEqual(before, self.settings.values)
        self.assertEqual(self.post('save', dict(self.conf, cookie='sid=x\nHeader: value')).json['ret'], 'error')
        self.assertEqual(before, self.settings.values)

    def test_escaped_markup_and_no_html_injection(self):
        self.settings.values['board_url'] = '<script>evil()</script>'
        response = self.client.get('/ff_gnuboard_downloader/basic/setting')
        self.assertNotIn(b'<script>evil()</script>', response.data)
        self.assertIn(b'&lt;script&gt;', response.data)

    def test_original_db_title_used_for_long_filenames(self):
        from ff_gnuboard_downloader.core import save_txt
        base = '아주 긴 제목' * 30
        for i in range(1, 4):
            title = base + ' %s화' % i
            path, sha = save_txt(self.source, title, str(i), b'synthetic', 'scope')
            self.module.engine.record('board', {'id': str(i)}, title, path, sha, 'completed')
        self.post('save', self.conf)
        self.post('preview')
        self.module.organizer.thread.join(5)
        rows = self.post('plan').json['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['count'], 3)
        self.assertEqual(rows[0]['title'], base)

    def test_download_completion_can_trigger_automatic_copy(self):
        from ff_gnuboard_downloader.core import validate
        for i in range(1, 4):
            (self.source / ('산책 %s화.txt' % i)).write_text('합성 문서')
        self.module.after_download(validate(dict(self.conf, auto_organize=True)))
        self.module.organizer.thread.join(5)
        self.assertEqual(self.module.organizer.snapshot()['copied'], 3)


if __name__ == '__main__':
    unittest.main()


