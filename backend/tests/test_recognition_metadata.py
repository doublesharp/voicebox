"""Real-shaped word metadata is bounded and never invented from script text."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from backend.services import transcribe


def test_numpy_scalars_from_installed_whisper_become_finite_plain_float_words():
    result = transcribe.normalize_recognition(
        {"text": " Ada ", "segments": [{"words": [{"word": " Ada ", "start": np.float64(0), "end": np.float64(0.5)}]}]},
        1,
    )
    assert result == {"text": "Ada", "words": [{"word": "Ada", "start": 0.0, "end": 0.5}]}
    assert type(result["words"][0]["start"]) is float


@pytest.mark.parametrize(
    "word",
    [
        {"word": "Ada", "start": float("nan"), "end": 0.5},
        {"word": "Ada", "start": False, "end": 0.5},
        {"word": "Ada", "start": 0.5, "end": 0.2},
        {"word": "Ada", "start": 0, "end": 2},
        {"word": "Ada", "start": 1.01, "end": 1.04},
        {"word": "", "start": 0, "end": 0.5},
    ],
)
def test_malformed_words_are_refused(word):
    with pytest.raises(ValueError, match="Invalid"):
        transcribe.normalize_recognition({"text": "Ada", "words": [word]}, 1)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"text": "Ada", "segments": None},
        {"text": "Ada", "segments": [None]},
        {"text": "x" * 10001, "words": []},
        {"text": "Ada", "segments": [{"words": [{}] * 2001}]},
    ],
)
def test_malformed_or_unbounded_recognition_is_refused(result):
    with pytest.raises(ValueError, match="Invalid|exceeds"):
        transcribe.normalize_recognition(result, 1)


@pytest.mark.asyncio
async def test_missing_cache_and_unavailable_word_timing_never_start_recognition(monkeypatch):
    model = SimpleNamespace(
        model_size="base", is_loaded=lambda: False, _is_model_cached=lambda size: False, transcribe_detailed=AsyncMock()
    )
    monkeypatch.setattr(transcribe, "get_whisper_model", lambda: model)
    with pytest.raises(ValueError, match="Download"):
        await transcribe.recognize_words("fixture.wav", 1)
    model.transcribe_detailed.assert_not_called()
    del model.transcribe_detailed
    with pytest.raises(ValueError, match="unavailable"):
        await transcribe.recognize_words("fixture.wav", 1)
    with pytest.raises(ValueError, match="120"):
        await transcribe.recognize_words("fixture.wav", 121)


@pytest.mark.asyncio
async def test_cached_selected_model_returns_actual_words_and_unknown_revision(monkeypatch):
    model = SimpleNamespace(
        model_size="base",
        is_loaded=lambda: False,
        _is_model_cached=lambda size: size == "base",
        transcribe_detailed=AsyncMock(return_value={"text": "Ada", "segments": []}),
    )
    monkeypatch.setattr(transcribe, "get_whisper_model", lambda: model)
    result = await transcribe.recognize_words("fixture.wav", 1, "en")
    assert result["text"] == "Ada"
    assert result["words"] == []
    assert result["recognizer"]["revision"] is None
    model.transcribe_detailed.assert_awaited_once_with("fixture.wav", language="en", model_size="base")


@pytest.mark.asyncio
async def test_missing_package_metadata_is_recorded_as_unknown(monkeypatch):
    model = SimpleNamespace(
        model_size="base",
        is_loaded=lambda: True,
        transcribe_detailed=AsyncMock(return_value={"text": "", "segments": []}),
    )
    monkeypatch.setattr(transcribe, "get_whisper_model", lambda: model)

    def missing(name):
        raise transcribe.PackageNotFoundError(name)

    monkeypatch.setattr(transcribe, "version", missing)
    result = await transcribe.recognize_words("fixture.wav", 1)
    assert result["recognizer"]["package_version"] is None
