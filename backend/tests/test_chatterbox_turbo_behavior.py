"""Speech adapter behavior with in-process model doubles and no downloads."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import torch

from backend.backends import chatterbox_turbo_backend as turbo


@pytest.fixture
def turbo_backend(monkeypatch, tmp_path):
    backend = turbo.ChatterboxTurboTTSBackend()
    monkeypatch.setattr(backend, "_get_device", lambda: "cpu")
    monkeypatch.setattr(turbo, "is_model_cached", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(turbo, "model_load_progress", lambda *_args: nullcontext())
    monkeypatch.setattr(turbo, "patch_chatterbox_f32", lambda _model: None)
    model = Mock()
    model.sr = 24000
    from backend.tests.test_chatterbox_turbo_conditioning_cache import conditioning

    model.conds = conditioning(-1)

    def prepare(*_args, **_kwargs):
        model.conds = conditioning(1)

    model.prepare_conditionals.side_effect = prepare
    model.generated_speakers = []

    def generate(*_args, **_kwargs):
        model.generated_speakers.append(model.conds.t3.speaker_emb[0, 0].item())
        return torch.tensor([[0.1, -0.2]])

    model.generate.side_effect = generate
    monkeypatch.setattr(turbo.cache, "_memory_cache", {})
    monkeypatch.setattr(turbo.cache, "_get_cache_dir", lambda: tmp_path / "cache")
    load = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "chatterbox", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "chatterbox.tts_turbo",
        SimpleNamespace(ChatterboxTurboTTS=SimpleNamespace(from_local=load), Conditionals=type(model.conds)),
    )
    import huggingface_hub

    download = Mock(return_value="/cached-model")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    return backend, model, load, download


@pytest.mark.asyncio
async def test_turbo_preserves_tags_reference_and_seed(turbo_backend, tmp_path, monkeypatch):
    backend, model, load, download = turbo_backend
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    seeds = []
    monkeypatch.setattr(turbo, "manual_seed", lambda seed, device: seeds.append((seed, device)))
    prompt, cached = await backend.create_voice_prompt(str(reference), "words")
    assert not cached
    audio, rate = await backend.generate("Hi [laugh]", prompt, seed=42)
    assert rate == 24000
    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, [0.1, -0.2])
    assert seeds == [(42, "cpu")]
    assert model.generate.call_args.args == ("Hi [laugh]",)
    model.prepare_conditionals.assert_called_once_with(str(reference), exaggeration=0.0, norm_loudness=True)
    assert model.generate.call_args.kwargs["audio_prompt_path"] is None
    assert model.generated_speakers == [1]
    assert model.conds is None
    assert load.call_args.args == ("/cached-model", "cpu")
    assert download.call_count == 1
    await backend.load_model()
    assert load.call_count == 1


@pytest.mark.asyncio
async def test_turbo_missing_reference_fails_without_using_default_voice(turbo_backend, tmp_path):
    backend, model, _, _ = turbo_backend
    with pytest.raises(FileNotFoundError):
        await backend.generate("Hi", {"ref_audio": str(tmp_path / "missing.wav")})
    model.generate.assert_not_called()
    assert model.conds is None


def test_turbo_restores_torch_load_if_initialization_fails(turbo_backend):
    backend, _, load, _ = turbo_backend
    original = torch.load
    load.side_effect = RuntimeError("load failed")
    with pytest.raises(RuntimeError, match="load failed"):
        backend._load_model_sync()
    assert torch.load is original
    assert not backend.is_loaded()


def test_turbo_and_multilingual_loads_do_not_patch_torch_at_same_time(turbo_backend, monkeypatch):
    import concurrent.futures
    import threading

    from backend.backends import chatterbox_backend as multilingual

    first, _, first_load, _ = turbo_backend
    second = multilingual.ChatterboxTTSBackend()
    monkeypatch.setattr(second, "_get_device", lambda: "cpu")
    monkeypatch.setattr(multilingual, "is_model_cached", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(multilingual, "model_load_progress", lambda *_args: nullcontext())
    monkeypatch.setattr(multilingual, "patch_chatterbox_f32", lambda _model: None)
    first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()

    def first_loader(*_args):
        first_entered.set()
        assert release.wait(timeout=5)
        return Mock()

    def second_loader(**_kwargs):
        second_entered.set()
        return SimpleNamespace(t3=SimpleNamespace(tfmr=SimpleNamespace()))

    first_load.side_effect = first_loader
    monkeypatch.setitem(
        sys.modules,
        "chatterbox.mtl_tts",
        SimpleNamespace(ChatterboxMultilingualTTS=SimpleNamespace(from_pretrained=second_loader)),
    )
    original = torch.load
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first._load_model_sync)
            assert first_entered.wait(timeout=5)
            b = pool.submit(second._load_model_sync)
            try:
                assert not second_entered.wait(timeout=0.3)
            finally:
                release.set()
                a.result(timeout=5)
                b.result(timeout=5)
        assert second_entered.is_set()
        assert torch.load is original
    finally:
        torch.load = original
