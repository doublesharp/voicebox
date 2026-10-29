"""Qwen and Whisper adapter behavior without model downloads."""

import asyncio
import sys
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import torch

from backend.backends import pytorch_backend as pt


@pytest.fixture
def stt_loader(monkeypatch):
    backend = pt.PyTorchSTTBackend()
    backend.device = 'cpu'
    monkeypatch.setattr(pt, 'is_model_cached', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(pt, 'model_load_progress', lambda *_args: nullcontext())
    clear = Mock()
    monkeypatch.setattr(pt, 'empty_device_cache', clear)
    processor = Mock()
    model = Mock()
    processor_load = Mock(return_value=processor)
    model_load = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        WhisperProcessor=SimpleNamespace(from_pretrained=processor_load),
        WhisperForConditionalGeneration=SimpleNamespace(from_pretrained=model_load),
    ))
    return backend, processor, model, processor_load, model_load, clear


@pytest.mark.parametrize('failure', ['model_load', 'device_transfer'])
def test_failed_whisper_switch_preserves_previous_model_and_processor(stt_loader, failure):
    backend, _, model, _, model_load, clear = stt_loader
    previous_model, previous_processor = object(), object()
    backend.model, backend.processor, backend.model_size = previous_model, previous_processor, 'base'
    if failure == 'model_load':
        model_load.side_effect = RuntimeError('load failed')
    else:
        model.to.side_effect = RuntimeError('load failed')
    with pytest.raises(RuntimeError, match='load failed'):
        backend._load_model_sync('small')
    assert backend.model is previous_model and backend.processor is previous_processor
    assert backend.model_size == 'base'
    clear.assert_called_once_with('cpu')


@pytest.mark.asyncio
async def test_cancelled_whisper_load_does_not_start_duplicate_worker(monkeypatch):
    backend = pt.PyTorchSTTBackend()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load(size):
        calls.append(size)
        entered.set()
        assert release.wait(timeout=5)
        backend.model, backend.processor, backend.model_size = object(), object(), size
    monkeypatch.setattr(backend, '_load_model_sync', load)
    first = asyncio.create_task(backend.load_model('base'))
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    second = asyncio.create_task(backend.load_model('base'))
    try:
        await asyncio.sleep(0.1)
        assert calls == ['base'] and not first.done()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None and calls == ['base']


@pytest.mark.asyncio
@pytest.mark.parametrize('long_audio', [False, True])
async def test_whisper_transcription_respects_audio_length_and_language(stt_loader, monkeypatch, long_audio):
    backend, processor, model, *_ = stt_loader
    backend.model, backend.processor = model, processor
    audio = np.zeros(pt.WHISPER_WINDOW_SAMPLES + (1 if long_audio else 0), dtype=np.float32)
    monkeypatch.setattr(pt, 'load_audio', lambda *_args, **_kwargs: (audio, 16000))
    class Inputs(dict):
        def to(self, device):
            assert device == 'cpu'
            return self
    processor.return_value = Inputs(input_features=torch.zeros(1), attention_mask=torch.ones(1))
    processor.batch_decode.return_value = [' transcript ']
    result = await backend.transcribe('input.wav', language='pl')
    assert result == 'transcript'
    kwargs = processor.call_args.kwargs
    assert ('return_attention_mask' in kwargs) is long_audio
    gen = model.generate.call_args.kwargs
    assert gen['language'] == 'pl' and gen['task'] == 'transcribe'
    assert ('return_timestamps' in gen) is long_audio


@pytest.mark.asyncio
async def test_qwen_tts_load_and_generation_preserve_options(monkeypatch):
    backend = pt.PyTorchTTSBackend()
    backend.device = 'cpu'
    monkeypatch.setattr(pt, 'is_model_cached', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(pt, 'model_load_progress', lambda *_args: nullcontext())
    model = Mock()
    model.generate_voice_clone.return_value = ([np.array([0.1, -0.2])], 24000)
    load = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, 'qwen_tts', SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=load)))
    seeds = []
    monkeypatch.setattr(pt, 'manual_seed', lambda seed, device: seeds.append((seed, device)))
    audio, rate = await backend.generate('Hello', ['voice'], language='ja', seed=9, instruct='whisper')
    assert rate == 24000 and np.allclose(audio, [0.1, -0.2])
    assert seeds == [(9, 'cpu')]
    assert model.generate_voice_clone.call_args.kwargs == {
        'text': 'Hello', 'voice_clone_prompt': ['voice'], 'language': 'japanese', 'instruct': 'whisper'
    }
    assert load.call_args.kwargs['torch_dtype'] == torch.float32


