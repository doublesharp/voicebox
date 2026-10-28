from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.studio.api import make_router
from backend.studio.storage import StudioStore


def test_projects_commands_and_conflicts(tmp_path):
    app = FastAPI()
    app.include_router(make_router(StudioStore(tmp_path)))
    with TestClient(app) as client:
        created = client.post("/studio/v1/projects", json={"name": "Demo", "script": "One\n\nTwo"})
        assert created.status_code == 200
        project = created.json()
        passage = project["document"]["passages"][0]
        payload = {
            "operation_id": "one",
            "expected_revision": 0,
            "type": "update_passage",
            "payload": {"passage_id": passage["id"], "text": "New"},
        }
        edited = client.post(f"/studio/v1/projects/{project['id']}/commands", json=payload)
        assert edited.status_code == 200 and edited.json()["revision"] == 1
        payload["operation_id"] = "two"
        conflict = client.post(f"/studio/v1/projects/{project['id']}/commands", json=payload)
        assert conflict.status_code == 409 and conflict.json()["detail"]["current_revision"] == 1


def test_archive_round_trip_owns_audio_and_remaps_project(tmp_path):
    import io
    import numpy as np
    import soundfile as sf

    app = FastAPI()
    app.include_router(make_router(StudioStore(tmp_path)))
    audio = io.BytesIO()
    sf.write(audio, np.zeros(200, dtype="float32"), 1000, format="WAV")
    with TestClient(app) as client:
        project = client.post("/studio/v1/projects", json={"name": "Demo", "script": "Hello"}).json()
        uploaded = client.post(
            f"/studio/v1/projects/{project['id']}/assets", files={"file": ("voice.wav", audio.getvalue(), "audio/wav")}
        )
        assert uploaded.status_code == 200
        waveform = client.get(f"/studio/v1/assets/{uploaded.json()['id']}/waveform?bins=32")
        assert waveform.status_code == 200
        assert waveform.json()["peaks"] == [[0.0, 0.0]] * 32
        preview = client.get(f"/studio/v1/assets/{uploaded.json()['id']}/preview?start_sample=0&frames=96")
        assert preview.status_code == 200
        assert sf.info(io.BytesIO(preview.content)).frames == 96
        archive = client.get(f"/studio/v1/projects/{project['id']}/archive")
        assert archive.status_code == 200
        imported = client.post(
            "/studio/v1/imports/archive", files={"file": ("project.zip", archive.content, "application/zip")}
        )
        assert imported.status_code == 200
        other = imported.json()
        assert other["id"] != project["id"] and other["assets"][0]["id"] != uploaded.json()["id"]
        assert client.get(f"/studio/v1/assets/{other['assets'][0]['id']}/audio").content == audio.getvalue()


def test_bad_archive_returns_validation_error_without_new_project(tmp_path):
    app = FastAPI()
    app.include_router(make_router(StudioStore(tmp_path)))
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.post(
            "/studio/v1/imports/archive", files={"file": ("broken.zip", b"not a zip", "application/zip")}
        )
        assert result.status_code == 422
        assert client.get("/studio/v1/projects").json()["items"] == []
