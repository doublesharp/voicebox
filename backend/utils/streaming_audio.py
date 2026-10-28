"""Bounded, incremental preview delivery without whole-file mastering.

Preview uses the requested text chunk boundaries, fixed unity gain and a
peak guard. Buffered/export paths retain full-file normalization and effects.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator, Awaitable, Callable

import numpy as np

from ..services.inference_scheduler import check_inference_cancelled, run_serialized_inference
from .audio import protect_audio_peak
from .chunked_tts import concatenate_audio_chunks, generate_chunked, split_text_into_chunks


async def iter_speech_audio(
    backend,
    text: str,
    voice_prompt: dict,
    *,
    language: str = "en",
    seed: int | None = None,
    instruct: str | None = None,
    max_chunk_chars: int = 800,
    crossfade_ms: int = 50,
    trim_fn=None,
    runaway_detector=None,
) -> AsyncIterator[tuple[np.ndarray, int]]:
    """Yield validated audio while retaining only a short crossfade tail.

    Engine generation remains one utterance at a time. This portable path
    reduces long-script latency without pretending every engine exposes a
    token/codec streaming interface.
    """
    pending = np.empty(0, dtype=np.float32)
    sample_rate = None
    for index, chunk_text in enumerate(split_text_into_chunks(text, max_chunk_chars)):
        check_inference_cancelled()
        audio, rate = await generate_chunked(
            backend, chunk_text, voice_prompt, language=language,
            seed=seed + index if seed is not None else None, instruct=instruct,
            max_chunk_chars=max_chunk_chars, crossfade_ms=crossfade_ms,
            trim_fn=trim_fn, runaway_detector=runaway_detector,
        )
        check_inference_cancelled()
        if rate <= 0 or (sample_rate is not None and rate != sample_rate):
            raise ValueError("The audio sample rate changed during incremental generation")
        sample_rate = rate
        audio = protect_audio_peak(np.asarray(audio, dtype=np.float32))
        if audio.ndim != 1:
            raise ValueError("Incremental preview requires mono audio")
        overlap = int(rate * crossfade_ms / 1000)
        joined = concatenate_audio_chunks([pending, audio], rate, crossfade_ms) if pending.size else audio
        keep = min(overlap, joined.size)
        if joined.size > keep:
            yield joined[:-keep] if keep else joined, rate
        pending = joined[-keep:].copy() if keep else np.empty(0, dtype=np.float32)
    if pending.size and sample_rate is not None:
        yield pending, sample_rate


def streaming_wav_header(sample_rate: int) -> bytes:
    """PCM16 mono RIFF header with unknown-length sentinel sizes.

    EOF terminates the data chunk. No Content-Length is promised, and clients
    that require a seekable finalized WAV should use the buffered endpoint.
    """
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ", 16,
        1, 1, sample_rate, sample_rate * 2, 2, 16, b"data", 0xFFFFFFFF,
    )


def pcm16_bytes(audio: np.ndarray) -> bytes:
    return (protect_audio_peak(audio) * 32767).astype("<i2").tobytes()


async def serialized_byte_stream(
    operation: Callable[[Callable[[bytes], Awaitable[None]]], Awaitable[None]],
) -> AsyncIterator[bytes]:
    """Bridge inference to a slow/disconnected client with bounded buffering.

    Closing the iterator cancels its caller task; the inference scheduler
    drains any synchronous engine worker before allowing the next request.
    Cancellation-aware queue writes cannot strand that worker on a full queue.
    """
    queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(maxsize=2)
    consumer_closed = False

    async def send(data: bytes) -> None:
        while True:
            check_inference_cancelled()
            if consumer_closed:
                raise asyncio.CancelledError()
            try:
                await asyncio.wait_for(queue.put(data), 0.1)
                return
            except TimeoutError:
                continue

    async def drive() -> None:
        try:
            await run_serialized_inference(lambda: operation(send))
        except Exception as error:
            if not consumer_closed:
                await queue.put(error)
        finally:
            if not consumer_closed:
                await queue.put(None)

    producer = asyncio.create_task(drive())
    try:
        while True:
            value = await queue.get()
            if value is None:
                return
            if isinstance(value, Exception):
                raise value
            yield value
    finally:
        consumer_closed = True
        if not producer.done():
            producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
