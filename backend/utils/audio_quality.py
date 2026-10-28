"""Offline waveform/transcript measurements, separate from model acceptance.

Silence is an energy threshold, not voice activity detection. Full-scale
samples are clipping candidates, not proof of audible clipping. No model is
loaded, and transcript comparisons consume only text supplied by the caller.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import numpy as np

QUALITY_CORPUS = (
    {
        "id": "names-en",
        "language": "en",
        "transcript_unit": "word",
        "text": "Dr. Maya Chen met Siobhan O'Connor and José Alvarez outside Edinburgh station.",
    },
    {
        "id": "numbers-en",
        "language": "en",
        "transcript_unit": "word",
        "text": "The total is twelve dollars and fifty cents. Call extension two zero five at nine thirty.",
    },
    {
        "id": "abbreviations-en",
        "language": "en",
        "transcript_unit": "word",
        "text": "Prof. Jones lives on St. George Street in the U.S. Please bring the report, e.g. the final draft.",
    },
    {
        "id": "pauses-en",
        "language": "en",
        "transcript_unit": "word",
        "text": "Before we continue... take a breath. The room is quiet. Now continue, and keep this final sentence intact.",
    },
    {
        "id": "narration-en",
        "language": "en",
        "transcript_unit": "word",
        "text": "The train arrived just before sunrise. Mira stepped onto the platform, carrying a small notebook. "
        "She had written down every detail of the journey, including the names of the people she met.\n\n"
        "At the end of the street, a bakery opened its doors. Warm light spilled across the pavement. "
        "Mira stopped, read the sign, and decided there was time for breakfast before her next adventure.\n\n"
        "The baker asked whether she was travelling alone. She explained that her brother would arrive later, "
        "after taking the morning ferry across the bay. They planned to walk to the old lighthouse together. "
        "Neither of them had visited this town before, so they had left the afternoon free to explore.\n\n"
        "As she waited, Mira opened her notebook and drew a map of the streets around the station. "
        "She marked the bakery, the bridge and a quiet garden beside the river. By the time breakfast arrived, "
        "she had already found three places worth returning to. The next page would have to wait until tomorrow.",
    },
    {
        "id": "questions-es",
        "language": "es",
        "transcript_unit": "word",
        "text": "¿Puedes decirme dónde está la estación? El próximo tren sale a las ocho y media. Gracias por tu ayuda.",
    },
    {
        "id": "sentences-zh",
        "language": "zh",
        "transcript_unit": "character",
        "text": "早上好。请在下一站下车，然后向左转。我们会在图书馆门口见面。",  # noqa: RUF001
    },
    {
        "id": "sentences-ja",
        "language": "ja",
        "transcript_unit": "character",
        "text": "おはようございます。次の駅で降りて、左に曲がってください。図書館の入口で会いましょう。",
    },
)


def _measure_blocks(
    blocks: Iterable[np.ndarray],
    sample_rate: int,
    channels: int,
    *,
    frame_ms: int = 20,
    silence_threshold_db: float = -40,
    clipping_threshold: float = 1 - 1 / 32768,
) -> dict:
    if sample_rate <= 0 or channels <= 0 or frame_ms <= 0:
        raise ValueError("Sample rate, channels and frame length must be positive")
    if not math.isfinite(silence_threshold_db) or not 0 < clipping_threshold <= 1:
        raise ValueError("Metric thresholds must be finite and within their supported ranges")
    frame_length = max(1, int(sample_rate * frame_ms / 1000))
    silence_level = 10 ** (silence_threshold_db / 20)
    sample_frames = nonfinite = near_full_scale = silent_frames = 0
    leading = silence_run = longest_silence = 0
    speech_seen = False
    peak = relative_energy = 0.0

    for block in blocks:
        block = np.asarray(block, dtype=np.float64)
        sample_frames += len(block)
        finite = np.isfinite(block)
        nonfinite += int(np.count_nonzero(~finite))
        near_full_scale += int(np.count_nonzero(finite & (np.abs(block) >= clipping_threshold)))
        if block.size == 0 or not finite.all():
            continue
        block_peak = float(np.max(np.abs(block)))
        if block_peak > peak:
            relative_energy *= (peak / block_peak) ** 2
            peak = block_peak
        if peak:
            relative_energy += float(np.sum((block / peak) ** 2, dtype=np.float64))

        for start in range(0, len(block), frame_length):
            frame = block[start : start + frame_length]
            frame_peak = float(np.max(np.abs(frame)))
            rms = frame_peak * float(np.sqrt(np.mean((frame / frame_peak) ** 2))) if frame_peak else 0.0
            if rms < silence_level:
                silent_frames += len(frame)
                silence_run += len(frame)
                longest_silence = max(longest_silence, silence_run)
                if not speech_seen:
                    leading += len(frame)
            else:
                speech_seen = True
                silence_run = 0

    valid = nonfinite == 0
    rms = peak * math.sqrt(relative_energy / (sample_frames * channels)) if sample_frames and peak else 0.0
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_frames": sample_frames,
        "duration_seconds": sample_frames / sample_rate,
        "valid_samples": valid,
        "nonfinite_samples": nonfinite,
        "peak": peak if valid else None,
        "peak_dbfs": 20 * math.log10(peak) if valid and peak else None,
        "rms": rms if valid else None,
        "rms_dbfs": 20 * math.log10(rms) if valid and rms else None,
        "near_full_scale_samples": near_full_scale,
        "clipping_threshold": clipping_threshold,
        "silence_threshold_db": silence_threshold_db,
        "silence_fraction": silent_frames / sample_frames if valid and sample_frames else None,
        "leading_silence_seconds": leading / sample_rate if valid else None,
        "trailing_silence_seconds": silence_run / sample_rate if valid else None,
        "longest_silence_seconds": longest_silence / sample_rate if valid else None,
    }


def analyze_waveform(audio: np.ndarray, sample_rate: int, **kwargs) -> dict:
    """Measure mono or frame-major multichannel PCM without modifying it."""
    audio = np.asarray(audio)
    if audio.ndim == 1:
        audio = audio[:, np.newaxis]
    if audio.ndim != 2 or audio.shape[1] == 0:
        raise ValueError("Expected mono or frame-major multichannel audio")
    return _measure_blocks([audio], sample_rate, audio.shape[1], **kwargs)


def analyze_audio_file(path: str | Path, **kwargs) -> dict:
    """Read a WAV in bounded blocks; no full audiobook decode is retained."""
    import soundfile as sf

    with sf.SoundFile(path) as source:
        if not source.format.startswith("WAV"):
            raise ValueError("The offline quality tier expects WAV input")
        frame_length = max(1, int(source.samplerate * kwargs.get("frame_ms", 20) / 1000))
        return _measure_blocks(
            source.blocks(blocksize=frame_length * 500, dtype="float64", always_2d=True),
            source.samplerate,
            source.channels,
            **kwargs,
        )


def transcript_metrics(reference: str, hypothesis: str, *, unit: str = "word") -> dict:
    """WER/CER and edit counts from supplied text, with bounded token counts.

    Tied minimal alignments prefer fewer substitutions. Repetition is a
    token-frequency excess over the reference, not an acoustic classification.
    """

    def tokenize(text):
        text = unicodedata.normalize("NFKC", text).casefold()
        if unit == "character":
            return [character for character in text if character.isalnum()]
        if unit == "word":
            return re.findall(r"\w+(?:['\u2019\-]\w+)*", text)
        raise ValueError("Transcript unit must be 'word' or 'character'")

    expected, actual = tokenize(reference), tokenize(hypothesis)
    if max(len(expected), len(actual)) > 2000:
        raise ValueError("Transcript comparisons are limited to 2000 tokens per sample")
    # Rows store (cost, substitutions, deletions, insertions), avoiding a full
    # quadratic matrix while retaining deterministic edit-count tie breaking.
    previous = [(i, 0, 0, i) for i in range(len(actual) + 1)]
    for i, expected_token in enumerate(expected, 1):
        current = [(i, 0, i, 0)]
        for j, actual_token in enumerate(actual, 1):
            if expected_token == actual_token:
                current.append(previous[j - 1])
            else:
                substitute, delete, insert = previous[j - 1], previous[j], current[j - 1]
                current.append(
                    min(
                        (substitute[0] + 1, substitute[1] + 1, substitute[2], substitute[3]),
                        (delete[0] + 1, delete[1], delete[2] + 1, delete[3]),
                        (insert[0] + 1, insert[1], insert[2], insert[3] + 1),
                    )
                )
        previous = current
    errors, substitutions, deletions, insertions = previous[-1]
    expected_counts, actual_counts = Counter(expected), Counter(actual)
    repeated = sum(max(0, count - max(1, expected_counts[token])) for token, count in actual_counts.items())
    return {
        "unit": unit,
        "reference_tokens": len(expected),
        "hypothesis_tokens": len(actual),
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "error_rate": errors / len(expected) if expected else (None if actual else 0.0),
        "extra_repeated_tokens": repeated,
        "transcript_source": "supplied",
    }
