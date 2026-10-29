"""Public Render API forwards validated processing choices."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.storage import StudioStore


def test_render_api_preserves_processing_selection(tmp_path, monkeypatch):
    from backend.studio import delivery

    captured = []

    class Delivery:
        def __init__(self, store):
            pass

        def submit(self, project_id, **body):
            captured.append(body)
            return {"id": "render"}

    monkeypatch.setattr(delivery, "StudioDelivery", Delivery)
    app = FastAPI()
    app.include_router(make_router(StudioStore(tmp_path)))
    client = TestClient(app)
    body = dict(idempotency_key="render", revision_id="checkpoint", kind="stems", format="wav", processing="processed")
    result = client.post("/studio/v1/projects/project/render-jobs", json=body)
    assert result.status_code == 202
    assert captured[0].get("processing") == "processed"
    assert (
        client.post("/studio/v1/projects/project/render-jobs", json={**body, "processing": "invalid"}).status_code
        == 422
    )
