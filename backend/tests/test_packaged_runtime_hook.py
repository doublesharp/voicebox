"""Source-level coverage for the PyInstaller runtime hook, with isolated imports."""

import importlib
import importlib.machinery
from pathlib import Path
import runpy
import sys
import types

import pytest


@pytest.fixture
def hook(monkeypatch):
    # The hook intentionally modifies global import state. Give it a private
    # meta-path and a fake torch parent so real torch remains untouched.
    monkeypatch.setattr(sys, 'meta_path', list(sys.meta_path))
    torch = types.ModuleType('torch')
    torch.__path__ = []
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'torch._dynamo', types.ModuleType('previous-dynamo'))
    monkeypatch.setitem(sys.modules, 'torch._dynamo.config', types.ModuleType('previous-config'))
    source = Path(__file__).resolve().parents[1] / 'pyi_rth_torch_compiler_disable.py'
    namespace = runpy.run_path(str(source))
    yield types.SimpleNamespace(**namespace)
    for name in ['torch._dynamo.fake', 'torch._dynamo.fake.deep']:
        sys.modules.pop(name, None)


def test_stub_supports_decorators_predicates_and_contexts(hook):
    def original(value):
        return value + 1
    assert hook._noop_callable(original) is original
    assert hook._noop_callable(recursive=False)(original) is original
    assert not hook._noop_callable()
    assert list(hook._noop_callable()) == []
    with pytest.raises(ValueError, match='visible'):
        with hook._noop_callable():
            raise ValueError('visible')


def test_installed_dynamo_stub_supports_nested_imports(hook):
    torch = sys.modules['torch']
    assert torch._dynamo is sys.modules['torch._dynamo']
    assert torch._dynamo.config is sys.modules['torch._dynamo.config']
    nested = importlib.import_module('torch._dynamo.fake.deep')
    assert nested.__name__ == 'torch._dynamo.fake.deep'
    assert nested.some_decorator(lambda: 42)() == 42
    with pytest.raises(AttributeError):
        getattr(nested, '__missing__')


@pytest.mark.parametrize('source', [
    'del obj\n',
    'value = 1\ndel obj',
    'value = 1\r\ndel obj\r\n',
    'value = 1\ndel obj\n',
])
def test_scipy_source_patch_handles_line_endings_and_end_of_file(hook, source):
    patched = hook._patch_scipy_distn_source(source)
    namespace = {}
    exec(compile(patched, 'scipy.py', 'exec'), namespace)
    assert 'obj' not in namespace


def test_masking_patch_changes_only_target_assignment(hook):
    source = ('other = is_torch_greater_or_equal("2.6", accept_dev=True)\n'
              '_is_torch_greater_or_equal_than_2_6 = is_torch_greater_or_equal("2.6", accept_dev=True)\n')
    patched = hook._patch_masking_utils_source(source)
    assert 'other = is_torch_greater_or_equal' in patched
    assert '_is_torch_greater_or_equal_than_2_6 = False' in patched
    assert hook._patch_masking_utils_source('unrelated = True\n') == 'unrelated = True\n'


class FakeLoader:
    path = 'fake-module.py'
    marker = 'delegated'
    def __init__(self, source=None, source_error=None):
        self.source = source
        self.source_error = source_error
        self.executions = 0
    def create_module(self, _spec):
        return None
    def get_source(self, _name):
        if self.source_error:
            raise self.source_error
        return self.source
    def exec_module(self, module):
        self.executions += 1
        module.fallback = True


def test_source_loader_patches_source_and_delegates_metadata(hook):
    inner = FakeLoader('value = 2\n')
    loader = hook._SourcePatchLoader(inner, lambda source: source.replace('2', '7'))
    module = types.ModuleType('example')
    module.__spec__ = importlib.machinery.ModuleSpec('example', loader, is_package=True)
    assert loader.create_module(module.__spec__) is None
    loader.exec_module(module)
    assert module.value == 7
    assert module.__path__ == module.__spec__.submodule_search_locations
    assert loader.marker == 'delegated'
    assert inner.executions == 0


@pytest.mark.parametrize('error', [None, OSError('source unavailable')])
def test_source_loader_falls_back_when_source_is_unavailable(hook, error):
    inner = FakeLoader(source_error=error)
    module = types.ModuleType('example')
    hook._SourcePatchLoader(inner, lambda source: source).exec_module(module)
    assert module.fallback is True
    assert inner.executions == 1


def test_scipy_loader_executes_patched_source_or_falls_back(hook):
    source = FakeLoader('value = 1\ndel obj\n')
    module = types.ModuleType('example')
    hook._ScipyDistnPrebindLoader(source).exec_module(module)
    assert module.value == 1
    assert source.executions == 0
    fallback = FakeLoader(source_error=OSError('missing source'))
    module = types.ModuleType('fallback')
    hook._ScipyDistnPrebindLoader(fallback).exec_module(module)
    assert module.obj is None and module.fallback is True


def test_source_finders_ignore_unrelated_modules_and_delegate(hook, monkeypatch):
    target = 'transformers.masking_utils'
    finder = hook._MaskingUtilsFinder()
    class BrokenFinder:
        def find_spec(self, *_args):
            raise RuntimeError('not available')
    class RealFinder:
        def find_spec(self, name, *_args):
            return importlib.machinery.ModuleSpec(name, FakeLoader('value = 1\n'))
    monkeypatch.setattr(sys, 'meta_path', [finder, BrokenFinder(), RealFinder()])
    assert finder.find_spec('unrelated') is None
    spec = finder.find_spec(target)
    assert isinstance(spec.loader, hook._SourcePatchLoader)
    assert hook._TransformersStubFinder().find_spec('unrelated') is None
    assert hook._TransformersStubFinder().find_spec('transformers.utils.auto_docstring') is not None
    assert hook._DynamoMetaPathFinder().find_spec('torch._dynamo') is None
