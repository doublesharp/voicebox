import asyncio
import json

import numpy as np
import pytest
import soundfile as sf

from backend.studio import voices
from backend.studio.jobs import StudioJobs
from backend.studio.storage import StudioStore, encode


def assigned(store, project, text="Hello", instruct="whisper"):
    passage = project["document"]["passages"][0]
    passage["text"] = text
    passage["direction"]["instruct"] = instruct
    reference = store.asset_dir / "fixture-reference.wav"
    sf.write(reference, np.zeros(100, dtype="float32"), 1000)
    asset = store.add_asset(project["id"], reference)
    reference.unlink()
    passage["voice_snapshot"] = {
        "engine": "qwen",
        "model_size": "1.7B",
        "voice_type": "cloned",
        "samples": [{"asset_id": asset["id"], "reference_text": "Hello"}],
    }
    with store.connect() as db:
        db.execute("UPDATE projects SET document=? WHERE id=?", (encode(project["document"]), project["id"]))
    return passage["id"]


def test_capabilities_follow_registered_variants_and_filter_unknown():
    all_engines = voices.engines()
    base = next(x for x in all_engines if x["id"] == "qwen")
    custom = next(x for x in all_engines if x["id"] == "qwen_custom_voice")
    assert not base["supports_instruct"]
    assert custom["supports_instruct"]
    assert base["registry_identity"]["hf_repo_id"]
    assert base["streaming"]["qualified"] is False
    assert next(x for x in all_engines if x["id"] == "omnivoice")["commercial_use"] == "restricted"
    assert all(x["commercial_use"] == "allowed" for x in voices.engines(commercial_only=True))