@pytest.mark.asyncio
async def test_whisper_model_switch_waits_for_active_transcription(stt_loader, monkeypatch):
    backend, processor, model, *_ = stt_loader
    backend.model, backend.processor = model, processor
    entered, release = threading.Event(), threading.Event()
    def load_audio(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return np.zeros(100, dtype=np.float32), 16000
    monkeypatch.setattr(pt, 'load_audio', load_audio)
    class Inputs(dict):
        def to(self, _device):
            return self
    processor.return_value = Inputs(input_features=torch.zeros(1))
    processor.batch_decode.return_value = ['speech']
    def switch(size):
        backend.model, backend.processor, backend.model_size = object(), object(), size
    monkeypatch.setattr(backend, '_load_model_sync', switch)
    first = asyncio.create_task(backend.transcribe('input.wav'))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(backend.load_model('small'))
    try:
        await asyncio.sleep(0.1)
        assert backend.model is model and not second.done()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert results == ['speech', None]
    assert backend.model_size == 'small'


@pytest.mark.asyncio
async def test_whisper_unload_waits_for_active_transcription(stt_loader, monkeypatch):
    from backend.routes import models as model_routes
    from backend.services import transcribe
    backend, processor, model, *_ = stt_loader
    backend.model, backend.processor = model, processor
    entered, release = threading.Event(), threading.Event()
    def load_audio(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return np.zeros(100, dtype=np.float32), 16000
    monkeypatch.setattr(pt, 'load_audio', load_audio)
    monkeypatch.setattr(transcribe, 'get_whisper_model', lambda: backend)
    monkeypatch.setattr(transcribe, 'get_stt_backend', lambda: backend)
    class Inputs(dict):
        def to(self, _device):
            return self
    processor.return_value = Inputs(input_features=torch.zeros(1))
    processor.batch_decode.return_value = ['speech']
    first = asyncio.create_task(backend.transcribe('input.wav'))
    assert await asyncio.to_thread(entered.wait, 5)
    unload = asyncio.create_task(model_routes.unload_model_by_name('whisper-base'))
    try:
        await asyncio.sleep(0.1)
        assert backend.model is model and not unload.done()
    finally:
        release.set()
        results = await asyncio.gather(first, unload, return_exceptions=True)
    assert results[0] == 'speech'
    assert results[1] == {'message': 'Model whisper-base unloaded successfully'}
    assert not backend.is_loaded()


@pytest.mark.asyncio
async def test_cancelled_mlx_whisper_transcription_holds_ownership(monkeypatch):
    from backend.backends import mlx_backend as mlx
    backend = mlx.MLXSTTBackend()
    backend.model = SimpleNamespace(generate=lambda *_args, **_kwargs: 'speech')
    entered, release = asyncio.Event(), asyncio.Event()
    async def run_on_thread(fn, *args):
        entered.set()
        await release.wait()
        return fn(*args)
    monkeypatch.setattr(mlx, '_run_on_mlx_thread', run_on_thread)
    first = asyncio.create_task(backend.transcribe('input.wav'))
    await entered.wait()
    first.cancel()
    try:
        await asyncio.sleep(0)
        assert not first.done() and backend._op_lock.locked()
    finally:
        release.set()
        result = await asyncio.gather(first, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert not backend._op_lock.locked()


@pytest.mark.asyncio
async def test_cancelled_mlx_whisper_load_holds_ownership(monkeypatch):
    from backend.backends import mlx_backend as mlx
    backend = mlx.MLXSTTBackend()
    entered, release = asyncio.Event(), asyncio.Event()
    async def run_on_thread(fn, *args):
        entered.set()
        await release.wait()
        return fn(*args)
    def load(size):
        backend.model, backend.model_size = object(), size
    monkeypatch.setattr(mlx, '_run_on_mlx_thread', run_on_thread)
    monkeypatch.setattr(backend, '_load_model_sync', load)
    first = asyncio.create_task(backend.load_model('base'))
    await entered.wait()
    first.cancel()
    try:
        await asyncio.sleep(0)
        assert not first.done() and backend._op_lock.locked()
    finally:
        release.set()
        result = await asyncio.gather(first, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert not backend._op_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['delete', 'shutdown'])
async def test_whisper_delete_and_shutdown_wait_for_transcription(stt_loader, monkeypatch, tmp_path, action):
    from backend import backends
    from backend.routes import models as model_routes
    from backend.services import transcribe
    from huggingface_hub import constants
    backend, processor, model, *_ = stt_loader
    backend.model, backend.processor = model, processor
    entered, release = threading.Event(), threading.Event()
    def load_audio(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return np.zeros(100, dtype=np.float32), 16000
    monkeypatch.setattr(pt, 'load_audio', load_audio)
    monkeypatch.setattr(transcribe, 'get_whisper_model', lambda: backend)
    monkeypatch.setattr(transcribe, 'get_stt_backend', lambda: backend)
    class Inputs(dict):
        def to(self, _device):
            return self
    processor.return_value = Inputs(input_features=torch.zeros(1))
    processor.batch_decode.return_value = ['speech']
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path))
    config = backends.get_model_config('whisper-base')
    cache = tmp_path / ('models--' + config.hf_repo_id.replace('/', '--'))
    cache.mkdir()
    first = asyncio.create_task(backend.transcribe('input.wav'))
    assert await asyncio.to_thread(entered.wait, 5)
    other = asyncio.create_task(model_routes.delete_model('whisper-base') if action == 'delete' else transcribe.unload_whisper_model_async())
    try:
        await asyncio.sleep(0.1)
        assert backend.model is model and not other.done() and cache.exists()
    finally:
        release.set()
        results = await asyncio.gather(first, other, return_exceptions=True)
    assert results[0] == 'speech' and not backend.is_loaded()
    if action == 'delete':
        assert results[1] == {'message': 'Model whisper-base deleted successfully'}
        assert not cache.exists()
    else:
        assert results[1] is None and cache.exists()
