"""Story export with real SQLite rows and WAVs, without a running application."""

import asyncio
import io

import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Generation, GenerationVersion, Story, StoryItem, VoiceProfile
from backend.routes.stories import export_story_audio as export_story_endpoint
from backend.services.stories import export_story_audio


@pytest.fixture
def story_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.add(Story(id="story", name="Story"))
        db.commit()
        yield db
    engine.dispose()


def add_clip(db, tmp_path, clip_id, audio, sample_rate, *, volume=1.0, start_time_ms=0, duration=None):
    path = tmp_path / f"{clip_id}.wav"
    sf.write(path, audio, sample_rate, subtype="FLOAT")
    db.add(
        Generation(
            id=clip_id,
            profile_id="voice",
            text="Speech",
            audio_path=path.name,
            duration=duration if duration is not None else len(audio) / sample_rate,
        )
    )
    db.flush()
    db.add(
        StoryItem(
            id=f"item-{clip_id}", story_id="story", generation_id=clip_id, start_time_ms=start_time_ms, volume=volume
        )
    )
    db.commit()


def exported(db):
    data = asyncio.run(export_story_audio("story", db))
    return sf.read(io.BytesIO(data), dtype="float32", always_2d=True)


def test_muted_story_clip_exports_silence(story_db, tmp_path):
    add_clip(story_db, tmp_path, "muted", np.full(2400, 0.2), 24000, volume=0.0)
    audio, _ = exported(story_db)
    np.testing.assert_array_equal(audio, np.zeros_like(audio))


def test_stereo_48khz_source_keeps_sample_rate_and_channels(story_db, tmp_path):
    source = np.column_stack([np.full(4800, 0.2), np.full(4800, -0.1)])
    add_clip(story_db, tmp_path, "stereo", source, 48000)
    audio, sample_rate = exported(story_db)
    assert sample_rate == 48000
    assert audio.shape == source.shape
    np.testing.assert_allclose(audio, source, atol=4e-5)


def test_mixed_rates_and_mono_are_mixed_in_native_stereo(story_db, tmp_path):
    stereo = np.column_stack([np.full(4800, 0.2), np.full(4800, -0.1)])
    add_clip(story_db, tmp_path, "stereo", stereo, 48000)
    add_clip(story_db, tmp_path, "mono", np.full(2400, 0.1), 24000)
    audio, sample_rate = exported(story_db)
    assert sample_rate == 48000
    assert audio.shape == (4800, 2)
    # Resampling edge transients are intentionally excluded from the comparison.
    np.testing.assert_allclose(audio[500:-500, 0], 0.3, atol=4e-5)
    np.testing.assert_allclose(audio[500:-500, 1], 0.0, atol=4e-5)


def test_version_audio_duration_is_not_cut_to_generation_metadata(story_db, tmp_path):
    add_clip(story_db, tmp_path, "gen", np.full(2400, 0.2), 24000)
    path = tmp_path / "long-version.wav"
    sf.write(path, np.full(7200, 0.1), 24000, subtype="FLOAT")
    story_db.add(GenerationVersion(id="version", generation_id="gen", label="effects", audio_path=path.name))
    story_db.flush()
    story_db.get(StoryItem, "item-gen").version_id = "version"
    story_db.commit()
    audio, sample_rate = exported(story_db)
    assert sample_rate == 24000
    assert len(audio) == 7200
    assert audio[-1, 0] == pytest.approx(0.1, abs=4e-5)


@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "missing_version", "wrong_version", "missing_generation", "empty_trim"]
)
def test_export_rejects_story_with_unusable_clip(story_db, tmp_path, failure):
    add_clip(story_db, tmp_path, "good", np.full(2400, 0.2), 24000)
    add_clip(story_db, tmp_path, "bad", np.full(2400, 0.2), 24000, start_time_ms=200)
    item = story_db.get(StoryItem, "item-bad")
    if failure == "missing":
        (tmp_path / "bad.wav").unlink()
    elif failure == "corrupt":
        (tmp_path / "bad.wav").write_bytes(b"not a wav")
    elif failure == "missing_version":
        item.version_id = "missing-version"
    elif failure == "wrong_version":
        story_db.add(GenerationVersion(id="other-version", generation_id="good", label="Other", audio_path="good.wav"))
        item.version_id = "other-version"
    elif failure == "missing_generation":
        story_db.delete(story_db.get(Generation, "bad"))
    else:
        item.trim_start_ms = 1000
    story_db.commit()
    with pytest.raises(ValueError, match="item-bad"):
        asyncio.run(export_story_audio("story", story_db))


