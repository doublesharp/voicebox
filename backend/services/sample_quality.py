"""Advisory waveform measurements for short voice-cloning references.

These are level heuristics, not speech recognition, source separation, or a
guarantee that a recording will make a good voice. Keep that distinction in the UI.
"""

import math
import subprocess
import unicodedata
from difflib import SequenceMatcher

import numpy as np
import soundfile as sf

MAX_DURATION_SECONDS = 60


def decode_reference(path):
    """Decode at most a minute; preserve channel energy for waveform checks."""
    try:
        with sf.SoundFile(path) as source:
            if (
                source.channels not in (1, 2)
                or not 8000 <= source.samplerate <= 192000
                or source.frames <= 0
                or source.frames > source.samplerate * MAX_DURATION_SECONDS
            ):
                raise ValueError("Use mono or stereo audio of at most 60 seconds (8-192 kHz).")
            return source.read(dtype="float32", always_2d=True), source.samplerate
    except (sf.LibsndfileError, RuntimeError):
        # Browser recordings can be WebM/Opus or M4A. Reuse the existing ffmpeg
        # locator and bound both decoded output and runtime; never follow URLs.
        from ..utils.ffmpeg import find_ffmpeg

        executable = find_ffmpeg()
        if executable is None:
            raise ValueError("This recording needs ffmpeg for analysis. Install ffmpeg or upload a WAV file.") from None
        try:
            result = subprocess.run(
                [
                    executable, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-protocol_whitelist", "file,pipe", "-i", str(path), "-vn",
                    "-t", str(MAX_DURATION_SECONDS + 1), "-ac", "2", "-ar", "24000",
                    "-f", "f32le", "pipe:1",
                ],
                capture_output=True, check=False, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Audio could not be decoded in time. Try a short WAV recording.") from exc
        if result.returncode or not result.stdout or len(result.stdout) % 8:
            raise ValueError("Audio could not be decoded. Try a WAV recording.") from None
        audio = np.frombuffer(result.stdout, dtype="<f4").reshape(-1, 2)
        if len(audio) > 24000 * MAX_DURATION_SECONDS:
            raise ValueError("Use a reference recording of at most 60 seconds.") from None
        return audio, 24000


def _db(value):
    return max(-120.0, 20 * math.log10(max(float(value), 1e-6)))


def _normalized_text(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in text).split())


def compare_transcripts(reference, recognized=None):
    if recognized is None:
        return {"status": "unchecked", "similarity": None, "recognized_text": None}
    if not isinstance(reference, str) or not isinstance(recognized, str):
        raise ValueError("Reference and recognized transcripts must be text.")
    expected, actual = _normalized_text(reference), _normalized_text(recognized)
    similarity = SequenceMatcher(None, expected, actual, autojunk=False).ratio() if expected and actual else 0.0
    return {
        "status": "similar" if similarity >= 0.9 else "review",
        "similarity": round(similarity, 3),
        "recognized_text": recognized,
    }


def analyze_reference(audio, sample_rate, transcript="", recognized_text=None):
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples[:, None]
    if (
        samples.ndim != 2 or samples.shape[1] not in (1, 2) or not len(samples)
        or not np.isfinite(samples).all() or type(sample_rate) is not int or sample_rate <= 0
        or len(samples) > sample_rate * MAX_DURATION_SECONDS
    ):
        raise ValueError("Reference audio must contain finite mono/stereo samples of at most 60 seconds.")
    window = max(1, round(sample_rate * 0.02))
    starts = np.arange(0, len(samples), window)
    lengths = np.minimum(window, len(samples) - starts)
    # Average channel energy rather than waveforms; opposite-phase stereo must
    # not be mistaken for silence and clipping on either channel must be visible.
    energy = np.mean(np.square(samples, dtype=np.float64), axis=1)
    rms_windows = np.sqrt(np.add.reduceat(energy, starts) / lengths)
    quiet = rms_windows < 10 ** (-45 / 20)
    peak = float(np.max(np.abs(samples)))
    clipped = float(np.mean(np.abs(samples) >= 0.999))
    silence = float(lengths[quiet].sum() / len(samples))
    nonquiet = np.flatnonzero(~quiet)
    leading = int(starts[nonquiet[0]]) if len(nonquiet) else len(samples)
    trailing = int(len(samples) - (starts[nonquiet[-1]] + lengths[nonquiet[-1]])) if len(nonquiet) else 0
    duration = len(samples) / sample_rate
    rms_db = _db(np.sqrt(np.mean(energy)))
    low, high = np.percentile(rms_windows, [20, 90])
    contrast = _db(high) - _db(low)
    issues = []

    def issue(code, message, suggestion):
        issues.append({"code": code, "message": message, "suggestion": suggestion})

    if clipped > 0.001:
        issue("clipping", "Peaks reach the digital ceiling in the decoded recording.",
              "Lower the microphone or input gain and record again; lowering this file's volume cannot repair clipped peaks.")
    if silence > 0.25 or leading / sample_rate > 0.5 or trailing / sample_rate > 0.5:
        issue("silence", "The recording includes substantial quiet time.",
              "Trim long pauses and silence at the start and end; keep a natural continuous reading.")
    if not len(nonquiet):
        issue("no_speech_level", "No audio above the quiet threshold was found.",
              "Check the selected microphone and record audible speech before using this reference.")
    elif rms_db < -32:
        issue("quiet", "The average recording level is low.",
              "Move a little closer to the microphone or raise input gain while leaving room below clipping.")
    if _db(low) > -38 and contrast < 14 and duration >= 2:
        issue("possible_noise", "Possible steady background noise or very little level variation.",
              "Listen for fans, hum, music, or other voices. Record in a quieter space if audible; continuous speech can also trigger this heuristic.")
    if duration < 3:
        issue("short", "The reference is shorter than three seconds.",
              "Record a few complete sentences from one speaker for a more representative voice sample.")
    elif duration > 30:
        issue("long", "The reference is longer than the 30-second profile limit.",
              "Choose a clear section of speech no longer than 30 seconds before adding it to a voice.")
    return {
        "method": "reference_levels_v1", "duration_seconds": duration,
        "sample_rate": sample_rate, "channels": samples.shape[1],
        "peak_dbfs": _db(peak), "rms_dbfs": rms_db,
        "clipped_fraction": clipped, "silence_fraction": silence,
        "leading_silence_seconds": leading / sample_rate, "trailing_silence_seconds": trailing / sample_rate,
        "quiet_level_dbfs": _db(low), "level_contrast_db": contrast,
        "noise_assessment": "heuristic", "issues": issues,
        "transcript": compare_transcripts(transcript, recognized_text),
    }
