"""CPU cloning boundary tests; fake ONNX runtime does not qualify speech quality."""

import asyncio
import importlib.util
import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


def test_cpu_multilingual_engine_is_available():
    assert importlib.util.find_spec("backend.backends.moss_tts_backend"), "Missing CPU multilingual cloning backend"


def module():
    from backend.backends import moss_tts_backend

    return moss_tts_backend


class Runtime:
    def __init__(self):
        self.encoded = 0
        self.voices = []
        self.threads = []
        self.rng = None

    def encode_reference(self, path):
        self.threads.append(threading.get_ident())
        self.encoded += 1
        audio, _ = sf.read(path)
        return np.full((2, 16), 1 if audio.mean() > 0 else 2, dtype=np.int32)

    def encode_text(self, text):
        return list(text.encode())

    def build_voice_clone_request_rows(self, codes, text):
        self.voices.append(codes[0][0])
        codes[0][0] = 999
        return {}

    def generate_audio_frames(self, rows, on_frame=None):
        self.threads.append(threading.get_ident())
        return [[self.voices[-1]] * 16] * 3

    def decode_full_audio(self, frames):
        return [np.full(20, frames[0][0], dtype=np.float32)] * 2, 20


@pytest.fixture
def backend(monkeypatch):
    result = module().MossTTSBackend()
    runtime = Runtime()
    monkeypatch.setattr(result, "_model_sync", lambda: runtime)
    result.model = runtime
    return result


@pytest.fixture
def reference(tmp_path):
    path = tmp_path / "reference.wav"
    sf.write(path, np.ones(2400, dtype=np.float32) * 0.1, 24000)
    return path


@pytest.mark.asyncio
async def test_encoded_cache_content_identity_and_voice_switching(backend, reference, tmp_path):
    runtime = backend.model
    first, hit = await backend.create_voice_prompt(str(reference), "First")
    assert not hit
    second, hit = await backend.create_voice_prompt(str(reference), "First")
    assert hit
    assert runtime.encoded == 1
    other = tmp_path / "other.wav"
    sf.write(other, np.full(2400, -0.1, dtype=np.float32), 24000)
    third, hit = await backend.create_voice_prompt(str(other), "First")
    assert not hit
    for prompt, want in [(first, 1), (third, 2), (second, 1)]:
        audio, sr = await backend.generate("Test.", prompt, seed=1)
        np.testing.assert_array_equal(audio, np.full(20, want, dtype=np.float32))
        assert sr == 48000
    assert runtime.voices == [1, 2, 1]
    assert all(t != threading.get_ident() for t in runtime.threads)
    first["audio_codes"][:] = 42
    restored, hit = await backend.create_voice_prompt(str(reference), "First")
    assert hit
    assert restored["audio_codes"][0, 0] == 1
    sf.write(reference, np.full(2400, -0.2, dtype=np.float32), 24000)
    changed, hit = await backend.create_voice_prompt(str(reference), "First")
    assert not hit
    assert changed["audio_codes"][0, 0] == 2
    _, hit = await backend.create_voice_prompt(str(reference), "Changed transcript")
    assert not hit


@pytest.mark.asyncio
async def test_fail_closed_prompt_language_instruction_and_no_cache(backend, reference):
    for invalid in [{}, {"preset_voice_id": "Junhao"}, {"engine": "another"}]:
        with pytest.raises(ValueError, match="MOSS-TTS|model size"):
            await backend.generate("Hi.", invalid)
    prompt, _ = await backend.create_voice_prompt(str(reference), "", use_cache=False)
    _, hit = await backend.create_voice_prompt(str(reference), "", use_cache=False)
    assert not hit
    assert backend.model.encoded == 2
    for kwargs in [{"language": "xx"}, {"instruct": "Whisper"}]:
        with pytest.raises(ValueError, match="MOSS-TTS|model size"):
            await backend.generate("Hi.", prompt, **kwargs)
    bad = dict(prompt, audio_codes=np.ones((2, 15), dtype=np.int32))
    with pytest.raises(ValueError, match="MOSS-TTS|model size"):
        await backend.generate("Hi.", bad)
    bad = dict(prompt, model_revision="wrong")
    with pytest.raises(ValueError, match="MOSS-TTS|model size"):
        await backend.generate("Hi.", bad)
    with pytest.raises(ValueError, match="MOSS-TTS|model size"):
        await backend.load_model("unknown")
    with pytest.raises(ValueError, match="MOSS-TTS|model size"):
        await backend.create_voice_prompt("", "")


@pytest.mark.asyncio
async def test_cache_is_bounded_and_unload_clears(backend, reference):
    for i in range(backend.PROMPT_CACHE_LIMIT + 2):
        await backend.create_voice_prompt(str(reference), str(i))
    assert len(backend._prompt_cache) == backend.PROMPT_CACHE_LIMIT
    backend.unload_model()
    assert not backend.is_loaded()
    assert not backend._prompt_cache


@pytest.mark.asyncio
async def test_physical_cancellation_drains_before_next_operation(backend, reference):
    entered = threading.Event()
    release = threading.Event()
    original = backend.model.encode_reference

    def blocked(path):
        entered.set()
        release.wait(5)
        return original(path)

    backend.model.encode_reference = blocked
    first = asyncio.create_task(backend.create_voice_prompt(str(reference), "a"))
    assert await asyncio.to_thread(entered.wait, 2)
    first.cancel()
    second = asyncio.create_task(backend.create_voice_prompt(str(reference), "b"))
    await asyncio.sleep(0.03)
    assert not first.done()
    assert not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert backend.model.encoded == 2


