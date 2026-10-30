"""Test the packaged NumPy compatibility fallback with real CPU tensors."""

import runpy
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import torch


HOOK = Path(__file__).resolve().parents[1] / 'pyi_rth_numpy_compat.py'


@pytest.fixture
def install_hook(monkeypatch):
    # Do not leave an import-time polling thread alive in the test process.
    monkeypatch.setattr(threading.Thread, 'start', lambda _thread: None)
    monkeypatch.setattr(time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(torch, '_vb_from_numpy_patched', False, raising=False)
    def install(original):
        monkeypatch.setattr(torch, 'from_numpy', original)
        hook = runpy.run_path(str(HOOK))
        hook['_patch_torch_from_numpy']()
        return torch.from_numpy
    return install


def numpy_unavailable(_array):
    raise RuntimeError('Numpy is not available')


@pytest.mark.parametrize('dtype', [
    'float16', 'float32', 'float64', 'int8', 'int16', 'int32', 'int64',
    'uint8', 'bool', 'complex64', 'complex128',
])
def test_fallback_preserves_tensor_values_shape_and_dtype(install_hook, dtype):
    convert = install_hook(numpy_unavailable)
    array = np.array([[0, 1, 2], [3, 4, 5]], dtype=dtype).T
    assert not array.flags.c_contiguous
    result = convert(array)
    assert tuple(result.shape) == array.shape
    assert result.tolist() == array.tolist()
    assert str(result.dtype) == f'torch.{dtype}'


@pytest.mark.parametrize('array', [np.array(4.5, dtype='float32'), np.empty((0, 2), dtype='int16')])
def test_fallback_preserves_scalar_and_empty_tensor_shape(install_hook, array):
    result = install_hook(numpy_unavailable)(array)
    assert tuple(result.shape) == array.shape
    assert result.tolist() == array.tolist()


def test_successful_native_conversion_is_returned_without_copying(install_hook):
    calls = []
    sentinel = object()
    def native(array):
        calls.append(array)
        return sentinel
    array = np.array([1, 2], dtype='float32')
    assert install_hook(native)(array) is sentinel
    assert calls == [array]


@pytest.mark.parametrize('dtype', ['uint16', '>f4'])
def test_fallback_rejects_unsupported_or_non_native_byte_order(install_hook, dtype):
    with pytest.raises(TypeError, match='unsupported numpy dtype'):
        install_hook(numpy_unavailable)(np.array([1, 2], dtype=dtype))


def test_fallback_does_not_hide_unrelated_torch_runtime_errors(install_hook):
    def native(_array):
        raise RuntimeError('unrelated conversion failure')
    with pytest.raises(RuntimeError, match='unrelated conversion failure'):
        install_hook(native)(np.array([1, 2], dtype='float32'))


def test_repeated_hook_installation_does_not_wrap_torch_twice(install_hook):
    convert = install_hook(numpy_unavailable)
    hook = runpy.run_path(str(HOOK))
    hook['_patch_torch_from_numpy']()
    assert torch.from_numpy is convert
