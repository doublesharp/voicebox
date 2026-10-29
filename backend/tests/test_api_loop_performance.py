"""API responsiveness and bounded database ownership at streaming boundaries."""

import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Capture, Generation
from backend.routes.generations import get_generation_status
from backend.services import captures
from backend.studio.api import Generate, make_router
from backend.studio.jobs import StudioJobs
from backend.studio.storage import StudioStore


@pytest.mark.asyncio
async def test_generation_status_returns_connection_before_yield_and_reads_live_updates(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'events.sqlite'}", pool_size=1, max_overflow=0, pool_timeout=0.05)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Generation(id="active", profile_id="voice", text="Queued", status="generating", source="mcp"))
        db.commit()
        response = await get_generation_status("active", db)
        try:
            assert '"source": "mcp"' in await anext(response.body_iterator)
            assert engine.pool.checkedout() == 0, "SSE retained its database connection between events"
            with Session(engine) as writer:
                writer.get(Generation, "active").status = "completed"
                writer.commit()
            assert '"status": "completed"' in await anext(response.body_iterator)
            assert engine.pool.checkedout() == 0
        finally:
            await response.body_iterator.aclose()
    engine.dispose()


@pytest.mark.asyncio
async def test_capture_decode_runs_off_loop_without_moving_database_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'capture.sqlite'}")
    Base.metadata.create_all(engine)
    loop_thread = threading.get_ident()
    decoded = []

    def decode(_):
        decoded.append(threading.get_ident())
        return np.zeros(8, dtype=np.float32), 8000

    async def transcribe(*_):
        return "Words"

    monkeypatch.setattr(captures, "load_audio", decode)
    monkeypatch.setattr(
        captures, "get_whisper_model", lambda: SimpleNamespace(model_size="tiny", transcribe=transcribe)
    )
    with Session(engine) as db:
        original_commit = db.commit

        def commit():
            assert threading.get_ident() == loop_thread
            original_commit()

        monkeypatch.setattr(db, "commit", commit)
        result = await captures.create_capture(
            audio_bytes=b"wav", filename="voice.wav", source="file", language="en", stt_model="tiny", db=db
        )
        assert result.transcript_raw == "Words"
        assert db.query(Capture).count() == 1
    engine.dispose()
    assert decoded
    assert loop_thread not in decoded, "capture decode blocked the API event loop"


@pytest.mark.asyncio
async def test_studio_admission_runs_off_loop_but_enqueue_stays_on_loop(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Demo", script="Hello")
    loop_thread = threading.get_ident()
    resolved, enqueued = [], []

    def capabilities(_):
        resolved.append(threading.get_ident())
        return {"languages": ["en"], "expressive_tags": [], "supports_instruct": False, "supports_seed": False}

    def enqueue(ident, coro):
        enqueued.append(threading.get_ident())
        coro.close()

    jobs = StudioJobs(store, enqueue=enqueue, capability_resolver=capabilities)
    route = next(
        r.endpoint for r in make_router(store, jobs).routes if getattr(r, "path", "").endswith("/generation-jobs")
    )
    result = await route(
        project["id"],
        Generate(idempotency_key="once", passage_ids=[project["document"]["passages"][0]["id"]], count=1, adopt=False),
    )
    assert len(result["jobs"]) == 1
    assert resolved
    assert loop_thread not in resolved, "Studio admission blocked the API loop"
    assert enqueued == [loop_thread]


@pytest.mark.asyncio
async def test_cancelled_capture_drains_decoder_before_removing_owned_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'cancel.sqlite'}")
    Base.metadata.create_all(engine)
    started, release = threading.Event(), threading.Event()

    def decode(_):
        started.set()
        assert release.wait(3)
        return np.zeros(8, dtype=np.float32), 8000

    monkeypatch.setattr(captures, "load_audio", decode)
    with Session(engine) as db:
        pending = asyncio.create_task(
            captures.create_capture(
                audio_bytes=b"owned", filename="voice.wav", source="file", language="en", stt_model="tiny", db=db
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not list(config.get_captures_dir().iterdir())
        assert db.query(Capture).count() == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_render_preflight_runs_off_loop_and_schedules_on_loop(tmp_path, monkeypatch):
    import soundfile as sf

    from backend.services import task_queue
    from backend.studio import rendering
    from backend.studio.api import Render

    store = StudioStore(tmp_path / "studio")
    project = store.create_project("Render")
    source = tmp_path / "source.wav"
    sf.write(source, np.zeros(100), 48000)
    asset = store.add_asset(project["id"], source)
    project = store.command(
        project["id"], "insert", 0, "insert_asset", {"asset_id": asset["id"], "start_sample": 0, "kind": "music"}
    )
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Approved")
    loop_thread = threading.get_ident()
    prepared, scheduled = [], []
    monkeypatch.setattr(rendering, "preflight_mp3", lambda: prepared.append(threading.get_ident()))

    def schedule(coro):
        scheduled.append(threading.get_ident())
        coro.close()

    monkeypatch.setattr(task_queue, "create_background_task", schedule)
    route = next(r.endpoint for r in make_router(store).routes if getattr(r, "path", "").endswith("/render-jobs"))
    result = await route(
        project["id"],
        Render(idempotency_key="render", revision_id=project["revisions"][0]["id"], kind="mix", format="mp3"),
    )
    assert result["job"]["status"] == "queued"
    assert prepared
    assert loop_thread not in prepared
    assert scheduled == [loop_thread]


@pytest.mark.asyncio
async def test_cancelled_studio_admission_still_enqueues_committed_job_once(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Demo", script="Hello")
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    enqueued = []

    def capabilities(_):
        started.set()
        assert release.wait(3)
        return {"languages": ["en"], "expressive_tags": [], "supports_instruct": False, "supports_seed": False}

    def enqueue(ident, coro):
        assert threading.get_ident() == loop_thread
        enqueued.append(ident)
        coro.close()

    jobs = StudioJobs(store, enqueue=enqueue, capability_resolver=capabilities)
    route = next(
        r.endpoint for r in make_router(store, jobs).routes if getattr(r, "path", "").endswith("/generation-jobs")
    )
    request = Generate(
        idempotency_key="cancelled", passage_ids=[project["document"]["passages"][0]["id"]], count=1, adopt=False
    )
    pending = asyncio.create_task(route(project["id"], request))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    replay = await route(project["id"], request)
    assert enqueued == [replay["jobs"][0]["id"]]
    assert len(store.get_project(project["id"])["jobs"]) == 1
