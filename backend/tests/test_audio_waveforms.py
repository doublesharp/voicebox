"""Generation waveforms use bounded source reads and revision-aware cached peaks."""

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import Base, Generation, GenerationVersion, VoiceProfile, get_db
from backend.routes.audio import router


@pytest.fixture
def waveform_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    path = tmp_path / "audio.wav"
    sf.write(path, np.tile(np.array([[-0.25, 0.5]], dtype=np.float32), (100000, 1)), 24000, subtype="FLOAT")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.add(Generation(id="gen", profile_id="voice", text="Speech", audio_path="audio.wav"))
        db.add(GenerationVersion(id="version", generation_id="gen", label="original", audio_path="audio.wav"))
        db.commit()
    app = FastAPI()
    app.include_router(router)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, path
    engine.dispose()


def test_waveforms_use_bounded_reads_and_preserve_source_duration(waveform_client, monkeypatch):
    client, _ = waveform_client
    reads = []
    original = sf.SoundFile.read

    def read(self, frames=-1, *args, **kwargs):
        reads.append(frames)
        return original(self, frames, *args, **kwargs)

    monkeypatch.setattr(sf.SoundFile, "read", read)
    response = client.get("/audio/gen/waveform?bins=32")
    assert response.status_code == 200
    data = response.json()
    assert data["frames"] == 100000
    assert data["channels"] == 2
    assert len(data["peaks"]) == 32
    assert data["peaks"][0] == [-0.25, 0.5]
    assert reads
    assert all(0 < size <= 65536 for size in reads)


def test_waveform_cache_tracks_file_revisions_and_checks_missing_sources(waveform_client, monkeypatch):
    client, path = waveform_client
    first = client.get("/audio/version/version/waveform?bins=32")
    assert first.status_code == 200
    sf.write(path, np.full(48000, 0.125, dtype=np.float32), 24000, subtype="FLOAT")
    changed = client.get("/audio/version/version/waveform?bins=32").json()
    assert changed["frames"] == 48000
    assert changed["peaks"][0] == [0.125, 0.125]
    path.unlink()
    assert client.get("/audio/version/version/waveform?bins=32").status_code == 404
    assert client.get("/audio/missing/waveform").status_code == 404


def test_warm_envelope_skips_source_scan(waveform_client, monkeypatch):
    from backend.utils import waveforms

    client, _ = waveform_client
    first = client.get("/audio/gen/waveform?bins=32")
    assert first.status_code == 200
    monkeypatch.setattr(waveforms, "_envelope", lambda *_: pytest.fail("warm peak cache rescanned audio"))
    assert client.get("/audio/gen/waveform?bins=32").json() == first.json()


def test_ffmpeg_only_source_uses_bounded_peak_decode(waveform_client):
    import shutil
    import subprocess

    from backend.utils.waveforms import waveform

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    _, path = waveform_client
    source = path.with_suffix(".m4a")
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-c:a", "aac", str(source)], check=True)
    data = waveform(source, path.parent / "fallback-peaks", 32)
    assert 3 < data["frames"] / data["sample_rate"] < 5
    assert len(data["peaks"]) == 32
    assert all(np.isfinite(pair).all() for pair in data["peaks"])


def test_waveform_decoder_uses_desktop_ffmpeg_location_with_minimal_path(tmp_path, monkeypatch):
    from pathlib import Path

    from backend.utils import waveforms
    from backend.utils.ffmpeg import find_ffmpeg

    executable = find_ffmpeg()
    if executable is None:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "desktop.wav"
    sf.write(source, np.zeros(80, dtype=np.float32), 8000)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("VOICEBOX_FFMPEG_DIR", str(Path(executable).parent))
    blocks = list(waveforms._decoded_blocks(source))
    assert sum(len(block) for block in blocks) == 240
