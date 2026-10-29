"""Archive scope is preserved through the central HTTP export/import path."""

import io
import json
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.storage import StudioStore
from backend.tests.test_studio_archive_scope import prepared


def test_archive_scope_http_and_missing_reference_roundtrip(tmp_path):
    _, _, complete = prepared(tmp_path)
    app = FastAPI()
    store = StudioStore(tmp_path / "clean")
    app.include_router(make_router(store))
    client = TestClient(app)
    first = client.post(
        "/studio/v1/imports/archive", files={"file": ("all.zip", complete.read_bytes(), "application/zip")}
    )
    assert first.status_code == 200
    project = first.json()
    path = f"/studio/v1/projects/{project['id']}/archive"
    assert client.get(path + "?take_scope=invalid").status_code == 422
    exported = client.get(path + "?take_scope=used&include_regeneration_references=false")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["scope"]["take_scope"] == "used"
        assert manifest["scope"]["include_regeneration_references"] is False
        assert len(manifest["assets"]) == 1
        assert manifest["project"]["document"]["passages"][0]["voice_snapshot"]["unavailable_references"]
    second = client.post(
        "/studio/v1/imports/archive", files={"file": ("playback.zip", exported.content, "application/zip")}
    )
    assert second.status_code == 200
    restored = second.json()
    audio = client.get(f"/studio/v1/assets/{restored['assets'][0]['id']}/audio")
    assert audio.status_code == 200
    assert audio.content == store.asset_path(project["takes"][0]["asset_id"]).read_bytes()
