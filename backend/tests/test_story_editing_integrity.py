"""Story editing behavior against the real SQLite models."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database import Base, Generation, Story, StoryItem, VoiceProfile
from backend.models import (
    StoryCreate, StoryItemBatchUpdate, StoryItemCreate, StoryItemMove, StoryItemSplit,
    StoryItemTrim, StoryItemUpdateTime, StoryItemVolumeUpdate,
)
from backend.services import stories


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(VoiceProfile(id="voice", name="Voice"))
        session.add(Story(id="story", name="Story"))
        session.add_all([
            Generation(id="a", profile_id="voice", text="First", duration=1, audio_path="missing-a.wav"),
            Generation(id="b", profile_id="voice", text="Second", duration=1, audio_path="missing-b.wav"),
        ])
        session.add_all([
            StoryItem(id="one", story_id="story", generation_id="a", start_time_ms=100, track=0),
            StoryItem(id="two", story_id="story", generation_id="b", start_time_ms=1500, track=0),
        ])
        session.commit()
        yield session
    engine.dispose()


async def test_invalid_batch_does_not_leave_partial_edits_in_session(db):
    updates = StoryItemBatchUpdate(updates=[
        StoryItemUpdateTime(generation_id="a", start_time_ms=800),
        StoryItemUpdateTime(generation_id="absent", start_time_ms=1000),
    ])
    assert await stories.update_story_item_times("story", updates, db) is False
    # A caller's later commit must not persist a rejected update.
    db.commit()
    assert db.get(StoryItem, "one").start_time_ms == 100


async def test_generation_keyed_edits_reject_ambiguous_split_clips(db):
    assert await stories.split_story_item("story", "one", StoryItemSplit(split_time_ms=500), db)
    before = {item.id: item.start_time_ms for item in db.query(StoryItem).all()}
    updates = StoryItemBatchUpdate(updates=[StoryItemUpdateTime(generation_id="a", start_time_ms=800)])
    assert await stories.update_story_item_times("story", updates, db) is False
    assert await stories.reorder_story_items("story", ["a", "b"], db) is None
    db.commit()
    assert {item.id: item.start_time_ms for item in db.query(StoryItem).all()} == before


async def test_reorder_rejects_repeated_generation_ids(db):
    assert await stories.reorder_story_items("story", ["a", "a", "b"], db) is None
    db.commit()
    assert db.get(StoryItem, "one").start_time_ms == 100
    assert db.get(StoryItem, "two").start_time_ms == 1500


async def test_batch_updates_unambiguous_clip_even_when_another_is_split(db):
    await stories.split_story_item("story", "one", StoryItemSplit(split_time_ms=500), db)
    updates = StoryItemBatchUpdate(updates=[StoryItemUpdateTime(generation_id="b", start_time_ms=2300)])
    assert await stories.update_story_item_times("story", updates, db) is True
    assert db.get(StoryItem, "two").start_time_ms == 2300


async def test_story_lifecycle_and_clip_edits(db):
    created = await stories.create_story(StoryCreate(name="Narration", description="Draft"), db)
    assert created.item_count == 0
    added = await stories.add_item_to_story(created.id, StoryItemCreate(generation_id="a"), db)
    assert added.start_time_ms == 0
    assert (await stories.add_item_to_story(created.id, StoryItemCreate(generation_id="a"), db)).id == added.id
    second = await stories.add_item_to_story(created.id, StoryItemCreate(generation_id="b"), db)
    assert second.start_time_ms == 1200
    assert (await stories.move_story_item(created.id, added.id, StoryItemMove(start_time_ms=300, track=1), db)).track == 1
    assert (await stories.trim_story_item(created.id, added.id, StoryItemTrim(trim_start_ms=100, trim_end_ms=200), db)).trim_end_ms == 200
    assert await stories.trim_story_item(created.id, added.id, StoryItemTrim(trim_start_ms=500, trim_end_ms=500), db) is None
    assert (await stories.update_story_item_volume(created.id, added.id, StoryItemVolumeUpdate(volume=0.5), db)).volume == 0.5
    detail = await stories.get_story(created.id, db)
    assert {item.id for item in detail.items} == {added.id, second.id}
    assert (await stories.update_story(created.id, StoryCreate(name="Final"), db)).item_count == 2
    assert next(story for story in await stories.list_stories(db) if story.id == created.id).item_count == 2
    assert await stories.remove_item_from_story(created.id, added.id, db) is True
    assert await stories.remove_item_from_story(created.id, added.id, db) is False
    assert await stories.delete_story(created.id, db) is True
    assert await stories.get_story(created.id, db) is None
    assert db.get(Generation, "a") is not None


async def test_missing_story_and_item_operations_do_not_change_data(db):
    assert await stories.get_story("missing", db) is None
    assert await stories.update_story("missing", StoryCreate(name="No"), db) is None
    assert await stories.delete_story("missing", db) is False
    assert await stories.add_item_to_story("missing", StoryItemCreate(generation_id="a"), db) is None
    assert await stories.add_item_to_story("story", StoryItemCreate(generation_id="missing"), db) is None
    assert await stories.move_story_item("story", "missing", StoryItemMove(start_time_ms=0), db) is None
    assert await stories.trim_story_item("story", "missing", StoryItemTrim(trim_start_ms=0, trim_end_ms=0), db) is None
    assert await stories.update_story_item_volume("story", "missing", StoryItemVolumeUpdate(volume=1), db) is None
    assert await stories.update_story_item_times("missing", StoryItemBatchUpdate(updates=[]), db) is False
    assert await stories.reorder_story_items("missing", ["a"], db) is None
