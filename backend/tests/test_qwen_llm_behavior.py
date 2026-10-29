"""LLM adapter behavior with deterministic in-process model doubles."""

import asyncio
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import torch

from backend.backends import mlx_backend, qwen_llm_backend as qwen


@pytest.fixture
def torch_loader(monkeypatch):
    backend = qwen.PyTorchQwenLLMBackend()
    backend.device = 'cpu'
    monkeypatch.setattr(qwen, 'model_load_progress', lambda *_args: nullcontext())
    monkeypatch.setattr(qwen, 'is_model_cached', lambda *_args, **_kwargs: True)
    clear = Mock()
    monkeypatch.setattr(qwen, 'empty_device_cache', clear)
    tokenizer = object()
    model = Mock()
    tokenizer_load = Mock(return_value=tokenizer)
    model_load = Mock(return_value=model)
    fake = SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer_load),
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=model_load),
    )
    monkeypatch.setitem(sys.modules, 'transformers', fake)
    return backend, tokenizer, model, tokenizer_load, model_load, clear


@pytest.mark.parametrize('failure', ['model', 'to', 'eval'])
def test_failed_torch_load_does_not_leave_partial_model_or_tokenizer(torch_loader, failure):
    backend, _, model, _, load, clear = torch_loader
    if failure == 'model':
        load.side_effect = RuntimeError('load failed')
    else:
        getattr(model, failure).side_effect = RuntimeError('load failed')
    with pytest.raises(RuntimeError, match='load failed'):
        backend._load_model_sync('0.6B')
    assert not backend.is_loaded()
    assert backend.tokenizer is None
    assert backend._current_model_size is None
    clear.assert_called_once_with('cpu')


@pytest.mark.asyncio
async def test_torch_model_load_reuses_same_size_and_switches_cleanly(torch_loader):
    backend, tokenizer, model, token_load, model_load, clear = torch_loader
    await backend.load_model('0.6B')
    await backend.load_model('0.6B')
    assert backend.model is model and backend.tokenizer is tokenizer
    assert token_load.call_count == model_load.call_count == 1
    await backend.load_model('1.7B')
    assert model_load.call_count == 2
    assert model_load.call_args.args == ('Qwen/Qwen3-1.7B',)
    clear.assert_called_once_with('cpu')
    backend.unload_model()
    assert backend.model is None and backend.tokenizer is None


