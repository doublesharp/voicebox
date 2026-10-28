"""Speech pauses must survive while obvious post-silence artifacts are removed."""

import numpy as np
import pytest

from backend.utils.audio import trim_tts_output

SR = 24000


def voiced(seconds=1.0, frequency=150.0):
    time = np.arange(round(SR * seconds), dtype=np.float64) / SR
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 3 * time) ** 2
    return (
        0.2 * envelope * (np.sin(2 * np.pi * frequency * time) + 0.3 * np.sin(4 * np.pi * frequency * time))
    ).astype(np.float32)


@pytest.mark.parametrize("gap_seconds", [0.8, 1.0, 1.2, 1.9, 2.5])
def test_deliberate_pause_keeps_both_utterances(gap_seconds):
    first, second = voiced(), voiced(frequency=220)
    audio = np.concatenate([first, np.zeros(round(SR * gap_seconds)), second]).astype(np.float32)
    result = trim_tts_output(audio, SR, fade_ms=0)
    np.testing.assert_array_equal(result, audio)


def test_quiet_room_noise_during_a_pause_does_not_remove_later_speech():
    gap = np.random.default_rng(7).normal(0, 10 ** (-45 / 20), round(1.5 * SR))
    audio = np.concatenate([voiced(), gap, voiced(frequency=220)]).astype(np.float32)
    np.testing.assert_array_equal(trim_tts_output(audio, SR, fade_ms=0), audio)


def test_stationary_noise_tail_is_still_removed_without_text_context():
    gap = np.zeros(round(1.2 * SR), dtype=np.float32)
    hiss = np.random.default_rng(11).normal(0, 0.08, SR).astype(np.float32)
    audio = np.concatenate([voiced(), gap, hiss])
    result = trim_tts_output(audio, SR, fade_ms=0)
    assert len(result) <= round(1.2 * SR)
    np.testing.assert_array_equal(result[:SR], audio[:SR])


def test_breathy_modulated_continuation_is_not_stationary_hiss():
    time = np.arange(SR) / SR
    whisper = np.random.default_rng(19).normal(0, 0.08, SR)
    whisper *= 0.1 + np.sin(2 * np.pi * 3 * time) ** 4
    audio = np.concatenate([voiced(), np.zeros(round(1.5 * SR)), whisper]).astype(np.float32)
    result = trim_tts_output(audio, SR, fade_ms=0)
    np.testing.assert_array_equal(result, audio)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "Wait... go!",
        "Read this much longer sentence before finishing with its final word.",
        "二〇二六年十月二十八日",
        "123456789",
    ],
)
def test_a_brief_final_word_is_preserved_without_a_completed_text_budget(text):
    audio = np.concatenate([voiced(), np.zeros(10 * SR), voiced(0.12, 220)])
    np.testing.assert_array_equal(trim_tts_output(audio, SR, fade_ms=0, text=text), audio)


def test_only_a_tiny_noise_residual_after_excessive_spoken_duration_uses_text_budget():
    hiss = np.random.default_rng(31).normal(0, 0.08, round(0.12 * SR)).astype(np.float32)
    audio = np.concatenate([voiced(6), np.zeros(SR), hiss])
    assert len(trim_tts_output(audio, SR, fade_ms=0, text="Hello.")) <= round(6.2 * SR)
    np.testing.assert_array_equal(trim_tts_output(audio, SR, fade_ms=0), audio)
    long_text = "A longer passage still has another word left after its expressive pause."
    np.testing.assert_array_equal(trim_tts_output(audio, SR, fade_ms=0, text=long_text), audio)


def test_a_later_noise_tail_is_removed_after_an_earlier_pause_is_preserved():
    noise = np.random.default_rng(2).normal(0, 0.08, SR).astype(np.float32)
    valid = np.concatenate([voiced(), np.zeros(round(1.5 * SR)), voiced(frequency=220)])
    audio = np.concatenate([valid, np.zeros(round(1.2 * SR)), noise]).astype(np.float32)
    result = trim_tts_output(audio, SR, fade_ms=0)
    np.testing.assert_array_equal(result[: len(valid)], valid)
    assert len(result) <= len(valid) + round(0.2 * SR)


def test_edge_only_mode_and_partial_final_frame_keep_their_audio():
    audio = np.concatenate([voiced(), np.zeros(SR), voiced(0.002, 220)])
    np.testing.assert_array_equal(trim_tts_output(audio, SR, fade_ms=0), audio)
    hiss = np.random.default_rng(3).normal(0, 0.08, SR).astype(np.float32)
    audio = np.concatenate([voiced(), np.zeros(SR), hiss])
    np.testing.assert_array_equal(
        trim_tts_output(audio, SR, fade_ms=0, max_internal_silence_ms=None),
        audio,
    )


