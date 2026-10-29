"""
Story management module.
"""

from typing import List, Optional
from dataclasses import dataclass
from datetime import UTC, datetime
import os
import math
import re
import uuid
import tempfile
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import func

from .. import config
from ..utils.ffmpeg import encode_audio
from ..models import (
    StoryCreate,
    StoryResponse,
    StoryDetailResponse,
    StoryItemDetail,
    StoryItemCreate,
    StoryItemBatchUpdate,
    StoryItemMove,
    StoryItemTrim,
    StoryItemVolumeUpdate,
    StoryItemSplit,
    StoryItemVersionUpdate,
)
from ..database import (
    Story as DBStory,
    StoryItem as DBStoryItem,
    Generation as DBGeneration,
    VoiceProfile as DBVoiceProfile,
)
from .history import _get_versions_for_generation, _get_versions_for_generations
from ..utils.audio_metadata import audio_duration
from ..utils.async_work import run_blocking


def _selected_item_audio(item: DBStoryItem, generation: DBGeneration, versions) -> tuple:
    """Resolve pinned/default audio from already fetched version metadata."""
    audio_path = generation.audio_path
    if item.version_id:
        audio_path = next((version.audio_path for version in versions or [] if version.id == item.version_id), audio_path)
    return audio_path, audio_duration(audio_path, fallback=generation.duration)


def _item_duration_ms(item: DBStoryItem, generation: DBGeneration, versions) -> int:
    _, duration = _selected_item_audio(item, generation, versions)
    return int((duration or 0.0) * 1000)


def _effective_item_duration_ms(item: DBStoryItem, generation: DBGeneration, versions) -> int:
    return max(0, _item_duration_ms(item, generation, versions) - item.trim_start_ms - item.trim_end_ms)


def _build_item_detail(
    item: DBStoryItem,
    generation: DBGeneration,
    profile_name: str,
    db: Session,
    version_info: Optional[tuple] = None,
) -> StoryItemDetail:
    """Build a StoryItemDetail using selected-file duration and version info."""
    versions, active_version_id = (
        version_info if version_info is not None else _get_versions_for_generation(generation.id, db)
    )
    audio_path, duration = _selected_item_audio(item, generation, versions)

    return StoryItemDetail(
        id=item.id,
        story_id=item.story_id,
        generation_id=item.generation_id,
        version_id=getattr(item, "version_id", None),
        start_time_ms=item.start_time_ms,
        track=item.track,
        trim_start_ms=getattr(item, "trim_start_ms", 0),
        trim_end_ms=getattr(item, "trim_end_ms", 0),
        created_at=item.created_at,
        profile_id=generation.profile_id,
        profile_name=profile_name,
        text=generation.text,
        language=generation.language,
        audio_path=audio_path,
        duration=duration,
        seed=generation.seed,
        instruct=generation.instruct,
        engine=generation.engine,
        volume=getattr(item, "volume", 1.0),
        generation_created_at=generation.created_at,
        versions=versions,
        active_version_id=active_version_id,
    )


