"""Turbo conditioning reuse at the async backend boundary (no model weights)."""

import asyncio
import threading
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import pytest
import torch
from chatterbox.models.t3.modules.cond_enc import T3Cond
from chatterbox.tts_turbo import Conditionals

from backend.backends import chatterbox_turbo_backend as turbo
from backend.utils import cache


def conditioning(speaker):
    return Conditionals(
        T3Cond(
            speaker_emb=torch.full((1, 256), float(speaker)),
            cond_prompt_speech_tokens=torch.ones((1, 3), dtype=torch.long),
            emotion_adv=torch.zeros((1, 1, 1)),
        ),
        dict(
            prompt_token=torch.ones((1, 3), dtype=torch.long),
            prompt_token_len=torch.tensor([3]),
            prompt_feat=torch.ones((1, 6, 80)),
            prompt_feat_len=None,
            embedding=torch.full((1, 192), float(speaker)),
        ),
    )


class Model:
    sr = 24000
    device = "cpu"

    def __init__(self):
        self.conds = conditioning(-1)
        self.prepared = []
        self.generated = []
        self.threads = []
        self.fail_prepare = False
        self.fail_generate = False

    def prepare_conditionals(self, path, exaggeration=0.5, norm_loudness=True):
        self.threads.append(threading.get_ident())
        self.prepared.append((path, exaggeration, norm_loudness))
        self.conds = conditioning(int(Path(path).read_text()))
        if self.fail_prepare:
            raise RuntimeError("encoder failure")

    def generate(self, text, audio_prompt_path=None, **kwargs):
        self.threads.append(threading.get_ident())
        if audio_prompt_path:
            self.prepare_conditionals(audio_prompt_path, exaggeration=0.0)
        speaker = self.conds.t3.speaker_emb[0, 0].item()
        self.generated.append((speaker, audio_prompt_path, kwargs))
        # Real T3 inference mutates this field. Cached CPU storage must be separate.
        self.conds.t3.speaker_emb.fill_(999)
        self.conds.t3.cond_prompt_speech_emb = torch.ones(1, 3, 4)
        if self.fail_generate:
            raise RuntimeError("inference failure")
        return torch.full((1, 12), speaker)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_get_cache_dir", lambda: tmp_path / "cache")
    cache.clear_voice_prompt_memory_cache()
    yield
    cache.clear_voice_prompt_memory_cache()


@pytest.fixture
def reference(tmp_path):
    path = tmp_path / "reference.wav"
    path.write_text("1")
    return path


def backend():
    result = turbo.ChatterboxTurboTTSBackend()
    result.model = Model()
    result._device = "cpu"
    return result


@pytest.mark.asyncio
async def test_encoded_hit_reuses_across_chunks_takes_and_warm_disk(reference, tmp_path):
    first = backend()
    prompt, hit = await first.create_voice_prompt(str(reference), "Reference.")
    assert hit is False
    prompt2, hit = await first.create_voice_prompt(str(reference), "Reference.")
    assert hit is True  # Metadata alone cannot qualify as an encoded hit.
    for current in (prompt, prompt2):
        audio, sr = await first.generate("Chunk", current, seed=42)
        np.testing.assert_array_equal(audio, np.ones(12, dtype=np.float32))
        assert sr == 24000
    assert len(first.model.prepared) == 1
    assert first.model.prepared[0][1:] == (0.0, True)

    disk = next((tmp_path / "cache").glob("*.prompt"))
    payload = torch.load(disk, weights_only=True, map_location="cpu")
    assert payload["t3"]["speaker_emb"].device.type == "cpu"
    assert payload["t3"]["speaker_emb"][0, 0] == 1
    assert payload["t3"]["cond_prompt_speech_emb"] is None
    cache.clear_voice_prompt_memory_cache()
    second = backend()
    restored, hit = await second.create_voice_prompt(str(reference), "Reference.")
    assert hit is True
    await second.generate("Another take", restored)
    assert second.model.prepared == []
    assert second.model.generated[0][0:2] == (1, None)


