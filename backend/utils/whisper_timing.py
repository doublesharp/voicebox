"""Scoped compatibility for the audited mlx-audio 0.4.1 Whisper head-name bug."""

from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version


@contextmanager
def recognized_word_timing(model):
    """Alias existing heads during inference; never infer supplied script words."""
    try:
        audited = version("mlx-audio") == "0.4.1"
    except PackageNotFoundError:
        audited = False
    alias = (
        audited
        and type(model).__module__ == "mlx_audio.stt.models.whisper.whisper"
        and type(model).__name__ == "Model"
        and not hasattr(model, "alignment_heads")
        and hasattr(model, "_alignment_heads")
    )
    if alias:
        model.alignment_heads = model._alignment_heads
    try:
        yield
    finally:
        if alias:
            del model.alignment_heads
