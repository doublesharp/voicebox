"""Reference codec reuse through the backend (tiny fake model, real MLX arrays)."""

import asyncio
import gc
import threading
import weakref
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

mx = pytest.importorskip("mlx.core")

from backend.backends import mlx_backend  # noqa: E402
from backend.utils import mlx_reference_cache  # noqa: E402
from backend.utils.mlx_reference_cache import MLXReferenceCache  # noqa: E402


class Codec:
    def __init__(self):
        self.calls = 0
        self.threads = []
        self.encoder_model = SimpleNamespace(valid_num_quantizers=2)
        self.encoder_valid_num_quantizers = 2
        self.input_sample_rate = 24000
        self.encode_downsample_rate = 1920

    def encode(self, audio):
        self.calls += 1
        self.threads.append(threading.get_ident())
        return mx.full((1, self.encoder_model.valid_num_quantizers, 3), int(audio.sum().item()), dtype=mx.int32)


class Model:
    def __init__(self):
        self.speech_tokenizer = Codec()
        self.config = SimpleNamespace(tts_model_type="base")
        self.samples = [1.0, 2.0, 3.0]
        self.seen = []

    def generate(self, text, ref_audio=None, ref_text=None, **kwargs):
        codes = self.speech_tokenizer.encode(mx.array(self.samples)[None, None, :])
        self.seen.append((text, ref_text, np.array(codes)))
        yield SimpleNamespace(audio=mx.array([0.0, 1.0]), sample_rate=24000)


@pytest.mark.asyncio
async def test_different_text_reuses_actual_reference_encoding(monkeypatch, tmp_path):
    model = Model()
    backend = mlx_backend.MLXTTSBackend()
    monkeypatch.setattr("mlx_audio.tts.load", lambda path: model)
    monkeypatch.setattr(backend, "_is_model_cached", lambda size: True)
    # The production gate accepts only the audited upstream classes/version.
    monkeypatch.setattr(mlx_reference_cache, "_supported_codec", lambda loaded: loaded.speech_tokenizer)
    await backend.load_model_async()
    reference = tmp_path / "synthetic.wav"
    reference.touch()
    prompt = {"ref_audio": str(reference), "ref_text": "reference transcript"}
    await backend.generate("First chunk.", prompt, seed=42)
    await backend.generate("A different take.", {**prompt, "ref_text": "current transcript"}, seed=42)
    assert [item[:2] for item in model.seen] == [
        ("First chunk.", "reference transcript"),
        ("A different take.", "current transcript"),
    ]
    assert model.speech_tokenizer.calls == 1
    assert model.speech_tokenizer.threads == [await mlx_backend._run_on_mlx_thread(threading.get_ident)]


@pytest.fixture
def supported(monkeypatch):
    monkeypatch.setattr(mlx_reference_cache, "_supported_codec", lambda loaded: loaded.speech_tokenizer)


async def run_scenario(scenario):
    # Even tiny MLX fixtures must create/materialize/restore arrays on the worker.
    return await mlx_backend._run_on_mlx_thread(scenario)


@pytest.mark.asyncio
async def test_input_content_options_and_unsupported_forms(supported):
    def scenario():
        model = Model()
        codec = model.speech_tokenizer
        cache = MLXReferenceCache()
        cache.bind(model)
        audio = mx.array([1.0, 2.0])[None, None, :]
        for _ in range(2):
            with cache.encoding(model):
                codec.encode(audio)
        assert codec.calls == 1
        with cache.encoding(model):
            codec.encode(audio + 1)
        assert codec.calls == 2
        codec.encoder_model.valid_num_quantizers = 1
        with cache.encoding(model):
            assert codec.encode(audio).shape == (1, 1, 3)
        assert codec.calls == 3
        with cache.encoding(model):
            codec.encode(audio=audio)  # Supported upstream form, deliberately bypassed.
            codec.encode(mx.array([1, 2], dtype=mx.int32))
            codec.encode(mx.array([1.0, 2.0]))
        assert codec.calls == 6
        assert "encode" not in vars(codec)
        # No finite-content cache key for NaN; do not send it to the toy encoder.
        assert cache._key(mx.array([float("nan")])[None, None, :], codec) is None
        codec.input_sample_rate = "unsupported"
        assert cache._key(audio, codec) is None
        with cache.encoding(model):
            codec.encode(audio)
        assert codec.calls == 7

    await run_scenario(scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["entries", "bytes", "oversized", "input", "disabled"])
