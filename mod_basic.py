import json
import os
import secrets
import tempfile
import threading
from pathlib import Path

from flask import jsonify, render_template, session
from framework import F, Job
from plugin import PluginModuleBase

from .core import DEFAULTS, Engine, Error, board_key, fingerprint, organizer_config, validate
from .organizer import Engine as Organizer
from .folders import list_folders
from .epub_export import Engine as EpubEngine, folders as epub_folders


class ModuleBasic(PluginModuleBase):
    db_default = dict(DEFAULTS)

    def __init__(self, P):
        super().__init__(P, first_menu='download', name='basic', scheduler_desc='그누보드 새 글 확인')
        root = Path(F.config['path_data']) / 'db'
        self.engine = Engine(root / 'ff_gnuboard_download.db')
        self.organizer = Organizer(root / 'ff_gnuboard_organize.db')
        self.epub = EpubEngine(root / 'ff_gnuboard_epub.db')
        self.engine.lock_path = self.organizer.lock_path = self.epub.lock_path = root / 'ff_gnuboard_job.lock'
        self.organizer.title_lookup = self.title_lookup
        self.cookie_path = root / 'ff_gnuboard_cookie.json'
        self.settings_lock = threading.RLock()

    def title_lookup(self, folder):
        with self.engine.connect() as db:
            rows = db.execute("SELECT path,title FROM posts WHERE status='completed'").fetchall()
        return {Path(path).name: title for path, title in rows if Path(path).parent.resolve() == Path(folder).resolve()}

    def raw_config(self):
        with self.settings_lock:
            result = {k: self.P.ModelSetting.get(k) or v for k, v in DEFAULTS.items()}
        root = Path(F.config['path_data']) / 'downloads' / 'gnuboard'
        result['download_path'] = result['download_path'] or str(root / 'inbox')
        result['target_path'] = result['target_path'] or str(root / 'sorted')
        result['short_path'] = result['short_path'] or str(Path(result['target_path']) / '단편')
        return result

    def config(self):
        return validate(self.raw_config())

    def plugin_load(self):
        self.engine.snapshot()
        self.organizer.snapshot()
        try:
            self.sync_schedule(self.config())
        except Error as exc:
            self.P.logger.warning('그누보드 설정 확인: %s', exc)

    def plugin_unload(self):
        self.epub.stop()
        self.engine.pause()
        self.organizer.stop()
        for suffix in ('', '_organize'):
            job_id = self.get_scheduler_name() + suffix
            if F.scheduler.is_include(job_id):
                F.scheduler.remove_job(job_id)

    def sync_schedule(self, config, previous=None):
        for suffix, enabled, interval, target, description in [
            ('', 'basic_auto_start', 'basic_interval', self.scheduler_function, '그누보드 새 글 확인'),
            ('_organize', 'organize_auto', 'organize_interval', self.organizer_schedule, 'TXT 제목별 정리'),
        ]:
            job_id = self.get_scheduler_name() + suffix
            included = F.scheduler.is_include(job_id)
            if not config[enabled]:
                if included:
                    F.scheduler.remove_job(job_id)
                continue
            if included and (previous is None or config[interval] == previous[interval]):
                continue
            if included:
                F.scheduler.remove_job(job_id)
            job = Job(self.P.package_name, job_id, str(config[interval]), target, description)
            F.scheduler.add_job_instance(job, run=False)

    def scheduler_function(self):
        with F.app.app_context():
            try:
                config = self.config()
                if config['basic_auto_start']:
                    self.engine.start('latest', config, cookie=self.read_cookie(config), on_finished=self.after_download)
            except (Error, OSError) as exc:
                self.engine.put('schedule_message', str(exc))
                self.P.logger.warning('그누보드 예약 수집: %s', exc)

    def organizer_schedule(self):
        with F.app.app_context():
            try:
                config = self.config()
                if config['organize_auto']:
                    self.organizer.start('scheduled', organizer_config(config))
            except (Error, OSError) as exc:
                self.P.logger.warning('그누보드 예약 정리: %s', exc)

    def after_download(self, config):
        if config['auto_organize']:
            try:
                self.organizer.start('scheduled', organizer_config(config))
            except (Error, OSError) as exc:
                self.engine.put('organize_message', '자동 정리 시작 실패: ' + str(exc))

    def read_cookie(self, config):
        try:
            data = json.loads(self.cookie_path.read_text(encoding='utf-8'))
            if config['board_url'] and data['board'] == board_key(config['board_url']):
                return data['cookie']
        except (OSError, ValueError, KeyError):
            pass
        return ''

    def save_cookie(self, raw, config):
        if raw.get('clear_cookie'):
            self.cookie_path.unlink(missing_ok=True)
            return
        value = str(raw.get('cookie', '')).strip()
        if not value:
            return
        if not config['board_url']:
            raise Error('쿠키를 저장하기 전에 게시판 주소를 입력하세요.')
        if value.lower().startswith('cookie:'):
            value = value[7:].strip()
        if len(value) > 16384 or '\n' in value or '\r' in value or '=' not in value:
            raise Error('쿠키는 Cookie 요청 헤더의 한 줄 값으로 입력하세요. JSON 형식은 지원하지 않습니다.')
        data = json.dumps({'board': board_key(config['board_url']), 'cookie': value}, ensure_ascii=False)
        fd, path = tempfile.mkstemp(dir=str(self.cookie_path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as writer:
                writer.write(data)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(path, str(self.cookie_path))
        finally:
            Path(path).unlink(missing_ok=True)

    def process_menu(self, page, req):
        if page not in ('download', 'organize', 'setting', 'history'):
            page = 'download'
        if not session.get('gnuboard_csrf'):
            session['gnuboard_csrf'] = secrets.token_urlsafe(32)
        return render_template('ff_gnuboard_downloader_basic.html', page=page,
            config=self.raw_config(), package_name=self.P.package_name,
            data_path=str(F.config['path_data']),
            csrf_token=session['gnuboard_csrf'], cookie_saved=self.cookie_path.is_file())

    def process_command(self, command, arg1, arg2, arg3, req):
        expected = session.get('gnuboard_csrf', '')
        given = req.form.get('csrf_token', '')
        if req.method != 'POST' or not expected or not secrets.compare_digest(expected, given):
            return jsonify(ret='error', msg='페이지를 새로고침한 후 다시 시도하세요.'), 403
        try:
            if command == 'epub_folders':
                return jsonify(ret='success', rows=epub_folders(self.raw_config()['target_path'], self.raw_config()['short_path']))
            if command == 'epub_start':
                selected = None if arg1 == 'all' else json.loads(arg2 or '[]')
                if arg1 not in ('all', 'selected'):
                    raise Error('EPUB 대상 선택이 올바르지 않습니다.')
                self.epub.start(self.raw_config()['target_path'], selected, self.raw_config()['short_path'])
                return jsonify(ret='success', msg='EPUB 생성을 시작했습니다.')
            if command == 'epub_stop':
                self.epub.stop()
                return jsonify(ret='success', msg='EPUB 생성 중지를 요청했습니다.')
            if command == 'folders':
                return jsonify(ret='success', **list_folders(arg1, arg2 or 1, arg3 == 'nearest'))
            if command == 'save_download_path':
                with self.settings_lock:
                    config = validate(dict(self.raw_config(), download_path=arg1 or ''))
                    self.P.ModelSetting.set('download_path', config['download_path'])
                return jsonify(ret='success', path=config['download_path'],
                               msg='다운로드 폴더를 저장했습니다. 다음 작업부터 적용됩니다. 기존 파일은 이동하지 않습니다.')
            if command == 'save':
                raw = json.loads(arg1 or '{}')
                config = validate(raw)
                with self.settings_lock:
                    previous = self.config()
                    self.save_cookie(raw, config)
                    for key, value in config.items():
                        self.P.ModelSetting.set(key, str(value))
                    self.sync_schedule(config, previous)
                return jsonify(ret='success', msg='설정을 저장했습니다. 다음 작업부터 적용됩니다.')
            if command == 'status':
                return jsonify(ret='success', state=self.engine.snapshot(), organize=self.organizer.snapshot(),
                    epub=self.epub.snapshot(),
                    schedule=F.scheduler.is_include(self.get_scheduler_name()),
                    organize_schedule=F.scheduler.is_include(self.get_scheduler_name() + '_organize'),
                    schedule_message=self.engine.get('schedule_message', ''),
                    organize_message=self.engine.get('organize_message', ''))
            if command == 'analysis':
                return jsonify(ret='success', analysis=self.engine.get('analysis'))
            if command == 'pause':
                return jsonify(ret='success', msg=self.engine.pause())
            if command == 'stop':
                if arg1 == 'organize':
                    self.organizer.stop()
                    return jsonify(ret='success', msg='정리 중지를 요청했습니다. 완료된 복사본은 유지합니다.')
                if arg1 == 'epub':
                    self.epub.stop()
                    return jsonify(ret='success', msg='EPUB 생성 중지를 요청했습니다.')
                if arg1 not in (None, '', 'download'):
                    raise Error('중지할 작업이 올바르지 않습니다.')
                return jsonify(ret='success', msg=self.engine.stop())
            if command == 'plan':
                return jsonify(ret='success', **self.organizer.preview_page(max(1, int(arg1 or 1))))
            if command == 'history':
                return jsonify(ret='success', **self.engine.post_history(max(1, int(arg1 or 1)), arg2 == 'failed'))
            if command == 'runs':
                engine = self.organizer if arg2 == 'organize' else self.engine
                return jsonify(ret='success', **engine.history(max(1, int(arg1 or 1))))
            if command == 'details':
                engine = self.organizer if arg3 == 'organize' else self.engine
                return jsonify(ret='success', **engine.details(int(arg1), max(1, int(arg2 or 1))))
            config = self.config()
            if command == 'preview':
                self.organizer.start('preview', organizer_config(config))
            elif command == 'copy':
                self.organizer.start('copy', organizer_config(config), json.loads(arg1 or '[]'))
            elif command == 'organize_now':
                self.organizer.start('scheduled', organizer_config(config))
            elif command in ('analyze', 'range', 'until', 'latest', 'resume'):
                self.engine.start(command, config, int(arg1 or 1), int(arg2 or arg1 or 1),
                                  cookie=self.read_cookie(config), on_finished=self.after_download)
            else:
                raise Error('지원하지 않는 명령입니다.')
            return jsonify(ret='success', msg='작업을 시작했습니다.')
        except (Error, OSError, ValueError, TypeError) as exc:
            return jsonify(ret='error', msg=str(exc))
        except Exception:
            self.P.logger.exception('그누보드 명령 처리 실패')
            return jsonify(ret='error', msg='처리 중 오류가 발생했습니다. FF 로그를 확인하세요.'), 500
