"""Native audio frames remain incremental without changing utterance joins."""

import asyncio

import numpy as np
import pytest

from backend.utils.chunked_tts import concatenate_audio_chunks, split_text_into_chunks
from backend.utils.streaming_audio import iter_speech_audio


@pytest.mark.asyncio
async def test_preview_uses_native_frames_before_the_utterance_finishes():
    release = asyncio.Event()
    closed = []

    class Backend:
        async def generate(self, *args, **kwargs):
            return np.full(24, 0.8, dtype=np.float32), 1000

        async def generate_stream(self, *args, **kwargs):
            try:
                yield np.full(8, 0.2, dtype=np.float32), 1000
                await release.wait()
                yield np.full(8, 0.3, dtype=np.float32), 1000
            finally:
                closed.append(True)

    stream = iter_speech_audio(Backend(), "One unfinished utterance.", {}, crossfade_ms=0)
    try:
        audio, rate = await asyncio.wait_for(anext(stream), 1)
        assert rate == 1000
        np.testing.assert_array_equal(audio, np.full(8, 0.2, dtype=np.float32))
        assert not release.is_set()
    finally:
        await stream.aclose()
    assert closed == [True]


@pytest.mark.asyncio
async def test_native_frames_are_consecutive_and_only_utterance_joins_crossfade():
    text = "A short sentence. " * 20
    utterances = split_text_into_chunks(text, 100)
    waves = [np.linspace(0.01 * i, 0.2 + 0.01 * i, 27, dtype=np.float32) for i in range(len(utterances))]

    class Backend:
        index = 0

        async def generate(self, *args, **kwargs):
            raise AssertionError("Native preview buffered a whole utterance")

        async def generate_stream(self, *args, **kwargs):
            wave = waves[self.index]
            self.index += 1
            # The first frames are shorter than the join, exercising head buffering.
            for frame in (wave[:2], wave[2:4], wave[4:11], wave[11:]):
                yield frame, 1000

    actual = [audio async for audio, _ in iter_speech_audio(Backend(), text, {}, max_chunk_chars=100, crossfade_ms=5)]
    expected = concatenate_audio_chunks(waves, 1000, crossfade_ms=5)
    np.testing.assert_allclose(np.concatenate(actual), expected, atol=1e-7)


@pytest.mark.asyncio
async def test_native_preview_rejects_rate_change_inside_one_utterance():
    class Backend:
        async def generate(self, *args, **kwargs):
            return np.ones(10, dtype=np.float32) * 0.1, 1000

        async def generate_stream(self, *args, **kwargs):
            yield np.ones(10, dtype=np.float32) * 0.1, 1000
            yield np.ones(10, dtype=np.float32) * 0.1, 2000

    with pytest.raises(ValueError, match="sample rate"):
        async for _ in iter_speech_audio(Backend(), "A single utterance.", {}, crossfade_ms=0):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["trim", "runaway"])
async def test_whole_utterance_validation_retains_buffered_fallback(guard):
    calls = []

    class Backend:
        async def generate(self, *args, **kwargs):
            calls.append("buffered")
            return np.ones(10, dtype=np.float32) * 0.1, 1000

        async def generate_stream(self, *args, **kwargs):
            calls.append("native")
            yield np.ones(10, dtype=np.float32), 1000

    kwargs = {"trim_fn": lambda audio, rate: audio} if guard == "trim" else {"runaway_detector": lambda *args: False}
    chunks = [
        audio async for audio, _ in iter_speech_audio(Backend(), "Validated utterance.", {}, crossfade_ms=0, **kwargs)
    ]
    assert calls == ["buffered"]
    assert len(chunks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["base", "custom", "design"])
async def test_mlx_native_stream_closes_worker_before_releasing_adapter(kind):
    import threading
    from types import SimpleNamespace

    from backend.backends.mlx_backend import MLXTTSBackend
    from backend.backends.qwen_mlx_styles_backend import QwenMLXCustomVoiceBackend, QwenMLXVoiceDesignBackend

    stopped = threading.Event()
    calls = []
    produced = []

    def results(**kwargs):
        calls.append((threading.get_ident(), kwargs))
        try:
            for index in range(100):
                produced.append(index)
                yield SimpleNamespace(audio=np.full(8, 0.1, dtype=np.float32), sample_rate=1000)
        finally:
            stopped.set()

    model = SimpleNamespace(
        generate=lambda text, **kwargs: results(text=text, **kwargs),
        generate_custom_voice=lambda **kwargs: results(**kwargs),
        generate_voice_design=lambda **kwargs: results(**kwargs),
    )
    backend = {"base": MLXTTSBackend, "custom": QwenMLXCustomVoiceBackend, "design": QwenMLXVoiceDesignBackend}[kind]()
    backend.model = model
    backend._current_model_size = backend.model_size
    stream = backend.generate_stream("Native speech.", {}, instruct="Happy.")
    first, rate = await asyncio.wait_for(anext(stream), 1)
    assert rate == 1000
    assert len(first) == 8
    await asyncio.sleep(0.03)
    assert len(produced) <= 4, "Synchronous inference ran ahead without bounded backpressure"
    await asyncio.wait_for(stream.aclose(), 1)
    assert stopped.is_set(), "Adapter released ownership before closing its physical iterator"
    assert not backend._op_lock.locked()
    assert calls[0][0] != threading.get_ident()
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["streaming_interval"] == 0.32
    if kind == "custom":
        assert calls[0][1]["speaker"] == "Ryan"
        assert calls[0][1]["instruct"] == "Happy."
    if kind == "design":
        assert calls[0][1]["instruct"].endswith("Happy.")


