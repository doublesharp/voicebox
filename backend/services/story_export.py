"""Bounded story mixing: source blocks and a file-backed two-pass peak guard."""

import math
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from ..utils.async_work import run_blocking
from ..utils.ffmpeg import find_ffmpeg

BLOCK_FRAMES = 65536


def _check_cancel(cancelled):
    if cancelled():
        raise RuntimeError("Audio export cancelled")


def _inspect_source(path, working, index, cancelled):
    if not path.is_file():
        raise ValueError("Audio source is missing")
    try:
        return path, sf.info(path)
    except (ValueError, RuntimeError, OSError):
        # Imported legacy formats may need ffmpeg; decode to disk, never RAM.
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise ValueError("Audio source could not be read") from None
        decoded = working / f"decoded-{index}.wav"
        with subprocess.Popen(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-c:a", "pcm_f32le", str(decoded)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 600
                while True:
                    _check_cancel(cancelled)
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Audio source decoding timed out")
                    try:
                        _, error = process.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise ValueError("Audio source could not be decoded: " + error.decode(errors="replace")[-1000:])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        return decoded, sf.info(decoded)


def _prepare_sources(clips, working, cancelled):
    sources = []
    for index, clip in enumerate(clips):
        _check_cancel(cancelled)
        try:
            path, info = _inspect_source(Path(clip["path"]), working, index, cancelled)
            start = int(clip["trim_start_ms"] / 1000 * info.samplerate)
            end = info.frames - int(clip["trim_end_ms"] / 1000 * info.samplerate)
            if start < 0 or start >= end or end > info.frames or info.samplerate <= 0 or info.channels <= 0:
                raise ValueError("Source has no audio after trimming")
            sources.append({**clip, "path": path, "info": info, "trim_start": start, "frames": end - start})
        except (ValueError, RuntimeError, OSError) as error:
            raise ValueError(f"Story item {clip['id']} audio could not be read: {error}") from error
    rate = max(source["info"].samplerate for source in sources)
    channels = max(source["info"].channels for source in sources)
    for index, source in enumerate(sources):
        source["start"] = int(source["start_time_ms"] / 1000 * rate)
        if source["start"] < 0 or not math.isfinite(source["volume"]):
            raise ValueError(f"Story item {source['id']} has invalid placement or volume")
        if source["info"].samplerate == rate:
            continue
        import soxr

        expected = math.ceil(source["frames"] * rate / source["info"].samplerate)
        path = working / f"resampled-{index}.wav"
        resampler = soxr.ResampleStream(
            source["info"].samplerate, rate, source["info"].channels, dtype="float32", quality="HQ"
        )
        with (
            sf.SoundFile(source["path"]) as audio,
            sf.SoundFile(
                path,
                "w",
                samplerate=rate,
                channels=audio.channels,
                subtype="FLOAT",
            ) as output,
        ):
            audio.seek(source["trim_start"])
            remaining = source["frames"]
            source_block = max(1, BLOCK_FRAMES * source["info"].samplerate // rate)
            while remaining:
                _check_cancel(cancelled)
                data = audio.read(min(source_block, remaining), dtype="float32", always_2d=True)
                if not len(data) or not np.isfinite(data).all():
                    raise ValueError(f"Story item {source['id']} audio is incomplete or invalid")
                remaining -= len(data)
                output.write(resampler.resample_chunk(data, last=remaining == 0))
            # Match librosa's ceil-based fixed output length.
            if output.tell() < expected:
                output.write(np.zeros((expected - output.tell(), audio.channels), dtype=np.float32))
            elif output.tell() > expected:
                output.truncate(expected)
        source.update(path=path, trim_start=0, frames=expected)
    return sources, rate, channels


def _render(clips, fmt, chapters_mode, cancelled, owned):
    from .stories import _derive_chapters_auto, _ffmpeg_encode, _make_tempfile

    with tempfile.TemporaryDirectory(prefix="voicebox-story-") as directory:
        working = Path(directory)
        sources, rate, channels = _prepare_sources(clips, working, cancelled)
        total = max(source["start"] + source["frames"] for source in sources)
        raw_mix = working / "mix.wav"
        peak = 0.0
        with sf.SoundFile(raw_mix, "w", samplerate=rate, channels=channels, subtype="FLOAT") as output:
            for begin in range(0, total, BLOCK_FRAMES):
                _check_cancel(cancelled)
                end = min(total, begin + BLOCK_FRAMES)
                block = np.zeros((end - begin, channels), dtype=np.float32)
                for source in sources:
                    lo, hi = max(begin, source["start"]), min(end, source["start"] + source["frames"])
                    if hi <= lo:
                        continue
                    with sf.SoundFile(source["path"]) as audio:
                        audio.seek(source["trim_start"] + lo - source["start"])
                        data = audio.read(hi - lo, dtype="float32", always_2d=True)
                    if len(data) != hi - lo or not np.isfinite(data).all():
                        raise ValueError(f"Story item {source['id']} audio is incomplete or invalid")
                    if data.shape[1] == 1 and channels > 1:
                        data = np.repeat(data, channels, axis=1)
                    elif data.shape[1] < channels:
                        data = np.pad(data, ((0, 0), (0, channels - data.shape[1])))
                    block[lo - begin : hi - begin] += data * source["volume"]
                if not np.isfinite(block).all():
                    raise ValueError("Story mix contains invalid samples")
                peak = max(peak, float(np.max(np.abs(block))))
                output.write(block)
        wav = _make_tempfile(".wav")
        owned.append(wav)
        gain = 0.999 / peak if peak > 0.999 else 1.0
        with (
            sf.SoundFile(raw_mix) as audio,
            sf.SoundFile(wav, "w", samplerate=rate, channels=channels, subtype="PCM_16") as output,
        ):
            while True:
                _check_cancel(cancelled)
                data = audio.read(BLOCK_FRAMES, dtype="float32", always_2d=True)
                if not len(data):
                    break
                output.write(data * gain)
        if fmt == "wav":
            return wav
        output = _make_tempfile("." + fmt)
        owned.append(output)
        chapters = _derive_chapters_auto(sources, math.ceil(total / rate * 1000)) if chapters_mode == "auto" else None
        _ffmpeg_encode(wav, output, fmt, chapters, cancelled=cancelled)
        wav.unlink(missing_ok=True)
        return output


async def render_story_file(clips, fmt, chapters_mode):
    cancelled = threading.Event()
    owned = []
    try:
        return await run_blocking(_render, clips, fmt, chapters_mode, cancelled.is_set, owned, _on_cancel=cancelled.set)
    except BaseException:
        # run_blocking drains file owners before this cleanup.
        for path in owned:
            path.unlink(missing_ok=True)
        raise
