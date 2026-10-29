"""Bounded CPU-only Qwen Base codec reuse for the audited mlx-audio 0.4.1 API.

Use ``encoding(model)`` on the MLX worker for the entire generate iterator.
Only codec codes are retained; text, speaker embeddings and KV state are not.
"""

import hashlib
import inspect
import threading
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version

import numpy as np


def _supported_codec(model):
    """Fail closed for versions/classes/signatures not audited with this shim."""
    try:
        if version("mlx-audio") != "0.4.1":
            return None
        from mlx_audio.tts.models.qwen3_tts.qwen3_tts import Model
        from mlx_audio.tts.models.qwen3_tts.speech_tokenizer import (
            Qwen3TTSSpeechTokenizer,
            Qwen3TTSSpeechTokenizerEncoder,
        )

        codec = model.speech_tokenizer
        if (
            type(model) is not Model
            or model.config.tts_model_type != "base"
            or type(codec) is not Qwen3TTSSpeechTokenizer
            or type(codec.encoder_model) is not Qwen3TTSSpeechTokenizerEncoder
            or "encode" in vars(codec)
            or "encode" in codec
            or tuple(inspect.signature(type(codec).encode).parameters) != ("self", "audio")
        ):
            return None
        return codec
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _Codes:
    payload: bytes
    dtype: str
    shape: tuple
    digest: bytes

    @classmethod
    def capture(cls, codes, quantizers):
        values = np.array(codes)  # Materialize on the MLX worker, never retain MLX values.
        if (
            values.dtype.kind not in "iu"
            or values.dtype.itemsize not in (1, 2, 4, 8)
            or values.ndim != 3
            or values.shape[:2] != (1, quantizers)
            or values.shape[2] == 0
        ):
            return None
        payload = values.tobytes()
        return cls(payload, values.dtype.str, values.shape, hashlib.sha256(payload).digest())

    def restore(self, quantizers):
        import mlx.core as mx

        dtype = np.dtype(self.dtype)
        if (
            dtype.kind not in "iu"
            or len(self.shape) != 3
            or self.shape[:2] != (1, quantizers)
            or not isinstance(self.shape[2], int)
            or self.shape[2] <= 0
            or len(self.payload) != self.shape[2] * quantizers * dtype.itemsize
            or hashlib.sha256(self.payload).digest() != self.digest
        ):
            raise ValueError("Malformed reference codec entry")
        # Own fresh CPU memory, then a fresh MLX array: neither caller can mutate
        # the immutable stored bytes. Preserve the exact integer dtype/shape.
        restored = mx.array(np.frombuffer(self.payload, dtype=dtype).reshape(self.shape).copy())
        mx.eval(restored)
        return restored


class MLXReferenceCache:
    """One loaded model/codec lifetime; defaults: 8 entries / 8 MiB of codes.

    ``clear`` is safe from the event-loop thread during an in-flight encode.
    A generation publishes only on successful exit and only into its original
    epoch. Locks never cover GPU work. Models/codecs are held by weakref only.
    """

    def __init__(self, max_entries=8, max_bytes=8 * 1024 * 1024, max_input_bytes=16 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_input_bytes = max_input_bytes
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._bytes = 0
        self._epoch = 0
        self._model = self._codec = None

    def clear(self, *, keep_model=False):
        with self._lock:
            self._epoch += 1
            self._entries.clear()
            self._bytes = 0
            if not keep_model:
                self._model = self._codec = None
            return self._epoch

    def bind(self, model):
        epoch = self.clear()
        codec = _supported_codec(model)
        if codec is not None:
            with self._lock:
                if epoch == self._epoch:
                    self._model, self._codec = weakref.ref(model), weakref.ref(codec)

    def _discard_entries(self, epoch):
        with self._lock:
            if epoch == self._epoch:
                self._entries.clear()
                self._bytes = 0

    def _key(self, audio, codec):
        import mlx.core as mx

        # 0.4.1 passes normalized mono float32 [1, 1, samples], with no encode
        # options. Bypass other forms, large inputs and nonfinite waveforms.
        if (
            not isinstance(audio, mx.array)
            or audio.dtype != mx.float32
            or audio.ndim != 3
            or audio.shape[:2] != (1, 1)
            or not 0 < audio.nbytes <= self.max_input_bytes
        ):
            return None
        values = np.array(audio)
        if not np.isfinite(values).all():
            return None
        options = (
            codec.encoder_model.valid_num_quantizers,
            codec.encoder_valid_num_quantizers,
            codec.input_sample_rate,
            codec.encode_downsample_rate,
        )
        if any(type(value) is not int or value <= 0 for value in options):
            return None
        return (values.dtype.str, values.shape, options, hashlib.sha256(memoryview(values)).digest())

    def _lookup(self, key, epoch):
        with self._lock:
            if epoch != self._epoch:
                return None
            entry = self._entries.pop(key, None)
            if entry is not None and not isinstance(entry, _Codes):
                self._entries.clear()
                self._bytes = 0
                return None
            if entry is not None:
                self._entries[key] = entry
            return entry

    def _publish(self, pending, epoch):
        with self._lock:
            if epoch != self._epoch:
                return
            for key, entry in pending.items():
                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._bytes -= len(previous.payload)
                self._entries[key] = entry
                self._bytes += len(entry.payload)
            while self._entries and (len(self._entries) > self.max_entries or self._bytes > self.max_bytes):
                _, entry = self._entries.popitem(last=False)
                self._bytes -= len(entry.payload)

    @contextmanager
    def encoding(self, model, enabled=True):
        codec = getattr(model, "speech_tokenizer", None)
        with self._lock:
            active = enabled is True and self._model is not None and self._model() is model and self._codec() is codec
            epoch = self._epoch
        if not active:
            yield
            return
        original = codec.encode
        # Never replace an instance override or another active wrapper.
        if getattr(original, "__func__", None) is not type(codec).encode:
            yield
            return
        worker = threading.get_ident()
        pending = OrderedDict()
        pending_bytes = 0

        def encode(*args, **kwargs):
            nonlocal pending_bytes
            if len(args) != 1 or kwargs or threading.get_ident() != worker:
                return original(*args, **kwargs)
            try:
                key = self._key(args[0], codec)
            except Exception:
                key = None  # A cache failure never substitutes another reference.
            if key is None:
                return original(*args, **kwargs)
            quantizers = key[2][0]
            entry = pending.get(key) or self._lookup(key, epoch)
            if entry is not None:
                try:
                    return entry.restore(quantizers)
                except Exception:
                    self._discard_entries(epoch)
            result = original(*args, **kwargs)
            try:
                import mlx.core as mx

                if result.nbytes <= self.max_bytes:
                    mx.eval(result)
                    entry = _Codes.capture(result, quantizers)
                    if entry is not None and self.max_entries > 0:
                        previous = pending.pop(key, None)
                        if previous is not None:
                            pending_bytes -= len(previous.payload)
                        pending[key] = entry
                        pending_bytes += len(entry.payload)
                        while pending and (len(pending) > self.max_entries or pending_bytes > self.max_bytes):
                            _, evicted = pending.popitem(last=False)
                            pending_bytes -= len(evicted.payload)
            except Exception:
                pass  # Encoding succeeded; optional CPU caching must not break it.
            return result

        codec.encode = encode
        try:
            yield
        except BaseException:
            raise  # No publication after failure, GeneratorExit or cancellation.
        else:
            self._publish(pending, epoch)
        finally:
            del codec.encode  # Restore class method; no bound-method reference cycle.
