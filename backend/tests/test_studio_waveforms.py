import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.studio.storage import StudioStore
from backend.studio.waveforms import make_waveform_router


def test_waveform_http_real_pcm_and_cache(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Wave")
    source = tmp_path / "source.wav"
    samples = np.zeros((64, 2), dtype="float32")
    samples[0] = [-0.75, 0.5]
    samples[63] = [-0.25, 0.875]
    sf.write(source, samples, 8000, subtype="FLOAT")
    asset = store.add_asset(project["id"], source)
    app = FastAPI()
    app.include_router(make_waveform_router(store))
    with TestClient(app) as client:
        url = f"/assets/{asset['id']}/waveform"
        result = client.get(url, params={"bins": 32})
        assert result.status_code == 200
        data = result.json()
        assert (data["sample_rate"], data["frames"], data["channels"]) == (8000, 64, 2)
        assert data["peaks"][0] == [-0.75, 0.5]
        assert data["peaks"][-1] == [-0.25, 0.875]
        monkeypatch.setattr(sf, "SoundFile", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache missed")))
        assert client.get(url, params={"bins": 32}).json() == data
        assert client.get(url, params={"bins": 31}).status_code == 422
        assert client.get("/assets/missing/waveform").status_code == 404
        store.asset_path(asset["id"]).unlink()
        assert client.get(url, params={"bins": 32}).status_code == 404


def test_reads_bounded_blocks_and_only_evicts_cache_entries(tmp_path, monkeypatch):
    from backend.studio import waveforms

    store = StudioStore(tmp_path / "store")
    project = store.create_project("Long")
    source = tmp_path / "source.wav"
    samples = np.zeros((80000, 2), dtype="float32")
    samples[40000, 1] = 0.75
    sf.write(source, samples, 48000, subtype="FLOAT")
    asset = store.add_asset(project["id"], source)
    real_soundfile = sf.SoundFile
    reads = []

    class Reader:
        def __init__(self, path):
            self.inner = real_soundfile(path)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def __len__(self):
            return len(self.inner)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def read(self, frames, **kwargs):
            reads.append(frames)
            return self.inner.read(frames, **kwargs)

    monkeypatch.setattr(sf, "SoundFile", Reader)
    monkeypatch.setattr(waveforms, "_CACHE_LIMIT", 1)
    cache = store.root / "waveform-cache"
    cache.mkdir()
    unrelated = cache / "keep.txt"
    unrelated.write_text("keep")
    app = FastAPI()
    app.include_router(waveforms.make_waveform_router(store))
    with TestClient(app) as client:
        result = client.get(f"/assets/{asset['id']}/waveform", params={"bins": 32})
        assert result.status_code == 200
        assert max(peak[1] for peak in result.json()["peaks"]) == 0.75
    assert len(reads) > 1 and max(reads) <= 32768
    assert unrelated.read_text() == "keep"
    assert not list(cache.glob("*.json"))