@pytest.mark.asyncio
async def test_legacy_paths_alternating_voices_and_builtin_are_independent(reference, tmp_path, monkeypatch):
    second = tmp_path / "second.wav"
    second.write_text("2")
    instance = backend()
    seeds = []
    monkeypatch.setattr(turbo, "manual_seed", lambda seed, device: seeds.append((seed, device)))
    for path in (reference, second, reference, None, second, None):
        prompt = {"ref_audio": str(path)} if path else {}
        await instance.generate("Chunk", prompt, seed=17)
    assert [row[0] for row in instance.model.generated] == [1, 2, 1, -1, 2, -1]
    assert len(instance.model.prepared) == 2
    assert all(row[1] is None for row in instance.model.generated)
    assert seeds == [(17, "cpu")] * 6
    assert all(
        row[2] == dict(temperature=0.8, top_k=1000, top_p=0.95, repetition_penalty=1.2)
        for row in instance.model.generated
    )


@pytest.mark.asyncio
async def test_content_transcript_model_and_format_changes_miss(reference, monkeypatch):
    instance = backend()
    prompt, _ = await instance.create_voice_prompt(str(reference), "One.")
    _, hit = await instance.create_voice_prompt(str(reference), "Two.")
    assert hit is False
    reference.write_text("2")
    await instance.generate("Chunk", prompt)
    assert instance.model.generated[-1][0] == 2
    _, hit = await instance.create_voice_prompt(str(reference), "One.")
    assert hit is True
    monkeypatch.setattr(turbo, "_CONDITIONING_FORMAT", "next-format")
    _, hit = await instance.create_voice_prompt(str(reference), "One.")
    assert hit is False
    instance._conditioning_model_id = "another-model-revision"
    _, hit = await instance.create_voice_prompt(str(reference), "One.")
    assert hit is False
    assert len(instance.model.prepared) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "bytes",
        "metadata",
        "format",
        "key",
        "missing",
        "nan",
        "dtype",
        "shape",
        "unknown",
        "non-tensor",
        "empty",
        "clap",
        "derived",
        "t3-token",
        "embedding",
        "token-length",
        "feature-length",
    ],
)
async def test_invalid_payload_reencodes_reference(reference, tmp_path, damage):
    instance = backend()
    await instance.create_voice_prompt(str(reference), "Reference.")
    disk = next((tmp_path / "cache").glob("*.prompt"))
    payload = torch.load(disk, weights_only=True)
    if damage == "metadata":
        payload = {"ref_audio": str(reference), "ref_text": "Reference."}
    elif damage == "format":
        payload["format"] = "old"
    elif damage == "key":
        payload["key"] = "another-speaker"
    elif damage == "missing":
        del payload["gen"]["embedding"]
    elif damage == "nan":
        payload["t3"]["speaker_emb"][0, 0] = float("nan")
    elif damage == "dtype":
        payload["gen"]["prompt_token"] = torch.ones(1, 3)
    elif damage == "shape":
        payload["gen"]["prompt_feat"] = torch.ones(1, 6, 1)
    elif damage == "unknown":
        payload["t3"]["unexpected"] = torch.ones(1)
    elif damage == "non-tensor":
        payload["t3"]["speaker_emb"] = [1.0]
    elif damage == "empty":
        payload["gen"]["prompt_token"] = torch.empty(1, 0, dtype=torch.long)
    elif damage == "clap":
        payload["t3"]["clap_emb"] = torch.ones(1)
    elif damage == "derived":
        payload["t3"]["cond_prompt_speech_emb"] = torch.ones(1, 3, 4)
    elif damage == "t3-token":
        payload["t3"]["cond_prompt_speech_tokens"][0, 0] = 6561
    elif damage == "embedding":
        payload["gen"]["embedding"] = torch.ones(1)
    elif damage == "token-length":
        payload["gen"]["prompt_token_len"][0] = 2
    elif damage == "feature-length":
        payload["gen"]["prompt_feat_len"] = torch.tensor([10])
    if damage == "bytes":
        disk.write_bytes(b"interrupted writer")
    else:
        torch.save(payload, disk)
    cache.clear_voice_prompt_memory_cache()
    prompt, hit = await instance.create_voice_prompt(str(reference), "Reference.")
    assert hit is False
    await instance.generate("Chunk", prompt)
    assert len(instance.model.prepared) == 2
    assert instance.model.generated[-1][0] == 1
    assert torch.load(disk, weights_only=True)["t3"]["speaker_emb"][0, 0] == 1


