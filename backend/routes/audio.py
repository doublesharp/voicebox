"""Audio file serving endpoints."""

import asyncio
import mimetypes
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from .. import config
from ..database import get_db
from ..services import history
from ..utils.ffmpeg import encode_audio
from ..utils.http import safe_content_disposition

router = APIRouter()


def _audio_media_type(path: Path) -> str:
    """Derive the Content-Type from the file extension.

    Imported audio retains its source format (.mp3, .m4a, .ogg, …) so a
    blanket ``audio/wav`` would mislead strict clients trying to decode
    via the response header instead of sniffing the bytes."""
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "audio/wav"


@router.get("/audio/version/{version_id}")
async def get_version_audio(version_id: str, db: Session = Depends(get_db)):
    """Serve audio for a specific version."""
    from ..services import versions as versions_mod

    version = versions_mod.get_version(version_id, db)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    audio_path = config.resolve_storage_path(version.audio_path)
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(
        audio_path,
        media_type=_audio_media_type(audio_path),
        filename=f"generation_{version.generation_id}_{version.label}{audio_path.suffix}",
    )


@router.get("/audio/{generation_id}")
async def get_audio(generation_id: str, db: Session = Depends(get_db)):
    """Serve generated audio file (serves the default version)."""
    generation = await history.get_generation(generation_id, db)
    if not generation:
        raise HTTPException(status_code=404, detail="Generation not found")

    audio_path = config.resolve_storage_path(generation.audio_path)
    if audio_path is None or not audio_path.is_file():
        detail = "Generation failed; no audio available" if generation.status == "failed" else "Audio file not found"
        raise HTTPException(status_code=404, detail=detail)

    return FileResponse(
        audio_path,
        media_type=_audio_media_type(audio_path),
        filename=f"generation_{generation_id}{audio_path.suffix}",
    )


@router.get("/samples/{sample_id}")
async def get_sample_audio(sample_id: str, db: Session = Depends(get_db)):
    """Serve profile sample audio file."""
    from ..database import ProfileSample as DBProfileSample

    sample = db.query(DBProfileSample).filter_by(id=sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="Sample not found")

    audio_path = config.resolve_storage_path(sample.audio_path)
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(
        audio_path,
        media_type="audio/wav",
        filename=f"sample_{sample_id}.wav",
    )


@router.get("/samples/{sample_id}/export-audio")
async def export_sample_audio(
    sample_id: str,
    format_: str = Query("mp3", alias="format"),
    db: Session = Depends(get_db),
):
    """Export a saved reference recording as MP3 or its lossless WAV source."""
    from ..database import ProfileSample as DBProfileSample

    fmt = format_.lower()
    if fmt not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format_}")

    sample = db.query(DBProfileSample).filter_by(id=sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="Sample not found")

    audio_path = config.resolve_storage_path(sample.audio_path)
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    safe_text = "".join(c for c in sample.reference_text[:30] if c.isalnum() or c in (" ", "-", "_")).strip()
    filename = f"sample-{safe_text or 'recording'}-{sample_id[:8]}.{fmt}"
    headers = {"Content-Disposition": safe_content_disposition("attachment", filename)}
    if fmt == "wav":
        return FileResponse(audio_path, media_type="audio/wav", headers=headers)

    fd, tmp_name = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        await asyncio.to_thread(encode_audio, audio_path, tmp_path, fmt)
        mp3_bytes = tmp_path.read_bytes()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return Response(content=mp3_bytes, media_type="audio/mpeg", headers=headers)
