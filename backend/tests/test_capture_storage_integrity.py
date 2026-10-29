"""Capture audio must survive failed transactions and shared references."""

from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Capture, ProfileSample, VoiceProfile
from backend.services import captures


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'captures.db'}")
    Base.metadata.create_all(engine)
    yield engine, tmp_path
    engine.dispose()


def capture_row(capture_id, audio_path):
    return Capture(id=capture_id, audio_path=audio_path, source="file", transcript_raw="original")


def test_failed_delete_keeps_audio_and_capture(storage, monkeypatch):
    engine, root = storage
    path = config.get_captures_dir() / "sample.wav"
    path.write_bytes(b"audio")
    with Session(engine) as db:
        db.add(capture_row("one", config.to_storage_path(path)))
        db.commit()
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"):
            captures.delete_capture("one", db)
    assert path.read_bytes() == b"audio"
    with Session(engine) as db:
        assert db.get(Capture, "one") is not None


def test_delete_preserves_audio_referenced_by_another_capture(storage):
    engine, _ = storage
    path = config.get_captures_dir() / "shared.wav"
    path.write_bytes(b"shared")
    with Session(engine) as db:
        db.add_all([capture_row("one", str(path)), capture_row("two", config.to_storage_path(path))])
        db.commit()
        assert captures.delete_capture("one", db) is True
        assert path.read_bytes() == b"shared"
        assert captures.delete_capture("two", db) is True
        assert not path.exists()


def test_delete_preserves_audio_referenced_by_a_voice_sample(storage):
    engine, _ = storage
    path = config.get_captures_dir() / "shared.wav"
    path.write_bytes(b"voice sample")
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.add(ProfileSample(id="sample", profile_id="voice", audio_path=str(path), reference_text="Hello"))
        db.add(capture_row("one", config.to_storage_path(path)))
        db.commit()
        assert captures.delete_capture("one", db) is True
        assert path.read_bytes() == b"voice sample"
        assert db.get(ProfileSample, "sample") is not None


@pytest.mark.asyncio
async def test_committed_capture_audio_survives_refresh_failure(storage, monkeypatch):
    engine, _ = storage
    monkeypatch.setattr(captures, "load_audio", lambda _: (np.zeros(8, dtype=np.float32), 8000))

    async def transcribe(*_args):
        return "spoken"

    monkeypatch.setattr(
        captures, "get_whisper_model", lambda: SimpleNamespace(model_size="turbo", transcribe=transcribe)
    )
    with Session(engine) as db:
        monkeypatch.setattr(db, "refresh", lambda *_args: (_ for _ in ()).throw(RuntimeError("refresh failed")))
        with pytest.raises(RuntimeError, match="refresh failed"):
            await captures.create_capture(
                audio_bytes=b"original", filename="capture.wav", source="file", language="en", stt_model="turbo", db=db
            )
    with Session(engine) as db:
        row = db.query(Capture).one()
        assert config.resolve_storage_path(row.audio_path).read_bytes() == b"original"


@pytest.mark.asyncio
async def test_partial_transcode_is_cleaned_on_failure(storage, monkeypatch):
    engine, _ = storage
    monkeypatch.setattr(captures, "load_audio", lambda _: (np.zeros(8, dtype=np.float32), 8000))

    def fail_write(path, *_args, **_kwargs):
        with open(path, "wb") as output:
            output.write(b"partial")
        raise RuntimeError("transcode failed")

    monkeypatch.setattr(captures.sf, "write", fail_write)
    with Session(engine) as db, pytest.raises(RuntimeError, match="transcode failed"):
        await captures.create_capture(
            audio_bytes=b"source", filename="capture.webm", source="file", language=None, stt_model="turbo", db=db
        )
    assert list(config.get_captures_dir().iterdir()) == []
    with Session(engine) as db:
        assert db.query(Capture).count() == 0


@pytest.mark.asyncio
async def test_capture_name_collision_never_overwrites_an_existing_recording(storage, monkeypatch):
    engine, _ = storage
    path = config.get_captures_dir() / "fixed.wav"
    path.write_bytes(b"existing")
    monkeypatch.setattr(captures.uuid, "uuid4", lambda: "fixed")
    with Session(engine) as db, pytest.raises(FileExistsError):
        await captures.create_capture(
            audio_bytes=b"new", filename="capture.wav", source="file", language=None, stt_model="turbo", db=db
        )
    assert path.read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_transcode_destination_collision_preserves_existing_recording(storage, monkeypatch):
    engine, _ = storage
    path = config.get_captures_dir() / "fixed.wav"
    path.write_bytes(b"existing")
    monkeypatch.setattr(captures.uuid, "uuid4", lambda: "fixed")
    monkeypatch.setattr(captures, "load_audio", lambda _: (np.zeros(8, dtype=np.float32), 8000))
    with Session(engine) as db, pytest.raises(FileExistsError):
        await captures.create_capture(
            audio_bytes=b"new", filename="capture.webm", source="file", language=None, stt_model="turbo", db=db
        )
    assert path.read_bytes() == b"existing"
    assert list(config.get_captures_dir().iterdir()) == [path]


@pytest.mark.asyncio
async def test_failed_capture_commit_cleans_owned_audio(storage, monkeypatch):
    engine, _ = storage
    monkeypatch.setattr(captures, "load_audio", lambda _: (np.zeros(8, dtype=np.float32), 8000))

    async def transcribe(*_args):
        return "spoken"

    monkeypatch.setattr(
        captures, "get_whisper_model", lambda: SimpleNamespace(model_size="turbo", transcribe=transcribe)
    )
    with Session(engine) as db:
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"):
            await captures.create_capture(
                audio_bytes=b"audio", filename="capture.wav", source="file", language=None, stt_model="turbo", db=db
            )
    assert list(config.get_captures_dir().iterdir()) == []
    with Session(engine) as db:
        assert db.query(Capture).count() == 0


@pytest.mark.asyncio
async def test_successful_capture_and_retranscription_clear_stale_refinement(storage, monkeypatch):
    engine, _ = storage
    monkeypatch.setattr(captures, "load_audio", lambda _: (np.zeros(8000, dtype=np.float32), 8000))
    calls = []

    async def transcribe(path, language, model):
        calls.append((path, language, model))
        return f"text {len(calls)}"

    monkeypatch.setattr(
        captures, "get_whisper_model", lambda: SimpleNamespace(model_size="turbo", transcribe=transcribe)
    )
    with Session(engine) as db:
        created = await captures.create_capture(
            audio_bytes=b"audio", filename="capture.wav", source="file", language="en", stt_model="turbo", db=db
        )
        assert created.duration_ms == 1000
        assert created.transcript_raw == "text 1"
        row = db.get(Capture, created.id)
        row.transcript_refined = "old refinement"
        row.llm_model = "old model"
        row.refinement_flags = "{}"
        db.commit()
        updated = await captures.retranscribe_capture(created.id, "small", "fr", db)
        assert updated.transcript_raw == "text 2"
        assert updated.transcript_refined is None
        assert updated.llm_model is None
        assert updated.refinement_flags is None
        assert updated.language == "fr"
        assert updated.stt_model == "small"
        assert config.resolve_storage_path(updated.audio_path).read_bytes() == b"audio"
        assert calls[-1][1:] == ("fr", "small")