@pytest.mark.parametrize('temperature', [0.0, 0.7])
def test_torch_generation_formats_examples_and_only_returns_new_tokens(torch_loader, temperature):
    backend, _, _, _, _, _ = torch_loader
    class Inputs(dict):
        def to(self, device):
            assert device == 'cpu'
            return self
    tokenizer = Mock()
    tokenizer.eos_token_id = 99
    tokenizer.apply_chat_template.return_value = 'formatted'
    tokenizer.return_value = Inputs(input_ids=torch.tensor([[1, 2]]))
    tokenizer.decode.return_value = ' answer '
    model = Mock()
    model.generate.return_value = torch.tensor([[1, 2, 7, 8]])
    backend.model, backend.tokenizer = model, tokenizer
    result = backend._generate_sync('question', 'system', 33, temperature, [('example', 'reply')])
    assert result == 'answer'
    assert tokenizer.apply_chat_template.call_args.args[0] == [
        {'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'example'},
        {'role': 'assistant', 'content': 'reply'}, {'role': 'user', 'content': 'question'},
    ]
    assert tokenizer.decode.call_args.args[0].tolist() == [7, 8]
    kwargs = model.generate.call_args.kwargs
    assert kwargs['max_new_tokens'] == 33 and kwargs['do_sample'] is (temperature > 0)
    assert ('temperature' in kwargs) is (temperature > 0)


@pytest.mark.asyncio
async def test_concurrent_mlx_load_requests_for_same_model_load_only_once(monkeypatch):
    backend = qwen.MLXQwenLLMBackend()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    def load(size):
        calls.append(size)
        backend.model = object()
        backend.tokenizer = object()
        backend._current_model_size = size
    monkeypatch.setattr(backend, '_load_model_sync', load)
    async def run_on_thread(fn, *args):
        entered.set()
        await release.wait()
        return fn(*args)
    monkeypatch.setattr(mlx_backend, '_run_on_mlx_thread', run_on_thread)
    first = asyncio.create_task(backend.load_model('0.6B'))
    await entered.wait()
    second = asyncio.create_task(backend.load_model('0.6B'))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert calls == ['0.6B']


@pytest.mark.asyncio
@pytest.mark.parametrize('temperature', [0.0, 0.8])
async def test_mlx_generate_loads_once_and_formats_chat(monkeypatch, temperature):
    backend = qwen.MLXQwenLLMBackend()
    monkeypatch.setattr(qwen, 'model_load_progress', lambda *_args: nullcontext())
    monkeypatch.setattr(qwen, 'is_model_cached', lambda *_args, **_kwargs: True)
    async def run_on_thread(fn, *args):
        return fn(*args)
    monkeypatch.setattr(mlx_backend, '_run_on_mlx_thread', run_on_thread)
    tokenizer = Mock()
    tokenizer.apply_chat_template.return_value = 'formatted prompt'
    model = object()
    load = Mock(return_value=(model, tokenizer))
    generate = Mock(return_value=' output ')
    sampler = Mock(return_value='sampler')
    monkeypatch.setitem(sys.modules, 'mlx_lm', SimpleNamespace(load=load, generate=generate))
    monkeypatch.setitem(sys.modules, 'mlx_lm.sample_utils', SimpleNamespace(make_sampler=sampler))
    result = await backend.generate('question', system='system', max_tokens=32, temperature=temperature,
                                    examples=[('example', 'reply')])
    assert result == 'output'
    assert load.call_args.args == ('mlx-community/Qwen3-0.6B-4bit',)
    assert generate.call_args.args == (model, tokenizer)
    assert generate.call_args.kwargs['prompt'] == 'formatted prompt'
    assert generate.call_args.kwargs['max_tokens'] == 32
    assert generate.call_args.kwargs['sampler'] == ('sampler' if temperature else None)
    assert sampler.call_count == (1 if temperature else 0)
    assert tokenizer.apply_chat_template.call_args.args[0][-1] == {'role': 'user', 'content': 'question'}
    await backend.generate('next', temperature=0)
    assert load.call_count == 1


@pytest.mark.parametrize('backend_cls', [qwen.PyTorchQwenLLMBackend, qwen.MLXQwenLLMBackend])
def test_unknown_model_size_is_rejected(backend_cls):
    backend = backend_cls()
    with pytest.raises(ValueError, match='Unknown Qwen3 size'):
        backend._get_model_path('unsupported')


@pytest.mark.asyncio
async def test_torch_generation_retains_requested_model_until_worker_finishes(monkeypatch):
    import threading
    backend = qwen.PyTorchQwenLLMBackend()
    entered = threading.Event()
    release = threading.Event()
    def load(size):
        backend.model = object()
        backend.tokenizer = object()
        backend._current_model_size = size
        backend.model_size = size
    def generate(prompt, *_args):
        if prompt == 'first':
            entered.set()
            assert release.wait(timeout=5)
        return backend._current_model_size
    monkeypatch.setattr(backend, '_load_model_sync', load)
    monkeypatch.setattr(backend, '_generate_sync', generate)
    monkeypatch.setattr(qwen, 'empty_device_cache', lambda _device: None)
    first = asyncio.create_task(backend.generate('first', model_size='0.6B'))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(backend.generate('second', model_size='1.7B'))
    try:
        await asyncio.sleep(0.1)
    finally:
        release.set()
    assert await first == '0.6B'
    assert await second == '1.7B'


@pytest.mark.asyncio
async def test_cancelled_torch_generation_drains_before_another_model_load(monkeypatch):
    import threading
    backend = qwen.PyTorchQwenLLMBackend()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def load(size):
        calls.append(size)
        backend.model = object()
        backend.tokenizer = object()
        backend._current_model_size = size
        backend.model_size = size
    def generate(prompt, *_args):
        if prompt == 'first':
            entered.set()
            assert release.wait(timeout=5)
        return backend._current_model_size
    monkeypatch.setattr(backend, '_load_model_sync', load)
    monkeypatch.setattr(backend, '_generate_sync', generate)
    monkeypatch.setattr(qwen, 'empty_device_cache', lambda _device: None)
    first = asyncio.create_task(backend.generate('first', model_size='0.6B'))
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    second = asyncio.create_task(backend.generate('second', model_size='1.7B'))
    try:
        await asyncio.sleep(0.1)
        assert calls == ['0.6B']
        assert not first.done()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] == '1.7B'


@pytest.mark.asyncio
async def test_cancelled_torch_load_drains_before_subsequent_load(monkeypatch):
    import threading
    backend = qwen.PyTorchQwenLLMBackend()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load(size):
        calls.append(size)
        if size == '0.6B':
            entered.set()
            assert release.wait(timeout=5)
        backend.model = object()
        backend.tokenizer = object()
        backend._current_model_size = size
        backend.model_size = size
    monkeypatch.setattr(backend, '_load_model_sync', load)
    monkeypatch.setattr(qwen, 'empty_device_cache', lambda _device: None)
    first = asyncio.create_task(backend.load_model('0.6B'))
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    second = asyncio.create_task(backend.load_model('1.7B'))
    try:
        await asyncio.sleep(0.1)
        assert calls == ['0.6B']
        assert not first.done()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    assert calls == ['0.6B', '1.7B']
    assert backend._current_model_size == '1.7B'


