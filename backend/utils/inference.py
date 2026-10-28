"""Compatibility exports for the shared Studio and legacy TTS owner."""

from ..services.inference_scheduler import (
    run_serialized_inference as run_inference,
    serialized_inference,
    shutdown_inference,
    start_inference,
)

__all__ = ["run_inference", "serialized_inference", "shutdown_inference", "start_inference"]
