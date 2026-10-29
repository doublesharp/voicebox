"""TADA adapter behavior with deterministic model and encoder doubles."""

import asyncio
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import soundfile as sf
import torch

from backend.backends import hume_backend as hume
from backend.utils import dac_shim


@pytest.fixture
def loader(monkeypatch):
    backend = hume.HumeTadaBackend()
    monkeypatch.setattr(backend, '_get_device', lambda: 'cpu')
    monkeypatch.setattr(hume, 'is_model_cached', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(hume, 'model_load_progress', lambda *_args: nullcontext())
    clear = Mock()
    monkeypatch.setattr(hume, 'empty_device_cache', clear)
    monkeypatch.setattr(dac_shim, 'install_dac_shim', lambda: None)
    import huggingface_hub
    download = Mock(side_effect=lambda repo_id, **_kwargs: '/cached/' + repo_id.split('/')[-1])
    monkeypatch.setattr(huggingface_hub, 'snapshot_download', download)
    encoder = Mock()
    encoder.to.return_value = encoder
    model = Mock()
    model.to.return_value = model
    model.parameters.return_value = iter([torch.nn.Parameter(torch.zeros(1))])
    encoder_load = Mock(return_value=encoder)
    model_load = Mock(return_value=model)
    class AlignerConfig:
        tokenizer_name = 'default'
    config = SimpleNamespace(tokenizer_name=None)
    monkeypatch.setitem(sys.modules, 'tada', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'tada.modules', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'tada.modules.aligner', SimpleNamespace(AlignerConfig=AlignerConfig))
    monkeypatch.setitem(sys.modules, 'tada.modules.encoder', SimpleNamespace(Encoder=SimpleNamespace(from_pretrained=encoder_load)))
    monkeypatch.setitem(sys.modules, 'tada.modules.tada', SimpleNamespace(
        TadaConfig=SimpleNamespace(from_pretrained=Mock(return_value=config)),
        TadaForCausalLM=SimpleNamespace(from_pretrained=model_load),
    ))
    return backend, encoder, model, encoder_load, model_load, download, clear, config


@pytest.mark.asyncio
async def test_load_reuses_model_and_sets_ungated_tokenizer(loader):
    backend, encoder, model, enc_load, model_load, download, clear, config = loader
    await backend.load_model('1B')
    assert backend.is_loaded() and backend.encoder is encoder and backend.model is model
    assert config.tokenizer_name == '/cached/Llama-3.2-1B'
    assert [c.kwargs['repo_id'] for c in download.call_args_list] == [hume.TADA_CODEC_REPO, hume.TADA_1B_REPO, hume.TADA_TOKENIZER_REPO]
    assert model_load.call_args.kwargs['torch_dtype'] == torch.float32
    await backend.load_model('1B')
    assert enc_load.call_count == model_load.call_count == 1
    backend.unload_model()
    assert backend.model is None and backend.encoder is None
    clear.assert_called_once_with('cpu')


@pytest.mark.parametrize('failure', ['encoder_eval', 'model_load', 'model_eval'])
def test_failed_initialization_releases_partial_encoder_and_model(loader, failure):
    backend, encoder, model, _, model_load, _, clear, _ = loader
    if failure == 'encoder_eval':
        encoder.eval.side_effect = RuntimeError('initialization failed')
    elif failure == 'model_load':
        model_load.side_effect = RuntimeError('initialization failed')
    else:
        model.eval.side_effect = RuntimeError('initialization failed')
    with pytest.raises(RuntimeError, match='initialization failed'):
        backend._load_model_sync('1B')
    assert backend.encoder is None and backend.model is None
    assert backend._device is None and not backend.is_loaded()
    clear.assert_called_once_with('cpu')


@pytest.mark.asyncio
async def test_cached_prompt_and_synthesis_restore_tensor_types(loader, monkeypatch):
    backend, _, model, *_ = loader
    backend.model = model
    backend.encoder = Mock()
    backend._device = 'cpu'
    prompt = {'embedding': torch.tensor([0.25], dtype=torch.float64), 'tokens': torch.tensor([2])}
    monkeypatch.setattr(hume, 'get_cache_key', lambda *_args: 'key')
    monkeypatch.setattr(hume, 'get_cached_voice_prompt', lambda _key: prompt)
    result, cached = await backend.create_voice_prompt('unused.wav', 'words')
    assert cached and result is prompt
    class EncoderOutput:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    monkeypatch.setitem(sys.modules, 'tada.modules.encoder', SimpleNamespace(EncoderOutput=EncoderOutput))
    model.generate.return_value = SimpleNamespace(audio=[torch.tensor([[0.1, -0.2]])])
    seeds = []
    monkeypatch.setattr(hume, 'manual_seed', lambda seed, device: seeds.append((seed, device)))
    audio, rate = await backend.generate('Hello', prompt, seed=4)
    assert rate == 24000 and np.allclose(audio, [0.1, -0.2]) and audio.dtype == np.float32
    restored = model.generate.call_args.kwargs['prompt']
    assert restored.embedding.dtype == torch.float32 and restored.tokens.dtype == torch.int64
    assert model.generate.call_args.kwargs['text'] == 'Hello' and seeds == [(4, 'cpu')]


@pytest.mark.asyncio
async def test_prompt_encoding_stereo_and_cache(loader, tmp_path, monkeypatch):
    backend, encoder, model, *_ = loader
    backend.model, backend.encoder, backend._device = model, encoder, 'cpu'
    @dataclass
    class Encoded:
        embedding: torch.Tensor
        words: list
    encoder.return_value = Encoded(torch.tensor([1.0]), ['reference'])
    audio_path = tmp_path / 'reference.wav'
    sf.write(audio_path, np.zeros((100, 2), dtype=np.float32), 16000)
    monkeypatch.setattr(hume, 'get_cache_key', lambda *_args: 'key')
    monkeypatch.setattr(hume, 'get_cached_voice_prompt', lambda _key: None)
    save = Mock()
    monkeypatch.setattr(hume, 'cache_voice_prompt', save)
    prompt, cached = await backend.create_voice_prompt(str(audio_path), 'reference')
    assert not cached and prompt['words'] == ['reference']
    assert encoder.call_args.args[0].shape == (2, 100)
    assert encoder.call_args.kwargs == {'text': ['reference'], 'sample_rate': 16000}
    save.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_load_keeps_ownership_until_worker_finishes(monkeypatch):
    backend = hume.HumeTadaBackend()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load(size):
        calls.append(size)
        entered.set()
        assert release.wait(timeout=5)
        backend.model = object()
    monkeypatch.setattr(backend, '_load_model_sync', load)
    first = asyncio.create_task(backend.load_model('1B'))
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    second = asyncio.create_task(backend.load_model('1B'))
    try:
        await asyncio.sleep(0.1)
        assert calls == ['1B'] and not first.done()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None and calls == ['1B']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['encode', 'generate'])
