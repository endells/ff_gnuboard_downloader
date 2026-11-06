from plugin import create_plugin_instance

P = create_plugin_instance({
    'filepath': __file__, 'use_db': True, 'use_default_setting': True,
    'home_module': 'basic', 'setting_menu': None, 'default_route': 'normal',
    'menu': {'uri': __package__, 'name': '그누보드 TXT 다운로더', 'list': [
        {'uri': 'basic/download', 'name': '다운로더'},
        {'uri': 'basic/organize', 'name': '정리'},
        {'uri': 'basic/setting', 'name': '설정 / 스케줄'},
        {'uri': 'basic/history', 'name': '이력 / 오류'},
    ]},
})
from .mod_basic import ModuleBasic
P.set_module_list([ModuleBasic])
