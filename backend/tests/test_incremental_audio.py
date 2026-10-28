import asyncio
import io

import numpy as np
import pytest
import soundfile as sf

from backend.utils.streaming_audio import iter_speech_audio


@pytest.mark.asyncio
async def test_audio_is_available_before_remaining_text_finishes():
    remaining = asyncio.Event()

    class Backend:
        calls = 0

        async def generate(self, text, prompt, language, seed, instruct):
            self.calls += 1
            if self.calls > 1:
                await remaining.wait()
            return np.full(2400, 0.1, dtype=np.float32), 24000

    backend = Backend()
    stream = iter_speech_audio(backend, 'First sentence. ' * 50, {}, max_chunk_chars=100, crossfade_ms=0)
    try:
        try:
            first, rate = await asyncio.wait_for(anext(stream), 0.1)
        except TimeoutError:
            pytest.fail('first audio waited for all remaining text')
        assert rate == 24000
        assert first.size == 2400
        assert not remaining.is_set()
    finally:
        remaining.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_streamed_crossfades_match_full_audio_assembly():
    from backend.utils.chunked_tts import concatenate_audio_chunks, split_text_into_chunks

    text = 'A short sentence. ' * 20
    pieces = []

    class Backend:
        async def generate(self, text, prompt, language, seed, instruct):
            audio = np.linspace(0, 0.2, 1000, dtype=np.float32)
            pieces.append(audio)
            return audio, 24000

    chunks = [chunk async for chunk, _ in iter_speech_audio(Backend(), text, {}, max_chunk_chars=100, crossfade_ms=5)]
    expected = concatenate_audio_chunks(pieces, 24000, crossfade_ms=5)
    np.testing.assert_allclose(np.concatenate(chunks), expected, atol=1e-7)
    assert len(pieces) == len(split_text_into_chunks(text, 100))


@pytest.mark.asyncio
async def test_preview_rejects_changed_sample_rate():
    class Backend:
        calls = 0
        async def generate(self, text, prompt, language, seed, instruct):
            self.calls += 1
            return np.full(1000, 0.1, dtype=np.float32), 24000 if self.calls == 1 else 48000

    with pytest.raises(ValueError, match='sample rate'):
        async for _ in iter_speech_audio(Backend(), 'Sentence. '*50, {}, max_chunk_chars=100):
            pass


def test_unknown_length_preview_wav_decodes_as_pcm():
    from backend.utils.streaming_audio import pcm16_bytes, streaming_wav_header

    samples = np.linspace(-0.25, 0.25, 2400, dtype=np.float32)
    encoded = streaming_wav_header(24000) + pcm16_bytes(samples)
    decoded, rate = sf.read(io.BytesIO(encoded))
    assert rate == 24000
    np.testing.assert_allclose(decoded, samples, atol=4e-5)


@pytest.mark.asyncio
async def test_slow_stream_has_bounded_backpressure_and_disconnect_cleans_up():
    from backend.utils.streaming_audio import serialized_byte_stream

    sent = []
    stopped = asyncio.Event()

    async def operation(send):
        try:
            for index in range(100):
                await send(bytes([index]))
                sent.append(index)
        finally:
            stopped.set()

    stream = serialized_byte_stream(operation)
    assert await anext(stream) == bytes([0])
    await asyncio.sleep(0.03)
    assert len(sent) <= 3
    await asyncio.wait_for(stream.aclose(), 1)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_stream_failure_is_propagated_and_next_request_can_run():
    from backend.services.inference_scheduler import run_serialized_inference
    from backend.utils.streaming_audio import serialized_byte_stream

    async def operation(send):
        await send(b'header')
        raise ValueError('synthesis failed')

    stream = serialized_byte_stream(operation)
    assert await anext(stream) == b'header'
    with pytest.raises(ValueError, match='synthesis failed'):
        await anext(stream)
    async def next_request():
        return 'ready'
    assert await asyncio.wait_for(run_serialized_inference(next_request), 1) == 'ready'
