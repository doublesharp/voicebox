"""
STT (Speech-to-Text) module - delegates to backend abstraction layer.
"""

from typing import Optional
from ..backends import get_stt_backend, STTBackend


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


async def unload_whisper_model_async():
    """Wait for active transcription before releasing the Whisper model."""
    backend = get_stt_backend()
    lock = getattr(backend, "_op_lock", None)
    if lock is not None:
        async with lock:
            backend.unload_model()
    else:
        backend.unload_model()
