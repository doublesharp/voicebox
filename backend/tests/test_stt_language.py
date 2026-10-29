"""STT request models accept the same language codes as the TTS ones.

``TranscriptionRequest`` and ``CaptureRetranscribeRequest`` only allowed the
original ten Qwen languages, so a Polish (or any other newer) code got a 422
even though Whisper transcribes it fine (#1095).
"""

import pytest
from pydantic import ValidationError

from backend import models


@pytest.mark.parametrize("model", [models.TranscriptionRequest, models.CaptureRetranscribeRequest])
def test_stt_language_matches_tts_languages(model):
    for language in (
        "zh",
        "en",
        "ja",
        "ko",
        "de",
        "fr",
        "ru",
        "pt",
        "es",
        "it",
        "he",
        "ar",
        "da",
        "el",
        "fi",
        "hi",
        "ms",
        "nl",
        "no",
        "pl",
        "sv",
        "sw",
        "tr",
    ):
        assert model(language=language).language == language
    assert model().language is None
    with pytest.raises(ValidationError):
        model(language="xx")


@pytest.fixture
def capture_api(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend import config
    from backend.database import Base, Capture, get_db
    from backend.routes.captures import router
    from backend.services import captures

    monkeypatch.setattr(config, "_data_dir", tmp_path)
    (tmp_path / "capture.wav").write_bytes(b"fake STT input")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(
            Capture(
                id="capture",
                audio_path="capture.wav",
                language="en",
                transcript_raw="Old text",
                transcript_refined="Old refined",
            )
        )
        db.commit()
    transcribe = AsyncMock(return_value="Polski tekst")
    monkeypatch.setattr(
        captures, "get_whisper_model", lambda: SimpleNamespace(model_size="base", transcribe=transcribe)
    )
    app = FastAPI()
    app.include_router(router)

    def override_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client, sessions, transcribe
    finally:
        engine.dispose()


def test_retranscribe_accepts_polish_and_persists_result(capture_api):
    from backend.database import Capture

    client, sessions, transcribe = capture_api
    response = client.post("/captures/capture/retranscribe", json={"language": "pl", "model": "base"})
    assert response.status_code == 200
    assert response.json()["transcript_raw"] == "Polski tekst"
    assert response.json()["language"] == "pl"
    assert transcribe.call_args.args[1:] == ("pl", "base")
    with sessions() as db:
        row = db.get(Capture, "capture")
        assert row.language == "pl"
        assert row.transcript_raw == "Polski tekst"
        assert row.transcript_refined is None


def test_retranscribe_rejects_invalid_language_before_stt(capture_api):
    client, _, transcribe = capture_api
    assert client.post("/captures/capture/retranscribe", json={"language": "xx"}).status_code == 422
    transcribe.assert_not_called()