async def test_cancelled_operation_waits_for_worker(loader, tmp_path, monkeypatch, operation):
    backend, encoder, model, *_ = loader
    backend.model, backend.encoder, backend._device = model, encoder, 'cpu'
    entered, release = threading.Event(), threading.Event()
    @dataclass
    class Encoded:
        embedding: torch.Tensor
    def work(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        if operation == 'encode':
            return Encoded(torch.tensor([1.0]))
        return SimpleNamespace(audio=[torch.tensor([0.1])])
    if operation == 'encode':
        audio_path = tmp_path / 'reference.wav'
        sf.write(audio_path, np.zeros(100, dtype=np.float32), 16000)
        encoder.side_effect = work
        task = asyncio.create_task(backend.create_voice_prompt(str(audio_path), 'words', use_cache=False))
    else:
        monkeypatch.setitem(sys.modules, 'tada.modules.encoder', SimpleNamespace(EncoderOutput=lambda **kw: kw))
        model.generate.side_effect = work
        task = asyncio.create_task(backend.generate('hello', {}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        release.set()
        results = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_switching_model_size_unloads_previous_components(loader):
    backend, _, _, enc_load, model_load, _, clear, config = loader
    await backend.load_model('1B')
    await backend.load_model('3B')
    assert enc_load.call_count == model_load.call_count == 2
    assert model_load.call_args.args == (hume.TADA_3B_ML_REPO,)
    assert backend.model_size == '3B' and config.tokenizer_name == '/cached/Llama-3.2-1B'
    clear.assert_called_once_with('cpu')


@pytest.mark.asyncio
async def test_model_is_not_ready_before_initialization_completes(loader):
    backend, _, model, _, _, _, _, _ = loader
    entered, release = threading.Event(), threading.Event()
    def evaluate():
        entered.set()
        assert release.wait(timeout=5)
    model.eval.side_effect = evaluate
    first = asyncio.create_task(backend.load_model('1B'))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(backend.load_model('1B'))
    try:
        await asyncio.sleep(0.1)
        assert not backend.is_loaded()
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert backend.is_loaded()
