"""The shared studio runtime follows the final configured application data root."""

from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes import StudioRuntime
from backend.studio.api import make_router


def test_router_registration_is_lazy_and_uses_final_data_directory(tmp_path):
    original = tmp_path / "original"
    final = tmp_path / "final"
    selected = {"path": original}
    runtime = StudioRuntime(data_dir=lambda: selected["path"])
    app = FastAPI()
    app.include_router(make_router(runtime.store, runtime.jobs))
    assert not original.exists()
    selected["path"] = final
    with TestClient(app) as client:
        created = client.post("/studio/v1/projects", json={"name": "Persisted", "script": "Hello"})
        assert created.status_code == 200
        assert client.get("/studio/v1/projects").json()["items"][0]["id"] == created.json()["id"]
    assert not original.exists()
    assert (final / "studio" / "studio.sqlite").is_file()


def test_concurrent_initialization_uses_one_store_and_job_manager(tmp_path):
    runtime = StudioRuntime(data_dir=lambda: tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: runtime.initialize(), range(12)))
    store, jobs = runtime._store, runtime._jobs
    runtime.initialize()
    assert runtime._store is store and runtime._jobs is jobs
    assert jobs.store is store
