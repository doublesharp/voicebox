"""
Audio processing utilities.
"""

import re
import unicodedata

import numpy as np
import soundfile as sf
import librosa
from typing import Tuple, Optional


def normalize_audio(
    audio: np.ndarray,
    target_db: float = -20.0,
    peak_limit: float = 0.85,
) -> np.ndarray:
    """
    Normalize RMS with a single gain capped by available peak headroom.
    
    Args:
        audio: Input audio array
        target_db: Target RMS level in dB
        peak_limit: Peak limit (0.0-1.0)
        
    Returns:
        Normalized audio array
    """
    audio = np.asarray(audio, dtype=np.float32)
    if not np.isfinite(audio).all():
        raise ValueError("Audio samples must be finite")
    if not np.isfinite(target_db) or not 0 < peak_limit <= 1:
        raise ValueError("Normalization requires a finite target and a peak limit in (0, 1]")
    if audio.size == 0:
        return audio.copy()

    peak = float(np.abs(audio).max())
    if peak == 0:
        return audio.copy()

    # Scale before squaring to avoid overflowing on otherwise finite inputs.
    relative_rms = float(np.sqrt(np.mean((audio / peak) ** 2, dtype=np.float64)))
    target_rms = 10 ** (target_db / 20)
    gain = min(target_rms / (peak * relative_rms), peak_limit / peak)
    return audio * gain


def protect_audio_peak(audio: np.ndarray, peak_limit: float = 0.999) -> np.ndarray:
    """Attenuate hot output before PCM encoding without flattening its peaks.

    This is a single linear gain, not a compressor or perceptual loudness
    normalizer. Quiet audio is left at its requested level, including when
    output normalization is disabled.
    """
    audio = np.asarray(audio, dtype=np.float32)
    if not np.isfinite(audio).all():
        raise ValueError("Audio samples must be finite")
    if not 0 < peak_limit <= 1:
        raise ValueError("Peak limit must be in (0, 1]")
    if audio.size == 0:
        return audio.copy()
    peak = float(np.abs(audio).max())
    return audio * (peak_limit / peak) if peak > peak_limit else audio.copy()


def load_audio(
    path: str,
    sample_rate: Optional[int] = 24000,
    mono: bool = True,
) -> Tuple[np.ndarray, int]:
    """
    Load audio file with normalization.
    
    Args:
        path: Path to audio file
        sample_rate: Target sample rate, or None to preserve the source rate
        mono: Convert to mono
        
    Returns:
        Tuple of (audio_array, sample_rate)
    """
    audio, sr = librosa.load(path, sr=sample_rate, mono=mono)
    return audio, sr


def save_audio(
    audio: np.ndarray,
    path: str,
    sample_rate: int = 24000,
) -> None:
    """
    Save audio file with atomic write and error handling.

    Writes to a temporary file first, then atomically renames to the
    target path.  This prevents corrupted/partial WAV files if the
    process is interrupted mid-write.

    Args:
        audio: Audio array
        path: Output path
        sample_rate: Sample rate

    Raises:
        OSError: If file cannot be written
    """
    from pathlib import Path
    import os

    temp_path = f"{path}.tmp"
    try:
        # Ensure parent directory exists
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        # Write to temporary file first (explicit format since .tmp
        # extension is not recognised by soundfile)
        sf.write(temp_path, audio, sample_rate, format='WAV')

        # Atomic rename to final path
        os.replace(temp_path, path)

    except Exception as e:
        # Clean up temp file on failure
        try:
            if Path(temp_path).exists():
                Path(temp_path).unlink()
        except Exception:
            pass  # Best effort cleanup

        raise OSError(f"Failed to save audio to {path}: {e}") from e


def has_tts_runaway(
    audio: np.ndarray,
    sample_rate: int = 24000,
    frame_ms: int = 20,
    silence_threshold_db: float = -40.0,
    max_internal_silence_ms: int = 2000,
) -> bool:
    """Detect speech followed by a long silence and then more output.

    This shape is a reliable signal that a TTS model missed EOS and resumed
    with hallucinated speech or codec noise. Leading and trailing silence do
    not count because they are not bounded by non-silent audio.
    """
    frame_len = int(sample_rate * frame_ms / 1000)
    if frame_len == 0 or len(audio) < frame_len:
        return False

    n_frames = len(audio) // frame_len
    threshold_linear = 10 ** (silence_threshold_db / 20)
    max_silence_frames = int(max_internal_silence_ms / frame_ms)
    seen_speech = False
    consecutive_silence = 0

    for i in range(n_frames):
        frame = audio[i * frame_len : (i + 1) * frame_len]
        is_speech = np.sqrt(np.mean(frame**2)) >= threshold_linear
        if is_speech:
            if seen_speech and consecutive_silence >= max_silence_frames:
                return True
            seen_speech = True
            consecutive_silence = 0
        elif seen_speech:
            consecutive_silence += 1

    return False