async def create_story(
    data: StoryCreate,
    db: Session,
) -> StoryResponse:
    """
    Create a new story.

    Args:
        data: Story creation data
        db: Database session

    Returns:
        Created story
    """
    db_story = DBStory(
        id=str(uuid.uuid4()),
        name=data.name,
        description=data.description,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    db.add(db_story)
    db.commit()
    db.refresh(db_story)

    item_count = db.query(func.count(DBStoryItem.id)).filter(DBStoryItem.story_id == db_story.id).scalar()

    response = StoryResponse.model_validate(db_story)
    response.item_count = item_count
    return response


async def list_stories(
    db: Session,
) -> List[StoryResponse]:
    """
    List all stories.

    Args:
        db: Database session

    Returns:
        List of stories with item counts
    """
    stories = db.query(DBStory).order_by(DBStory.updated_at.desc()).all()

    if not stories:
        return []

    # Batch-fetch all story item counts in one query to avoid an N+1 pattern
    # (previously there was one COUNT query per story in the loop below).
    story_ids = [s.id for s in stories]
    count_rows = (
        db.query(DBStoryItem.story_id, func.count(DBStoryItem.id).label("cnt"))
        .filter(DBStoryItem.story_id.in_(story_ids))
        .group_by(DBStoryItem.story_id)
        .all()
    )
    item_counts = {row.story_id: row.cnt for row in count_rows}

    result = []
    for story in stories:
        response = StoryResponse.model_validate(story)
        response.item_count = item_counts.get(story.id, 0)
        result.append(response)

    return result


async def get_story(
    story_id: str,
    db: Session,
) -> Optional[StoryDetailResponse]:
    """
    Get a story with all its items.

    Args:
        story_id: Story ID
        db: Database session

    Returns:
        Story with items or None if not found
    """
    story = db.query(DBStory).filter_by(id=story_id).first()
    if not story:
        return None

    items = (
        db.query(DBStoryItem, DBGeneration, DBVoiceProfile.name.label("profile_name"))
        .join(DBGeneration, DBStoryItem.generation_id == DBGeneration.id)
        .join(DBVoiceProfile, DBGeneration.profile_id == DBVoiceProfile.id)
        .filter(DBStoryItem.story_id == story_id)
        .order_by(DBStoryItem.start_time_ms)
        .all()
    )

    version_info = _get_versions_for_generations([generation.id for _, generation, _ in items], db)
    item_details = []
    for item, generation, profile_name in items:
        item_details.append(_build_item_detail(item, generation, profile_name, db, version_info[generation.id]))

    response = StoryDetailResponse.model_validate(story)
    response.items = item_details
    return response


async def update_story(
    story_id: str,
    data: StoryCreate,
    db: Session,
) -> Optional[StoryResponse]:
    """
    Update a story.

    Args:
        story_id: Story ID
        data: Update data
        db: Database session

    Returns:
        Updated story or None if not found
    """
    story = db.query(DBStory).filter_by(id=story_id).first()
    if not story:
        return None

    story.name = data.name
    story.description = data.description
    story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(story)

    item_count = db.query(func.count(DBStoryItem.id)).filter(DBStoryItem.story_id == story.id).scalar()

    response = StoryResponse.model_validate(story)
    response.item_count = item_count
    return response


async def delete_story(
    story_id: str,
    db: Session,
) -> bool:
    """
    Delete a story and all its items.

    Args:
        story_id: Story ID
        db: Database session

    Returns:
        True if deleted, False if not found
    """
    story = db.query(DBStory).filter_by(id=story_id).first()
    if not story:
        return False

    # Delete all items
    db.query(DBStoryItem).filter_by(story_id=story_id).delete()

    # Delete story
    db.delete(story)
    db.commit()

    return True


async def add_item_to_story(
    story_id: str,
    data: StoryItemCreate,
    db: Session,
) -> Optional[StoryItemDetail]:
    """
    Add a generation to a story.

    Args:
        story_id: Story ID
        data: Item creation data
        db: Database session

    Returns:
        Created item detail or None if story/generation not found
    """
    # Verify story exists
    story = db.query(DBStory).filter_by(id=story_id).first()
    if not story:
        return None

    # Verify generation exists
    generation = db.query(DBGeneration).filter_by(id=data.generation_id).first()
    if not generation:
        return None

    # Do not commit an item that cannot be serialized or played. Incomplete
    # and failed generations can have no audio path or known duration.
    duration = audio_duration(generation.audio_path, fallback=generation.duration)
    if not generation.audio_path or duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Generation has no playable audio")

    # Check if generation is already in story
    existing = db.query(DBStoryItem).filter_by(story_id=story_id, generation_id=data.generation_id).first()
    if existing:
        # Return existing item
        profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()
        return _build_item_detail(existing, generation, profile.name if profile else "Unknown", db)

    # Get track from data or default to 0
    track = data.track if data.track is not None else 0

    # Calculate start_time_ms if not provided
    if data.start_time_ms is not None:
        start_time_ms = data.start_time_ms
    else:
        existing_items = (
            db.query(DBStoryItem, DBGeneration)
            .join(DBGeneration, DBStoryItem.generation_id == DBGeneration.id)
            .filter(
                DBStoryItem.story_id == story_id,
                DBStoryItem.track == track,
            )
            .all()
        )

        if not existing_items:
            start_time_ms = 0
        else:
            version_info = _get_versions_for_generations([gen.id for _, gen in existing_items], db)
            max_end_time_ms = 0
            for item, gen in existing_items:
                item_end_ms = item.start_time_ms + _effective_item_duration_ms(item, gen, version_info[gen.id][0])
                max_end_time_ms = max(max_end_time_ms, item_end_ms)

            # Add 200ms gap after the last item
            start_time_ms = max_end_time_ms + 200

    # Create item
    item = DBStoryItem(
        id=str(uuid.uuid4()),
        story_id=story_id,
        generation_id=data.generation_id,
        start_time_ms=start_time_ms,
        track=track,
        created_at=datetime.now(UTC),
    )

    db.add(item)

    # Update story updated_at
    story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(item)

    # Get profile name
    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()

    return _build_item_detail(item, generation, profile.name if profile else "Unknown", db)


async def move_story_item(
    story_id: str,
    item_id: str,
    data: StoryItemMove,
    db: Session,
) -> Optional[StoryItemDetail]:
    """
    Move a story item (update position and/or track).

    Args:
        story_id: Story ID
        item_id: Story item ID
        data: New position and track data
        db: Database session

    Returns:
        Updated item detail or None if not found
    """
    # Get the item
    item = (
        db.query(DBStoryItem)
        .filter_by(
            id=item_id,
            story_id=story_id,
        )
        .first()
    )
    if not item:
        return None

    # Get the generation
    generation = db.query(DBGeneration).filter_by(id=item.generation_id).first()
    if not generation:
        return None

    # Update position and track
    item.start_time_ms = data.start_time_ms
    item.track = data.track

    # Update story updated_at
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story:
        story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(item)

    # Get profile name
    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()

    return _build_item_detail(item, generation, profile.name if profile else "Unknown", db)


async def remove_item_from_story(
    story_id: str,
    item_id: str,
    db: Session,
) -> bool:
    """
    Remove a story item from a story.

    Args:
        story_id: Story ID
        item_id: Story item ID to remove
        db: Database session

    Returns:
        True if removed, False if not found
    """
    item = (
        db.query(DBStoryItem)
        .filter_by(
            id=item_id,
            story_id=story_id,
        )
        .first()
    )
    if not item:
        return False

    # Delete item
    db.delete(item)

    # Update story updated_at
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story:
        story.updated_at = datetime.now(UTC)

    db.commit()
    return True


async def trim_story_item(
    story_id: str,
    item_id: str,
    data: StoryItemTrim,
    db: Session,
) -> Optional[StoryItemDetail]:
    """
    Trim a story item (update trim_start_ms and trim_end_ms).

    Args:
        story_id: Story ID
        item_id: Story item ID
        data: Trim data (trim_start_ms, trim_end_ms)
        db: Database session

    Returns:
        Updated item detail or None if not found
    """
    # Get the item
    item = (
        db.query(DBStoryItem)
        .filter_by(
            id=item_id,
            story_id=story_id,
        )
        .first()
    )
    if not item:
        return None

    # Get the generation
    generation = db.query(DBGeneration).filter_by(id=item.generation_id).first()
    if not generation:
        return None

    # Validate trim values don't exceed duration
    version_info = _get_versions_for_generation(generation.id, db)
    max_duration_ms = _item_duration_ms(item, generation, version_info[0])
    if data.trim_start_ms + data.trim_end_ms >= max_duration_ms:
        return None  # Invalid trim - would result in zero or negative duration

    # Update trim values
    item.trim_start_ms = data.trim_start_ms
    item.trim_end_ms = data.trim_end_ms

    # Update story updated_at
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story:
        story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(item)

    # Get profile name
    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()

    return _build_item_detail(item, generation, profile.name if profile else "Unknown", db, version_info)


async def update_story_item_volume(
    story_id: str,
    item_id: str,
    data: StoryItemVolumeUpdate,
    db: Session,
) -> Optional[StoryItemDetail]:
    """Update a story item's playback volume (per-clip linear gain)."""
    item = (
        db.query(DBStoryItem)
        .filter_by(id=item_id, story_id=story_id)
        .first()
    )
    if not item:
        return None
    generation = db.query(DBGeneration).filter_by(id=item.generation_id).first()
    if not generation:
        return None

    item.volume = data.volume

    story = db.query(DBStory).filter_by(id=story_id).first()
    if story:
        story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(item)

    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()
    return _build_item_detail(item, generation, profile.name if profile else "Unknown", db)


async def split_story_item(
    story_id: str,
    item_id: str,
    data: StoryItemSplit,
    db: Session,
) -> Optional[List[StoryItemDetail]]:
    """
    Split a story item at a given time, creating two clips.

    Args:
        story_id: Story ID
        item_id: Story item ID to split
        data: Split data (split_time_ms - time within clip to split at)
        db: Database session

    Returns:
        List of two updated item details (original and new) or None if not found/invalid
    """
    # Get the item with a row lock to prevent concurrent splits on the
    # same clip (e.g. from rapid double-clicks racing each other).
    item = (
        db.query(DBStoryItem)
        .filter_by(
            id=item_id,
            story_id=story_id,
        )
        .with_for_update()
        .first()
    )
    if not item:
        return None

    # Get the generation
    generation = db.query(DBGeneration).filter_by(id=item.generation_id).first()
    if not generation:
        return None

    # Calculate effective duration and validate split point
    current_trim_start = getattr(item, "trim_start_ms", 0)
    current_trim_end = getattr(item, "trim_end_ms", 0)
    version_info = _get_versions_for_generation(generation.id, db)
    original_duration_ms = _item_duration_ms(item, generation, version_info[0])
    effective_duration_ms = original_duration_ms - current_trim_start - current_trim_end

    # Validate split_time_ms is within the effective duration
    if data.split_time_ms <= 0 or data.split_time_ms >= effective_duration_ms:
        return None  # Invalid split point

    # Calculate the absolute time in the original audio where we're splitting
    absolute_split_ms = current_trim_start + data.split_time_ms

    # Update original clip: trim from the end
    item.trim_end_ms = original_duration_ms - absolute_split_ms

    # Create new clip: starts after the split, trimmed from the start
    new_item = DBStoryItem(
        id=str(uuid.uuid4()),
        story_id=story_id,
        generation_id=item.generation_id,  # Same generation, different trim
        version_id=getattr(item, "version_id", None),  # Preserve pinned version
        start_time_ms=item.start_time_ms + data.split_time_ms,
        track=item.track,
        trim_start_ms=absolute_split_ms,
        trim_end_ms=current_trim_end,
        volume=getattr(item, "volume", 1.0),
        created_at=datetime.now(UTC),
    )

    db.add(new_item)

    # Update story updated_at
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story:
        story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(item)
    db.refresh(new_item)

    # Get profile name
    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()
    profile_name = profile.name if profile else "Unknown"

    return [
        _build_item_detail(item, generation, profile_name, db, version_info),
        _build_item_detail(new_item, generation, profile_name, db, version_info),
    ]


async def duplicate_story_item(
    story_id: str,
    item_id: str,
    db: Session,
) -> Optional[StoryItemDetail]:
    """
    Duplicate a story item, creating a copy with all properties.

    Args:
        story_id: Story ID
        item_id: Story item ID to duplicate
        db: Database session

    Returns:
        New item detail or None if not found
    """
    # Get the original item
    original_item = (
        db.query(DBStoryItem)
        .filter_by(
            id=item_id,
            story_id=story_id,
        )
        .first()
    )
    if not original_item:
        return None

    # Get the generation
    generation = db.query(DBGeneration).filter_by(id=original_item.generation_id).first()
    if not generation:
        return None

    # Calculate effective duration
    current_trim_start = getattr(original_item, "trim_start_ms", 0)
    current_trim_end = getattr(original_item, "trim_end_ms", 0)
    version_info = _get_versions_for_generation(generation.id, db)
    original_duration_ms = _item_duration_ms(original_item, generation, version_info[0])
    effective_duration_ms = original_duration_ms - current_trim_start - current_trim_end

    # Create duplicate item - place it right after the original
    new_item = DBStoryItem(
        id=str(uuid.uuid4()),
        story_id=story_id,
        generation_id=original_item.generation_id,  # Same generation as original
        version_id=getattr(original_item, "version_id", None),  # Preserve pinned version
        start_time_ms=original_item.start_time_ms + effective_duration_ms + 200,  # 200ms gap
        track=original_item.track,
        trim_start_ms=current_trim_start,
        trim_end_ms=current_trim_end,
        volume=getattr(original_item, "volume", 1.0),
        created_at=datetime.now(UTC),
    )

    db.add(new_item)

    # Update story updated_at
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story:
        story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(new_item)

    # Get profile name
    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()

    return _build_item_detail(new_item, generation, profile.name if profile else "Unknown", db, version_info)


async def update_story_item_times(
    story_id: str,
    data: StoryItemBatchUpdate,
    db: Session,
) -> bool:
    """
    Update story item timecodes.

    Args:
        story_id: Story ID
        data: Batch update data with timecodes
        db: Database session

    Returns:
        True if updated, False if story not found or invalid
    """
    story = db.query(DBStory).filter_by(id=story_id).first()
    if not story:
        return False

    # Get all items for this story
    items = db.query(DBStoryItem).filter_by(story_id=story_id).all()
    item_map = {}
    ambiguous_ids = set()
    for item in items:
        if item.generation_id in item_map:
            ambiguous_ids.add(item.generation_id)
        item_map[item.generation_id] = item

    # Validate the entire request before changing any item. Generation IDs
    # cannot identify one clip after a split or duplication.
    seen = set()
    for update in data.updates:
        if (update.generation_id not in item_map or update.generation_id in ambiguous_ids
                or update.generation_id in seen):
            return False
        seen.add(update.generation_id)

    for update in data.updates:
        item_map[update.generation_id].start_time_ms = update.start_time_ms

    # Update story updated_at
    story.updated_at = datetime.now(UTC)

    db.commit()
    return True


async def reorder_story_items(
    story_id: str,
    generation_ids: List[str],
    db: Session,
    gap_ms: int = 200,
) -> Optional[List[StoryItemDetail]]:
    """
    Reorder story items and recalculate timecodes.

    Args:
        story_id: Story ID
        generation_ids: List of generation IDs in the desired order
        db: Database session
        gap_ms: Gap in milliseconds between items (default 200ms)

    Returns:
        Updated list of story items with new timecodes, or None if invalid
    """
    story = db.query(DBStory).filter_by(id=story_id).first()
    if not story:
        return None

    # Get all items for this story with their generation data
    items_with_gen = (
        db.query(DBStoryItem, DBGeneration, DBVoiceProfile.name.label("profile_name"))
        .join(DBGeneration, DBStoryItem.generation_id == DBGeneration.id)
        .join(DBVoiceProfile, DBGeneration.profile_id == DBVoiceProfile.id)
        .filter(DBStoryItem.story_id == story_id)
        .all()
    )

    # Create maps for quick lookup
    item_map = {item.generation_id: (item, gen, profile_name) for item, gen, profile_name in items_with_gen}

    # Verify all generation IDs belong to this story
    if (len(items_with_gen) != len(item_map)
            or len(generation_ids) != len(item_map)
            or set(generation_ids) != set(item_map.keys())):
        return None

    version_info = _get_versions_for_generations(list(item_map), db)
    # Recalculate timecodes based on the selected files and existing trims.
    current_time_ms = 0
    updated_items = []

    for gen_id in generation_ids:
        item, generation, profile_name = item_map[gen_id]

        # Update the item's start time
        item.start_time_ms = current_time_ms

        # Calculate the duration in ms
        duration_ms = _effective_item_duration_ms(item, generation, version_info[generation.id][0])

        # Move to next position (current end + gap)
        current_time_ms += duration_ms + gap_ms

        # Build the response item
        updated_items.append(_build_item_detail(item, generation, profile_name, db, version_info[generation.id]))

    # Update story updated_at
    story.updated_at = datetime.now(UTC)

    db.commit()
    return updated_items


async def set_story_item_version(
    story_id: str,
    item_id: str,
    data: StoryItemVersionUpdate,
    db: Session,
) -> Optional[StoryItemDetail]:
    """
    Pin a story item to a specific generation version.

    Args:
        story_id: Story ID
        item_id: Story item ID
        data: Version update data (version_id or null for default)
        db: Database session

    Returns:
        Updated item detail or None if not found
    """
    item = (
        db.query(DBStoryItem)
        .filter_by(
            id=item_id,
            story_id=story_id,
        )
        .first()
    )
    if not item:
        return None

    generation = db.query(DBGeneration).filter_by(id=item.generation_id).first()
    if not generation:
        return None

    # Validate version_id belongs to this generation if provided
    selected_audio_path = generation.audio_path
    if data.version_id:
        from ..database import GenerationVersion as DBGenerationVersion

        version = (
            db.query(DBGenerationVersion)
            .filter_by(
                id=data.version_id,
                generation_id=item.generation_id,
            )
            .first()
        )
        if not version:
            return None
        selected_audio_path = version.audio_path

    selected_duration = audio_duration(selected_audio_path, fallback=generation.duration)
    trim_ms = item.trim_start_ms + item.trim_end_ms
    if trim_ms and trim_ms >= int((selected_duration or 0.0) * 1000):
        return None

    item.version_id = data.version_id

    # Update story updated_at
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story:
        story.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(item)

    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()

    return _build_item_detail(item, generation, profile.name if profile else "Unknown", db)


@dataclass
class _Chapter:
    """Single chapter boundary used when exporting a story to m4b/mp3."""

    start_ms: int
    end_ms: int
    title: str


# Latin sentences end with [.!?] + whitespace; CJK sentences end with [。！？]
# and conventionally have no following whitespace, so the boundary is the mark
# itself. Both lookbehinds are zero-width — split() just consumes whitespace
# when present.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


def _chapter_title_from_text(text: Optional[str], max_len: int = 80) -> str:
    """Derive a chapter title from the linked generation's text.

    Takes the first sentence (or the leading slice if the sentence is long).
    """
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return "Chapter"
    first = _SENTENCE_BREAK.split(cleaned, maxsplit=1)[0].strip()
    if not first:
        return "Chapter"
    if len(first) > max_len:
        return first[:max_len].rstrip() + "…"
    return first


def _derive_chapters_auto(
    segments: List[dict],
    total_duration_ms: int,
) -> List[_Chapter]:
    """One chapter per story segment, ordered by start_time_ms.

    Each segment dict must carry ``start_time_ms`` and ``text``. The final
    chapter runs to ``total_duration_ms``. Segments with identical
    ``start_time_ms`` are deduped (only the first contributes a chapter
    boundary) so multi-track items don't produce zero-length chapters.
    """
    ordered = sorted(segments, key=lambda s: s["start_time_ms"])
    chapters: List[_Chapter] = []
    for seg in ordered:
        start = int(seg["start_time_ms"])
        if chapters and chapters[-1].start_ms == start:
            continue
        chapters.append(
            _Chapter(start_ms=start, end_ms=0, title=_chapter_title_from_text(seg.get("text")))
        )
    for i, ch in enumerate(chapters):
        ch.end_ms = chapters[i + 1].start_ms if i + 1 < len(chapters) else total_duration_ms
    # Skip degenerate trailing chapter that would have zero duration.
    return [ch for ch in chapters if ch.end_ms > ch.start_ms]


def _escape_ffmetadata(value: str) -> str:
    """Escape a metadata value for an FFMETADATA1 file.

    Per the spec (https://ffmpeg.org/ffmpeg-formats.html#Metadata-1) ``=``,
    ``;``, ``#``, ``\\``, and literal newlines must be backslash-escaped, or
    ffmpeg will reject (or silently mangle) the chapter entry.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\n", "\\n")
    )


def _write_ffmetadata(chapters: List[_Chapter], path: Path) -> None:
    """Serialize chapter list to an FFMETADATA1 file ffmpeg can ingest via ``-i``."""
    lines = [";FFMETADATA1"]
    for ch in chapters:
        lines.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={ch.start_ms}",
                f"END={ch.end_ms}",
                f"title={_escape_ffmetadata(ch.title)}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ffmpeg_encode(
    wav_path: Path,
    out_path: Path,
    fmt: str,
    chapters: Optional[List[_Chapter]],
    *,
    cancelled=None,
) -> None:
    """Transcode WAV → fmt with optional embedded chapter metadata.

    Raises RuntimeError on missing ffmpeg or non-zero exit.
    """
    meta_path: Optional[Path] = None
    try:
        if chapters:
            meta_path = wav_path.with_suffix(".chapters.txt")
            _write_ffmetadata(chapters, meta_path)
        encode_audio(wav_path, out_path, fmt, metadata_path=meta_path, cancelled=cancelled)
    finally:
        if meta_path is not None:
            meta_path.unlink(missing_ok=True)


def _make_tempfile(suffix: str) -> Path:
    """Create a closed-on-return temp file path with the given suffix.

    Uses ``tempfile.mkstemp`` rather than ``NamedTemporaryFile(delete=False).name``
    so the OS file descriptor is released immediately instead of lingering
    until garbage collection (Ruff SIM115).
    """
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(name)


async def export_story_file(story_id: str, db: Session, fmt: str = "wav", chapters_mode: str = "none") -> Optional[Path]:
    """Snapshot row references on their owning thread, then mix bounded blocks off-loop."""
    from ..database import GenerationVersion
    from .story_export import render_story_file

    if not db.query(DBStory).filter_by(id=story_id).first():
        return None
    items = (db.query(DBStoryItem, DBGeneration).outerjoin(DBGeneration, DBStoryItem.generation_id == DBGeneration.id)
             .filter(DBStoryItem.story_id == story_id).order_by(DBStoryItem.start_time_ms).all())
    if not items:
        return None
    fmt = (fmt or "wav").lower()
    if fmt not in ("wav", "mp3", "m4b"):
        raise ValueError(f"Unsupported export format: {fmt}")
    clips = []
    for item, generation in items:
        if generation is None:
            raise ValueError(f"Story item {item.id} references a missing generation")
        stored = generation.audio_path
        if item.version_id:
            version = db.query(GenerationVersion).filter_by(id=item.version_id, generation_id=generation.id).first()
            if version is None:
                raise ValueError(f"Story item {item.id} references a missing audio version")
            stored = version.audio_path
        path = config.resolve_storage_path(stored)
        if path is None:
            raise ValueError(f"Story item {item.id} audio file is missing")
        clips.append(dict(id=item.id, path=str(path), trim_start_ms=item.trim_start_ms or 0,
                          trim_end_ms=item.trim_end_ms or 0, start_time_ms=item.start_time_ms,
                          volume=1.0 if item.volume is None else float(item.volume), text=generation.text))
    return await render_story_file(clips, fmt, chapters_mode)


async def export_story_audio(story_id: str, db: Session, fmt: str = "wav", chapters_mode: str = "none") -> Optional[bytes]:
    """Compatibility helper; HTTP delivery uses the file-backed variant directly."""
    path = await export_story_file(story_id, db, fmt, chapters_mode)
    if path is None:
        return None
    try:
        return await run_blocking(path.read_bytes)
    finally:
        path.unlink(missing_ok=True)