async def test_bounded_eviction_and_bypass(supported, limit):
    def scenario():
        model = Model()
        codec = model.speech_tokenizer
        cache = MLXReferenceCache(
            max_entries=0 if limit == "disabled" else 1 if limit == "entries" else 8,
            max_bytes=23 if limit == "oversized" else 24 if limit == "bytes" else 1024,
            max_input_bytes=4 if limit == "input" else 1024,
        )
        cache.bind(model)
        a = mx.array([1.0, 2.0])[None, None, :]
        b = a + 1
        for audio in (a, b, a):
            with cache.encoding(model):
                codec.encode(audio)
        assert codec.calls == 3
        assert cache._bytes <= cache.max_bytes
        assert len(cache._entries) <= cache.max_entries

    await run_scenario(scenario)


@pytest.mark.asyncio
async def test_cpu_ownership_dtype_shape_and_mutation_isolation(supported):
    def scenario():
        model = Model()
        codec = model.speech_tokenizer
        cache = MLXReferenceCache()
        cache.bind(model)
        audio = mx.array([1.0, 2.0])[None, None, :]
        with cache.encoding(model):
            first = codec.encode(audio)
        first[0, 0, 0] = 999
        with cache.encoding(model):
            restored = codec.encode(audio)
            assert restored.dtype == mx.int32
            assert restored.shape == (1, 2, 3)
            assert np.array_equal(np.array(restored), np.full((1, 2, 3), 3))
            restored[0, 0, 0] = 777
        with cache.encoding(model):
            assert codec.encode(audio)[0, 0, 0].item() == 3
        assert codec.calls == 1
        assert len(cache._entries) == 1
        entry = next(iter(cache._entries.values()))
        assert isinstance(entry.payload, bytes)
        assert all(not isinstance(value, mx.array | np.ndarray) for value in vars(entry).values())
        model_ref, codec_ref = weakref.ref(model), weakref.ref(codec)
        del model, codec
        gc.collect()
        assert model_ref() is None
        assert codec_ref() is None

    await run_scenario(scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["payload", "dtype", "shape", "object"])
async def test_malformed_entry_is_a_miss(supported, corruption):
    def scenario():
        model = Model()
        cache = MLXReferenceCache()
        cache.bind(model)
        audio = mx.array([1.0, 2.0])[None, None, :]
        with cache.encoding(model):
            model.speech_tokenizer.encode(audio)
        key, entry = next(iter(cache._entries.items()))
        cache._entries[key] = {
            "payload": replace(entry, payload=b"bad"),
            "dtype": replace(entry, dtype="garbage"),
            "shape": replace(entry, shape=(1, 3, 2)),
            "object": object(),
        }[corruption]
        with cache.encoding(model):
            assert model.speech_tokenizer.encode(audio).sum().item() == 18
        assert model.speech_tokenizer.calls == 2
        with cache.encoding(model):
            model.speech_tokenizer.encode(audio)
        assert model.speech_tokenizer.calls == 2

    await run_scenario(scenario)


