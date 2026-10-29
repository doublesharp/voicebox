"""Recoverable storage operations are connected to the central HTTP router."""

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.storage import StudioStore


def test_central_storage_preview_cleanup_and_restore(tmp_path):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Unused import")
    audio = tmp_path / "unused.wav"
    sf.write(audio, np.full(80, 0.1), 48000)
    asset = store.add_asset(project["id"], audio)
    original = store.asset_path(asset["id"]).read_bytes()
    app = FastAPI()
    app.include_router(make_router(store))
    client = TestClient(app)
    path = f"/studio/v1/projects/{project['id']}/storage"
    preview = client.get(path)
    assert preview.status_code == 200
    assert [item["id"] for item in preview.json()["candidates"]] == [asset["id"]]
    body = dict(operation_id="cleanup-http", preview_token=preview.json()["preview_token"], asset_ids=[asset["id"]])
    moved = client.post(path + "/cleanup", json=body)
    assert moved.status_code == 200
    assert client.post(path + "/cleanup", json=body).json() == moved.json()
    trash = client.get(path).json()["trash"]
    assert len(trash) == 1
    restored = client.post(path + "/restore", json=dict(operation_id="restore-http", receipt_ids=[trash[0]["id"]]))
    assert restored.status_code == 200
    assert store.asset_path(asset["id"]).read_bytes() == original
    assert sf.info(store.asset_path(asset["id"])).frames == 80
