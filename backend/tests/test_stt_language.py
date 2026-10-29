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


def test_retranscribe_auto_clears_previous_language_and_refinement(capture_api):
    from backend.database import Capture

    client, sessions, transcribe = capture_api
    response = client.post('/captures/capture/retranscribe', json={'model': 'base'})
    assert response.status_code == 200
    assert transcribe.call_args.args[1:] == (None, 'base')
    assert response.json()['language'] is None
    assert response.json()['transcript_refined'] is None
    with sessions() as db:
        row = db.get(Capture, 'capture')
        assert row.language is None
        assert row.transcript_refined is None


def test_capture_list_audio_and_delete_workflow(capture_api):
    client, sessions, _ = capture_api
    listing = client.get('/captures').json()
    assert listing['total'] == 1
    assert listing['items'][0]['id'] == 'capture'
    assert client.get('/captures/capture').json()['transcript_raw'] == 'Old text'
    assert client.get('/captures/capture/audio').content == b'fake STT input'
    assert client.delete('/captures/capture').status_code == 200
    assert client.get('/captures/capture').status_code == 404
    assert client.get('/captures/capture/audio').status_code == 404
    assert client.delete('/captures/capture').status_code == 404
    assert client.get('/captures').json()['total'] == 0


@pytest.mark.parametrize('query', ['limit=0', 'limit=201', 'offset=-1'])
def test_capture_list_rejects_invalid_pagination(capture_api, query):
    client, _, _ = capture_api
    assert client.get('/captures?' + query).status_code == 400


def test_retranscribe_missing_audio_returns_gone(capture_api):
    from backend import config
    client, _, transcribe = capture_api
    (config._data_dir / 'capture.wav').unlink()
    response = client.post('/captures/capture/retranscribe', json={})
    assert response.status_code == 410
    transcribe.assert_not_called()
    assert client.get('/captures/capture/audio').status_code == 404


def test_retranscribe_missing_capture_returns_not_found(capture_api):
    client, _, transcribe = capture_api
    assert client.post('/captures/missing/retranscribe', json={}).status_code == 404
    transcribe.assert_not_called()


def test_capture_create_and_refine_use_requested_settings(capture_api, monkeypatch):
    import io
    from unittest.mock import AsyncMock

    import numpy as np
    import soundfile as sf
    from backend.services import captures

    client, _, transcribe = capture_api
    audio = io.BytesIO()
    sf.write(audio, np.zeros(800, dtype=np.float32), 8000, format='WAV')
    response = client.post('/captures', data={'source': 'recording', 'language': 'pl', 'stt_model': 'base'},
                           files={'file': ('input.wav', audio.getvalue(), 'audio/wav')})
    assert response.status_code == 200, response.text
    created = response.json()
    assert created['source'] == 'recording'
    assert created['language'] == 'pl'
    assert created['transcript_raw'] == 'Polski tekst'
    assert created['auto_refine'] is True
    assert created['allow_auto_paste'] is True
    assert transcribe.call_args.args[1:] == ('pl', 'base')
    assert client.get(f"/captures/{created['id']}/audio").status_code == 200

    refine = AsyncMock(return_value=('Refined text', '1.7B'))
    monkeypatch.setattr(captures, 'refine_transcript', refine)
    response = client.post(f"/captures/{created['id']}/refine", json={
        'model_size': '1.7B',
        'flags': {'smart_cleanup': False, 'self_correction': True, 'preserve_technical': False},
    })
    assert response.status_code == 200, response.text
    assert response.json()['transcript_refined'] == 'Refined text'
    assert response.json()['llm_model'] == '1.7B'
    assert refine.call_args.kwargs['language'] == 'pl'
    assert refine.call_args.kwargs['model_size'] == '1.7B'
    assert refine.call_args.args[1].smart_cleanup is False
    assert client.get(f"/captures/{created['id']}").json()['transcript_refined'] == 'Refined text'


@pytest.mark.parametrize('payload, source', [(b'', 'file'), (b'audio', 'invalid')])
def test_capture_creation_rejects_empty_or_invalid_source(capture_api, payload, source):
    client, _, transcribe = capture_api
    response = client.post('/captures', data={'source': source}, files={'file': ('input.wav', payload, 'audio/wav')})
    assert response.status_code == 400
    assert client.get('/captures').json()['total'] == 1
    transcribe.assert_not_called()


def test_refine_missing_capture_returns_not_found(capture_api):
    client, _, _ = capture_api
    assert client.post('/captures/missing/refine', json={}).status_code == 404