@pytest.mark.asyncio
async def test_model_codec_identity_clear_and_inflight_unload(supported):
    def scenario():
        model = Model()
        cache = MLXReferenceCache()
        cache.bind(model)
        audio = mx.array([1.0, 2.0])[None, None, :]
        with cache.encoding(model):
            model.speech_tokenizer.encode(audio)
            # Cross-thread invalidation while the context still has staged codes.
            thread = threading.Thread(target=cache.clear)
            thread.start()
            thread.join()
        assert not cache._entries
        cache.bind(model)
        with cache.encoding(model):
            model.speech_tokenizer.encode(audio)
        assert model.speech_tokenizer.calls == 2
        other = Model()
        with cache.encoding(other):
            other.speech_tokenizer.encode(audio)
        assert other.speech_tokenizer.calls == 1
        cache.bind(other)
        assert not cache._entries
        with cache.encoding(other):
            other.speech_tokenizer.encode(audio)
        assert other.speech_tokenizer.calls == 2
        other.speech_tokenizer = Codec()  # Same model, different actual codec lifetime.
        with cache.encoding(other):
            other.speech_tokenizer.encode(audio)
        assert other.speech_tokenizer.calls == 1
        assert not any("encode" in vars(item.speech_tokenizer) for item in (model, other))

    await run_scenario(scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError, GeneratorExit])
async def test_failure_does_not_publish_and_restores_encoder(supported, failure):
    def scenario():
        model = Model()
        cache = MLXReferenceCache()
        cache.bind(model)

        def failing_generation():
            with cache.encoding(model):
                model.speech_tokenizer.encode(mx.ones((1, 1, 4)))
                raise failure()

        with pytest.raises(failure):
            failing_generation()
        assert not cache._entries
        assert "encode" not in vars(model.speech_tokenizer)

    await run_scenario(scenario)


@pytest.mark.asyncio
async def test_prompt_use_cache_false_and_normalized_content_changes(monkeypatch, supported, tmp_path):
    model = Model()
    backend = mlx_backend.MLXTTSBackend()
    monkeypatch.setattr("mlx_audio.tts.load", lambda path: model)
    monkeypatch.setattr(backend, "_is_model_cached", lambda size: True)
    reference = tmp_path / "synthetic.wav"
    reference.touch()
    prompt, was_cached = await backend.create_voice_prompt(str(reference), "current text", use_cache=False)
    assert was_cached is False
    await backend.generate("first", prompt)
    await backend.generate("second", prompt)
    assert model.speech_tokenizer.calls == 2
    prompt.pop("_mlx_reference_cache")
    await backend.generate("third", prompt)
    model.samples = [9.0, 8.0, 7.0]  # Same path/transcript, changed actual encoder input.
    await backend.generate("fourth", prompt)
    assert model.speech_tokenizer.calls == 4
    assert not np.array_equal(model.seen[-2][2], model.seen[-1][2])
    backend.unload_model()
    assert not backend._reference_cache._entries
    await backend.generate("reloaded", prompt)
    assert model.speech_tokenizer.calls == 5
    backend.model_size = "0.6B"
    await backend.generate("resized", prompt)
    assert model.speech_tokenizer.calls == 6


def test_installed_version_class_and_signature_gate(monkeypatch):
    # No real weights: build only the exact upstream class shells for API gating.
    from mlx_audio.tts.models.qwen3_tts.qwen3_tts import Model as UpstreamModel
    from mlx_audio.tts.models.qwen3_tts.speech_tokenizer import (
        Qwen3TTSSpeechTokenizer,
        Qwen3TTSSpeechTokenizerEncoder,
    )

    model = UpstreamModel.__new__(UpstreamModel)
    codec = Qwen3TTSSpeechTokenizer.__new__(Qwen3TTSSpeechTokenizer)
    encoder = Qwen3TTSSpeechTokenizerEncoder.__new__(Qwen3TTSSpeechTokenizerEncoder)
    codec.encoder_model = encoder
    model.speech_tokenizer = codec
    model.config = SimpleNamespace(tts_model_type="base")
    assert mlx_reference_cache._supported_codec(model) is codec
    model.config.tts_model_type = "voice_design"
    assert mlx_reference_cache._supported_codec(model) is None
    model.config.tts_model_type = "base"
    codec.encode = lambda audio: audio
    assert mlx_reference_cache._supported_codec(model) is None
    del codec.encode
    monkeypatch.setattr(mlx_reference_cache, "version", lambda package: "0.4.2")
    assert mlx_reference_cache._supported_codec(model) is None
    assert mlx_reference_cache._supported_codec(object()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["unload", "cancel"])
