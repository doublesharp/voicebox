"""Sample exports preserve recordings and produce playable MP3 files."""

import json
import subprocess

import numpy as np
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from backend import config
from backend.database import Base, ProfileSample, VoiceProfile, get_db
from backend.routes.audio import router
from backend.utils import ffmpeg
from backend.utils.audio import save_audio


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    samples = tmp_path / "profiles"
    samples.mkdir()
    tone = 0.2 * np.sin(np.linspace(0, 2 * np.pi * 440 * 2, 48000)).astype(np.float32)
    save_audio(tone, str(samples / "sample.wav"), 24000)
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(VoiceProfile(id="profile-1", name="My Voice"))
        db.add(
            ProfileSample(
                id="sample-1", profile_id="profile-1", audio_path="profiles/sample.wav", reference_text="Hello there!"
            )
        )
        db.add(
            ProfileSample(
                id="missing-file",
                profile_id="profile-1",
                audio_path="profiles/missing.wav",
                reference_text="Missing file",
            )
        )
        db.commit()
    app = FastAPI()
    app.include_router(router)

    def get_session():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = get_session
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def test_wav_export_preserves_recording(client, tmp_path):
    response = client.get("/samples/sample-1/export-audio?format=wav")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content == (tmp_path / "profiles/sample.wav").read_bytes()
    assert 'filename="sample-Hello there-sample-1.wav"' in response.headers["content-disposition"]


@pytest.mark.parametrize("sample_id", ["unknown", "missing-file"])
def test_missing_recording_is_404(client, sample_id):
    assert client.get(f"/samples/{sample_id}/export-audio").status_code == 404


def test_unsupported_format_is_400(client):
    assert client.get("/samples/sample-1/export-audio?format=ogg").status_code == 400


def test_missing_ffmpeg_has_actionable_error(client, monkeypatch):
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", lambda name="ffmpeg": None)
    response = client.get("/samples/sample-1/export-audio")
    assert response.status_code == 503
    assert response.json()["detail"] == ffmpeg.MISSING_MESSAGE


def test_failed_encode_cleans_up(client, monkeypatch):
    from backend.routes import audio

    outputs = []

    def fail_encode(source, output, fmt):
        outputs.append(output)
        output.write_bytes(b"partial MP3")
        raise RuntimeError("Encoder failed")

    monkeypatch.setattr(audio, "encode_audio", fail_encode)
    response = client.get("/samples/sample-1/export-audio")
    assert response.status_code == 503
    assert response.json()["detail"] == "Encoder failed"
    assert len(outputs) == 1
    assert not outputs[0].exists()


@pytest.mark.skipif(not ffmpeg.find_ffmpeg() or not ffmpeg.find_ffmpeg("ffprobe"), reason="ffmpeg required")
def test_mp3_export_is_playable_and_preserves_source(client, tmp_path):
    original = (tmp_path / "profiles/sample.wav").read_bytes()
    response = client.get("/samples/sample-1/export-audio")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert 'filename="sample-Hello there-sample-1.mp3"' in response.headers["content-disposition"]
    output = tmp_path / "export.mp3"
    output.write_bytes(response.content)
    probe = subprocess.run(
        [ffmpeg.find_ffmpeg("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    assert info["format"]["format_name"] == "mp3"
    assert info["streams"][0]["codec_name"] == "mp3"
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.2
    assert (tmp_path / "profiles/sample.wav").read_bytes() == original
    assert sorted(p.name for p in (tmp_path / "profiles").iterdir()) == ["sample.wav"]
