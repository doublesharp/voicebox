"""
Generation versions management module.

Each generation can have multiple audio versions: a clean (unprocessed)
version and any number of processed versions with different effects chains.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import weakref
from typing import Optional

from sqlalchemy.orm import Session

from .. import config
from ..database import (
    Generation as DBGeneration,
    GenerationVersion as DBGenerationVersion,
    StoryItem as DBStoryItem,
)
from ..models import EffectConfig, GenerationVersionResponse
from ..utils.audio_metadata import audio_duration

logger = logging.getLogger(__name__)

_generation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def generation_lock(generation_id: str) -> asyncio.Lock:
    """Serialize version mutations while DSP yields to a worker thread."""
    lock = _generation_locks.get(generation_id)
    if lock is None:
        lock = asyncio.Lock()
        _generation_locks[generation_id] = lock
    return lock


def original_version(generation_id: str, db: Session) -> Optional[DBGenerationVersion]:
    return (
        db.query(DBGenerationVersion)
        .filter_by(generation_id=generation_id, effects_chain=None, source_version_id=None)
        .order_by(DBGenerationVersion.created_at)
        .first()
    )


def ensure_version_mutable(version: DBGenerationVersion, db: Session, *, editing: bool = False) -> None:
    """Keep originals, source provenance, and pinned story clips intact."""
    original = original_version(version.generation_id, db)
    if original and original.id == version.id:
        raise ValueError("Original audio cannot be edited or deleted")
    if editing and version.effects_chain is None and version.source_version_id is None:
        raise ValueError("Apply effects to this unprocessed take to create an editable version")
    if db.query(DBGenerationVersion).filter_by(source_version_id=version.id).first():
        raise ValueError("This version is used as a source. Remove its derived versions first")
    if db.query(DBStoryItem).filter_by(version_id=version.id).first():
        raise ValueError("This version is pinned in a story. Switch the story clip to another version first")


def replace_version_audio(
    version_id: str,
    audio_path: str,
    effects_chain: list[dict],
    source_version_id: str,
    db: Session,
) -> GenerationVersionResponse:
    """Publish a fully written render, then reclaim the previous audio file."""
    version = db.query(DBGenerationVersion).filter_by(id=version_id).one()
    db.refresh(version)
    ensure_version_mutable(version, db, editing=True)
    previous_path = version.audio_path
    version.audio_path = audio_path
    # An empty list still represents an editable render, not an original take.
    version.effects_chain = json.dumps(effects_chain)
    version.source_version_id = source_version_id
    if version.is_default:
        generation = db.query(DBGeneration).filter_by(id=version.generation_id).one()
        _select_generation_audio(generation, audio_path)
    db.commit()
    db.refresh(version)
    _remove_unused_audio(previous_path, db)
    return _version_response(version)


def _select_generation_audio(generation: DBGeneration, audio_path: str) -> None:
    generation.audio_path = audio_path
    duration = audio_duration(audio_path)
    if duration is not None:
        generation.duration = duration


def _remove_unused_audio(audio_path: str, db: Session) -> None:
    if db.query(DBGenerationVersion).filter_by(audio_path=audio_path).first():
        return
    if db.query(DBGeneration).filter_by(audio_path=audio_path).first():
        return
    resolved = config.resolve_storage_path(audio_path)
    if resolved is not None:
        try:
            resolved.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove superseded version audio %s", resolved)


def _version_response(v: DBGenerationVersion) -> GenerationVersionResponse:
    """Convert a DB version row to a Pydantic response."""
    effects_chain = None
    if v.effects_chain:
        raw = json.loads(v.effects_chain)
        effects_chain = [EffectConfig(**e) for e in raw]
    return GenerationVersionResponse(
        id=v.id,
        generation_id=v.generation_id,
        label=v.label,
        audio_path=v.audio_path,
        effects_chain=effects_chain,
        source_version_id=v.source_version_id,
        is_default=v.is_default,
        created_at=v.created_at,
    )


def list_versions(generation_id: str, db: Session) -> list[GenerationVersionResponse]:
    """List all versions for a generation."""
    versions = (
        db.query(DBGenerationVersion)
        .filter_by(generation_id=generation_id)
        .order_by(DBGenerationVersion.created_at)
        .all()
    )
    return [_version_response(v) for v in versions]


def get_version(version_id: str, db: Session) -> Optional[GenerationVersionResponse]:
    """Get a specific version by ID."""
    v = db.query(DBGenerationVersion).filter_by(id=version_id).first()
    if not v:
        return None
    return _version_response(v)


def get_default_version(generation_id: str, db: Session) -> Optional[GenerationVersionResponse]:
    """Get the default version for a generation."""
    v = db.query(DBGenerationVersion).filter_by(generation_id=generation_id, is_default=True).first()
    if not v:
        # Fallback: return the first version
        v = (
            db.query(DBGenerationVersion)
            .filter_by(generation_id=generation_id)
            .order_by(DBGenerationVersion.created_at)
            .first()
        )
    if not v:
        return None
    return _version_response(v)


def create_version(
    generation_id: str,
    label: str,
    audio_path: str,
    db: Session,
    effects_chain: Optional[list[dict]] = None,
    is_default: bool = False,
    source_version_id: Optional[str] = None,
) -> GenerationVersionResponse:
    """Create a new version for a generation.

    If ``is_default`` is True, all other versions for this generation
    are un-defaulted first.
    """
    if is_default:
        _clear_defaults(generation_id, db)

    version = DBGenerationVersion(
        id=str(uuid.uuid4()),
        generation_id=generation_id,
        label=label,
        audio_path=audio_path,
        effects_chain=json.dumps(effects_chain) if effects_chain is not None else None,
        source_version_id=source_version_id,
        is_default=is_default,
    )
    db.add(version)
    if is_default:
        gen = db.query(DBGeneration).filter_by(id=generation_id).first()
        if gen:
            _select_generation_audio(gen, audio_path)
    db.commit()
    db.refresh(version)

    return _version_response(version)


def set_default_version(version_id: str, db: Session) -> Optional[GenerationVersionResponse]:
    """Set a version as the default for its generation."""
    version = db.query(DBGenerationVersion).filter_by(id=version_id).first()
    if not version:
        return None

    _clear_defaults(version.generation_id, db)
    version.is_default = True
    gen = db.query(DBGeneration).filter_by(id=version.generation_id).first()
    if gen:
        _select_generation_audio(gen, version.audio_path)
    db.commit()
    db.refresh(version)

    return _version_response(version)


def delete_version(version_id: str, db: Session) -> bool:
    """Delete a version. Cannot delete the last remaining version."""
    version = db.query(DBGenerationVersion).filter_by(id=version_id).first()
    if not version:
        return False

    ensure_version_mutable(version, db)

    # Don't allow deleting the last version
    count = db.query(DBGenerationVersion).filter_by(generation_id=version.generation_id).count()
    if count <= 1:
        return False

    was_default = version.is_default
    gen_id = version.generation_id

    # Delete the file only when no other version owns it. A locked file raises
    # before any DB change so the user can stop playback and retry.
    audio_path = config.resolve_storage_path(version.audio_path)
    shared = (
        db.query(DBGenerationVersion)
        .filter(
            DBGenerationVersion.audio_path == version.audio_path,
            DBGenerationVersion.id != version.id,
        )
        .first()
    )
    shared_generation = (
        db.query(DBGeneration)
        .filter(
            DBGeneration.audio_path == version.audio_path,
            DBGeneration.id != gen_id,
        )
        .first()
    )
    staged_path = None
    if not shared and not shared_generation and audio_path is not None and audio_path.exists():
        # Keep deletion reversible until the DB transaction commits. A locked
        # Windows file fails here before any row/default changes.
        staged_path = audio_path.with_name(f".{audio_path.name}.{uuid.uuid4().hex}.deleted")
        audio_path.rename(staged_path)
    try:
        db.delete(version)
        db.flush()
        if was_default:
            first = (
                db.query(DBGenerationVersion)
                .filter_by(generation_id=gen_id)
                .order_by(DBGenerationVersion.created_at)
                .first()
            )
            if first:
                first.is_default = True
                gen = db.query(DBGeneration).filter_by(id=gen_id).first()
                if gen:
                    _select_generation_audio(gen, first.audio_path)
        db.commit()
    except Exception:
        db.rollback()
        if staged_path is not None:
            staged_path.rename(audio_path)
        raise
    if staged_path is not None:
        try:
            staged_path.unlink()
        except OSError:
            logger.warning("Could not remove deleted version audio %s", staged_path)

    return True


def delete_versions_for_generation(generation_id: str, db: Session, commit: bool = True) -> int:
    """Delete all versions for a generation (used when deleting a generation).

    This runs as part of a wider cascade — deleting one generation, sweeping
    failed ones, or deleting a whole profile. A version file the OS won't let
    us remove (locked by playback on Windows) must not abort that cascade and
    strand the caller half-deleted, so the row goes regardless and the leaked
    file is logged. ``delete_version`` keeps raising, because a single
    user-initiated delete should fail loudly.

    Pass ``commit=False`` when the caller owns the transaction and will commit
    the whole cascade itself.
    """
    versions = db.query(DBGenerationVersion).filter_by(generation_id=generation_id).all()
    count = 0
    for v in versions:
        audio_path = config.resolve_storage_path(v.audio_path)
        if audio_path is not None and audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                logger.warning("Could not delete version audio %s; removing the row anyway", audio_path)
        db.delete(v)
        count += 1
    if count > 0 and commit:
        db.commit()
    return count


def _clear_defaults(generation_id: str, db: Session) -> None:
    """Clear the is_default flag on all versions for a generation."""
    db.query(DBGenerationVersion).filter_by(generation_id=generation_id, is_default=True).update({"is_default": False})
    db.flush()
