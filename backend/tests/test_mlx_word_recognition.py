"""Structured Whisper recognition retains worker ownership and installed API compatibility."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend.backends.mlx_backend import MLXSTTBackend


@pytest.fixture
def audio_file(tmp_path):
    import numpy as np
    import soundfile as sf

    path = tmp_path / "fixture.wav"
    sf.write(path, np.zeros(16000, dtype=np.float32), 16000)
    return path


@pytest.mark.asyncio
async def test_installed_whisper_head_alias_is_scoped_to_word_recognition(monkeypatch, audio_file):
    from backend.utils import whisper_timing

    monkeypatch.setattr(whisper_timing, "version", lambda name: "0.4.1")

    class Model:
        __module__ = "mlx_audio.stt.models.whisper.whisper"
        _alignment_heads = object()

        def generate(self, audio, **kwargs):
            assert self.alignment_heads is self._alignment_heads
            assert kwargs == {"word_timestamps": True, "language": "en"}
            return SimpleNamespace(text="Ada", segments=[{"words": [{"word": "Ada", "start": 0.0, "end": 0.5}]}])

    backend = MLXSTTBackend("base")
    backend.model = Model()
    result = await backend.transcribe_detailed(str(audio_file), "en")
    assert result["text"] == "Ada"
    assert not hasattr(backend.model, "alignment_heads")


@pytest.mark.asyncio
async def test_detailed_load_is_cached_only_pinned_and_on_the_same_worker(monkeypatch, tmp_path, audio_file):
    import sys

    import huggingface_hub

    from backend.backends import mlx_backend

    calls = []

    class Model:
        def generate(self, audio, **options):
            calls.append(("generate", threading.get_ident(), options))
            return {"text": "Ada", "segments": []}

    def snapshot_download(**kwargs):
        assert kwargs["local_files_only"] is True
        calls.append(("snapshot", threading.get_ident(), kwargs))
        return str(tmp_path / ("a" * 40))

    def load(path):
        calls.append(("load", threading.get_ident(), path))
        return Model()

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", SimpleNamespace(load=load))
    monkeypatch.setattr(mlx_backend, "empty_mlx_cache", lambda: None)
    backend = MLXSTTBackend("base")
    assert await backend.transcribe_detailed(str(audio_file)) == {"text": "Ada", "segments": []}
    assert len({row[1] for row in calls}) == 1
    assert calls[0][2]["repo_id"] == "openai/whisper-base"
    assert backend._recognition_revision == "a" * 40
    backend.unload_model()
    assert backend._recognition_revision is None


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_physical_recognition_lock_until_worker_drains(audio_file):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Model:
        def generate(self, *args, **kwargs):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return {"text": "Ada", "segments": []}

    backend = MLXSTTBackend("base")
    backend.model = Model()
    first = asyncio.create_task(backend.transcribe_detailed(str(audio_file)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(backend.transcribe_detailed(str(audio_file)))
        await asyncio.sleep(0.02)
        assert len(calls) == 1
        assert not first.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second)["text"] == "Ada"
    assert len(set(calls)) == 1


@pytest.mark.asyncio
async def test_unload_during_detailed_load_prevents_late_model_publication(monkeypatch, tmp_path, audio_file):
    import sys

    import huggingface_hub

    from backend.backends import mlx_backend

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path / ("a" * 40)))

    def load(path):
        entered.set()
        assert release.wait(5)
        return object()

    monkeypatch.setitem(sys.modules, "mlx_audio.stt", SimpleNamespace(load=load))
    monkeypatch.setattr(mlx_backend, "empty_mlx_cache", lambda: None)
    backend = MLXSTTBackend("base")
    task = asyncio.create_task(backend.transcribe_detailed(str(audio_file)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        backend.unload_model()
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="unloaded"):
        await task
    assert backend.model is None


def test_whisper_shim_never_changes_unsupported_or_unknown_runtime(monkeypatch):
    from backend.utils import whisper_timing

    class Model:
        __module__ = "mlx_audio.stt.models.whisper.whisper"
        _alignment_heads = object()

    model = Model()

    def missing(name):
        raise whisper_timing.PackageNotFoundError(name)

    monkeypatch.setattr(whisper_timing, "version", missing)
    with whisper_timing.recognized_word_timing(model):
        assert not hasattr(model, "alignment_heads")
    monkeypatch.setattr(whisper_timing, "version", lambda name: "0.4.1")

    def failing_generation():
        with whisper_timing.recognized_word_timing(model):
            assert model.alignment_heads is model._alignment_heads
            raise RuntimeError("failed")

    with pytest.raises(RuntimeError, match="failed"):
        failing_generation()
    assert not hasattr(model, "alignment_heads")


@pytest.mark.asyncio
async def test_extensionless_studio_audio_is_decoded_as_mono_16khz_on_the_model_worker(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    from backend.utils import audio

    path = tmp_path / "opaque-studio-asset"
    sf.write(path, np.full((4800, 2), 0.1, dtype=np.float32), 48000, format="WAV")
    threads = []
    original = audio.load_audio

    def decode(*args, **kwargs):
        threads.append(threading.get_ident())
        assert kwargs == {"sample_rate": 16000, "mono": True}
        return original(*args, **kwargs)

    monkeypatch.setattr(audio, "load_audio", decode)

    class Model:
        def generate(self, samples, **kwargs):
            threads.append(threading.get_ident())
            assert isinstance(samples, np.ndarray)
            assert samples.dtype == np.float32
            assert samples.shape == (1600,)
            assert np.isfinite(samples).all()
            assert kwargs == {"word_timestamps": True, "language": "en"}
            return {"text": "Ada", "segments": []}

    backend = MLXSTTBackend("base")
    backend.model = Model()
    assert await backend.transcribe_detailed(str(path), "en") == {"text": "Ada", "segments": []}
    assert len(threads) == 2
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()