def _text_duration_budget(text: str | None) -> float | None:
    """A deliberately slow reading budget, used only for tiny residual bursts.

    This is not alignment or a speech-completeness test. Count wide-script
    characters and digits generously, and exclude nonspoken bracket tags.
    Internal pauses never count toward the preceding spoken duration.
    """
    if not text:
        return None
    spoken = re.sub(r"\[[^\]]*\]", "", text)
    units = sum(
        8 if char.isdigit() else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in spoken
        if char.isalnum()
    )
    return max(5.0, units / 3.0) if units else None


def _stationary_noise_tail(
    frames: np.ndarray, rms: np.ndarray, frame_seconds: float, min_duration: float = 0.3
) -> bool:
    """Recognize sustained broadband hiss, preserving modulated/tonal output.

    A quiet or short continuation alone is not evidence of hallucination.
    Require nearly steady energy and a flat spectrum for the minimum duration.
    More than 40 ms of non-noise-like content keeps the entire continuation.
    """
    if len(frames) * frame_seconds < min_duration:
        return False
    mean_rms = float(np.mean(rms))
    if mean_rms <= 0 or float(np.std(rms)) / mean_rms > 0.15:
        return False
    power = np.abs(np.fft.rfft(frames * np.hanning(frames.shape[1]), axis=1))[:, 1:] ** 2
    if not power.shape[1]:
        return False
    epsilon = np.finfo(np.float64).tiny
    arithmetic_mean = np.mean(power, axis=1)
    flatness = np.exp(np.mean(np.log(np.maximum(power, epsilon)), axis=1)) / np.maximum(
        arithmetic_mean, epsilon
    )
    noise_like = (flatness >= 0.25) & (arithmetic_mean > np.finfo(np.float64).eps)
    return np.count_nonzero(~noise_like) * frame_seconds <= 0.04


def trim_tts_output(
    audio: np.ndarray,
    sample_rate: int = 24000,
    frame_ms: int = 20,
    silence_threshold_db: float = -40.0,
    min_silence_ms: int = 200,
    max_internal_silence_ms: int | None = 1000,
    fade_ms: int = 30,
    *,
    text: str | None = None,
) -> np.ndarray:
    """Trim edges and guarded post-silence artifacts without discarding pauses.

    A long gap is a candidate boundary, not proof that the sentence ended.
    Preserve resumed speech unless the remaining active audio is stationary
    broadband noise for at least 300 ms. Tiny noise bursts (60-250 ms) also
    require preceding active audio beyond a generous text-duration budget.
    A duration estimate alone never discards a continuation. Waveform
    heuristics cannot identify hallucinated words reliably.

    ``max_internal_silence_ms=None`` disables internal cuts (used by Kokoro).
    Keep up to ``min_silence_ms`` of the original trailing silence and apply
    the existing cosine fade. ``text`` is optional for legacy callers.
    """
    frame_len = int(sample_rate * frame_ms / 1000)
    if frame_len <= 0 or len(audio) < frame_len:
        return audio

    # Include the final partial frame so a short final syllable is not lost.
    n_frames = (len(audio) + frame_len - 1) // frame_len
    padded = np.pad(audio, (0, n_frames * frame_len - len(audio)))
    frames = padded.reshape(n_frames, frame_len)
    counts = np.full(n_frames, frame_len)
    counts[-1] = len(audio) - (n_frames - 1) * frame_len
    rms = np.sqrt(np.sum(frames.astype(np.float64) ** 2, axis=1) / counts)
    is_speech = rms >= 10 ** (silence_threshold_db / 20)
    speech_frames = np.flatnonzero(is_speech)
    first_speech = max(0, int(speech_frames[0]) - 1) if len(speech_frames) else 0
    cut_frame = n_frames

    if max_internal_silence_ms is not None and len(speech_frames) > 1:
        frame_seconds = frame_len / sample_rate
        max_silence_frames = max(1, int(np.ceil(max_internal_silence_ms / (frame_seconds * 1000))))
        budget = _text_duration_budget(text)
        gaps = np.flatnonzero(np.diff(speech_frames) - 1 >= max_silence_frames)
        for gap in gaps:
            next_speech = int(speech_frames[gap + 1])
            remaining = speech_frames[gap + 1:]
            short_after_complete = (
                budget is not None
                and (gap + 1) * frame_seconds >= budget
                and len(remaining) * frame_seconds <= 0.25
            )
            minimum_noise = 0.06 if short_after_complete else 0.3
            if _stationary_noise_tail(frames[remaining], rms[remaining], frame_seconds, minimum_noise):
                cut_frame = next_speech  # retain original silence before the suspect tail
                break

    # Trim the retained region's trailing silence, keeping a natural tail.
    min_silence_frames = max(0, int(min_silence_ms / (frame_len / sample_rate * 1000)))
    end_frame = cut_frame
    while end_frame > first_speech and not is_speech[end_frame - 1]:
        end_frame -= 1
    end_frame = min(end_frame + min_silence_frames, cut_frame)
    trimmed = audio[first_speech * frame_len:min(end_frame * frame_len, len(audio))].copy()

    fade_samples = int(sample_rate * fade_ms / 1000)
    if fade_samples > 0 and len(trimmed) > fade_samples:
        fade = np.cos(np.linspace(0, np.pi / 2, fade_samples)) ** 2
        trimmed[-fade_samples:] *= fade
    return trimmed


