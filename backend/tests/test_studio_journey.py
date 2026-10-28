"""Real ASGI/SQLite/audio journey; only the ML engine is replaced by PCM."""

import asyncio
import hashlib
import io

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI

from backend.services import task_queue
from backend.studio.api import make_router
from backend.studio.jobs import StudioJobs
from backend.studio.storage import StudioStore


def fixture_capabilities(voice):
    return {"languages": ["en"], "expressive_tags": [], "supports_instruct": False, "supports_seed": False}


@pytest.mark.asyncio
async def test_three_passage_delivery_and_clean_profile_archive(tmp_path, monkeypatch):
    tasks = []

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    async def finish():
        await asyncio.gather(*tasks)
        tasks.clear()

    def voice(store, project_id, profile_id):
        return {
            "engine": "fixture",
            "model_size": "default",
            "voice_type": "preset",
            "preset_engine": "fixture",
            "preset_voice_id": "narrator",
        }

    async def synth(request):
        return np.sin(np.arange(2400) * 0.1).astype("float32") * 0.1, 24000

    store = StudioStore(tmp_path / "author", voice_resolver=voice)
    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: schedule(coro)
    )
    monkeypatch.setattr(task_queue, "create_background_task", schedule)
    app = FastAPI()
    app.include_router(make_router(store, jobs))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://studio.test") as client:
        response = await client.post(
            "/studio/v1/projects", json={"name": "Narration", "script": "First\n\nSecond\n\nThird"}
        )
        assert response.status_code == 200
        project = response.json()
        ident = project["id"]
        ids = [p["id"] for p in project["document"]["passages"]]

        async def command(kind, payload):
            nonlocal project
            response = await client.post(
                f"/studio/v1/projects/{ident}/commands",
                json={
                    "operation_id": f"command-{project['revision']}-{kind}",
                    "expected_revision": project["revision"],
                    "type": kind,
                    "payload": payload,
                },
            )
            assert response.status_code == 200, response.text
            project = response.json()

        await command("assign_voice", {"passage_ids": ids, "profile_id": "voice"})
        request = {"idempotency_key": "alternatives", "passage_ids": ids, "count": 2, "adopt": False}
        response = await client.post(f"/studio/v1/projects/{ident}/generation-jobs", json=request)
        assert response.status_code == 202
        assert len(response.json()["jobs"]) == 6
        repeated = await client.post(f"/studio/v1/projects/{ident}/generation-jobs", json=request)
        assert repeated.json() == response.json()
        await finish()
        project = (await client.get(f"/studio/v1/projects/{ident}")).json()
        assert len(project["takes"]) == 6
        for passage_id in ids:
            take = next(t for t in project["takes"] if t["passage_id"] == passage_id)
            await command("select_take", {"passage_id": passage_id, "take_id": take["id"], "replace_clips": False})
        await command("insert_passages", {"passage_ids": ids, "start_sample": 0, "gap_ms": 200})
        response = await client.post(
            f"/studio/v1/projects/{ident}/revisions",
            json={"operation_id": "checkpoint", "expected_revision": project["revision"], "name": "Approved narration"},
        )
        project = response.json()
        revision_id = project["revisions"][0]["id"]
        approved = await client.post(
            f"/studio/v1/projects/{ident}/approvals",
            json={"operation_id": "approval", "revision_id": revision_id, "note": "Local approval"},
        )
        assert approved.status_code == 200
        await command("update_passage", {"passage_id": ids[0], "text": "Changed draft"})
        response = await client.post(
            f"/studio/v1/projects/{ident}/render-jobs",
            json={"idempotency_key": "deliver", "revision_id": revision_id, "kind": "mix", "format": "wav"},
        )
        assert response.status_code == 202, response.text
        await finish()
        result = (await client.get("/studio/v1/jobs/" + response.json()["job"]["id"])).json()
        assert result["status"] == "completed", result
        deliverable = result["result"]["deliverables"][0]
        delivered = await client.get("/studio/v1/deliverables/" + deliverable["id"] + "/download")
        assert delivered.status_code == 200
        info = sf.info(io.BytesIO(delivered.content))
        assert (info.samplerate, info.channels, info.subtype, info.frames) == (48000, 2, "PCM_24", 33600)
        archive = await client.get(f"/studio/v1/projects/{ident}/archive")
        assert archive.status_code == 200, archive.text[:200]

    # This profile has no legacy DB, voice resolver or model cache.
    clean = StudioStore(tmp_path / "clean-profile")
    clean_app = FastAPI()
    clean_app.include_router(make_router(clean))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=clean_app), base_url="http://clean.test") as client:
        response = await client.post(
            "/studio/v1/imports/archive", files={"file": ("narration.zip", archive.content, "application/zip")}
        )
        assert response.status_code == 200, response.text
        restored = response.json()
        assert restored["id"] != ident and len(restored["takes"]) == 6
        assert restored["document"]["passages"][0]["text"] == "Changed draft"
        revision = restored["revisions"][0]
        assert revision["document"]["passages"][0]["text"] == "First"
        assert restored["approvals"][0]["revision_id"] == revision["id"]
        for asset in restored["assets"]:
            recording = await client.get("/studio/v1/assets/" + asset["id"] + "/audio")
            assert recording.status_code == 200
            assert hashlib.sha256(recording.content).hexdigest() == asset["hash"]
        head = await client.post(
            f"/studio/v1/projects/{restored['id']}/commands",
            json={
                "operation_id": "restore",
                "expected_revision": 0,
                "type": "restore_revision",
                "payload": {"revision_id": revision["id"]},
            },
        )
        assert head.status_code == 200
        assert head.json()["document"]["passages"][0]["text"] == "First"
