"""Cancellation retains capture audio until the synchronous STT owner drains."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Capture
from backend.services import captures


@pytest.mark.asyncio
async def test_cancelled_capture_waits_for_stt_source_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    monkeypatch.setattr(captures, "load_audio", lambda _: (np.zeros(8, dtype=np.float32), 8000))
    engine = create_engine(f"sqlite:///{tmp_path / 'stt.sqlite'}")
    Base.metadata.create_all(engine)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    reads = []

    async def transcribe(path, *_):
        def read():
            started.set()
            try:
                assert release.wait(3)
                reads.append(Path(path).read_bytes() if Path(path).exists() else None)
                return "Words"
            finally:
                finished.set()

        return await asyncio.to_thread(read)

    monkeypatch.setattr(
        captures, "get_whisper_model", lambda: SimpleNamespace(model_size="tiny", transcribe=transcribe)
    )
    try:
        with Session(engine) as db:
            pending = asyncio.create_task(
                captures.create_capture(
                    audio_bytes=b"owned", filename="audio.wav", source="file", language="en", stt_model="tiny", db=db
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 1)
                pending.cancel()
                await asyncio.sleep(0.01)
                retained = bool(list(config.get_captures_dir().iterdir()))
                draining = not pending.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert await asyncio.to_thread(finished.wait, 1)
            assert retained, "cancelled capture removed its STT source before the worker drained"
            assert draining, "cancelled capture completed while the STT worker was still active"
            assert reads == [b"owned"]
            assert not list(config.get_captures_dir().iterdir())
            assert db.query(Capture).count() == 0
    finally:
        release.set()
        engine.dispose()
