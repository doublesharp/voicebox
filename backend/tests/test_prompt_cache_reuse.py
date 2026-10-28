"""Voice references reuse safe prompts without crossing backend/model boundaries."""

import asyncio
import sys
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
import torch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import backends, config
from backend.backends import mlx_backend, pytorch_backend
from backend.database import Base, ProfileSample, VoiceProfile
from backend.services import profiles
from backend.utils import cache


@dataclass
class PromptItem:
    ref_code: torch.Tensor | None
    ref_spk_embedding: torch.Tensor
    x_vector_only_mode: bool
    icl_mode: bool
    ref_text: str | None = None


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_get_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(profiles, "_get_cache_dir", lambda: tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    cache.clear_voice_prompt_memory_cache()
    monkeypatch.setitem(
        sys.modules,
        "qwen_tts.inference.qwen3_tts_model",
        SimpleNamespace(VoiceClonePromptItem=PromptItem),
    )
    yield
    cache.clear_voice_prompt_memory_cache()


def fake_pytorch(monkeypatch, calls, size="1.7B"):
    monkeypatch.setattr(pytorch_backend, "get_torch_device", lambda **kwargs: "cpu")
    backend = pytorch_backend.PyTorchTTSBackend(size)
    backend._current_model_size = size
    backend.load_model_async = AsyncMock()

    def encode(**kwargs):
        calls.append((size, kwargs["ref_text"]))
        return [PromptItem(torch.tensor([[1, 2]]), torch.tensor([0.2, 0.3]), False, True, kwargs["ref_text"])]

    backend.model = SimpleNamespace(create_voice_clone_prompt=encode)
    return backend


@pytest.mark.asyncio
async def test_qwen_reuses_prompt_from_memory_and_weights_only_disk(tmp_path, monkeypatch):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference-content")
    calls = []
    backend = fake_pytorch(monkeypatch, calls)

    first, first_hit = await backend.create_voice_prompt(str(reference), "Hello.")
    second, second_hit = await backend.create_voice_prompt(str(reference), "Hello.")
    cache.clear_voice_prompt_memory_cache()
    third, third_hit = await backend.create_voice_prompt(str(reference), "Hello.")

    assert calls == [("1.7B", "Hello.")]
    assert (first_hit, second_hit, third_hit) == (False, True, True)
    for result in (first, second, third):
        assert isinstance(result[0], PromptItem)
        assert result[0].ref_text == "Hello."
        assert torch.equal(result[0].ref_code, torch.tensor([[1, 2]]))
    disk_files = list((tmp_path / "cache").glob("*.prompt"))
    assert len(disk_files) == 1
    payload = torch.load(disk_files[0], weights_only=True)
    assert isinstance(payload, dict)


@pytest.mark.asyncio
async def test_qwen_cache_isolated_by_model_backend_content_and_text(tmp_path, monkeypatch):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"first-content")
    calls = []
    large = fake_pytorch(monkeypatch, calls, "1.7B")
    small = fake_pytorch(monkeypatch, calls, "0.6B")
    await large.create_voice_prompt(str(reference), "Hello.")
    _, hit = await small.create_voice_prompt(str(reference), "Hello.")
    assert hit is False

    mlx = mlx_backend.MLXTTSBackend("1.7B")
    mlx.load_model_async = AsyncMock()
    mlx_prompt, mlx_hit = await mlx.create_voice_prompt(str(reference), "Hello.")
    assert mlx_hit is False
    assert mlx_prompt["ref_audio"] == str(reference)

    await large.create_voice_prompt(str(reference), "Different text.")
    reference.write_bytes(b"changed-content")
    _, content_hit = await large.create_voice_prompt(str(reference), "Hello.")
    assert content_hit is False
    assert len(calls) == 4
    assert len(list((tmp_path / "cache").glob("*.prompt"))) == 5


