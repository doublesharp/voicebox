"""Reference guidance is advisory, bounded, and never starts model downloads."""

import io
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config
from backend.routes.sample_quality import make_sample_quality_router
from backend.utils.ffmpeg import find_ffmpeg


def wav(samples, rate=24000):
    output = io.BytesIO()
    sf.write(output, samples, rate, format="WAV", subtype="FLOAT")
    return output.getvalue()


def analyze(client, samples, **fields):
    return client.post(
        "/sample-quality/analyze", files={"file": ("sample.wav", wav(samples), "audio/wav")}, data=fields
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    app = FastAPI()
    app.include_router(make_sample_quality_router())
    with TestClient(app) as result:
        yield result
    assert not list(tmp_path.rglob("*.wav")), "Analysis must remove disposable recordings"


def test_clipping_and_excess_silence_have_actionable_guidance(client):
    samples = np.concatenate((np.zeros(24000), np.tile([1.0, -1.0], 12000), np.zeros(24000)))
    response = analyze(client, samples, transcript="Reference words")
    assert response.status_code == 200
    result = response.json()
    assert result["duration_seconds"] == 3
    assert result["clipped_fraction"] > 0.3
    assert result["silence_fraction"] > 0.6
    issues = {item["code"]: item for item in result["issues"]}
    assert "clipping" in issues and "gain" in issues["clipping"]["suggestion"].lower()
    assert "silence" in issues and "trim" in issues["silence"]["suggestion"].lower()
    assert result["transcript"]["status"] == "unchecked"


def test_stereo_analysis_does_not_cancel_opposite_phase_speech(client):
    tone = 0.15 * np.sin(2 * np.pi * 250 * np.arange(24000 * 5) / 24000)
    response = analyze(client, np.column_stack((tone, -tone)))
    assert response.status_code == 200
    result = response.json()
    assert result["channels"] == 2
    assert result["silence_fraction"] == 0
    assert result["rms_dbfs"] > -30


def test_possible_noise_is_qualified_and_silence_is_not_called_clean(client):
    samples = np.random.default_rng(42).normal(0, 0.035, 24000 * 6)
    result = analyze(client, samples).json()
    issue = next(item for item in result["issues"] if item["code"] == "possible_noise")
    assert "possible" in issue["message"].lower()
    assert result["noise_assessment"] == "heuristic"
    silence = analyze(client, np.zeros(24000 * 4)).json()
    assert any(item["code"] == "no_speech_level" for item in silence["issues"])


def test_transcript_check_compares_recognized_words_and_ignores_punctuation(client):
    samples = np.zeros(24000)
    result = analyze(client, samples, transcript="Hello, WORLD!", recognized_text="hello world").json()
    assert result["transcript"]["status"] == "similar"
    result = analyze(client, samples, transcript="Hello world", recognized_text="A different sentence").json()
    assert result["transcript"]["status"] == "review"
    assert result["transcript"]["recognized_text"] == "A different sentence"


def test_upload_rejects_invalid_audio_and_long_decoded_input(client):
    response = client.post("/sample-quality/analyze", files={"file": ("bad.wav", b"not audio", "audio/wav")})
    assert response.status_code == 422
    assert analyze(client, np.zeros(24000 * 61)).status_code == 422


def test_upload_size_is_bounded_and_temporary_input_removed(client, monkeypatch):
    from backend.routes import sample_quality

    monkeypatch.setattr(sample_quality, "MAX_UPLOAD_BYTES", 16)
    response = analyze(client, np.zeros(24000))
    assert response.status_code == 413


@pytest.mark.skipif(not find_ffmpeg(), reason="ffmpeg needed to create a browser recording")
def test_browser_webm_recording_is_decoded_with_bounded_fallback(client, tmp_path):
    source, recording = tmp_path / "source.wav", tmp_path / "browser.webm"
    source.write_bytes(wav(0.1 * np.sin(2 * np.pi * 200 * np.arange(24000 * 2) / 24000)))
    subprocess.run(
        [find_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-c:a", "libopus", str(recording)], check=True, capture_output=True, timeout=20,
    )
    source.unlink()
    response = client.post(
        "/sample-quality/analyze", files={"file": ("browser.webm", recording.read_bytes(), "audio/webm")},
    )
    assert response.status_code == 200
    assert abs(response.json()["duration_seconds"] - 2) < 0.1
    assert response.json()["silence_fraction"] < 0.1


def test_transcript_check_never_downloads_missing_model(client, monkeypatch):
    from backend.routes import sample_quality

    model = SimpleNamespace(
        model_size="base", is_loaded=lambda: False, _is_model_cached=lambda size: False,
        transcribe=AsyncMock(), load_model_async=AsyncMock(),
    )
    monkeypatch.setattr(sample_quality, "get_whisper_model", lambda: model)
    response = client.post(
        "/sample-quality/transcribe", files={"file": ("sample.wav", wav(np.zeros(24000)), "audio/wav")}
    )
    assert response.status_code == 409
    assert "Models" in response.json()["detail"]
    model.transcribe.assert_not_called()
    model.load_model_async.assert_not_called()


def test_explicit_cached_transcript_check_returns_text_and_cleans_pcm(client, monkeypatch):
    from backend.routes import sample_quality

    seen = []

    async def transcribe(path, language, size):
        from pathlib import Path

        assert Path(path).is_file()
        seen.append((path, language, size))
        return "A test recording"

    model = SimpleNamespace(model_size="base", is_loaded=lambda: True, transcribe=transcribe)
    monkeypatch.setattr(sample_quality, "get_whisper_model", lambda: model)
    response = client.post(
        "/sample-quality/transcribe", data={"language": "en"},
        files={"file": ("sample.wav", wav(np.zeros(24000)), "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "A test recording"
    assert seen[0][1:] == ("en", "base")
