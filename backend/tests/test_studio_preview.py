import io
import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.studio.storage import StudioStore


def test_preview_returns_owned_bounded_phase_correct_wav(tmp_path):
    from backend.studio.preview import make_preview_router
    from backend.studio.rendering import _source_range

    store = StudioStore(tmp_path / "store")
    project = store.create_project("Preview")
    source = tmp_path / "source.wav"
    sf.write(source, np.sin(np.arange(24000) / 30), 24000, subtype="FLOAT")
    asset = store.add_asset(project["id"], source)
    app = FastAPI()
    app.include_router(make_preview_router(store), prefix="/studio/v1")
    client = TestClient(app)
    url = "/studio/v1/assets/" + asset["id"] + "/preview"
    response = client.get(url, params={"start_sample": 12001, "frames": 1234})
    assert response.status_code == 200
    data, rate = sf.read(io.BytesIO(response.content), always_2d=True)
    assert rate == 48000 and len(data) == 1234
    expected = _source_range(source, sf.info(source), 48000, 12001, 13235)
    np.testing.assert_allclose(data, expected, atol=1e-6)
    for params in (
        {"start_sample": -1, "frames": 2},
        {"start_sample": 0, "frames": 240001},
        {"start_sample": 47999, "frames": 2},
        {"start_sample": 0, "frames": 0},
    ):
        assert client.get(url, params=params).status_code == 422
    assert client.get("/studio/v1/assets/missing/preview", params={"start_sample": 0, "frames": 1}).status_code == 404