@pytest.mark.asyncio
async def test_native_worker_propagates_failure_after_buffered_frames_and_recovers():
    from contextlib import aclosing

    from backend.backends.mlx_backend import _run_on_mlx_thread
    from backend.utils.native_stream import iter_native_worker

    def failing():
        yield np.ones(3), 1000
        raise ValueError("Decoder failed")

    frames = []
    async with aclosing(iter_native_worker(failing, _run_on_mlx_thread)) as stream:
        with pytest.raises(ValueError, match="Decoder failed"):  # noqa: PT012 - consume and inspect buffered frames
            async for audio, _ in stream:
                frames.append(audio)
    assert len(frames) == 1
    assert await _run_on_mlx_thread(lambda: "available") == "available"


@pytest.mark.asyncio
async def test_native_worker_stall_is_bounded_and_releases_executor():
    import threading

    from backend.backends.mlx_backend import _run_on_mlx_thread
    from backend.utils.native_stream import iter_native_worker

    stopped = threading.Event()

    def frames():
        try:
            for _ in range(100):
                yield np.ones(3), 1000
        finally:
            stopped.set()

    stream = iter_native_worker(frames, _run_on_mlx_thread, stall_timeout=0.03)
    try:
        await anext(stream)
        assert await asyncio.wait_for(_run_on_mlx_thread(lambda: stopped.is_set()), 1)
        with pytest.raises(TimeoutError, match="stalled"):
            async for _ in stream:
                pass
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_cancelled_stream_retains_lock_until_physical_worker_finishes():
    import threading
    from types import SimpleNamespace

    from backend.backends.qwen_mlx_styles_backend import QwenMLXCustomVoiceBackend

    entered, release = threading.Event(), threading.Event()

    def generate(**kwargs):
        entered.set()
        release.wait(2)
        yield SimpleNamespace(audio=np.ones(8) * 0.1, sample_rate=1000)

    backend = QwenMLXCustomVoiceBackend()
    backend.model = SimpleNamespace(generate_custom_voice=generate)
    backend._current_model_size = backend.model_size
    stream = backend.generate_stream("Waiting.", {})
    task = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert backend._op_lock.locked()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()
    assert not backend._op_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        ([(np.ones((2, 2)), 1000)], "mono"),
        ([(np.array([float("nan")]), 1000)], "finite"),
        ([(np.ones(3), True)], "sample rate"),
        ([(np.ones(3), 1.5)], "sample rate"),
        ([(np.zeros(0), 1000)], "no audio"),
    ],
)
async def test_invalid_native_audio_is_rejected(chunks, message):
    class Backend:
        async def generate_stream(self, *args, **kwargs):
            for frame in chunks:
                yield frame

    with pytest.raises(ValueError, match=message):
        async for _ in iter_speech_audio(Backend(), "Input.", {}, crossfade_ms=0):
            pass


def test_audio_frames_materialize_and_reject_invalid_data():
    from backend.utils.native_stream import audio_frame

    original = np.ones(3, dtype=np.float32)
    frame, rate = audio_frame(original, 24000)
    original[:] = 0
    assert rate == 24000
    assert np.all(frame == 1)
    with pytest.raises(ValueError, match="sample rate"):
        audio_frame(original, 0)
    with pytest.raises(ValueError, match="finite mono"):
        audio_frame(np.array([np.inf]), 24000)


@pytest.mark.asyncio
async def test_invalid_native_timeout_is_rejected_before_worker_submission():
    from backend.utils.native_stream import iter_native_worker

    def submit(_):
        raise AssertionError("Invalid timeout started a worker")

    for timeout in (0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="timeout"):
            await anext(iter_native_worker(lambda: iter(()), submit, stall_timeout=timeout))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["base", "custom", "design"])
