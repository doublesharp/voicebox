"""
Voice profile export/import module.

Handles exporting profiles to ZIP archives and importing them back.
Also handles exporting individual generations.
"""

import json
import zipfile
import io
import shutil
import uuid
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session

from ..models import VoiceProfileResponse
from ..database import VoiceProfile as DBVoiceProfile, ProfileSample as DBProfileSample, Generation as DBGeneration, GenerationVersion as DBGenerationVersion
from .profiles import create_profile, add_profile_sample
from ..models import VoiceProfileCreate
from .. import config


# Match Studio's archive-wide limits; metadata is kept smaller because it is
# parsed in memory. Audio is copied in chunks instead of materialized at once.
MAX_IMPORT_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_IMPORT_ENTRIES = 10_000
MAX_IMPORT_METADATA_BYTES = 8 * 1024 * 1024
IMPORT_COPY_CHUNK_BYTES = 1024 * 1024


def _validate_import_archive(archive: zipfile.ZipFile) -> list[str]:
    entries = archive.infolist()
    if len(entries) > MAX_IMPORT_ENTRIES or sum(entry.file_size for entry in entries) > MAX_IMPORT_EXPANDED_BYTES:
        raise ValueError("archive exceeds limits")
    names = set()
    for entry in entries:
        if entry.filename in names:
            raise ValueError(f"duplicate archive entry: {entry.filename}")
        names.add(entry.filename)
        if entry.filename in {"manifest.json", "samples.json"} and entry.file_size > MAX_IMPORT_METADATA_BYTES:
            raise ValueError(f"archive metadata exceeds limits: {entry.filename}")
    return [entry.filename for entry in entries]


def _get_unique_profile_name(name: str, db: Session) -> str:
    """
    Get a unique profile name by appending a number if needed.
    
    Args:
        name: Original profile name
        db: Database session
        
    Returns:
        Unique profile name
    """
    base_name = name
    counter = 1
    
    while True:
        existing = db.query(DBVoiceProfile).filter_by(name=name).first()
        if not existing:
            return name
        
        name = f"{base_name} ({counter})"
        counter += 1


def export_profile_to_zip(profile_id: str, db: Session) -> bytes:
    """
    Export a voice profile to a ZIP archive.
    
    Args:
        profile_id: Profile ID to export
        db: Database session
        
    Returns:
        ZIP file contents as bytes
        
    Raises:
        ValueError: If profile not found or has no samples
    """
    # Get profile
    profile = db.query(DBVoiceProfile).filter_by(id=profile_id).first()
    if not profile:
        raise ValueError(f"Profile {profile_id} not found")
    
    # Get all samples
    samples = db.query(DBProfileSample).filter_by(profile_id=profile_id).all()
    if not samples:
        raise ValueError(f"Profile {profile_id} has no samples")
    
    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Check if profile has avatar
        has_avatar = False
        if profile.avatar_path:
            avatar_path = config.resolve_storage_path(profile.avatar_path)
            if avatar_path is not None and avatar_path.exists():
                has_avatar = True
                # Add avatar to ZIP root with original extension
                avatar_ext = avatar_path.suffix
                zip_file.write(avatar_path, f"avatar{avatar_ext}")

        # Create manifest.json
        manifest = {
            "version": "1.0",
            "profile": {
                "name": profile.name,
                "description": profile.description,
                "language": profile.language,
            },
            "has_avatar": has_avatar,
        }
        zip_file.writestr("manifest.json", json.dumps(manifest, indent=2))

        # Create samples.json mapping
        samples_data = {}
        profile_dir = config.get_profiles_dir() / profile_id

        for sample in samples:
            # Get filename from audio_path (should be {sample_id}.wav)
            audio_path = config.resolve_storage_path(sample.audio_path)
            if audio_path is None:
                raise ValueError(f"Audio file not found: {sample.audio_path}")
            filename = audio_path.name

            # Read audio file
            if not audio_path.exists():
                raise ValueError(f"Audio file not found: {audio_path}")

            # Add to samples directory in ZIP
            zip_path = f"samples/{filename}"
            zip_file.write(audio_path, zip_path)

            # Map filename to reference text
            samples_data[filename] = sample.reference_text

        zip_file.writestr("samples.json", json.dumps(samples_data, indent=2))
    
    zip_buffer.seek(0)
    return zip_buffer.read()


