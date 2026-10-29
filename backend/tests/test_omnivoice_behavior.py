"""OmniVoice adapter behavior with deterministic models and cache doubles."""

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import torch

from backend.backends import omnivoice_backend as omni


@pytest.fixture
def adapter(monkeypatch):
    backend = omni.OmniVoiceBackend()
    backend._device = 'cpu'
    model = Mock()
    model.sampling_rate = 24000
    model.create_voice_clone_prompt.return_value = SimpleNamespace(
        ref_audio_tokens=torch.tensor([1, 2]), ref_text='recognized', ref_rms=0.5
    )
    model.generate.return_value = [np.array([0.1, -0.2], dtype=np.float32)]
    backend.model = model
    class VoiceClonePrompt:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    monkeypatch.setitem(sys.modules, 'omnivoice', SimpleNamespace(VoiceClonePrompt=VoiceClonePrompt))
    monkeypatch.setattr(omni, 'get_cache_key', lambda *_args: 'key')
    return backend, model


@pytest.mark.asyncio
async def test_create_prompt_uses_valid_cache_without_encoding(adapter, monkeypatch):
    backend, model = adapter
    cached = {'format_version': 1, 'ref_audio_tokens': torch.tensor([9]), 'ref_text': 'words', 'ref_rms': 0.2}
    monkeypatch.setattr(omni, 'get_cached_voice_prompt', lambda _key: cached)
    result, hit = await backend.create_voice_prompt('voice.wav', 'words')
    assert hit and result is cached
    model.create_voice_clone_prompt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('cached', [
    {'format_version': 0, 'ref_audio_tokens': torch.tensor([1]), 'ref_text': 'old', 'ref_rms': 0.5},
    {'format_version': 1, 'ref_audio_tokens': torch.tensor([1])},
    {'format_version': 1, 'ref_audio_tokens': torch.tensor([1]), 'ref_text': 'words'},
    None,
])
async def test_stale_or_incomplete_cached_prompt_is_reencoded(adapter, monkeypatch, cached):
    backend, model = adapter
    save = Mock()
    monkeypatch.setattr(omni, 'get_cached_voice_prompt', lambda _key: cached)
    monkeypatch.setattr(omni, 'cache_voice_prompt', save)
    result, hit = await backend.create_voice_prompt('voice.wav', 'words')
    assert not hit
    assert result['ref_text'] == 'recognized' and result['ref_rms'] == 0.5
    assert result['ref_audio_tokens'].tolist() == [1, 2]
    model.create_voice_clone_prompt.assert_called_once_with(ref_audio='voice.wav', ref_text='words')
    save.assert_called_once()
    assert backend._rebuild_clone_prompt(result).ref_text == 'recognized'


@pytest.mark.asyncio
async def test_empty_reference_text_loads_asr_once_and_can_skip_cache(adapter, monkeypatch):
    backend, model = adapter
    monkeypatch.setattr(omni, 'get_cache_key', Mock(side_effect=AssertionError('cache disabled')))
    for _ in range(2):
        prompt, cached = await backend.create_voice_prompt('voice.wav', '', use_cache=False)
        assert not cached and prompt['ref_text'] == 'recognized'
    model.load_asr_model.assert_called_once()
    assert model.create_voice_clone_prompt.call_args.kwargs['ref_text'] is None


@pytest.mark.asyncio
async def test_generation_uses_clone_prompt_language_seed_and_instructions(adapter, monkeypatch):
    backend, model = adapter
    seeds = []
    monkeypatch.setattr(omni, 'manual_seed', lambda seed, device: seeds.append((seed, device)))
    prompt = {'format_version': 1, 'ref_audio_tokens': torch.tensor([2]), 'ref_text': 'spoken', 'ref_rms': 0.5}
    audio, rate = await backend.generate('Hello', prompt, language='ar', seed=7, instruct='female')
    assert rate == 24000 and np.allclose(audio, [0.1, -0.2])
    assert seeds == [(7, 'cpu')]
    kwargs = model.generate.call_args.kwargs
    assert kwargs['text'] == 'Hello' and kwargs['language'] == 'arb' and kwargs['num_step'] == 32
    assert kwargs['instruct'] == 'female' and kwargs['voice_clone_prompt'].ref_text == 'spoken'


@pytest.mark.asyncio
async def test_generation_reports_invalid_instruction_and_rejects_stale_prompt(adapter):
    backend, model = adapter
    model.generate.side_effect = ValueError('invalid instruct value')
    with pytest.raises(ValueError, match='fixed set of attributes'):
        await backend.generate('Hi', {}, instruct='make it blue')
    with pytest.raises(ValueError, match='format version'):
        await backend.generate('Hi', {'format_version': 0, 'ref_audio_tokens': [1]})


@pytest.mark.asyncio
async def test_cancelled_model_load_does_not_allow_duplicate_live_load(monkeypatch):
    backend = omni.OmniVoiceBackend()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load():
        calls.append('load')
        entered.set()
        assert release.wait(timeout=5)
        backend.model = object()
    monkeypatch.setattr(backend, '_load_model_sync', load)
    first = asyncio.create_task(backend.load_model())
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    second = asyncio.create_task(backend.load_model())
    try:
        await asyncio.sleep(0.1)
        assert calls == ['load']
        assert not first.done()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None and calls == ['load']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['encode', 'generate'])
async def test_cancelled_operation_waits_for_its_audio_worker(adapter, monkeypatch, operation):
    backend, model = adapter
    entered, release = threading.Event(), threading.Event()
    def wait_for_release(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        if operation == 'encode':
            return SimpleNamespace(ref_audio_tokens=torch.tensor([1]), ref_text='words', ref_rms=0.1)
        return [np.array([0.1], dtype=np.float32)]
    if operation == 'encode':
        model.create_voice_clone_prompt.side_effect = wait_for_release
        task = asyncio.create_task(backend.create_voice_prompt('voice.wav', 'words', use_cache=False))
    else:
        model.generate.side_effect = wait_for_release
        task = asyncio.create_task(backend.generate('Hello', {}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