@pytest.mark.asyncio
async def test_bypass_does_not_read_or_write_encoded_cache(reference, tmp_path):
    instance = backend()
    await instance.create_voice_prompt(str(reference), "Reference.")
    original = next((tmp_path / "cache").glob("*.prompt")).read_bytes()
    before = len(instance.model.prepared)
    prompt, hit = await instance.create_voice_prompt(str(reference), "Reference.", use_cache=False)
    assert hit is False
    for _ in range(2):
        await instance.generate("Chunk", prompt)
    assert len(instance.model.prepared) >= before + 2
    assert next((tmp_path / "cache").glob("*.prompt")).read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "encoder", "inference"])
async def test_failures_never_fall_back_to_previous_speaker(reference, failure):
    instance = backend()
    await instance.generate("First", {"ref_audio": str(reference)})
    if failure == "missing":
        reference.unlink()
    elif failure == "encoder":
        reference.write_text("2")
        instance.model.fail_prepare = True
    else:
        instance.model.fail_generate = True
    before = len(instance.model.generated)
    with pytest.raises((RuntimeError, FileNotFoundError)):
        await instance.generate("Fails", {"ref_audio": str(reference)})
    assert len(instance.model.generated) == before + (failure == "inference")
    instance.model.fail_prepare = instance.model.fail_generate = False
    await instance.generate("Default", {})
    assert instance.model.generated[-1][0] == -1


@pytest.mark.asyncio
async def test_unload_discards_defaults_and_reloaded_model_uses_cpu_disk(reference, monkeypatch):
    instance = backend()
    await instance.create_voice_prompt(str(reference), "Reference.")
    old = instance.model
    monkeypatch.setattr(turbo, "empty_device_cache", lambda device: None)
    instance.unload_model()
    assert not instance.is_loaded()
    instance.model = Model()
    instance.model.conds = conditioning(-2)
    instance._device = "cpu"
    _, hit = await instance.create_voice_prompt(str(reference), "Reference.")
    assert hit is True
    await instance.generate("Default", {})
    assert instance.model.generated[-1][0] == -2
    assert old.generated == []


@pytest.mark.asyncio
async def test_encoding_restoration_and_generation_run_in_worker_under_ownership(reference, monkeypatch):
    from backend.services import inference_scheduler

    instance = backend()
    main_thread = threading.get_ident()
    ownership = []
    original = Conditionals.to

    def moved(self, device):
        ownership.append((threading.get_ident(), inference_scheduler._scope.get() is not None))
        return original(self, device)

    monkeypatch.setattr(Conditionals, "to", moved)
    prompt, _ = await instance.create_voice_prompt(str(reference), "Reference.")
    await asyncio.gather(*(instance.generate("Chunk", prompt) for _ in range(3)))
    assert main_thread not in instance.model.threads
    assert len(instance.model.prepared) == 1
    assert len(instance.model.generated) == 3
    assert ownership
    assert all(thread != main_thread and owned for thread, owned in ownership)


@pytest.mark.asyncio
async def test_current_chunk_pipeline_reuses_encoded_reference(reference):
    from backend.utils.chunked_tts import generate_chunked

    instance = backend()
    prompt, _ = await instance.create_voice_prompt(str(reference), "Reference.")
    audio, sr = await generate_chunked(
        instance,
        "First sentence. Second sentence. Third sentence.",
        prompt,
        max_chunk_chars=20,
        crossfade_ms=0,
    )
    assert sr == 24000
    assert len(audio) >= 36
    assert len(instance.model.generated) >= 3
    assert len(instance.model.prepared) == 1


@pytest.mark.asyncio
async def test_same_content_at_new_path_reuses_encoded_voice(reference, tmp_path):
    instance = backend()
    await instance.create_voice_prompt(str(reference), "Reference.")
    moved = tmp_path / "moved.wav"
    reference.rename(moved)
    prompt, hit = await instance.create_voice_prompt(str(moved), "Reference.")
    assert hit is True
    assert prompt["ref_audio"] == str(moved)
    await instance.generate("Chunk", prompt)
    assert len(instance.model.prepared) == 1
    assert instance.model.generated[-1][0] == 1