def test_export_endpoint_reports_missing_audio_as_bad_request(story_db, tmp_path):
    add_clip(story_db, tmp_path, "bad", np.full(2400, 0.2), 24000)
    (tmp_path / "bad.wav").unlink()
    with pytest.raises(HTTPException) as error:
        asyncio.run(export_story_endpoint("story", "wav", "none", story_db))
    assert error.value.status_code == 400
    assert "item-bad" in error.value.detail


def test_export_http_uses_bounded_mix_and_file_backed_delivery(story_db, tmp_path, monkeypatch):
    from fastapi.responses import FileResponse

    add_clip(story_db, tmp_path, "long", np.full(200000, 0.2, dtype=np.float32), 24000)
    zeros = np.zeros

    def bounded_zeros(shape, *args, **kwargs):
        frames = shape[0] if isinstance(shape, tuple) else shape
        assert frames <= 65536, "export allocated an entire story instead of a bounded block"
        return zeros(shape, *args, **kwargs)

    monkeypatch.setattr(np, "zeros", bounded_zeros)
    response = asyncio.run(export_story_endpoint("story", "wav", "none", story_db))
    assert isinstance(response, FileResponse), "HTTP export buffered all encoded bytes"
    path = response.path
    try:
        assert sf.info(path).frames == 200000
        assert sf.info(path).samplerate == 24000
    finally:
        from pathlib import Path

        Path(path).unlink(missing_ok=True)


def test_block_mix_applies_one_global_peak_gain_and_preserves_trim_placement(story_db, tmp_path):
    add_clip(story_db, tmp_path, "a", np.full(80000, 0.8, dtype=np.float32), 24000)
    add_clip(story_db, tmp_path, "b", np.full(80000, 0.8, dtype=np.float32), 24000, start_time_ms=100)
    item = story_db.get(StoryItem, "item-b")
    item.trim_start_ms = 10
    item.trim_end_ms = 20
    story_db.commit()
    audio, rate = exported(story_db)
    assert rate == 24000
    assert len(audio) == 2400 + 80000 - 240 - 480
    assert audio[100, 0] == pytest.approx(0.4995, abs=4e-5)
    assert audio[3000, 0] == pytest.approx(0.999, abs=4e-5)
    assert audio[70000, 0] == pytest.approx(0.999, abs=4e-5)
    assert audio[-100, 0] == pytest.approx(0.4995, abs=4e-5)


@pytest.mark.asyncio
async def test_file_response_removes_export_if_client_send_fails(tmp_path):
    from backend.utils.file_response import TemporaryFileResponse

    path = tmp_path / "export.wav"
    path.write_bytes(b"wave")

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(_):
        raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="client disconnected"):
        await TemporaryFileResponse(path)({"type": "http", "method": "GET", "headers": []}, receive, send)
    assert not path.exists()


@pytest.mark.asyncio
async def test_cancelled_export_drains_writer_before_removing_owned_file(tmp_path, monkeypatch):
    import threading

    from backend.services import stories
    from backend.services.story_export import render_story_file

    source = tmp_path / "source.wav"
    sf.write(source, np.full(100, 0.1), 24000)
    output = tmp_path / "owned.wav"
    started, release = threading.Event(), threading.Event()

    def allocate(_):
        output.touch()
        started.set()
        assert release.wait(3)
        return output

    monkeypatch.setattr(stories, "_make_tempfile", allocate)
    clips = [dict(id="clip", path=str(source), trim_start_ms=0, trim_end_ms=0, start_time_ms=0, volume=1, text="Hello")]
    pending = asyncio.create_task(render_story_file(clips, "wav", "none"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not output.exists()
