"""Speech adapter behavior with in-process model doubles and no downloads."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import torch

from backend.backends import chatterbox_turbo_backend as turbo
from backend.backends import qwen_custom_voice_backend as custom


@pytest.fixture
def custom_backend(monkeypatch):
    backend = custom.QwenCustomVoiceBackend()
    monkeypatch.setattr(custom, 'is_model_cached', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(custom, 'model_load_progress', lambda *_args: nullcontext())
    model = Mock()
    model.generate_custom_voice.return_value = ([np.array([0.1, 0.2], dtype=np.float32)], 24000)
    load = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, 'qwen_tts', SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=load)))
    return backend, model, load


@pytest.mark.asyncio
@pytest.mark.parametrize('device', ['cpu', 'cuda'])
async def test_custom_voice_load_and_generation_options(custom_backend, device):
    backend, model, load = custom_backend
    backend.device = device
    prompt, cached = await backend.create_voice_prompt('unused.wav', 'unused')
    assert not cached and prompt['preset_voice_id'] == 'Ryan'
    audio, rate = await backend.generate('Hello', {'preset_voice_id': 'Vivian'}, language='zh', instruct='whisper')
    assert rate == 24000 and np.allclose(audio, [0.1, 0.2])
    assert model.generate_custom_voice.call_args.kwargs == {
        'text': 'Hello', 'language': 'Chinese', 'speaker': 'Vivian', 'instruct': 'whisper'
    }
    kwargs = load.call_args.kwargs
    if device == 'cpu':
        assert kwargs == {'torch_dtype': torch.float32, 'low_cpu_mem_usage': False}
    else:
        assert kwargs == {'device_map': device, 'torch_dtype': torch.bfloat16}
    await backend.load_model('1.7B')
    assert load.call_count == 1


@pytest.mark.asyncio
async def test_custom_voice_defaults_unknown_language_and_empty_instruction(custom_backend):
    backend, model, _ = custom_backend
    backend.device = 'cpu'
    await backend.generate('Hi', {}, language='unknown', instruct='')
    assert model.generate_custom_voice.call_args.kwargs == {'text': 'Hi', 'language': 'Auto', 'speaker': 'Ryan'}


def test_custom_voice_unload_releases_the_selected_device_cache(custom_backend, monkeypatch):
    backend, _, _ = custom_backend
    backend.device = 'xpu'
    backend.model = object()
    backend._current_model_size = '1.7B'
    clear = Mock()
    monkeypatch.setattr(custom, 'empty_device_cache', clear, raising=False)
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    backend.unload_model()
    assert backend.model is None and backend._current_model_size is None
    clear.assert_called_once_with('xpu')