@pytest.mark.asyncio
async def test_no_builtin_never_uses_last_cloned_voice(reference):
    instance = backend()
    instance.model.conds = None
    await instance.generate("Clone", {"ref_audio": str(reference)})
    with pytest.raises(RuntimeError, match="no default voice"):
        await instance.generate("Default", {})
    assert len(instance.model.generated) == 1
    assert instance.model.conds is None


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["changed-reference", "invalid-encoder-output"])
async def test_failed_encoding_never_publishes_a_cache_entry(reference, tmp_path, problem):
    instance = backend()
    original = instance.model.prepare_conditionals

    def prepare(*args, **kwargs):
        original(*args, **kwargs)
        if problem == "changed-reference":
            reference.write_text("2")
        else:
            instance.model.conds.gen["embedding"] = torch.tensor([float("nan")])

    instance.model.prepare_conditionals = prepare
    with pytest.raises(RuntimeError, match="changed|Invalid"):
        await instance.create_voice_prompt(str(reference), "Reference.")
    assert not list((tmp_path / "cache").glob("*.prompt"))
    assert instance.model.conds is None


@pytest.mark.asyncio
async def test_cancelled_worker_holds_ownership_until_inference_finishes(reference):
    instance = backend()
    started, release = threading.Event(), threading.Event()
    original = instance.model.generate

    def generate(text, **kwargs):
        if text == "Blocking":
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test worker did not drain")
        return original(text, **kwargs)

    instance.model.generate = generate
    running = asyncio.create_task(instance.generate("Blocking", {"ref_audio": str(reference)}))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        running.cancel()
        queued = asyncio.create_task(instance.generate("Default", {}))
        await asyncio.sleep(0)
        assert not queued.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
    audio, _ = await queued
    np.testing.assert_array_equal(audio, np.full(12, -1, dtype=np.float32))
    assert [row[0] for row in instance.model.generated] == [1, -1]


@pytest.mark.asyncio
async def test_loaded_snapshot_revision_is_part_of_cache_identity(reference, tmp_path, monkeypatch):
    import huggingface_hub
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    revision = ["revision-a"]
    loaded = []

    def from_local(path, device):
        model = Model()
        loaded.append(model)
        return model

    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: tmp_path / revision[0])
    monkeypatch.setattr(ChatterboxTurboTTS, "from_local", staticmethod(from_local))
    monkeypatch.setattr(turbo, "model_load_progress", lambda *args: nullcontext())
    monkeypatch.setattr(turbo, "patch_chatterbox_f32", lambda model: None)
    monkeypatch.setattr(turbo.ChatterboxTurboTTSBackend, "_get_device", lambda self: "cpu")
    monkeypatch.setattr(turbo.ChatterboxTurboTTSBackend, "_is_model_cached", lambda self: True)
    for current, expected_hit in (("revision-a", False), ("revision-a", True), ("revision-b", False)):
        revision[0] = current
        instance = turbo.ChatterboxTurboTTSBackend()
        _, hit = await instance.create_voice_prompt(str(reference), "Reference.")
        assert hit is expected_hit
    assert [len(model.prepared) for model in loaded] == [1, 0, 1]


@pytest.mark.asyncio
async def test_device_restoration_failure_clears_speaker_before_default(reference, monkeypatch):
    instance = backend()
    await instance.create_voice_prompt(str(reference), "Reference.")
    original = Conditionals.to

    def fail(self, device):
        raise RuntimeError("device restoration failed")

    monkeypatch.setattr(Conditionals, "to", fail)
    with pytest.raises(RuntimeError, match="device restoration"):
        await instance.generate("Fails", {"ref_audio": str(reference)})
    assert instance.model.conds is None
    assert instance.model.generated == []
    monkeypatch.setattr(Conditionals, "to", original)
    await instance.generate("Default", {})
    assert instance.model.generated[-1][0] == -1


@pytest.mark.asyncio
async def test_invalid_entry_is_removed_even_when_reencoding_fails(reference, tmp_path):
    instance = backend()
    await instance.create_voice_prompt(str(reference), "Reference.")
    payload = next(iter(cache._memory_cache.values()))
    del payload["gen"]["embedding"]
    instance.model.fail_prepare = True
    with pytest.raises(RuntimeError, match="encoder failure"):
        await instance.create_voice_prompt(str(reference), "Reference.")
    assert not list((tmp_path / "cache").glob("*.prompt"))
    assert cache._memory_cache == {}
    assert instance.model.conds is None
