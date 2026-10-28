import asyncio
import numpy as np
import pytest
from backend.studio.storage import StudioStore
from backend.studio.jobs import StudioJobs


def fixture_capabilities(voice):
    return {"languages": ["en"], "expressive_tags": [], "supports_instruct": False, "supports_seed": False}


@pytest.mark.asyncio
async def test_late_generation_is_saved_but_cannot_override_newer_intent(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Job", script="Hello")
    p = project["document"]["passages"][0]
    started = asyncio.Event()
    release = asyncio.Event()

    async def synth(request):
        started.set()
        await release.wait()
        return np.zeros(1000, dtype="float32"), 1000

    pending = []
    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: pending.append(coro)
    )
    first = jobs.submit(project["id"], "request", [p["id"]], 1, True)
    assert jobs.submit(project["id"], "request", [p["id"]], 1, True) == first
    task = asyncio.create_task(pending.pop())
    await started.wait()
    store.command(project["id"], "edit", 0, "update_passage", {"passage_id": p["id"], "text": "Edited"})
    release.set()
    await task
    project = store.get_project(project["id"])
    assert len(project["takes"]) == 1
    assert project["document"]["passages"][0]["selected_take_id"] is None
    assert project["jobs"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_keeps_running_work_from_adopting_and_retry_is_explicit(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Job", script="Hello")
    p = project["document"]["passages"][0]
    started = asyncio.Event()
    release = asyncio.Event()

    async def synth(request):
        started.set()
        await release.wait()
        return np.zeros(100, dtype="float32"), 1000

    queued = []
    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: queued.append(coro)
    )
    job = jobs.submit(project["id"], "request", [p["id"]], 1, True)[0]
    task = asyncio.create_task(queued.pop())
    await started.wait()
    assert jobs.cancel(job["id"])["status"] == "cancelling"
    assert not task.done()
    release.set()
    await task
    assert jobs.get(job["id"])["status"] == "cancelled"
    assert store.get_project(project["id"])["takes"] == []
    with pytest.raises(ValueError):
        jobs.retry(job["id"])


@pytest.mark.asyncio
async def test_restore_invalidates_inflight_selection(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Job", script="Hello")
    pid = project["document"]["passages"][0]["id"]
    project = store.checkpoint(project["id"], "rev", 0, "Approved")
    started = asyncio.Event()
    release = asyncio.Event()

    async def synth(request):
        started.set()
        await release.wait()
        return np.zeros(100, dtype="float32"), 1000

    queued = []
    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: queued.append(coro)
    )
    jobs.submit(project["id"], "request", [pid], 1, True)
    task = asyncio.create_task(queued.pop())
    await started.wait()
    store.command(project["id"], "restore", 0, "restore_revision", {"revision_id": project["revisions"][0]["id"]})
    release.set()
    await task
    project = store.get_project(project["id"])
    assert len(project["takes"]) == 1 and project["document"]["passages"][0]["selected_take_id"] is None


@pytest.mark.asyncio
async def test_shared_inference_slot_remains_owned_after_caller_cancel():
    from backend.utils.chunked_tts import generate_chunked

    first_started = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()

    class Backend:
        async def generate(self, text, *args):
            if text == "first":
                first_started.set()
                await release.wait()
            else:
                second_started.set()
            return np.zeros(10, dtype="float32"), 1000

    backend = Backend()
    first = asyncio.create_task(generate_chunked(backend, "first", {}))
    await first_started.wait()
    first.cancel()
    second = asyncio.create_task(generate_chunked(backend, "second", {}))
    await asyncio.sleep(0.02)
    assert not second_started.is_set() and not first.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second


@pytest.mark.asyncio
async def test_render_frozen_revision_publishes_managed_audio(tmp_path):
    from backend.studio.delivery import StudioDelivery

    store = StudioStore(tmp_path)
    project = store.create_project("Render", script="Hello")
    pid = project["document"]["passages"][0]["id"]
    queued = []

    async def synth(request):
        return np.zeros(1000, dtype="float32"), 1000

    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: queued.append(coro)
    )
    jobs.submit(project["id"], "generate", [pid], 1, True)
    await queued.pop()
    project = store.get_project(project["id"])
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": [pid]})
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Ready")
    work = []
    delivery = StudioDelivery(store, schedule=lambda coro: work.append(coro))
    job = delivery.submit(project["id"], "render", project["revisions"][0]["id"], "mix", "wav")
    await work.pop()
    result = jobs.get(job["id"])
    assert result["status"] == "completed", result["error"]
    output = result["result"]["deliverables"][0]
    path, _ = delivery.path(output["id"])
    import soundfile as sf

    assert sf.info(str(path)).samplerate == 48000


@pytest.mark.asyncio
async def test_startup_resumes_queued_and_marks_running_interrupted(tmp_path):
    store = StudioStore(tmp_path)
    p = store.create_project("Recover", script="A\n\nB")
    ids = [x["id"] for x in p["document"]["passages"]]
    pending = []

    async def synth(request):
        return np.zeros(100, dtype="float32"), 1000

    first = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: pending.append(coro)
    )
    submitted = first.submit(p["id"], "request", ids, 1, False)
    for coro in pending:
        coro.close()
    pending.clear()
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (submitted[1]["id"],))
    first.recover_on_startup()
    assert first.get(submitted[1]["id"])["status"] == "interrupted"
    assert len(pending) == 1
    await pending.pop()
    assert first.get(submitted[0]["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_archive_round_trip_preserves_take_and_clip_graph(tmp_path):
    from backend.studio.archive import write_archive
    from backend.studio.portability import import_archive

    store = StudioStore(tmp_path / "studio")
    project = store.create_project("Portable", script="Hello")
    pid = project["document"]["passages"][0]["id"]
    pending = []

    async def synth(request):
        return np.zeros(100, dtype="float32"), 1000

    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: pending.append(coro)
    )
    jobs.submit(project["id"], "gen", [pid], 1, True)
    await pending.pop()
    project = store.get_project(project["id"])
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": [pid]})
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Review")
    archive = tmp_path / "project.zip"
    write_archive(project, project["assets"], store.asset_path, archive)
    imported = import_archive(store, archive)
    clip = imported["document"]["clips"][0]
    take = imported["takes"][0]
    assert clip["take_id"] == take["id"] and clip["asset_id"] == take["asset_id"]
    assert imported["document"]["passages"][0]["selected_take_id"] == take["id"]
    assert clip["asset_id"] != project["assets"][0]["id"]


@pytest.mark.asyncio
async def test_startup_dispatches_queued_render_to_delivery(tmp_path, monkeypatch):
    from backend.studio.delivery import StudioDelivery
    from backend.services import task_queue

    store = StudioStore(tmp_path)
    project = store.create_project("Render recovery", script="Hello")
    pending = []

    async def synth(request):
        return np.zeros(100, dtype="float32"), 1000

    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: pending.append(coro)
    )
    pid = project["document"]["passages"][0]["id"]
    jobs.submit(project["id"], "generate", [pid], 1, True)
    await pending.pop()
    project = store.get_project(project["id"])
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": [pid]})
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Ready")
    delivery = StudioDelivery(store, schedule=lambda coro: pending.append(coro))
    job = delivery.submit(project["id"], "render", project["revisions"][0]["id"], "mix", "wav")
    pending.pop().close()
    monkeypatch.setattr(task_queue, "create_background_task", lambda coro: pending.append(coro))
    jobs.recover_on_startup()
    assert len(pending) == 1
    await pending.pop()
    assert jobs.get(job["id"])["status"] == "completed"
