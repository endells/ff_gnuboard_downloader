"""FF setup import가 실패해 제목까지 사라지는 회귀를 검사합니다."""
import builtins
import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from jinja2 import ChoiceLoader, DictLoader


ROOT = Path(__file__).resolve().parents[1]


class Startup(unittest.TestCase):
    def load(self, missing=None, constructor_error=None, load_error=None):
        app = Flask('startup-test', template_folder=str(ROOT / 'templates'))
        app.jinja_loader = ChoiceLoader([app.jinja_loader, DictLoader({
            'base.html': '<html><body>{% block content %}{% endblock %}</body></html>'})])
        delegate_calls = []

        class Base:
            def __init__(self, P, **kwargs):
                self.P = P
                self.__dict__.update(kwargs)

        class Worker:
            db_default = {'basic_auto_start': 'False'}

            def __init__(self, P):
                if constructor_error:
                    raise constructor_error

            def plugin_load(self):
                if load_error:
                    raise load_error
                delegate_calls.append('load')

            def plugin_unload(self):
                delegate_calls.append('unload')

            def scheduler_function(self):
                delegate_calls.append('schedule')

            def process_menu(self, page, req):
                return '정상 화면'

            def process_command(self, *args):
                return '정상 명령'

        package = types.ModuleType('startup_fixture')
        package.__path__ = [str(ROOT)]
        plugin = types.ModuleType('plugin')
        plugin.PluginModuleBase = Base
        worker = types.ModuleType('startup_fixture.mod_basic')
        worker.ModuleBasic = Worker

        def create(setting):
            p = types.SimpleNamespace(plugin_info=json.loads((ROOT / 'info.yaml').read_text()),
                                      logger=logging.getLogger('startup'), setting=setting)
            p.set_module_list = lambda classes: setattr(p, 'module_list', [cls(p) for cls in classes])
            return p

        plugin.create_plugin_instance = create
        normal_import = builtins.__import__

        def import_with_missing(name, *args, **kwargs):
            if missing and name == 'mod_basic':
                raise ModuleNotFoundError("No module named '%s'" % missing, name=missing)
            return normal_import(name, *args, **kwargs)

        with patch.dict(sys.modules, {'plugin': plugin, 'startup_fixture': package,
                                     'startup_fixture.mod_basic': worker}), \
                patch('builtins.__import__', side_effect=import_with_missing):
            spec = importlib.util.spec_from_file_location('startup_fixture.setup', ROOT / 'setup.py')
            setup = importlib.util.module_from_spec(spec)
            with patch.object(logging.getLogger('startup'), 'exception'):
                spec.loader.exec_module(setup)
                setup.P.module_list[0].plugin_load()
        return app, setup.P, setup.P.module_list[0], delegate_calls

    def test_normal_setup_preserves_metadata_menu_and_delegate(self):
        _, p, module, calls = self.load()
        self.assertEqual(p.plugin_info['title'], '그누보드 TXT 다운로더')
        self.assertEqual(p.plugin_info['version'], '0.1.8')
        self.assertEqual(p.plugin_info['startup_status'], 'ready')
        self.assertEqual(len(p.setting['menu']['list']), 4)
        self.assertEqual(module.process_menu('download', None), '정상 화면')
        self.assertEqual(module.process_command('status', None, None, None, None), '정상 명령')
        module.scheduler_function()
        module.plugin_unload()
        self.assertEqual(calls, ['load', 'schedule', 'unload'])

    def test_missing_dependencies_keep_name_and_actionable_page(self):
        for missing in ('requests', 'bs4', 'fcntl'):
            with self.subTest(missing=missing):
                app, p, module, calls = self.load(missing=missing)
                self.assertEqual(p.plugin_info['title'], '그누보드 TXT 다운로더')
                self.assertEqual(p.plugin_info['startup_status'], 'error')
                with app.test_request_context():
                    html = module.process_menu('download', None)
                    self.assertIn('파일 설치 완료 · 실행 준비 실패', html)
                    self.assertIn(missing, html)
                    self.assertIn('Linux' if missing == 'fcntl' else 'pip install -r', html)
                    response, status = module.process_command('latest', None, None, None, None)
                    self.assertEqual(status, 503)
                    self.assertEqual(response.json['ret'], 'error')
                module.scheduler_function()
                self.assertFalse(calls)

    def test_constructor_failure_and_error_markup_escape(self):
        app, p, module, _ = self.load(constructor_error=PermissionError('<script>blocked</script>'))
        with app.test_request_context():
            html = module.process_menu('setting', None)
        self.assertIn('PermissionError', html)
        self.assertNotIn('<script>blocked</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('초기화 실패', p.plugin_info['description'])

    def test_plugin_load_failure_keeps_menu_and_stops_worker(self):
        app, p, module, calls = self.load(load_error=OSError('database unavailable'))
        self.assertEqual(p.plugin_info['startup_status'], 'error')
        self.assertEqual(calls, ['unload'])
        with app.test_request_context():
            self.assertIn('database unavailable', module.process_menu('history', None))
        module.scheduler_function()
        self.assertEqual(calls, ['unload'])


if __name__ == '__main__':
    unittest.main()
