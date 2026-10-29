"""Pitch-preserving speaking rate shared by generated speech APIs."""

import math

import numpy as np


def apply_speed(audio: np.ndarray, sample_rate: int, speed: float) -> np.ndarray:
    """Time-stretch without changing pitch; rates below one lengthen speech.

    Keep the input untouched at the normal rate. Model adapters must not
    receive this value: several accept a speed argument but ignore it.
    """
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("Speech speed must be finite and positive")
    if abs(speed - 1.0) < 1e-3 or audio.size == 0:
        return audio
    import librosa

    stretched = librosa.effects.time_stretch(audio.astype(np.float32), rate=speed)
    return stretched.astype(audio.dtype, copy=False)
