"""Pinned story editing uses selected version duration, including effect tails."""

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Generation, Story, StoryItem, VoiceProfile
from backend.models import StoryItemCreate, StoryItemSplit, StoryItemTrim
from backend.services import stories, versions


@pytest.fixture
def pinned_story(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    sf.write(tmp_path / "short.wav", np.zeros(2400), 24000)
    sf.write(tmp_path / "long.wav", np.zeros(7200), 24000)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.add(Story(id="story", name="Story"))
        db.add(Generation(id="gen", profile_id="voice", text="Hello", duration=0.3, audio_path="long.wav"))
        db.add(Generation(id="next", profile_id="voice", text="Next", duration=0.1, audio_path="short.wav"))
        db.commit()
        original = versions.create_version("gen", "original", "short.wav", db)
        versions.create_version("gen", "effects", "long.wav", db, effects_chain=[], is_default=True)
        db.add(
            StoryItem(
                id="pinned", story_id="story", generation_id="gen", version_id=original.id, start_time_ms=0, track=0
            )
        )
        db.commit()
        yield db
    engine.dispose()


async def test_split_preserves_both_halves_of_pinned_short_audio(pinned_story):
    details = await stories.split_story_item("story", "pinned", StoryItemSplit(split_time_ms=50), pinned_story)
    assert details is not None
    left, right = details
    assert left.duration == pytest.approx(0.1)
    assert right.duration == pytest.approx(0.1)
    assert left.trim_end_ms == 50
    assert right.trim_start_ms == 50
    assert right.start_time_ms == 50
    assert left.version_id == right.version_id


async def test_split_and_trim_cannot_extend_past_pinned_source(pinned_story):
    assert await stories.split_story_item("story", "pinned", StoryItemSplit(split_time_ms=150), pinned_story) is None
    assert (
        await stories.trim_story_item("story", "pinned", StoryItemTrim(trim_start_ms=0, trim_end_ms=150), pinned_story)
        is None
    )
    assert pinned_story.get(StoryItem, "pinned").trim_end_ms == 0


async def test_duplicate_and_auto_append_use_selected_effective_duration(pinned_story):
    duplicate = await stories.duplicate_story_item("story", "pinned", pinned_story)
    assert duplicate.start_time_ms == 300  # 100 ms selected clip + 200 ms gap.
    appended = await stories.add_item_to_story("story", StoryItemCreate(generation_id="next"), pinned_story)
    assert appended.start_time_ms == 600  # Duplicate ends at 400 ms + gap.


async def test_reorder_uses_selected_version_duration(pinned_story):
    await stories.add_item_to_story("story", StoryItemCreate(generation_id="next", start_time_ms=1000), pinned_story)
    ordered = await stories.reorder_story_items("story", ["gen", "next"], pinned_story)
    assert ordered[1].start_time_ms == 300