@pytest.mark.asyncio
async def test_unload_endpoint_waits_for_active_torch_generation(monkeypatch):
    import threading
    from backend import backends
    from backend.routes import models as model_routes

    backend = qwen.PyTorchQwenLLMBackend()
    backend.model = object()
    backend.tokenizer = object()
    backend._current_model_size = backend.model_size = '0.6B'
    entered, release = threading.Event(), threading.Event()
    def generate(*_args):
        entered.set()
        assert release.wait(timeout=5)
        return 'ready' if backend.model is not None else 'unloaded'
    monkeypatch.setattr(backend, '_generate_sync', generate)
    monkeypatch.setattr(qwen, 'empty_device_cache', lambda _device: None)
    monkeypatch.setattr(backends, 'get_llm_backend_for_engine', lambda _engine: backend)
    first = asyncio.create_task(backend.generate('speak', model_size='0.6B'))
    assert await asyncio.to_thread(entered.wait, 5)
    unload = asyncio.create_task(model_routes.unload_model_by_name('qwen3-0.6b'))
    try:
        await asyncio.sleep(0.1)
        assert backend.is_loaded()
        assert not unload.done()
    finally:
        release.set()
        results = await asyncio.gather(first, unload, return_exceptions=True)
    assert results[0] == 'ready'
    assert results[1] == {'message': 'Model qwen3-0.6b unloaded successfully'}
    assert not backend.is_loaded()


@pytest.mark.asyncio
async def test_cancelled_mlx_generation_keeps_lock_until_worker_finishes(monkeypatch):
    backend = qwen.MLXQwenLLMBackend()
    backend.model = object()
    backend.tokenizer = object()
    backend._current_model_size = '0.6B'
    entered, release = asyncio.Event(), asyncio.Event()
    async def run_on_thread(fn, *args):
        entered.set()
        await release.wait()
        return fn(*args)
    monkeypatch.setattr(mlx_backend, '_run_on_mlx_thread', run_on_thread)
    monkeypatch.setattr(backend, '_generate_sync', lambda *_args: 'done')
    first = asyncio.create_task(backend.generate('first'))
    await entered.wait()
    first.cancel()
    try:
        await asyncio.sleep(0)
        assert not first.done()
        assert backend._op_lock.locked()
    finally:
        release.set()
        result = await asyncio.gather(first, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert not backend._op_lock.locked()


@pytest.mark.asyncio
async def test_delete_endpoint_waits_for_active_torch_generation(tmp_path, monkeypatch):
    import threading
    from backend import backends
    from backend.routes import models as model_routes
    from huggingface_hub import constants

    backend = qwen.PyTorchQwenLLMBackend()
    backend.model = object()
    backend.tokenizer = object()
    backend._current_model_size = backend.model_size = '0.6B'
    entered, release = threading.Event(), threading.Event()
    def generate(*_args):
        entered.set()
        assert release.wait(timeout=5)
        return 'ready' if backend.model is not None else 'unloaded'
    monkeypatch.setattr(backend, '_generate_sync', generate)
    monkeypatch.setattr(qwen, 'empty_device_cache', lambda _device: None)
    monkeypatch.setattr(backends, 'get_llm_backend_for_engine', lambda _engine: backend)
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path))
    config = backends.get_model_config('qwen3-0.6b')
    cache = tmp_path / ('models--' + config.hf_repo_id.replace('/', '--'))
    cache.mkdir()
    first = asyncio.create_task(backend.generate('speak', model_size='0.6B'))
    assert await asyncio.to_thread(entered.wait, 5)
    deleting = asyncio.create_task(model_routes.delete_model('qwen3-0.6b'))
    try:
        await asyncio.sleep(0.1)
        assert cache.exists() and backend.is_loaded()
        assert not deleting.done()
    finally:
        release.set()
        results = await asyncio.gather(first, deleting, return_exceptions=True)
    assert results[0] == 'ready'
    assert results[1] == {'message': 'Model qwen3-0.6b deleted successfully'}
    assert not cache.exists() and not backend.is_loaded()


@pytest.mark.asyncio
async def test_shutdown_unload_waits_for_active_torch_generation(monkeypatch):
    import threading
    from backend.services import llm

    backend = qwen.PyTorchQwenLLMBackend()
    backend.model = object()
    backend.tokenizer = object()
    backend._current_model_size = backend.model_size = '0.6B'
    entered, release = threading.Event(), threading.Event()
    def generate(*_args):
        entered.set()
        assert release.wait(timeout=5)
        return 'ready' if backend.model is not None else 'unloaded'
    monkeypatch.setattr(backend, '_generate_sync', generate)
    monkeypatch.setattr(qwen, 'empty_device_cache', lambda _device: None)
    monkeypatch.setattr(llm, 'get_llm_backend', lambda: backend)
    first = asyncio.create_task(backend.generate('speak', model_size='0.6B'))
    assert await asyncio.to_thread(entered.wait, 5)
    shutdown = asyncio.create_task(llm.unload_llm_model_async())
    try:
        await asyncio.sleep(0.1)
        assert backend.is_loaded()
        assert not shutdown.done()
    finally:
        release.set()
        results = await asyncio.gather(first, shutdown, return_exceptions=True)
    assert results == ['ready', None]
    assert not backend.is_loaded()
