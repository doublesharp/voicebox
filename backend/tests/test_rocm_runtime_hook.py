"""Exercise the frozen ROCm SDK lookup hook with isolated fake packages."""

import importlib
import platform
import runpy
import sys
import types
from pathlib import Path

import pytest


HOOK = Path(__file__).resolve().parents[1] / 'pyi_rth_rocm_sdk.py'


@pytest.fixture
def sdk(monkeypatch):
    module = types.ModuleType('rocm_sdk')
    module._dist_info = types.SimpleNamespace(ALL_LIBRARIES={}, determine_target_family=lambda: 'gfx-test')
    monkeypatch.setitem(sys.modules, 'rocm_sdk', module)
    runpy.run_path(str(HOOK))
    return module


def entry(name, *, target=False, dll='*.dll', so='*.so', windows='bin', posix='lib'):
    package = types.SimpleNamespace(
        is_target_specific=target,
        logical_name=name,
        get_py_package_name=lambda family: f'{name}_{family}' if family else name,
    )
    return types.SimpleNamespace(package=package, dll_pattern=dll, so_pattern=so,
                                 windows_relpath=windows, posix_relpath=posix)


@pytest.mark.parametrize('system,directory,pattern', [('Linux', 'lib', 'lib*.so'), ('Windows', 'bin', '*.dll')])
def test_finds_sorted_libraries_and_target_packages(sdk, monkeypatch, tmp_path, system, directory, pattern):
    monkeypatch.setattr(platform, 'system', lambda: system)
    core = entry('core', dll=pattern, so=pattern)
    gpu = entry('gpu', target=True, dll=pattern, so=pattern)
    sdk._dist_info.ALL_LIBRARIES.update({'core': core, 'gpu': gpu})
    names = []
    roots = {}
    for name in ['core', 'gpu_gfx-test']:
        root = tmp_path / name
        (root / directory).mkdir(parents=True)
        for filename in (['libz.so', 'liba.so'] if system == 'Linux' else ['z.dll', 'a.dll']):
            (root / directory / filename).write_bytes(b'fixture')
        roots[name] = types.SimpleNamespace(__file__=str(root / '__init__.py'))
    def load(name):
        names.append(name)
        return roots[name]
    monkeypatch.setattr(importlib, 'import_module', load)
    paths = sdk.find_libraries('core', 'gpu')
    assert names == ['core', 'gpu_gfx-test']
    assert [path.name for path in paths] == (['liba.so', 'liba.so'] if system == 'Linux' else ['a.dll', 'a.dll'])


def test_windows_skips_libraries_without_dlls(sdk, monkeypatch):
    monkeypatch.setattr(platform, 'system', lambda: 'Windows')
    sdk._dist_info.ALL_LIBRARIES['headers'] = entry('headers', dll=None)
    monkeypatch.setattr(importlib, 'import_module', lambda _name: pytest.fail('skipped entry must not import'))
    assert sdk.find_libraries('headers') == []


def test_unknown_library_reports_requested_name(sdk):
    with pytest.raises(ModuleNotFoundError, match="Unknown rocm library 'missing'"):
        sdk.find_libraries('missing')


def test_missing_packages_are_aggregated_and_sorted(sdk, monkeypatch):
    sdk._dist_info.ALL_LIBRARIES.update({'z': entry('zeta'), 'a': entry('alpha')})
    def missing(name):
        raise ModuleNotFoundError(f'No module named {name}', name=name)
    monkeypatch.setattr(importlib, 'import_module', missing)
    with pytest.raises(ModuleNotFoundError, match='alpha, zeta') as caught:
        sdk.find_libraries('z', 'a', 'z')
    assert 'build_binary.py' in str(caught.value)


def test_dependency_import_errors_are_not_misreported_as_missing_backend(sdk, monkeypatch):
    sdk._dist_info.ALL_LIBRARIES['x'] = entry('backend')
    def missing_dependency(_name):
        raise ModuleNotFoundError('missing dependency', name='another_dependency')
    monkeypatch.setattr(importlib, 'import_module', missing_dependency)
    with pytest.raises(ModuleNotFoundError, match='missing dependency') as caught:
        sdk.find_libraries('x')
    assert caught.value.name == 'another_dependency'


@pytest.mark.parametrize('system', ['Linux', 'Windows'])
def test_missing_library_file_reports_search_path(sdk, monkeypatch, tmp_path, system):
    monkeypatch.setattr(platform, 'system', lambda: system)
    sdk._dist_info.ALL_LIBRARIES['hip'] = entry('core')
    monkeypatch.setattr(importlib, 'import_module', lambda _name: types.SimpleNamespace(__file__=str(tmp_path / '__init__.py')))
    with pytest.raises(FileNotFoundError, match="Could not find rocm library 'hip'") as caught:
        sdk.find_libraries('hip')
    assert str(tmp_path) in str(caught.value)


def test_absent_optional_sdk_does_not_break_startup(monkeypatch):
    monkeypatch.setitem(sys.modules, 'rocm_sdk', None)
    runpy.run_path(str(HOOK))
