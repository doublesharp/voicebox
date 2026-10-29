"""Long-form synthesis preserves text markers and consistent audio timing."""
import numpy as np
import pytest

from backend.utils.chunked_tts import generate_chunked, split_text_into_chunks


@pytest.mark.parametrize('text,tag', [
    ('x' * 90 + ' [clear throat] then continue speaking', '[clear throat]'),
    ('x' * 96 + '[laugh] and continue speaking', '[laugh]'),
    ('x' * 90 + '[abc。def] and continue speaking', '[abc。def]'),
])
def test_chunk_boundaries_never_split_a_complete_tag(text, tag):
    chunks = split_text_into_chunks(text, 100)
    assert any(tag in chunk for chunk in chunks)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert ''.join(chunks).replace(' ', '') == text.replace(' ', '')


@pytest.mark.asyncio
async def test_buffered_generation_rejects_rate_changes_between_text_chunks():
    class Backend:
        calls = 0
        async def generate(self, *_args, **_kwargs):
            self.calls += 1
            return np.ones(8, dtype=np.float32), 1000 if self.calls == 1 else 2000
    with pytest.raises(ValueError, match='sample rate'):
        await generate_chunked(Backend(), 'one two three four five', {}, max_chunk_chars=8, crossfade_ms=0)


@pytest.mark.asyncio
async def test_buffered_retry_rejects_rate_changes_between_retried_chunks():
    class Backend:
        calls = 0
        async def generate(self, text, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return np.array([9], dtype=np.float32), 1000
            return np.ones(8, dtype=np.float32), 1000 if self.calls == 2 else 2000
    with pytest.raises(ValueError, match='sample rate'):
        await generate_chunked(Backend(), 'a' * 120 + ' ' + 'b' * 120, {},
                               max_chunk_chars=500, crossfade_ms=0,
                               runaway_detector=lambda audio, _rate: audio[0] == 9)


@pytest.mark.asyncio
@pytest.mark.parametrize('rate', [0, -1, True, 1000.5])
async def test_buffered_generation_rejects_invalid_sample_rates(rate):
    class Backend:
        async def generate(self, *_args, **_kwargs):
            return np.ones(8, dtype=np.float32), rate
    with pytest.raises(ValueError, match='sample rate'):
        await generate_chunked(Backend(), 'short', {})


@pytest.mark.parametrize('tag', ['[laugh]', '[clear throat]', '[abc。def]', '[whispering]'])
def test_tag_is_atomic_at_every_offset_near_a_chunk_boundary(tag):
    for offset in range(40):
        text = 'x' * offset + tag + ' y' * 15
        chunks = split_text_into_chunks(text, 20)
        assert any(tag in chunk for chunk in chunks), (tag, offset, chunks)
        assert all(len(chunk) <= 20 for chunk in chunks)
        assert ''.join(chunks).replace(' ', '') == text.replace(' ', '')


def test_overlong_tag_fails_explicitly_instead_of_being_split():
    with pytest.raises(ValueError, match='tag exceeds'):
        split_text_into_chunks('[a tag longer than the limit] rest', 10)


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
def test_invalid_chunk_limit_is_rejected_without_hanging(limit):
    with pytest.raises(ValueError, match='max_chars'):
        split_text_into_chunks('some text', limit)


def test_multidot_abbreviations_do_not_force_a_sentence_break():
    chunks = split_text_into_chunks('Intro e.g. example continues with more words', 20)
    assert chunks[0] == 'Intro e.g. example'


def test_sentence_after_number_and_decimal_at_cut_are_not_confused():
    assert split_text_into_chunks('One 2025. More words continue', 12)[0] == 'One 2025.'
    assert split_text_into_chunks('value 1.23 remains here', 8)[0] == 'value'


@pytest.mark.asyncio
async def test_streamed_preview_uses_the_same_atomic_tag_boundaries():
    from backend.utils.streaming_audio import iter_speech_audio
    class Backend:
        def __init__(self): self.calls = []
        async def generate_stream(self, text, *_args):
            self.calls.append(text)
            yield np.ones(8, dtype=np.float32) * 0.1, 1000
    backend = Backend()
    text = 'x' * 90 + ' [clear throat] and continue speaking'
    frames = [audio async for audio, _ in iter_speech_audio(backend, text, {}, max_chunk_chars=100, crossfade_ms=0)]
    assert frames
    assert any('[clear throat]' in chunk for chunk in backend.calls)
    assert all(len(chunk) <= 100 for chunk in backend.calls)


def test_large_script_does_not_rescan_all_remaining_tags_for_every_chunk(monkeypatch):
    from backend.utils import chunked_tts
    original = chunked_tts._PARA_TAG_RE
    scanned = 0
    class CountingPattern:
        def finditer(self, text):
            nonlocal scanned
            scanned += len(text)
            return original.finditer(text)
    monkeypatch.setattr(chunked_tts, '_PARA_TAG_RE', CountingPattern())
    text = '[laugh] word ' * 3500
    chunks = split_text_into_chunks(text, 100)
    assert sum(chunk.count('[laugh]') for chunk in chunks) == 3500
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert scanned <= 2 * len(text)


@pytest.mark.asyncio
async def test_numpy_integer_sample_rate_is_accepted():
    class Backend:
        async def generate(self, *_args, **_kwargs):
            return np.ones(3, dtype=np.float32), np.int64(24000)
    audio, rate = await generate_chunked(Backend(), 'short', {})
    assert audio.tolist() == [1, 1, 1]
    assert rate == 24000
    assert type(rate) is int