def test_text_chunking_preserves_words_and_bounds():
    assert "".join(module()._text_chunks("one two three", lambda t: list(t.encode()), 7)) == "one two three"
    chunks = module()._text_chunks("A sentence. " + ("longword " * 80), lambda t: list(t.encode()), 75)
    assert "".join(chunks).replace(" ", "") == ("A sentence. " + ("longword " * 80)).replace(" ", "")
    assert all(len(c.encode()) <= 75 for c in chunks)


@pytest.mark.asyncio
async def test_unload_during_load_cannot_republish(monkeypatch):
    from contextlib import nullcontext

    mod = module()
    engine = mod.MossTTSBackend()
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(mod, "_snapshot", lambda *args, **kwargs: Path("."))
    monkeypatch.setattr(mod, "model_load_progress", lambda *args: nullcontext())

    def loading(*args):
        entered.set()
        release.wait(5)
        return Runtime()

    monkeypatch.setattr(mod, "_load_runtime", loading)
    operation = asyncio.create_task(engine.load_model())
    assert await asyncio.to_thread(entered.wait, 2)
    engine.unload_model()
    release.set()
    await operation
    assert not engine.is_loaded()


@pytest.mark.asyncio
async def test_unload_during_encode_cannot_repopulate_cache(backend, reference):
    original = backend.model.encode_reference

    def unloading(path):
        backend.unload_model()
        return original(path)

    backend.model.encode_reference = unloading
    await backend.create_voice_prompt(str(reference), "Reference")
    assert not backend.is_loaded()
    assert not backend._prompt_cache


@pytest.mark.asyncio
async def test_invalid_or_truncated_generation_is_not_success(backend, reference):
    prompt, _ = await backend.create_voice_prompt(str(reference), "Reference")
    original = backend.model.generate_audio_frames
    backend.model.generate_audio_frames = lambda *args, **kwargs: []
    with pytest.raises(RuntimeError, match="no speech"):
        await backend.generate("Test.", prompt)
    backend.model.generate_audio_frames = lambda *args, **kwargs: [[1] * 16] * 375
    with pytest.raises(ValueError, match="limit"):
        await backend.generate("Test.", prompt)
    backend.model.generate_audio_frames = original
    backend.model.decode_full_audio = lambda frames: ([np.array([np.nan], dtype=np.float32)], 1)
    with pytest.raises(RuntimeError, match="invalid audio"):
        await backend.generate("Test.", prompt)


def test_reference_preprocessing_is_bounded_finite_and_handles_stereo(tmp_path):
    mod = module()
    for channels in [1, 2]:
        path = tmp_path / f"{channels}.wav"
        sf.write(path, np.ones((24000 * 11, channels), dtype=np.float32) * 0.01, 24000)
        audio = mod._reference_audio(path)
        assert audio.shape == (1, 2, 480000)
        assert audio.dtype == np.float32
    path = tmp_path / "invalid.wav"
    sf.write(path, np.zeros((1, 3), dtype=np.float32), 24000)
    with pytest.raises(ValueError, match="MOSS-TTS|model size"):
        mod._reference_audio(path)
    sf.write(path, np.array([np.nan], dtype=np.float32), 48000, subtype="FLOAT")
    with pytest.raises(ValueError, match="nonfinite"):
        mod._reference_audio(path)
    sf.write(path, np.empty((0,), dtype=np.float32), 48000)
    with pytest.raises(ValueError, match="MOSS-TTS|model size"):
        mod._reference_audio(path)


def test_readiness_requires_both_exact_snapshots_and_all_files(monkeypatch, tmp_path):
    mod = module()
    engine = mod.MossTTSBackend()
    calls = []

    def cached(repo, revision, files, **kwargs):
        calls.append((repo, revision, tuple(files), kwargs))
        if repo == mod.CODEC_REPO:
            raise FileNotFoundError("missing codec")
        return tmp_path

    monkeypatch.setattr(mod, "_snapshot", cached)
    assert not engine._is_model_cached()
    assert [c[1] for c in calls] == [mod.TTS_REVISION, mod.CODEC_REVISION]
    assert all(c[3] == {"local_only": True} for c in calls)
    monkeypatch.setattr(mod, "_snapshot", lambda *args, **kwargs: tmp_path)
    assert engine._is_model_cached()


def test_snapshot_helper_rejects_partial_revision(monkeypatch, tmp_path):
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *args, **kwargs: str(tmp_path))
    with pytest.raises(FileNotFoundError):
        module()._snapshot("model", "revision", ("missing.onnx",), local_only=True)


@pytest.mark.asyncio
async def test_generate_checks_cancellation_at_frame_boundary(backend, reference):
    prompt, _ = await backend.create_voice_prompt(str(reference), "Reference")
    entered = threading.Event()
    release = threading.Event()
    observed = threading.Event()

    def generate(rows, on_frame):
        entered.set()
        release.wait(5)
        try:
            on_frame([], 0, [1] * 16)
        except asyncio.CancelledError:
            observed.set()
            raise
        return [[1] * 16]

    backend.model.generate_audio_frames = generate
    operation = asyncio.create_task(backend.generate("Test.", prompt))
    assert await asyncio.to_thread(entered.wait, 2)
    operation.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert observed.is_set()
