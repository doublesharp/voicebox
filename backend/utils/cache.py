"""
Voice prompt caching utilities.
"""

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

import torch

from .. import config

logger = logging.getLogger(__name__)


def _get_cache_dir() -> Path:
    """Get cache directory from config."""
    return config.get_cache_dir()


# In-memory cache - can store dict (voice prompt) or tensor (legacy)
_memory_cache: dict[str, Union[torch.Tensor, Dict[str, Any]]] = {}


def get_cache_key(audio_path: str, reference_text: str, *, namespace: str | None = None) -> str:
    """
    Generate cache key from audio file and reference text.

    Args:
        audio_path: Path to audio file
        reference_text: Reference text
        namespace: Engine/backend/model/cache-format identity for new formats.

    Returns:
        Content/transcript hash, isolated by namespace when provided.
    """
    # Older adapters retain their existing keys. New cache formats must supply
    # a namespace including engine/backend/model/format before enabling reuse.
    digest = hashlib.sha256() if namespace is not None else hashlib.md5()
    if namespace is not None:
        digest.update(json.dumps([namespace, reference_text], ensure_ascii=False).encode("utf-8"))
        digest.update(b"\0")
    with open(audio_path, "rb") as f:
        while block := f.read(1024 * 1024):
            digest.update(block)
    if namespace is None:
        digest.update(reference_text.encode("utf-8"))
    return digest.hexdigest()


def get_cached_voice_prompt(
    cache_key: str,
) -> Optional[Union[torch.Tensor, Dict[str, Any]]]:
    """
    Get cached voice prompt if available.

    Args:
        cache_key: Cache key

    Returns:
        Cached voice prompt (dict or tensor) or None
    """
    # Check in-memory cache
    if cache_key in _memory_cache:
        return _memory_cache[cache_key]

    # Check disk cache
    cache_file = _get_cache_dir() / f"{cache_key}.prompt"
    if cache_file.exists():
        try:
            prompt = torch.load(cache_file, weights_only=True)
            _memory_cache[cache_key] = prompt
            return prompt
        except Exception:
            # Cache file corrupted, delete it
            cache_file.unlink(missing_ok=True)

    return None


def cache_voice_prompt(
    cache_key: str,
    voice_prompt: Union[torch.Tensor, Dict[str, Any]],
) -> None:
    """
    Cache voice prompt to memory and disk.

    Args:
        cache_key: Cache key
        voice_prompt: Voice prompt (dict or tensor)
    """
    # Store in memory
    _memory_cache[cache_key] = voice_prompt

    # A failed/interrupted writer must not expose a partial prompt to a reader.
    cache_dir = _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{cache_key}.prompt"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=f"{cache_key}.", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            torch.save(voice_prompt, handle)
        os.replace(temporary_path, cache_file)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def clear_voice_prompt_memory_cache() -> None:
    """
    Drop the in-memory voice prompt cache without touching the disk cache.

    Backends call this when a TTS model unloads: the cached prompts (tensors
    or device-backed dicts produced by that model) would otherwise keep
    referencing memory forever, since nothing else ever clears this
    process-lifetime dict. The disk cache is left alone, so the next
    generation just reloads the prompt from disk instead of recomputing it.
    """
    _memory_cache.clear()


def clear_voice_prompt_cache() -> int:
    """
    Clear all voice prompt caches (memory and disk).

    Returns:
        Number of cache files deleted
    """
    # Clear memory cache
    _memory_cache.clear()

    # Clear disk cache
    cache_dir = _get_cache_dir()
    deleted_count = 0

    if cache_dir.exists():
        # Delete prompt cache files
        for cache_file in cache_dir.glob("*.prompt"):
            try:
                cache_file.unlink()
                deleted_count += 1
            except Exception as e:
                logger.warning("Failed to delete cache file %s: %s", cache_file, e)

        # Delete combined audio files
        for audio_file in cache_dir.glob("combined_*.wav"):
            try:
                audio_file.unlink()
                deleted_count += 1
            except Exception as e:
                logger.warning("Failed to delete combined audio file %s: %s", audio_file, e)

    return deleted_count


def clear_profile_cache(profile_id: str) -> int:
    """
    Clear cache files for a specific profile.

    Args:
        profile_id: Profile ID

    Returns:
        Number of cache files deleted
    """
    cache_dir = _get_cache_dir()
    deleted_count = 0

    if cache_dir.exists():
        # Delete combined audio files for this profile
        pattern = f"combined_{profile_id}_*.wav"
        for audio_file in cache_dir.glob(pattern):
            try:
                audio_file.unlink()
                deleted_count += 1
            except Exception as e:
                logger.warning("Failed to delete combined audio file %s: %s", audio_file, e)

    return deleted_count
