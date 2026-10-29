"""Corrupt generation snapshots fail archive validation, without server errors."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.storage import StudioStore
from backend.tests.test_studio_archive_scope import prepared, rewrite_archive


@pytest.mark.parametrize("bad", ["malformed", [], True])
def test_bad_frozen_passage_returns_validation_error_without_publication(tmp_path, bad):
    _, _, original = prepared(tmp_path)

    def corrupt(manifest):
        manifest["project"]["takes"][0]["request"]["passage"] = bad

    archive = rewrite_archive(original, tmp_path / "bad.zip", mutate=corrupt)
    store = StudioStore(tmp_path / "store")
    app = FastAPI()
    app.include_router(make_router(store))
    client = TestClient(app, raise_server_exceptions=False)
    result = client.post(
        "/studio/v1/imports/archive", files={"file": ("bad.zip", archive.read_bytes(), "application/zip")}
    )
    assert result.status_code == 422
    assert store.list_projects() == []
    assert list(store.asset_dir.iterdir()) == []
