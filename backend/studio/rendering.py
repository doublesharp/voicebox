"""Offline, sample-domain studio rendering. Sources are never modified."""

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .timing import resampled_frames


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _gain(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("gain must be finite and nonnegative")
    return float(value)


BLOCK_FRAMES = 65536
MAX_DURATION_SECONDS = 7200


def _check_cancel(cancelled):
    if cancelled and cancelled():
        raise ValueError("Render cancelled")


def _process(command, cancelled=None):
    """Reap an encoder before returning, including on cancellation."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        started = time.monotonic()
        try:
            while True:
                _check_cancel(cancelled)
                if time.monotonic() - started > 3600:
                    raise ValueError("Encoder time limit exceeded")
                try:
                    out, err = process.communicate(timeout=0.2)
                    if process.returncode:
                        raise ValueError("Audio encoder or meter failed: " + err.decode(errors="replace")[-1000:])
                    return err.decode(errors="replace")
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def _measure(path, cancelled=None):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"status": "unavailable", "integrated_lufs": None, "true_peak_dbtp": None}
    _check_cancel(cancelled)
    try:
        stderr = _process(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-i",
                str(path),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ],
            cancelled,
        )
        report = json.loads(stderr[stderr.rindex("{") : stderr.rindex("}") + 1])
        loudness, peak = float(report["input_i"]), float(report["input_tp"])
        if not math.isfinite(loudness) or not math.isfinite(peak):
            return {"status": "unavailable", "integrated_lufs": None, "true_peak_dbtp": None}
        return {"status": "measured", "integrated_lufs": loudness, "true_peak_dbtp": peak, "method": "ffmpeg loudnorm"}
    except (ValueError, KeyError, json.JSONDecodeError):
        _check_cancel(cancelled)
        return {"status": "unavailable", "integrated_lufs": None, "true_peak_dbtp": None}


def _source_range(path, info, rate, start, end):
    """Read only a working-sample range, retaining the resampler's global phase."""
    with sf.SoundFile(str(path)) as source:
        if info.samplerate == rate:
            source.seek(start)
            data = source.read(end - start, dtype="float64", always_2d=True)
        else:
            divisor = math.gcd(rate, info.samplerate)
            up, down = rate // divisor, info.samplerate // divisor
            # scipy's default FIR extends 10*max(up,down) samples on either side.
            padding = math.ceil(10 * max(up, down) / up) + 2
            left = max(0, (math.floor(start * down / up) - padding) // down * down)
            right = min(info.frames, math.ceil(end * down / up) + padding)
            source.seek(left)
            data = source.read(right - left, dtype="float64", always_2d=True)
            data = resample_poly(data, up, down, axis=0)
            offset = start - left * up // down
            data = data[offset : offset + end - start]
    if len(data) != end - start or not np.isfinite(data).all():
        raise ValueError("Invalid source audio range")
    return data


def render_project(
    document, assets, resolve_asset, output_path, *, kind="mix", format="wav", cancelled=None, progress=None
):
    if kind not in ("mix", "stems") or format not in ("wav", "mp3"):
        raise ValueError("unsupported render kind or format")
    rate = _integer(document["sample_rate"], "sample rate")
    channels = document["channels"]
    if not 8000 <= rate <= 192000 or type(channels) is not int or channels not in (1, 2):
        raise ValueError("unsupported working format")
    tracks = document["tracks"]
    if len(tracks) > 128 or len(document["clips"]) > 10000:
        raise ValueError("Render exceeds track or clip limits")
    by_id = {t["id"]: t for t in tracks}
    if len(by_id) != len(tracks):
        raise ValueError("duplicate track")
    solo = any(t.get("solo", False) and not t.get("mute", False) for t in tracks)
    active = {t["id"] for t in tracks if not t.get("mute", False) and (not solo or t.get("solo", False))}
    parts = []
    sources = {}
    length = 0
    for clip in document["clips"]:
        _check_cancel(cancelled)
        track_id, asset_id = clip["track_id"], clip["asset_id"]
        if track_id not in by_id or asset_id not in assets:
            raise ValueError("unknown track or asset")
        start = _integer(clip["start_sample"], "start")
        left = _integer(clip["trim_start_sample"], "trim start")
        right = _integer(clip["trim_end_sample"], "trim end")
        fade_in = _integer(clip["fade_in_samples"], "fade in")
        fade_out = _integer(clip["fade_out_samples"], "fade out")
        gain = _gain(clip["gain"]) * _gain(by_id[track_id].get("gain", 1))
        if not math.isfinite(gain):
            raise ValueError("Invalid combined gain")
        if asset_id not in sources:
            path = Path(resolve_asset(asset_id))
            info = sf.info(str(path))
            if info.channels not in (1, 2) or not 1000 <= info.samplerate <= 384000 or info.frames <= 0:
                raise ValueError("invalid source audio")
            sources[asset_id] = path, info
        path, info = sources[asset_id]
        frames = resampled_frames(info.frames, info.samplerate, rate)
        duration = frames - left - right
        if duration <= 0:
            raise ValueError("trim removes source")
        if fade_in > duration or fade_out > duration:
            raise ValueError("fade exceeds clip")
        if start + duration > rate * MAX_DURATION_SECONDS:
            raise ValueError("Render exceeds two-hour duration limit")
        if track_id in active:
            parts.append((track_id, start, duration, left, gain, fade_in, fade_out, path, info))
            length = max(length, start + duration)
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    warnings = []
    files = []
    measurements = {}
    # Unique names make each publication independent of earlier deliverables.
    staging = Path(tempfile.mkdtemp(prefix=".render-", dir=output))
    published = []
    try:
        targets = [(None, "mix")] if kind == "mix" else [(t["id"], "stem") for t in tracks if t["id"] in active]
        for target_index, (track_id, file_kind) in enumerate(targets):
            peak = 0.0
            intermediate = staging / f"{uuid.uuid4().hex}.wav"
            # Block mixing keeps memory independent of project/source duration.
            with sf.SoundFile(str(intermediate), "w", samplerate=rate, channels=channels, subtype="FLOAT") as stream:
                for offset in range(0, max(length, 1), BLOCK_FRAMES):
                    _check_cancel(cancelled)
                    end = min(offset + BLOCK_FRAMES, max(length, 1))
                    mixed = np.zeros((end - offset, channels), dtype=np.float64)
                    for tid, start, duration, left, gain, fade_in, fade_out, path, info in parts:
                        if track_id is not None and tid != track_id:
                            continue
                        lo, hi = max(offset, start), min(end, start + duration)
                        if lo >= hi:
                            continue
                        local = lo - start
                        data = _source_range(path, info, rate, left + local, left + hi - start)
                        if channels == 2 and info.channels == 1:
                            data = np.repeat(data, 2, axis=1)
                        elif channels == 1 and info.channels == 2:
                            data = data.mean(axis=1, keepdims=True)
                        scale = np.full(hi - lo, gain)
                        positions = np.arange(local, local + hi - lo)
                        if fade_in:
                            scale *= np.minimum(positions / fade_in, 1.0)
                        if fade_out:
                            scale *= np.minimum((duration - positions) / fade_out, 1.0)
                        mixed[lo - offset : hi - offset] += data * scale[:, None]
                    if not np.isfinite(mixed).all():
                        raise ValueError("Invalid mixed samples")
                    peak = max(peak, float(np.max(np.abs(mixed))))
                    stream.write(mixed)
                    if progress:
                        progress(target_index * max(length, 1) + end, len(targets) * max(length, 1))
            normalize = kind == "mix" and document.get("mix", {}).get("normalize", False)
            if peak > 1:
                warnings.append(
                    f"{file_kind} exceeds full scale before encoding" + ("; output clips" if not normalize else "")
                )
            mix = document.get("mix", {})
            ffmpeg = shutil.which("ffmpeg")
            encoded = staging / f"{uuid.uuid4().hex}.{format}"
            if normalize:
                if not ffmpeg:
                    raise ValueError("normalization requires ffmpeg loudnorm")
                target = float(mix.get("target_lufs", -16))
                tp = float(mix.get("max_true_peak_dbtp", -1.5))
                if not math.isfinite(target) or not math.isfinite(tp) or not -70 <= target <= -5 or not -9 <= tp <= 0:
                    raise ValueError("unsupported loudness target")
                filter_arg = f"loudnorm=I={target}:TP={tp}:LRA=11"
            else:
                filter_arg = "anull"
            if format == "mp3" or normalize:
                if not ffmpeg:
                    raise ValueError("ffmpeg encoder unavailable")
                codec = ["-c:a", "libmp3lame"] if format == "mp3" else ["-c:a", "pcm_s24le"]
                _process(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(intermediate),
                        "-af",
                        filter_arg,
                        "-ar",
                        str(rate),
                        "-ac",
                        str(channels),
                        *codec,
                        str(encoded),
                    ],
                    cancelled,
                )
            else:
                with (
                    sf.SoundFile(str(intermediate)) as source,
                    sf.SoundFile(str(encoded), "w", samplerate=rate, channels=channels, subtype="PCM_24") as target,
                ):
                    while True:
                        _check_cancel(cancelled)
                        block = source.read(BLOCK_FRAMES, dtype="float64", always_2d=True)
                        if not len(block):
                            break
                        target.write(block)
            measurement = _measure(encoded, cancelled)
            measurement["pre_encode_sample_peak"] = peak
            if measurement["status"] != "measured":
                warnings.append(
                    f"{file_kind} loudness and true peak unavailable (silence, short audio, or meter unavailable)"
                )
            name = f"{file_kind}-{uuid.uuid4().hex}.{format}"
            final = output / name
            _check_cancel(cancelled)
            os.replace(encoded, final)
            published.append(final)
            files.append(
                {
                    "path": str(final),
                    "kind": file_kind,
                    "track_id": track_id,
                    "sample_rate": rate,
                    "channels": channels,
                    "processing": "master" if kind == "mix" else "dry",
                }
            )
            measurements[name] = measurement
            intermediate.unlink()
        if kind == "stems" and document.get("mix", {}).get("normalize"):
            warnings.append("Dry stems preserve relative track levels; master normalization omitted")
        return {"files": files, "measurements": measurements, "warnings": warnings}
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
