"""打包清单不能为了发现模块而执行可选组件；namespace 厂商也不能漏。"""
from build_backend import litellm_hidden_imports


def test_litellm_manifest_excludes_subtree_not_prefix(tmp_path):
    for path in ('__init__.py', 'proxy/ui_crud_endpoints/__init__.py',
                 'proxy/ui_crud_endpoints/routes.py', 'proxy/ui_crud_endpoints_other.py',
                 'llms/dashscope/chat/transformation.py', 'litellm_core_utils/helper.py'):
        source = tmp_path / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("raise RuntimeError('禁止发现阶段导入')", encoding='utf8')
    assert litellm_hidden_imports(tmp_path, {'litellm.binary'}, ['litellm.proxy.ui_crud_endpoints']) == [
        'litellm', 'litellm.binary', 'litellm.litellm_core_utils.helper',
        'litellm.llms.dashscope.chat.transformation', 'litellm.proxy.ui_crud_endpoints_other']


def test_eager_native_extension_is_never_excluded(tmp_path):
    from build_backend import build_litellm_excludes
    for folder in ['llms', 'proxy']:
        (tmp_path / folder).mkdir()
    native = 'litellm.rust_bridge._native'
    assert native not in build_litellm_excludes({native}, tmp_path)


def test_build_hook_collects_only_non_ui_data(tmp_path, monkeypatch):
    """执行实际生成的 hook，比较全量清单；不启动编译，也不覆盖现有 exe。"""
    import runpy
    from pathlib import Path
    import pytest
    import build_backend as build
    hooks = pytest.importorskip('PyInstaller.utils.hooks')
    monkeypatch.setattr(build, '__file__', str(tmp_path / 'backend/build_backend.py'))
    monkeypatch.setattr(build, 'detect_litellm_import_profile', lambda: (set(), tmp_path))
    monkeypatch.setattr(build, 'build_litellm_excludes', lambda *_: [])
    monkeypatch.setattr(build, 'litellm_hidden_imports', lambda *_: ['litellm'])

    class StopBeforeCompile(Exception):
        pass

    def inspect_args(args, **kwargs):
        assert {arg for arg in args if arg.startswith('--collect-submodules=lark_oapi')} == {
            '--collect-submodules=lark_oapi.api.im.v1', '--collect-submodules=lark_oapi.ws',
        }
        assert '--collect-data=litellm' not in args
        hook_dir = Path(args[args.index('--additional-hooks-dir') + 1])
        actual = set(runpy.run_path(str(hook_dir / 'hook-litellm.py'))['datas'])
        full = set(hooks.collect_data_files('litellm'))
        ui = {row for row in full if Path(row[1]).is_relative_to('litellm/proxy/_experimental/out')}
        assert full - ui and actual == full - ui
        raise StopBeforeCompile

    monkeypatch.setattr(build.subprocess, 'run', inspect_args)
    with pytest.raises(StopBeforeCompile):
        build.build_and_move()