async def test_native_adapter_finishes_on_worker_and_clears_unloaded_model(kind, monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    from backend.backends import mlx_backend, qwen_mlx_styles_backend as styles

    seeds, clears, requests = [], [], []
    monkeypatch.setitem(sys.modules, "mlx.core", SimpleNamespace(random=SimpleNamespace(seed=seeds.append)))
    monkeypatch.setattr(mlx_backend, "empty_mlx_cache", lambda: clears.append(True))
    monkeypatch.setattr(styles, "empty_mlx_cache", lambda: clears.append(True))
    cls = {
        "base": mlx_backend.MLXTTSBackend,
        "custom": styles.QwenMLXCustomVoiceBackend,
        "design": styles.QwenMLXVoiceDesignBackend,
    }[kind]
    backend = cls()

    def generate(*args, **kwargs):
        requests.append(kwargs)
        yield SimpleNamespace(audio=np.ones(8, dtype=np.float32) * 0.2, sample_rate=1000)
        backend.model = None
        yield SimpleNamespace(audio=np.ones(8, dtype=np.float32) * 0.3, sample_rate=1000)

    model = SimpleNamespace(generate=generate, generate_custom_voice=generate, generate_voice_design=generate)
    backend.model = model
    backend._current_model_size = backend.model_size
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"local fixture")
    prompt = {"ref_audio": str(reference), "ref_text": "Reference."} if kind == "base" else {}
    chunks = [frame async for frame in backend.generate_stream("Target.", prompt, language="fr", seed=7)]
    assert len(chunks) == 2
    assert seeds == [7]
    assert clears == [True]
    if kind == "base":
        assert requests[0]["ref_audio"] == str(reference)
        assert requests[0]["ref_text"] == "Reference."
        assert requests[0]["lang_code"] == "french"
    else:
        assert requests[0]["language"] == "french"


@pytest.mark.asyncio
async def test_native_clone_missing_reference_never_calls_model(tmp_path):
    from types import SimpleNamespace

    from backend.backends.mlx_backend import MLXTTSBackend, VoiceCloningError

    backend = MLXTTSBackend()
    backend.model = SimpleNamespace(generate=lambda *args, **kwargs: pytest.fail("Missing reference reached the model"))
    backend._current_model_size = backend.model_size
    with pytest.raises(VoiceCloningError, match="missing"):
        await anext(backend.generate_stream("Target.", {"ref_audio": str(tmp_path / "missing.wav")}))
    assert not backend._op_lock.locked()


@pytest.mark.asyncio
async def test_native_clone_failure_does_not_render_a_replacement_voice(tmp_path):
    from types import SimpleNamespace

    from backend.backends.mlx_backend import MLXTTSBackend, VoiceCloningError

    backend = MLXTTSBackend()
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"local fixture")

    def generate(*args, **kwargs):
        raise RuntimeError("Reference encoder failed")
        yield  # Keep the exact generator interface.

    backend.model = SimpleNamespace(generate=generate)
    backend._current_model_size = backend.model_size
    with pytest.raises(VoiceCloningError, match="no replacement voice"):
        await anext(backend.generate_stream("Target.", {"ref_audio": str(reference)}))
    assert not backend._op_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["base", "custom", "design"])
async def test_native_adapters_reject_rate_change_before_invalid_frame(kind):
    from types import SimpleNamespace

    from backend.backends.mlx_backend import MLXTTSBackend
    from backend.backends.qwen_mlx_styles_backend import QwenMLXCustomVoiceBackend, QwenMLXVoiceDesignBackend

    def generate(*args, **kwargs):
        for rate in (1000, 2000):
            yield SimpleNamespace(audio=np.ones(4) * 0.1, sample_rate=rate)

    cls = {"base": MLXTTSBackend, "custom": QwenMLXCustomVoiceBackend, "design": QwenMLXVoiceDesignBackend}[kind]
    backend = cls()
    backend.model = SimpleNamespace(generate=generate, generate_custom_voice=generate, generate_voice_design=generate)
    backend._current_model_size = backend.model_size
    stream = backend.generate_stream("Speech.", {})
    assert (await anext(stream))[1] == 1000
    with pytest.raises(ValueError, match="sample rates"):
        await anext(stream)
    assert not backend._op_lock.locked()


@pytest.mark.asyncio
async def test_native_base_loads_requested_model_on_the_same_worker(monkeypatch):
    import threading
    from types import SimpleNamespace

    from backend.backends.mlx_backend import MLXTTSBackend

    calls = []
    backend = MLXTTSBackend("0.6B")

    def generate(*args, **kwargs):
        calls.append(("generate", threading.get_ident()))
        yield SimpleNamespace(audio=np.ones(4) * 0.1, sample_rate=24000)

    def load(size):
        calls.append((size, threading.get_ident()))
        backend.model = SimpleNamespace(generate=generate)
        backend._current_model_size = size

    monkeypatch.setattr(backend, "_reload_sync", load)
    frames = [frame async for frame in backend.generate_stream("Speech.", {})]
    assert len(frames) == 1
    assert calls[0][0] == "0.6B"
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != threading.get_ident()


@pytest.mark.asyncio
async def test_cancelled_native_worker_failure_does_not_override_cancellation():
    import threading
    from types import SimpleNamespace

    from backend.backends.qwen_mlx_styles_backend import QwenMLXCustomVoiceBackend

    entered, release = threading.Event(), threading.Event()

    def generate(**kwargs):
        entered.set()
        release.wait(2)
        raise RuntimeError("Worker failed while cancelling")
        yield

    backend = QwenMLXCustomVoiceBackend()
    backend.model = SimpleNamespace(generate_custom_voice=generate)
    backend._current_model_size = backend.model_size
    stream = backend.generate_stream("Speech.", {})
    task = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.02)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()
    assert not backend._op_lock.locked()
