"""
STT (Speech-to-Text) module - delegates to backend abstraction layer.
"""

import math
from importlib.metadata import PackageNotFoundError, version
from numbers import Real

from ..backends import STTBackend, get_stt_backend


def get_whisper_model() -> STTBackend:
    """
    Get STT backend instance (MLX or PyTorch based on platform).

    Returns:
        STT backend instance
    """
    return get_stt_backend()


def unload_whisper_model():
    """Unload Whisper model to free memory."""
    backend = get_stt_backend()
    backend.unload_model()


def normalize_recognition(result, duration):
    """Validate recognizer-owned word times; these do not align supplied text."""
    if not isinstance(result, dict) and not hasattr(result, "__dict__"):
        raise ValueError("Invalid recognition result")
    value = result if isinstance(result, dict) else vars(result)
    text = value.get("text")
    segments = value.get("segments", [])
    words = value.get("words")
    if words is None:
        if not isinstance(segments, list) or len(segments) > 2000:
            raise ValueError("Invalid recognition segments")
        words = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("words", []), list):
                raise ValueError("Invalid recognition words")
            words.extend(segment.get("words", []))
            if len(words) > 2000:
                raise ValueError("Recognition exceeds the bounded word limit")
    if not isinstance(text, str) or len(text) > 10000 or not isinstance(words, list) or len(words) > 2000:
        raise ValueError("Recognition exceeds the bounded text/word limit")
    result_words = []
    previous = 0.0
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("word"), str) or not word["word"].strip() or len(word["word"]) > 200:
            raise ValueError("Invalid recognized word")
        start, end = word.get("start"), word.get("end")
        if (any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) for value in (start, end))
                or not previous <= start < min(end, duration) or end > duration + 0.05):
            raise ValueError("Invalid recognized word timing")
        result_words.append({"word": word["word"].strip(), "start": float(start), "end": float(min(end, duration))})
        previous = start
    return {"text": text.strip(), "words": result_words}


async def recognize_words(audio_path, duration, language=None, model_size=None):
    """Explicit cached-only local word recognition, with truthful capabilities."""
    if not math.isfinite(duration) or not 0 < duration <= 120:
        raise ValueError("Speech review supports nonempty takes of at most 120 seconds")
    backend = get_whisper_model()
    size = model_size or backend.model_size
    detailed = getattr(backend, "transcribe_detailed", None)
    if not callable(detailed):
        raise ValueError("Word timestamps are unavailable on this recognition backend")
    if not backend.is_loaded() and not backend._is_model_cached(size):
        raise ValueError("Download the selected Whisper model in Server → Models before speech review")
    result = await detailed(str(audio_path), language=language, model_size=size)
    recognized = normalize_recognition(result, duration)
    try:
        package_version = version("mlx-audio")
    except PackageNotFoundError:
        package_version = None
    return {**recognized, "recognizer": {"backend": type(backend).__name__, "model_size": size,
            "package_version": package_version, "revision": getattr(backend, "_recognition_revision", None)}}
