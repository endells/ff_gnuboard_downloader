"""수집 모듈 초기화에 실패해도 FF의 이름·메뉴·오류 안내는 유지합니다."""
import shlex
import sys
from pathlib import Path

from flask import jsonify, render_template
from plugin import PluginModuleBase


class ModuleBootstrap(PluginModuleBase):
    db_default = {}

    def __init__(self, P):
        super().__init__(P, first_menu='download', name='basic', scheduler_desc='그누보드 새 글 확인')
        self.delegate = None
        self.error = None
        self.missing = None
        try:
            # requests/bs4/fcntl 오류가 setup.py import 전체를 중단하지 않도록 격리합니다.
            from .mod_basic import ModuleBasic
            self.delegate = ModuleBasic(P)
            self.db_default = self.delegate.db_default
        except Exception as exc:
            self.failed(exc)

    def failed(self, exc):
        self.error = '%s: %s' % (type(exc).__name__, exc)
        self.missing = getattr(exc, 'name', None) if isinstance(exc, ImportError) else None
        self.P.plugin_info['startup_status'] = 'error'
        self.P.plugin_info['description'] = '초기화 실패 — 플러그인 메뉴에서 오류와 해결 방법을 확인하세요.'
        self.P.logger.exception('그누보드 TXT 다운로더 초기화 실패')

    def plugin_load(self):
        if self.error is None:
            try:
                self.delegate.plugin_load()
                self.P.plugin_info['startup_status'] = 'ready'
            except Exception as exc:
                self.failed(exc)
                self.plugin_unload()

    def plugin_unload(self):
        if self.delegate is not None:
            try:
                self.delegate.plugin_unload()
            except Exception:
                self.P.logger.exception('그누보드 작업 종료 실패')

    def scheduler_function(self):
        if self.error is None:
            return self.delegate.scheduler_function()

    def process_menu(self, page, req):
        if self.error is None:
            return self.delegate.process_menu(page, req)
        requirements = Path(__file__).resolve().parent / 'requirements.txt'
        command = ' '.join(shlex.quote(part) for part in
                           (sys.executable, '-m', 'pip', 'install', '-r', str(requirements)))
        return render_template('ff_gnuboard_downloader_startup.html',
                               info=self.P.plugin_info, error=self.error,
                               missing=self.missing, install_command=command)

    def process_command(self, command, arg1, arg2, arg3, req):
        if self.error is not None:
            return jsonify(ret='error', msg='초기화 실패: ' + self.error), 503
        return self.delegate.process_command(command, arg1, arg2, arg3, req)