def test_trimming_does_not_mutate_source_and_fades_the_retained_edge():
    audio = voiced().copy()
    original = audio.copy()
    result = trim_tts_output(audio, SR)
    np.testing.assert_array_equal(audio, original)
    assert abs(result[-1]) < 1e-8
    np.testing.assert_array_equal(result[: -round(0.03 * SR)], original[: -round(0.03 * SR)])


@pytest.mark.asyncio
async def test_chunk_callbacks_receive_the_actual_text_and_legacy_callbacks_still_work():
    from backend.utils.chunked_tts import generate_chunked, split_text_into_chunks

    class Backend:
        async def generate(self, text, *_args):
            return np.full(2400, 0.2, dtype=np.float32), SR

    text = "First sentence. Second sentence. Third sentence."
    seen = []

    def contextual(audio, sample_rate, *, text):
        seen.append(text)
        return audio

    result, _ = await generate_chunked(Backend(), text, {}, max_chunk_chars=20, crossfade_ms=0, trim_fn=contextual)
    assert seen == split_text_into_chunks(text, 20)
    legacy_calls = []

    def legacy(audio, sample_rate):
        legacy_calls.append(sample_rate)
        return audio

    legacy_result, _ = await generate_chunked(Backend(), text, {}, max_chunk_chars=20, crossfade_ms=0, trim_fn=legacy)
    np.testing.assert_array_equal(legacy_result, result)
    assert len(legacy_calls) == len(seen)


@pytest.mark.asyncio
async def test_retry_fallback_receives_each_retried_text_instead_of_the_original():
    from backend.utils.chunked_tts import generate_chunked

    generated, trimmed = [], []

    class Backend:
        async def generate(self, text, *_args):
            generated.append(text)
            return np.full(2400, 0.2, dtype=np.float32), SR

    def contextual(audio, sample_rate, *, text):
        trimmed.append(text)
        return audio

    await generate_chunked(
        Backend(),
        "A short sentence. " * 16,
        {},
        trim_fn=contextual,
        runaway_detector=lambda *_args: True,
        crossfade_ms=0,
    )
    assert trimmed
    assert trimmed == [text for text in generated if len(text) <= 100]
    assert all(text != generated[0] for text in trimmed)


@pytest.mark.asyncio
async def test_a_callback_type_error_is_not_retried_as_a_legacy_call():
    from backend.utils.chunked_tts import generate_chunked

    calls = []

    class Backend:
        async def generate(self, text, *_args):
            return voiced(), SR

    def broken(audio, sample_rate, *, text=None):
        calls.append(text)
        raise TypeError("failure inside callback")

    with pytest.raises(TypeError, match="failure inside callback"):
        await generate_chunked(Backend(), "Hello.", {}, trim_fn=broken)
    assert calls == ["Hello."]


def test_even_a_slow_reading_estimate_cannot_discard_a_voiced_final_word():
    audio = np.concatenate([voiced(6), np.zeros(SR), voiced(0.12, 220)])
    np.testing.assert_array_equal(trim_tts_output(audio, SR, fade_ms=0, text="Hello."), audio)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_protection", [False, True])
async def test_chunk_pipeline_preserves_pauses_with_and_without_retry_protection(retry_protection):
    from backend.utils.audio import has_tts_runaway
    from backend.utils.chunked_tts import generate_chunked

    audio = np.concatenate([voiced(), np.zeros(round(2.5 * SR)), voiced(frequency=220)])

    class Backend:
        async def generate(self, text, *_args):
            return audio, SR

    result, _ = await generate_chunked(
        Backend(),
        "First. Second.",
        {},
        trim_fn=trim_tts_output,
        runaway_detector=has_tts_runaway if retry_protection else None,
    )
    assert len(result) == len(audio)
    np.testing.assert_array_equal(result[: -round(0.03 * SR)], audio[: -round(0.03 * SR)])


@pytest.mark.asyncio
async def test_exhausted_chatterbox_retry_still_removes_the_noise_tail():
    from backend.utils.audio import has_tts_runaway
    from backend.utils.chunked_tts import generate_chunked

    hiss = np.random.default_rng(50).normal(0, 0.08, SR).astype(np.float32)
    audio = np.concatenate([voiced(), np.zeros(round(2.5 * SR)), hiss])

    class Backend:
        async def generate(self, text, *_args):
            return audio, SR

    result, _ = await generate_chunked(
        Backend(),
        "Hello.",
        {},
        trim_fn=trim_tts_output,
        runaway_detector=has_tts_runaway,
    )
    assert len(result) == round(1.2 * SR)
    np.testing.assert_array_equal(result[:SR], audio[:SR])