async def import_profile_from_zip(file_bytes: bytes, db: Session) -> VoiceProfileResponse:
    """
    Import a voice profile from a ZIP archive.
    
    Args:
        file_bytes: ZIP file contents
        db: Database session
        
    Returns:
        Created profile
        
    Raises:
        ValueError: If ZIP is invalid or missing required files
    """
    zip_buffer = io.BytesIO(file_bytes)
    
    try:
        with zipfile.ZipFile(zip_buffer, 'r') as zip_file:
            # Validate ZIP structure
            namelist = _validate_import_archive(zip_file)
            
            if "manifest.json" not in namelist:
                raise ValueError("ZIP archive missing manifest.json")
            
            if "samples.json" not in namelist:
                raise ValueError("ZIP archive missing samples.json")
            
            # Read manifest
            manifest_data = json.loads(zip_file.read("manifest.json"))
            
            if "version" not in manifest_data:
                raise ValueError("Invalid manifest.json: missing version")
            
            if "profile" not in manifest_data:
                raise ValueError("Invalid manifest.json: missing profile")
            
            profile_data = manifest_data["profile"]
            
            # Read samples mapping
            samples_data = json.loads(zip_file.read("samples.json"))
            
            if not isinstance(samples_data, dict):
                raise ValueError("Invalid samples.json: must be a dictionary")

            # Validate all entries before creating a persistent profile.
            for filename in samples_data:
                if not filename.endswith('.wav'):
                    raise ValueError(f"Invalid sample filename: {filename} (must be .wav)")
                zip_path = f"samples/{filename}"
                if zip_path not in namelist:
                    raise ValueError(f"Sample file not found in ZIP: {zip_path}")
            
            # Get unique profile name
            original_name = profile_data.get("name", "Imported Profile")
            unique_name = _get_unique_profile_name(original_name, db)
            
            # Create profile
            profile_create = VoiceProfileCreate(
                name=unique_name,
                description=profile_data.get("description"),
                language=profile_data.get("language", "en"),
            )
            
            profile_dir = None
            try:
                profile = await create_profile(profile_create, db, commit=False)
                profile_dir = config.get_profiles_dir() / profile.id
                for filename, reference_text in samples_data.items():
                    import tempfile
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                            tmp_path = tmp.name
                            with zip_file.open(f"samples/{filename}") as source:
                                shutil.copyfileobj(source, tmp, length=IMPORT_COPY_CHUNK_BYTES)
                        await add_profile_sample(
                            profile.id, tmp_path, reference_text, db, commit=False,
                        )
                    finally:
                        if tmp_path is not None:
                            Path(tmp_path).unlink(missing_ok=True)
                db.commit()
            except BaseException:
                db.rollback()
                if profile_dir is not None:
                    shutil.rmtree(profile_dir, ignore_errors=True)
                raise

            # Avatar import is optional and happens after the profile is committed.
            avatar_files = [f for f in namelist if f.startswith("avatar.")]
            if avatar_files:
                try:
                    import tempfile
                    avatar_file = avatar_files[0]
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(suffix=Path(avatar_file).suffix, delete=False) as tmp:
                            tmp_path = tmp.name
                            with zip_file.open(avatar_file) as source:
                                shutil.copyfileobj(source, tmp, length=IMPORT_COPY_CHUNK_BYTES)
                        from .profiles import upload_avatar
                        await upload_avatar(profile.id, tmp_path, db)
                    finally:
                        if tmp_path is not None:
                            Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

            return profile
            
    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in archive: {e}")
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"Error importing profile: {str(e)}")


def export_generation_to_zip(generation_id: str, db: Session) -> bytes:
    """
    Export a generation to a ZIP archive.
    
    Args:
        generation_id: Generation ID to export
        db: Database session
        
    Returns:
        ZIP file contents as bytes
        
    Raises:
        ValueError: If generation not found
    """
    # Get generation
    generation = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not generation:
        raise ValueError(f"Generation {generation_id} not found")
    
    # Get profile info
    profile = db.query(DBVoiceProfile).filter_by(id=generation.profile_id).first()
    if not profile:
        raise ValueError(f"Profile {generation.profile_id} not found")
    
    # Get all versions for this generation
    versions = (
        db.query(DBGenerationVersion)
        .filter_by(generation_id=generation_id)
        .order_by(DBGenerationVersion.created_at)
        .all()
    )

    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Build version manifest entries
        version_entries = []
        version_paths = []
        used_filenames = set()
        for index, v in enumerate(versions):
            v_path = config.resolve_storage_path(v.audio_path)
            if v_path is None or not v_path.is_file():
                raise ValueError(f"Audio file not found: {v.audio_path}")
            effects_chain = None
            if v.effects_chain:
                effects_chain = json.loads(v.effects_chain)
            filename = v_path.name
            if filename in used_filenames:
                suffix = 1
                while filename in used_filenames:
                    filename = f"{v_path.stem}-{index + 1}-{suffix}{v_path.suffix}"
                    suffix += 1
            used_filenames.add(filename)
            version_paths.append(v_path)
            version_entries.append({
                "id": v.id,
                "label": v.label,
                "is_default": v.is_default,
                "effects_chain": effects_chain,
                "source_version_id": v.source_version_id,
                "created_at": v.created_at.isoformat(),
                "filename": filename,
            })

        manifest = {
            "version": "1.0",
            "generation": {
                "id": generation.id,
                "text": generation.text,
                "language": generation.language,
                "duration": generation.duration,
                "seed": generation.seed,
                "instruct": generation.instruct,
                "engine": generation.engine,
                "model_size": generation.model_size,
                "synthesis_config": generation.synthesis_config,
                "source": generation.source,
                "is_favorited": bool(generation.is_favorited),
                "created_at": generation.created_at.isoformat(),
            },
            "profile": {
                "id": profile.id,
                "name": profile.name,
                "description": profile.description,
                "language": profile.language,
            },
            "versions": version_entries,
        }
        zip_file.writestr("manifest.json", json.dumps(manifest, indent=2))
        
        # Add all version audio files
        for v_path, entry in zip(version_paths, version_entries):
            # A missing file must fail the export, never produce an incomplete archive.
            zip_file.write(v_path, f"audio/{entry['filename']}")

        # Fallback: if no versions exist, include the generation's main audio
        if not versions:
            audio_path = config.resolve_storage_path(generation.audio_path)
            if audio_path is None or not audio_path.is_file():
                raise ValueError(f"Audio file not found: {generation.audio_path}")
            zip_file.write(audio_path, f"audio/{audio_path.name}")
    
    zip_buffer.seek(0)
    return zip_buffer.read()


