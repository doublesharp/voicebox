from datetime import UTC, datetime

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Generation, StoryItem, VoiceProfile
from backend.services import stories, versions


def test_duration_tracks_default_version_and_pinned_story_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    sf.write(tmp_path / "short.wav", np.zeros(2400), 24000)
    sf.write(tmp_path / "long.wav", np.zeros(7200), 24000)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.add(Generation(id="gen", profile_id="voice", text="Hello", duration=0.1, audio_path="short.wav"))
        db.commit()
        original = versions.create_version("gen", "original", "short.wav", db, is_default=True)
        processed = versions.create_version("gen", "effects", "long.wav", db, effects_chain=[], is_default=True)
        generation = db.get(Generation, "gen")
        assert generation.duration == pytest.approx(0.3)
        item = StoryItem(
            id="item",
            story_id="story",
            generation_id="gen",
            version_id=original.id,
            start_time_ms=0,
            track=0,
            trim_start_ms=0,
            trim_end_ms=0,
            volume=1.0,
            created_at=datetime.now(UTC),
        )
        detail = stories._build_item_detail(item, generation, "Voice", db)
        assert detail.duration == pytest.approx(0.1)
        versions.set_default_version(original.id, db)
        assert db.get(Generation, "gen").duration == pytest.approx(0.1)
        versions.set_default_version(processed.id, db)
        assert db.get(Generation, "gen").duration == pytest.approx(0.3)
        assert versions.delete_version(processed.id, db)
        assert db.get(Generation, "gen").duration == pytest.approx(0.1)
    engine.dispose()


def test_replacing_default_version_refreshes_duration_including_reused_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    sf.write(tmp_path / "original.wav", np.zeros(2400), 24000)
    sf.write(tmp_path / "effects.wav", np.zeros(7200), 24000)
    sf.write(tmp_path / "replacement.wav", np.zeros(12000), 24000)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.add(Generation(id="gen", profile_id="voice", text="Hello", duration=0.1, audio_path="original.wav"))
        db.commit()
        original = versions.create_version("gen", "original", "original.wav", db, is_default=True)
        processed = versions.create_version(
            "gen", "effects", "effects.wav", db, effects_chain=[], source_version_id=original.id, is_default=True
        )
        versions.replace_version_audio(processed.id, "replacement.wav", [], original.id, db)
        assert db.get(Generation, "gen").duration == pytest.approx(0.5)
        sf.write(tmp_path / "replacement.wav", np.zeros(16800), 24000)
        versions.replace_version_audio(processed.id, "replacement.wav", [], original.id, db)
        assert db.get(Generation, "gen").duration == pytest.approx(0.7)
    engine.dispose()


def test_metadata_reads_only_headers_and_retries_missing_or_replaced_files(tmp_path, monkeypatch):
    from backend.utils import audio_metadata

    monkeypatch.setattr(config, "_data_dir", tmp_path)
    assert audio_metadata.audio_duration("future.wav", fallback=0.8) == 0.8
    sf.write(tmp_path / "future.wav", np.zeros(2400), 24000)
    info = sf.info
    headers = []

    def counted_info(path):
        headers.append(path)
        return info(path)

    monkeypatch.setattr(sf, "info", counted_info)
    monkeypatch.setattr(sf, "read", lambda *args, **kwargs: pytest.fail("duration must not decode PCM"))
    assert audio_metadata.audio_duration("future.wav") == pytest.approx(0.1)
    assert audio_metadata.audio_duration("future.wav") == pytest.approx(0.1)
    assert len(headers) == 1
    sf.write(tmp_path / "future.wav", np.zeros(4800), 24000)
    assert audio_metadata.audio_duration("future.wav") == pytest.approx(0.2)
    assert len(headers) == 2
    (tmp_path / "broken.wav").write_bytes(b"not audio")
    assert audio_metadata.audio_duration("broken.wav", fallback=0.9) == 0.9