async def test_backend_inflight_encode_cannot_repopulate_after_invalidation(monkeypatch, supported, tmp_path, action):
    entered, release = threading.Event(), threading.Event()

    class PausingCodec(Codec):
        def encode(self, audio):
            result = super().encode(audio)
            entered.set()
            assert release.wait(10), "test worker was not released"
            return result

    model = Model()
    model.speech_tokenizer = PausingCodec()
    backend = mlx_backend.MLXTTSBackend()
    monkeypatch.setattr("mlx_audio.tts.load", lambda path: model)
    monkeypatch.setattr(backend, "_is_model_cached", lambda size: True)
    reference = tmp_path / "synthetic.wav"
    reference.touch()
    task = asyncio.create_task(backend.generate("inflight", {"ref_audio": str(reference), "ref_text": "current"}))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if action == "unload":
            backend.unload_model()
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()
    if action == "unload":
        await task
        assert backend.model is None
    await mlx_backend._run_on_mlx_thread(lambda: None)  # Drain the cancelled executor call.
    assert not backend._reference_cache._entries
    assert "encode" not in vars(model.speech_tokenizer)
    if action == "cancel":
        # Cancellation invalidates the old epoch, while later requests can
        # still cache on the same loaded model/codec.
        prompt = {"ref_audio": str(reference), "ref_text": "later"}
        await backend.generate("after cancel", prompt)
        await backend.generate("another take", prompt)
        assert model.speech_tokenizer.calls == 2


@pytest.mark.asyncio
async def test_cache_failures_and_encoder_failure_are_safe_misses(monkeypatch, supported):
    def scenario():
        model = Model()
        codec = model.speech_tokenizer
        cache = MLXReferenceCache()
        cache.bind(model)
        audio = mx.ones((1, 1, 3))
        with monkeypatch.context() as patch:
            patch.setattr(cache, "_key", lambda *args: 1 / 0)
            with cache.encoding(model):
                codec.encode(audio)
        assert codec.calls == 1
        assert not cache._entries
        with monkeypatch.context() as patch:
            patch.setattr(mlx_reference_cache._Codes, "capture", lambda *args: 1 / 0)
            with cache.encoding(model):
                codec.encode(audio)
        assert codec.calls == 2
        assert not cache._entries

        # Instance overrides are bypassed, including their original exceptions.
        def broken(audio):
            raise RuntimeError("encoder failed")

        codec.encode = broken
        with pytest.raises(RuntimeError, match="encoder failed"), cache.encoding(model):
            codec.encode(audio)
        assert codec.encode is broken
        assert not cache._entries

    await run_scenario(scenario)


@pytest.mark.asyncio
async def test_lru_and_multiple_encodes_in_one_generation_are_bounded(supported):
    def scenario():
        model = Model()
        codec = model.speech_tokenizer
        cache = MLXReferenceCache(max_entries=2, max_bytes=48)
        cache.bind(model)
        a, b, c = [mx.full((1, 1, 3), float(value)) for value in (1, 2, 3)]
        for audio in (a, b, a, c, a):
            with cache.encoding(model):
                codec.encode(audio)
        assert codec.calls == 3  # A hit promotes A, so C evicts B.
        with cache.encoding(model):
            codec.encode(b)
            codec.encode(b)  # Reuse within the same generation, before publication.
            codec.encode(c)
            codec.encode(a)
        assert codec.calls == 4
        assert cache._bytes == 48
        assert len(cache._entries) == 2

    await run_scenario(scenario)


@pytest.mark.asyncio
async def test_uint32_codes_round_trip_on_worker():
    def scenario():
        values = mx.array([[[0, 2**32 - 1]]], dtype=mx.uint32)
        entry = mlx_reference_cache._Codes.capture(values, quantizers=1)
        restored = entry.restore(quantizers=1)
        assert restored.dtype == mx.uint32
        assert restored.shape == values.shape
        assert np.array_equal(np.array(restored), np.array(values))

    await run_scenario(scenario)
