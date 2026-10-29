"""
Serial generation queue — ensures only one TTS inference runs at a time
to avoid GPU contention.
"""

import asyncio
import traceback
from dataclasses import dataclass
from typing import Coroutine, Literal

# Keep references to fire-and-forget background tasks to prevent GC
_background_tasks: set = set()


@dataclass
class GenerationJob:
    """Queued generation work plus the generation ID it belongs to."""

    generation_id: str
    coro: Coroutine
    cancelled: bool = False


# Generation queue — serializes TTS inference to avoid GPU contention
_generation_queue: asyncio.Queue = None  # type: ignore  # initialized at startup
_generation_worker_task: asyncio.Task | None = None
_queued_generation_jobs: dict[str, GenerationJob] = {}
_running_generation_tasks: dict[str, asyncio.Task] = {}


def create_background_task(coro) -> asyncio.Task:
    """Create a background task and prevent it from being garbage collected."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _generation_worker(queue: asyncio.Queue, queued: dict[str, GenerationJob], running: dict[str, asyncio.Task]):
    """Worker that processes generation tasks one at a time."""
    while True:
        job = await queue.get()
        try:
            if job.cancelled:
                job.coro.close()
                continue

            task = asyncio.create_task(job.coro)
            running[job.generation_id] = task
            if queued.get(job.generation_id) is job:
                queued.pop(job.generation_id)
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() or not task.cancelled():
                    raise
        except Exception:
            traceback.print_exc()
            await _force_fail_if_active(
                job.generation_id,
                "Worker exited without writing terminal status",
            )
        finally:
            running.pop(job.generation_id, None)
            if queued.get(job.generation_id) is job:
                queued.pop(job.generation_id)
            queue.task_done()


async def _force_fail_if_active(generation_id: str, error: str) -> None:
    """Best-effort recovery — flip an active row to failed if the worker
    bailed before writing a terminal status. Catches the case where the gen
    coroutine's own status-write raised (e.g. SQLite lock contention)."""
    try:
        from ..database import Generation as DBGeneration, get_db
        from . import history

        db = next(get_db())
        try:
            gen = db.query(DBGeneration).filter_by(id=generation_id).first()
            if gen is None:
                return
            if (gen.status or "completed") not in ("loading_model", "generating"):
                return
            await history.update_generation_status(
                generation_id=generation_id,
                status="failed",
                db=db,
                error=error,
            )
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def enqueue_generation(generation_id: str, coro):
    """Add a generation coroutine to the serial queue."""
    if _generation_queue is None:
        coro.close()
        raise RuntimeError("Generation queue has not been initialized")
    if generation_id in _queued_generation_jobs or generation_id in _running_generation_tasks:
        coro.close()
        raise ValueError(f"Generation {generation_id} is already queued or running")

    job = GenerationJob(generation_id=generation_id, coro=coro)
    _queued_generation_jobs[generation_id] = job
    _generation_queue.put_nowait(job)


def cancel_generation(generation_id: str) -> Literal["queued", "running"] | None:
    """Cancel a queued or running generation if it is still active."""
    running_task = _running_generation_tasks.get(generation_id)
    if running_task is not None:
        running_task.cancel()
        return "running"

    job = _queued_generation_jobs.pop(generation_id, None)
    if job is not None:
        job.cancelled = True
        return "queued"

    return None


def init_queue(force: bool = False):
    """Initialize the generation queue and start the worker.

    Must be called once during application startup (inside a running event loop).
    """
    global _generation_queue, _generation_worker_task
    global _queued_generation_jobs, _running_generation_tasks

    if _generation_worker_task is not None and not _generation_worker_task.done():
        if not force:
            return
        _generation_worker_task.cancel()
        for task in list(_running_generation_tasks.values()):
            task.cancel()
        while True:
            try:
                job = _generation_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            job.coro.close()
            _generation_queue.task_done()

    _generation_queue = asyncio.Queue()
    _queued_generation_jobs = {}
    _running_generation_tasks = {}
    _generation_worker_task = create_background_task(
        _generation_worker(_generation_queue, _queued_generation_jobs, _running_generation_tasks)
    )