async def import_generation_from_zip(file_bytes: bytes, db: Session) -> dict:
    """
    Import a generation from a ZIP archive.
    
    Args:
        file_bytes: ZIP file contents
        db: Database session
        
    Returns:
        Dictionary with generation ID and profile info
        
    Raises:
        ValueError: If ZIP is invalid or missing required files
    """
    from pathlib import Path
    import tempfile
    import shutil
    from datetime import UTC, datetime
    from .. import config
    
    zip_buffer = io.BytesIO(file_bytes)
    
    try:
        with zipfile.ZipFile(zip_buffer, 'r') as zip_file:
            # Validate ZIP structure
            namelist = _validate_import_archive(zip_file)
            
            if "manifest.json" not in namelist:
                raise ValueError("ZIP archive missing manifest.json")
            
            # Read manifest
            manifest_data = json.loads(zip_file.read("manifest.json"))
            
            if "version" not in manifest_data:
                raise ValueError("Invalid manifest.json: missing version")
            
            if "generation" not in manifest_data:
                raise ValueError("Invalid manifest.json: missing generation data")
            
            generation_data = manifest_data["generation"]
            profile_data = manifest_data.get("profile", {})
            
            # Validate required fields
            required_fields = ["text", "language", "duration"]
            for field in required_fields:
                if field not in generation_data:
                    raise ValueError(f"Invalid manifest.json: missing generation.{field}")
            
            synthesis_config = generation_data.get("synthesis_config")
            if synthesis_config is not None and not isinstance(synthesis_config, str):
                raise ValueError("Invalid generation synthesis config")
            source_name = generation_data.get("source") or "manual"
            if not isinstance(source_name, str):
                raise ValueError("Invalid generation source")
            favorite = generation_data.get("is_favorited", False)
            if not isinstance(favorite, bool):
                raise ValueError("Invalid generation favorite flag")
            created_at = generation_data.get("created_at")
            if created_at is None:
                created_at = datetime.now(UTC)
            else:
                if not isinstance(created_at, str):
                    raise ValueError("Invalid generation creation time")
                try:
                    created_at = datetime.fromisoformat(created_at)
                except ValueError as exc:
                    raise ValueError("Invalid generation creation time") from exc

            # Find audio file in archive
            audio_files = [f for f in namelist if f.startswith("audio/") and f.endswith(".wav")]
            if not audio_files:
                raise ValueError("No audio file found in ZIP archive")
            
            version_data = manifest_data.get("versions", [])
            if not isinstance(version_data, list) or any(not isinstance(v, dict) for v in version_data):
                raise ValueError("Invalid generation versions")
            if not version_data:
                # Archives predating version support contain just the original audio.
                version_data = [{"label": "original", "filename": audio_files[0][6:], "is_default": True}]

            known_ids = set()
            default_indices = []
            version_times = []
            for index, version in enumerate(version_data):
                filename = version.get("filename")
                if not isinstance(filename, str) or f"audio/{filename}" not in audio_files:
                    if version.get("is_default"):
                        raise ValueError(f"Default version audio not found in ZIP: audio/{filename}")
                    raise ValueError(f"version audio not found in ZIP: audio/{filename}")
                version_id = version.get("id")
                if version_id is not None:
                    if not isinstance(version_id, str) or not version_id or version_id in known_ids:
                        raise ValueError("Invalid or duplicate version ID")
                    known_ids.add(version_id)
                if not isinstance(version.get("is_default", False), bool):
                    raise ValueError("Invalid default version flag")
                if version.get("is_default", False):
                    default_indices.append(index)
                if version.get("effects_chain") is not None and not isinstance(version["effects_chain"], list):
                    raise ValueError("Invalid version effects chain")
                if version.get("label") is not None and not isinstance(version["label"], str):
                    raise ValueError("Invalid version label")
                version_time = version.get("created_at")
                if version_time is not None:
                    if not isinstance(version_time, str):
                        raise ValueError("Invalid version creation time")
                    try:
                        version_time = datetime.fromisoformat(version_time)
                    except ValueError as exc:
                        raise ValueError("Invalid version creation time") from exc
                version_times.append(version_time)
            if len(default_indices) > 1:
                raise ValueError("Multiple default versions")
            default_index = default_indices[0] if default_indices else 0
            by_id = {v["id"]: v for v in version_data if v.get("id") is not None}
            validated_sources = set()
            for version in version_data:
                seen = set()
                source_id = version.get("source_version_id")
                while source_id is not None:
                    if not isinstance(source_id, str) or source_id not in by_id or source_id in seen:
                        raise ValueError("Invalid version source relationship")
                    if source_id in validated_sources:
                        break
                    seen.add(source_id)
                    source_id = by_id[source_id].get("source_version_id")
                validated_sources.update(seen)

            # Check if we should match an existing profile or create metadata
            profile_id = None
            profile_name = profile_data.get("name", "Unknown Profile")
            
            # Try to find matching profile by name
            if profile_name and profile_name != "Unknown Profile":
                existing_profile = db.query(DBVoiceProfile).filter_by(name=profile_name).first()
                if existing_profile:
                    profile_id = existing_profile.id
            
            # If no matching profile, use a placeholder or the first available profile
            if not profile_id:
                # Get any profile, or None if no profiles exist
                any_profile = db.query(DBVoiceProfile).first()
                if any_profile:
                    profile_id = any_profile.id
                    profile_name = any_profile.name
                else:
                    raise ValueError("No voice profiles found. Please create a profile before importing generations.")
            
            generations_dir = config.get_generations_dir()
            generations_dir.mkdir(parents=True, exist_ok=True)
            new_generation_id = str(uuid.uuid4())
            created_files = []
            rows = []
            id_map = {}
            try:
                for index, version in enumerate(version_data):
                    version_id = str(uuid.uuid4())
                    if version.get("id") is not None:
                        id_map[version["id"]] = version_id
                    audio_dest = generations_dir / f"{new_generation_id}_{version_id}.wav"
                    with audio_dest.open("xb") as target:
                        created_files.append(audio_dest)
                        with zip_file.open(f"audio/{version['filename']}") as source:
                            shutil.copyfileobj(source, target, length=IMPORT_COPY_CHUNK_BYTES)
                    rows.append(DBGenerationVersion(
                        id=version_id, generation_id=new_generation_id,
                        label=version.get("label") or f"take-{index + 1}",
                        audio_path=config.to_storage_path(audio_dest),
                        effects_chain=json.dumps(version["effects_chain"]) if version.get("effects_chain") is not None else None,
                        is_default=index == default_index,
                        created_at=version_times[index] or datetime.now(UTC),
                    ))

                db_generation = DBGeneration(
                    id=new_generation_id, profile_id=profile_id,
                    text=generation_data["text"], language=generation_data["language"],
                    audio_path=rows[default_index].audio_path, duration=generation_data["duration"],
                    seed=generation_data.get("seed"), instruct=generation_data.get("instruct"),
                    engine=generation_data.get("engine") or "qwen",
                    model_size=generation_data.get("model_size"), synthesis_config=synthesis_config,
                    source=source_name, is_favorited=favorite, created_at=created_at,
                )
                db.add(db_generation)
                db.add_all(rows)
                # Insert rows before assigning lineage so sources may appear later in the archive.
                db.flush()
                for row, version in zip(rows, version_data):
                    if version.get("source_version_id") is not None:
                        row.source_version_id = id_map[version["source_version_id"]]
                db.commit()
            except BaseException:
                db.rollback()
                for path in created_files:
                    path.unlink(missing_ok=True)
                raise
            # Values are already known; a failed refresh after commit must not
            # make a successful import look failed and invite a duplicate retry.
            return {
                "id": new_generation_id,
                "profile_id": profile_id,
                "profile_name": profile_name,
                "text": generation_data["text"],
                "message": f"Generation imported successfully (assigned to profile: {profile_name})",
            }

    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in archive: {e}")
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"Error importing generation: {str(e)}")
