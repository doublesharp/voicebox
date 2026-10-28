import hashlib

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.storage import StudioStore


def test_audition_levels_are_bounded_cached_source_measurements(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Comparison")
    source = tmp_path / "long.wav"
    sf.write(source, np.full((48000 * 20, 2), 0.25), 48000, subtype="FLOAT")
    asset = store.add_asset(project["id"], source)
    before = hashlib.sha256(store.asset_path(asset["id"]).read_bytes()).hexdigest()
    app = FastAPI()
    # Local registration deliberately exercises root's eventual integration hook.
    from backend.studio import audition

    app.include_router(audition.make_audition_router(store), prefix="/studio/v1")
    client = TestClient(app)
    reads = []
    original = sf.SoundFile.read

    def read(audio, frames=-1, *args, **kwargs):
        reads.append(frames)
        assert 0 < frames <= 16384 // audio.channels
        return original(audio, frames, *args, **kwargs)

    monkeypatch.setattr(sf.SoundFile, "read", read)
    url = f"/studio/v1/assets/{asset['id']}/audition-level"
    response = client.get(url)
    assert response.status_code == 200
    result = response.json()
    assert result["method"] == "sample_rms_v1"
    assert result["rms"] == 0.25
    assert result["peak"] == 0.25
    assert result["frames"] == 48000 * 20
    assert sum(reads) == result["measured_frames"] <= 240000
    assert result["status"] == "measured"
    reads.clear()
    assert client.get(url).json() == result
    assert reads == []
    duplicate = store.add_asset(project["id"], source)
    assert duplicate["hash"] == asset["hash"]
    assert client.get(f"/studio/v1/assets/{duplicate['id']}/audition-level").json() == result
    assert reads == []
    assert hashlib.sha256(store.asset_path(asset["id"]).read_bytes()).hexdigest() == before
    store.asset_path(asset["id"]).unlink()
    assert client.get(url).status_code == 404
    assert client.get("/studio/v1/assets/missing/audition-level").status_code == 404


def test_silent_and_invalid_pcm_are_explicit(tmp_path):
    from backend.studio.audition import make_audition_router

    store = StudioStore(tmp_path / "store")
    project = store.create_project("Silence")
    app = FastAPI()
    app.include_router(make_audition_router(store), prefix="/studio/v1")
    client = TestClient(app)
    for name, value, status in (("silence", 0, 200), ("invalid", np.nan, 422)):
        source = tmp_path / (name + ".wav")
        sf.write(source, np.full(300, value, dtype="float64"), 24000, subtype="FLOAT")
        asset = store.add_asset(project["id"], source)
        result = client.get(f"/studio/v1/assets/{asset['id']}/audition-level")
        assert result.status_code == status
        if status == 200:
            assert result.json()["status"] == "silence"
            assert result.json()["rms"] == 0