@pytest.mark.asyncio
async def test_multisample_reference_preparation_reuses_and_invalidates(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    a.write_bytes(b"sample-a")
    b.write_bytes(b"sample-b")
    db.add(VoiceProfile(id="profile", name="Test", language="en"))
    db.add_all([
        ProfileSample(id="a", profile_id="profile", audio_path=str(a), reference_text="One."),
        ProfileSample(id="b", profile_id="profile", audio_path=str(b), reference_text="Two."),
    ])
    db.commit()
    combined = AsyncMock(return_value=(np.ones(100, dtype=np.float32) * 0.1, "One. Two."))
    prompted = AsyncMock(return_value=({"prompt": "ready"}, False))
    backend = SimpleNamespace(combine_voice_prompts=combined, create_voice_prompt=prompted, model_size="1.7B")
    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda engine: backend)

    try:
        await asyncio.gather(
            profiles.create_voice_prompt_for_profile("profile", db),
            profiles.create_voice_prompt_for_profile("profile", db),
        )
        first_path = prompted.call_args.args[0]
        await profiles.create_voice_prompt_for_profile("profile", db)
        assert combined.await_count == 1
        assert prompted.call_args.args[0] == first_path

        sample = db.get(ProfileSample, "b")
        sample.reference_text = "Changed."
        db.commit()
        await profiles.create_voice_prompt_for_profile("profile", db)
        assert combined.await_count == 2
        assert prompted.call_args.args[0] != first_path

        b.write_bytes(b"changed-sample")
        await profiles.create_voice_prompt_for_profile("profile", db)
        assert combined.await_count == 3

        profiles.clear_profile_cache("profile")
        assert not list((tmp_path / "cache").glob("combined_profile_*.wav"))
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing", "unsupported", "midstream"])
async def test_mlx_clone_failure_never_generates_an_unconditioned_voice(tmp_path, problem):
    reference = tmp_path / "reference.wav"
    if problem != "missing":
        reference.write_bytes(b"reference")
    calls = []

    class CloneModel:
        def generate(self, text, ref_audio=None, ref_text=None, lang_code=None):
            calls.append(ref_audio)
            if problem == "midstream" and ref_audio:
                yield SimpleNamespace(audio=np.ones(10), sample_rate=24000)
                raise RuntimeError("codec failure")
            yield SimpleNamespace(audio=np.ones(20), sample_rate=24000)

    class UnsupportedModel:
        def generate(self, text, lang_code=None):
            calls.append(None)
            yield SimpleNamespace(audio=np.ones(20), sample_rate=24000)

    backend = mlx_backend.MLXTTSBackend()
    backend.model = UnsupportedModel() if problem == "unsupported" else CloneModel()
    backend._current_model_size = backend.model_size
    with pytest.raises(RuntimeError, match="[Vv]oice|[Cc]lon|[Rr]eference"):
        await backend.generate("Hello.", {"ref_audio": str(reference), "ref_text": "Reference."})
    assert None not in calls
    assert len(calls) <= 1


@pytest.mark.asyncio
async def test_mlx_kwargs_capable_clone_preserves_reference(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    seen = []

    class Model:
        def generate(self, text, **kwargs):
            seen.append(kwargs.get("ref_audio"))
            yield SimpleNamespace(audio=np.ones(20), sample_rate=24000)

    backend = mlx_backend.MLXTTSBackend()
    backend.model = Model()
    backend._current_model_size = backend.model_size
    await backend.generate("Hello.", {"ref_audio": str(reference), "ref_text": "Reference."})
    assert seen == [str(reference)]


@pytest.mark.asyncio
async def test_mlx_metadata_hit_uses_current_reference_path(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(b"same-reference")
    second.write_bytes(b"same-reference")
    backend = mlx_backend.MLXTTSBackend()
    backend.load_model_async = AsyncMock()
    await backend.create_voice_prompt(str(first), "Reference.")
    first.write_bytes(b"different-content")

    prompt, hit = await backend.create_voice_prompt(str(second), "Reference.")

    assert hit is True
    assert prompt["ref_audio"] == str(second)


@pytest.mark.asyncio
async def test_incomplete_qwen_cache_payload_is_recomputed(tmp_path, monkeypatch):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    calls = []
    backend = fake_pytorch(monkeypatch, calls)
    await backend.create_voice_prompt(str(reference), "Hello.")
    payload = next(iter(cache._memory_cache.values()))
    del payload["items"][0]["ref_code"]

    prompt, hit = await backend.create_voice_prompt(str(reference), "Hello.")

    assert hit is False
    assert len(calls) == 2
    assert prompt[0].ref_text == "Hello."


@pytest.mark.asyncio
async def test_reference_decode_does_not_block_the_event_loop(monkeypatch):
    from backend.backends import base

    loop_thread = threading.get_ident()
    decode_threads = []

    def load(path, **kwargs):
        decode_threads.append(threading.get_ident())
        return np.ones(100, dtype=np.float32) * 0.1, 24000

    monkeypatch.setattr(base, "load_audio", load)
    audio, text = await base.combine_voice_prompts(["one.wav", "two.wav"], ["One.", "Two."])

    assert len(audio) == 200
    assert text == "One. Two."
    assert len(decode_threads) == 2
    assert loop_thread not in decode_threads


@pytest.mark.asyncio
@pytest.mark.parametrize(("voice", "expected"), [("bf_emma", "b"), ("bm_george", "b"), ("af_heart", "a")])
async def test_kokoro_selects_english_dialect_from_voice(monkeypatch, voice, expected):
    from backend.backends.kokoro_backend import KokoroTTSBackend

    pipeline_languages = []

    class Pipeline:
        def __init__(self, lang_code, **kwargs):
            pipeline_languages.append(lang_code)

        def __call__(self, text, voice, speed):
            yield SimpleNamespace(audio=np.ones(2400, dtype=np.float32) * 0.1)

    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(KPipeline=Pipeline))
    backend = KokoroTTSBackend()
    backend._model = object()
    backend.load_model = AsyncMock()
    await backend.generate("Hello.", {"preset_voice_id": voice}, language="en")
    assert pipeline_languages == [expected]
