"""Filesystem health checks must not damage files while probing storage."""

from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes import health


@pytest.fixture
def health_client(tmp_path, monkeypatch):
    directories = {name: tmp_path / name for name in ("generations", "captures", "profiles", "data")}
    for name, path in directories.items():
        path.mkdir()
        monkeypatch.setattr(health.config, f"get_{name}_dir", lambda path=path: path)
    app = FastAPI()
    app.include_router(health.router)
    with TestClient(app) as client:
        yield client, directories


def test_filesystem_probe_preserves_existing_file_and_cleans_up(health_client):
    client, directories = health_client
    existing = directories["generations"] / ".voicebox_probe"
    existing.write_text("user data")
    before = {name: set(path.iterdir()) for name, path in directories.items()}

    response = client.get("/health/filesystem")

    assert response.status_code == 200
    assert response.json()["healthy"] is True
    assert all(item["writable"] for item in response.json()["directories"])
    assert existing.read_text() == "user data"
    assert {name: set(path.iterdir()) for name, path in directories.items()} == before


def test_missing_directory_is_reported_without_creating_it(health_client):
    client, directories = health_client
    directories["captures"].rmdir()
    response = client.get("/health/filesystem")
    assert response.status_code == 200
    assert response.json()["healthy"] is False
    missing = next(item for item in response.json()["directories"] if item["path"] == str(directories["captures"]))
    assert missing["exists"] is False
    assert missing["writable"] is False
    assert missing["error"] == "Directory does not exist"
    assert not directories["captures"].exists()


def test_low_disk_space_marks_health_unhealthy(health_client, monkeypatch):
    client, _ = health_client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _path: usage(1024 * 1024 * 1000, 0, 1024 * 1024 * 100))
    body = client.get("/health/filesystem").json()
    assert body["healthy"] is False
    assert body["disk_free_mb"] == 100
    assert all(item["writable"] for item in body["directories"])


def test_probe_does_not_follow_existing_symlink(health_client):
    client, directories = health_client
    target = directories["data"] / "keep.txt"
    target.write_text("important")
    link = directories["profiles"] / ".voicebox_probe"
    link.symlink_to(target)

    assert client.get("/health/filesystem").json()["healthy"] is True
    assert link.is_symlink()
    assert target.read_text() == "important"


def test_permission_error_is_reported_without_removing_other_files(health_client, monkeypatch):
    client, directories = health_client
    existing = directories["generations"] / ".voicebox_probe"
    existing.write_text("untouched")
    original = health.tempfile.NamedTemporaryFile

    def create_probe(*args, **kwargs):
        if kwargs.get("dir") == directories["generations"]:
            raise PermissionError("denied")
        return original(*args, **kwargs)

    monkeypatch.setattr(health.tempfile, "NamedTemporaryFile", create_probe)
    body = client.get("/health/filesystem").json()
    first = next(item for item in body["directories"] if item["path"] == str(directories["generations"]))
    assert body["healthy"] is False
    assert first["exists"] is True
    assert first["writable"] is False
    assert first["error"] == "Permission denied"
    assert existing.read_text() == "untouched"


def test_concurrent_checks_use_independent_probes(health_client, monkeypatch):
    client, directories = health_client
    original = health.tempfile.NamedTemporaryFile
    barrier, lock = Barrier(2, timeout=5), Lock()
    probes = []

    def create_probe(*args, **kwargs):
        probe = original(*args, **kwargs)
        if kwargs.get("dir") == directories["generations"]:
            with lock:
                probes.append(probe.name)
            barrier.wait()
        return probe

    monkeypatch.setattr(health.tempfile, "NamedTemporaryFile", create_probe)

    def check(_):
        # Separate clients have separate event loops, as separate server workers do.
        with TestClient(client.app) as concurrent_client:
            return concurrent_client.get("/health/filesystem")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(check, range(2)))
    assert all(response.json()["healthy"] for response in responses)
    assert len(set(probes)) == 2
    assert list(directories["generations"].iterdir()) == []
