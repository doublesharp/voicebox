"""Bounded header-only metadata reads for immutable rendered audio files."""

import math
from functools import lru_cache
from pathlib import Path

import soundfile as sf

from .. import config


@lru_cache(maxsize=512)
def _duration_from_header(path: str, inode: int, size: int, modified_ns: int, changed_ns: int) -> float:
    # Stat fields are cache identity, including replacements at the same path.
    info = sf.info(path)
    duration = info.frames / info.samplerate
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("Invalid audio duration")
    return duration


def audio_duration(audio_path: str | Path | None, fallback: float | None = None) -> float | None:
    """Read duration without decoding PCM; preserve the caller's legacy fallback.

    Missing/corrupt audio must retain the existing API behavior rather than
    make history or a story unreadable. Failed reads are not cached, so a file
    that becomes available later can be measured on the next request.
    """
    if audio_path is None:
        return fallback
    try:
        resolved = config.resolve_storage_path(str(audio_path))
        if resolved is None:
            return fallback
        stat = resolved.stat()
        return _duration_from_header(str(resolved), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except (OSError, RuntimeError, ValueError, ZeroDivisionError):
        return fallback