def preprocess_reference_audio(
    audio: np.ndarray,
    sample_rate: int,
    peak_target: float = 0.95,
    trim_top_db: float = 40.0,
    edge_padding_ms: int = 100,
) -> np.ndarray:
    """
    Clean up a reference-audio sample before validation/storage.

    Removes DC offset, trims leading/trailing silence, and caps the peak so a
    slightly-hot recording doesn't get rejected downstream as "clipping". The
    goal is to accept reasonable real-world recordings — not to repair badly
    distorted ones. True clipping artifacts inside the waveform can't be
    recovered by peak scaling and will still sound bad.

    Args:
        audio: Mono audio array.
        sample_rate: Sample rate of ``audio`` in Hz.
        peak_target: Peak amplitude cap in [0, 1]. Applied only if the input
            peak exceeds this value.
        trim_top_db: Silence threshold for edge trimming, in dB below peak.
            40 dB sits below normal speech dynamic range (≈30 dB) so soft
            trailing syllables are preserved, while still catching obvious
            leading/trailing silence. Lower values are more aggressive;
            librosa's own default is 60.
        edge_padding_ms: Milliseconds of padding to add back at each edge
            *only if* trimming shortened the waveform, so TTS engines have a
            brief silence to anchor on without ever making the output longer
            than the input.

    Returns:
        Preprocessed audio array (float32).
    """
    audio = audio.astype(np.float32, copy=False)

    if audio.size == 0:
        return audio

    audio = audio - float(np.mean(audio))

    trimmed, _ = librosa.effects.trim(audio, top_db=trim_top_db)
    if 0 < trimmed.size < audio.size:
        pad_each = int(sample_rate * edge_padding_ms / 1000)
        # Never pad past the original length — for near-max-duration uploads
        # an unconditional pad would push them over the 30 s ceiling and
        # trigger a spurious "too long" rejection.
        headroom = (audio.size - trimmed.size) // 2
        pad = min(pad_each, max(headroom, 0))
        if pad > 0:
            trimmed = np.pad(trimmed, (pad, pad), mode="constant")
        audio = trimmed

    peak = float(np.abs(audio).max())
    if peak > peak_target and peak > 0:
        audio = audio * (peak_target / peak)

    return audio


def validate_reference_audio(
    audio_path: str,
    min_duration: float = 2.0,
    max_duration: float = 30.0,
    min_rms: float = 0.01,
) -> Tuple[bool, Optional[str]]:
    """
    Validate reference audio for voice cloning.

    Args:
        audio_path: Path to audio file
        min_duration: Minimum duration in seconds
        max_duration: Maximum duration in seconds
        min_rms: Minimum RMS level

    Returns:
        Tuple of (is_valid, error_message)
    """
    result = validate_and_load_reference_audio(
        audio_path, min_duration, max_duration, min_rms
    )
    return (result[0], result[1])


def validate_and_load_reference_audio(
    audio_path: str,
    min_duration: float = 2.0,
    max_duration: float = 30.0,
    min_rms: float = 0.01,
) -> Tuple[bool, Optional[str], Optional[np.ndarray], Optional[int]]:
    """
    Validate and load reference audio in a single pass.

    Applies :func:`preprocess_reference_audio` before checks so that
    slightly-hot recordings aren't rejected as clipping. Duration and RMS
    checks run on the preprocessed waveform.

    Returns:
        Tuple of (is_valid, error_message, audio_array, sample_rate)
    """
    try:
        audio, sr = load_audio(audio_path)
        audio = preprocess_reference_audio(audio, sr)
        duration = len(audio) / sr

        if duration < min_duration:
            return False, f"Audio too short (minimum {min_duration} seconds)", None, None
        if duration > max_duration:
            return False, f"Audio too long (maximum {max_duration} seconds)", None, None

        rms = np.sqrt(np.mean(audio**2))
        if rms < min_rms:
            return False, "Audio is too quiet or silent", None, None

        return True, None, audio, sr
    except Exception as e:
        return False, f"Error validating audio: {str(e)}", None, None