@pytest.mark.asyncio
async def test_submission_freezes_effective_direction_and_attempt_lineage(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Frozen", script="Hello")
    pid = assigned(store, project)
    pending, seen = [], []

    async def synth(request):
        seen.append(request)
        return np.zeros(100, dtype="float32"), 1000

    jobs = StudioJobs(store, synth=synth, enqueue=lambda ident, coro: pending.append(coro))
    job = jobs.submit(project["id"], "key", [pid], 1, True)[0]
    assert jobs.get(job["id"])["phase"] == "queued"
    await pending.pop()
    assert seen[0]["passage"]["direction"]["instruct"] == "whisper"
    assert seen[0]["effective_direction"]["instruct"] is None
    result = jobs.get(job["id"])
    assert result["phase"] == "completed"
    assert len(result["attempts"]) == 1
    take = store.get_project(project["id"])["takes"][0]
    assert take["request"]["creation_job_id"] == job["id"]
    assert take["request"]["creation_attempt"] == 1


def test_batch_rejects_unsupported_tag_without_mutating_text(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Batch", script="Hello\n\nGoodbye")
    pid = assigned(store, project, text="Hello [laugh]")
    pending = []
    jobs = StudioJobs(store, synth=lambda r: None, enqueue=lambda ident, coro: pending.append(coro))
    with pytest.raises(ValueError, match=r"\[laugh\]"):
        jobs.submit(project["id"], "key", [pid], 1, False)
    assert not pending
    assert store.get_project(project["id"])["document"]["passages"][0]["text"] == "Hello [laugh]"


@pytest.mark.asyncio
async def test_retry_records_attempts_and_preserves_frozen_request(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Retry", script="One")
    pid = assigned(store, project, text="One [chapter 2]")
    pending, calls = [], []

    async def synth(request):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("engine failed")
        return np.zeros(100, dtype="float32"), 1000

    jobs = StudioJobs(store, synth=synth, enqueue=lambda ident, coro: pending.append(coro))
    job = jobs.submit(project["id"], "retry", [pid], 1, False)[0]
    await pending.pop()
    assert jobs.get(job["id"])["attempts"][0]["phase"] == "failed"
    store.command(project["id"], "edit", 0, "update_passage", {"passage_id": pid, "text": "Changed"})
    jobs.retry(job["id"])
    await pending.pop()
    record = jobs.get(job["id"])
    assert [a["phase"] for a in record["attempts"]] == ["failed", "completed"]
    assert calls[1]["passage"]["text"] == "One [chapter 2]"
    assert store.get_project(project["id"])["takes"][0]["request"]["creation_attempt"] == 2


def test_migration_three_retains_v2_request_and_backup(tmp_path):
    import sqlite3

    from backend.studio.migrations import migrate

    path = tmp_path / "studio.sqlite"
    migrate(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE job_phase_events")
        db.execute("DROP TABLE job_attempts")
        db.execute("PRAGMA user_version=2")
        db.execute("INSERT INTO job_requests VALUES ('project','key','request','response')")
    migrate(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute("SELECT request,response FROM job_requests").fetchone() == ("request", "response")
    backup = next((tmp_path / "backups").glob("studio-v2-*.sqlite"))
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT request,response FROM job_requests").fetchone() == ("request", "response")


def test_batch_validates_later_passage_atomically(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Batch", script="First\n\nSecond")
    first = assigned(store, project)
    project = store.get_project(project["id"])
    second = project["document"]["passages"][1]
    second["text"] = "Second [cough]"
    second["voice_snapshot"] = project["document"]["passages"][0]["voice_snapshot"]
    with store.connect() as db:
        db.execute("UPDATE projects SET document=? WHERE id=?", (encode(project["document"]), project["id"]))
    queued = []
    jobs = StudioJobs(store, synth=lambda request: None, enqueue=lambda ident, coro: queued.append(coro))
    with pytest.raises(ValueError, match=r"\[cough\]"):
        jobs.submit(project["id"], "batch", [first, second["id"]], 2, False)
    assert queued == []
    assert store.get_project(project["id"])["jobs"] == []


@pytest.mark.asyncio
async def test_cancelled_custom_synth_drains_and_records_attempt(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Cancel", script="Hello")
    pid = assigned(store, project)
    started, release = asyncio.Event(), asyncio.Event()

    async def synth(request):
        started.set()
        await release.wait()
        return np.zeros(100, dtype="float32"), 1000

    pending = []
    jobs = StudioJobs(store, synth=synth, enqueue=lambda ident, coro: pending.append(coro))
    job = jobs.submit(project["id"], "cancel", [pid], 1, True)[0]
    task = asyncio.create_task(pending.pop())
    await started.wait()
    assert jobs.get(job["id"])["phase"] == "running"  # custom adapter has no asserted model phases
    assert jobs.cancel(job["id"])["phase"] == "cancelling"
    assert not task.done()
    release.set()
    await task
    result = jobs.get(job["id"])
    assert result["phase"] == "cancelled"
    assert result["attempts"][0]["phase"] == "cancelled"
    assert not store.get_project(project["id"])["takes"]


@pytest.mark.asyncio
async def test_real_adapter_boundary_records_loading_and_generating(tmp_path, monkeypatch):
    from backend import backends

    store = StudioStore(tmp_path)
    project = store.create_project("Phases", script="Hello")
    pid = assigned(store, project)
    loading, generating = asyncio.Event(), asyncio.Event()
    load_release, generate_release = asyncio.Event(), asyncio.Event()

    async def noop(*args):
        pass

    async def load(*args):
        loading.set()
        await load_release.wait()

    class Model:
        async def create_voice_prompt(self, *args, **kwargs):
            return {}, None

        async def generate(self, *args, **kwargs):
            generating.set()
            await generate_release.wait()
            return np.zeros(100, dtype="float32"), 1000

    monkeypatch.setattr(backends, "ensure_model_cached_or_raise", noop)
    monkeypatch.setattr(backends, "load_engine_model", load)
    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda engine: Model())
    pending = []
    jobs = StudioJobs(store, enqueue=lambda ident, coro: pending.append(coro))
    job = jobs.submit(project["id"], "phases", [pid], 1, False)[0]
    task = asyncio.create_task(pending.pop())
    await loading.wait()
    assert jobs.get(job["id"])["phase"] == "loading_model"
    load_release.set()
    await generating.wait()
    assert jobs.get(job["id"])["phase"] == "generating"
    generate_release.set()
    await task
    attempt = jobs.get(job["id"])["attempts"][0]
    assert [event["phase"] for event in attempt["phases"]] == [
        "running",
        "loading_model",
        "generating",
        "processing",
        "completed",
    ]


@pytest.mark.asyncio
async def test_legacy_queued_request_is_upgraded_without_changing_replay(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Legacy", script="Hello")
    pid = assigned(store, project)
    pending, seen = [], []

    async def synth(request):
        seen.append(request)
        return np.zeros(100, dtype="float32"), 1000

    jobs = StudioJobs(store, synth=synth, enqueue=lambda ident, coro: pending.append(coro))
    response = jobs.submit(project["id"], "legacy", [pid], 1, False)
    with store.connect() as db:
        frozen = json.loads(db.execute("SELECT request FROM jobs WHERE id=?", (response[0]["id"],)).fetchone()[0])
        frozen.pop("capabilities")
        frozen.pop("effective_direction")
        frozen["passage"]["voice_snapshot"].pop("capabilities")
        db.execute("UPDATE jobs SET request=? WHERE id=?", (encode(frozen), response[0]["id"]))
    await pending.pop()
    assert seen[0]["effective_direction"]["instruct"] is None
    assert jobs.submit(project["id"], "legacy", [pid], 1, False) == response
    with store.connect() as db:
        assert (
            json.loads(db.execute("SELECT request FROM jobs WHERE id=?", (response[0]["id"],)).fetchone()[0])[
                "capabilities"
            ]["registry_identity"]["engine"]
            == "qwen"
        )


def test_changed_registry_snapshot_requires_reassignment(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Stale", script="Hello")
    pid = assigned(store, project)
    project = store.get_project(project["id"])
    project["document"]["passages"][0]["voice_snapshot"]["capabilities"] = {"supports_instruct": True}
    with store.connect() as db:
        db.execute("UPDATE projects SET document=? WHERE id=?", (encode(project["document"]), project["id"]))
    jobs = StudioJobs(store, enqueue=lambda ident, coro: coro.close())
    with pytest.raises(ValueError, match="reassign"):
        jobs.submit(project["id"], "stale", [pid], 1, False)
    assert store.get_project(project["id"])["jobs"] == []
