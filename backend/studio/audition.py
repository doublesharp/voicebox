"""Bounded native PCM samples for temporary listening-level comparisons, not LUFS.

Integration: include make_audition_router(store) in the /studio/v1 router.
Measurements use up to five one-second windows across the source (240000 frames
maximum), never a whole-source download or unbounded decode. Caches are disposable.
"""

import json
import math
import os
import re
import threading
import uuid

from fastapi import APIRouter, HTTPException

from .storage import NotFound

_lock = threading.Lock()
_METHOD = "sample_rms_v1"
_CACHE_LIMIT = 4 * 1024 * 1024


def _measure(path):
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(path) as audio:
        frames, rate, channels = len(audio), audio.samplerate, audio.channels
        if frames <= 0 or rate <= 0 or channels not in (1, 2):
            raise ValueError("Invalid source format")
        width = min(rate, 48000, max(1, frames // 5))
        starts = sorted({(frames - width) * index // 4 for index in range(5)})
        measured, squares, peak = 0, 0.0, 0.0
        for start in starts:
            audio.seek(start)
            remaining = width
            while remaining:
                count = min(remaining, 16384 // channels)
                data = audio.read(count, dtype="float64", always_2d=True)
                if len(data) != count or not np.isfinite(data).all():
                    raise ValueError("Invalid source PCM")
                squares += float(np.sum(data * data))
                peak = max(peak, float(np.max(np.abs(data))))
                measured += len(data)
                remaining -= len(data)
        rms = math.sqrt(squares / (measured * channels))
        if not math.isfinite(rms) or not math.isfinite(peak):
            raise ValueError("Invalid source levels")
        rms = min(peak, rms)  # Floating-point summation can overshoot a constant peak slightly.
        return dict(
            method=_METHOD,
            status="measured" if rms > 1e-9 else "silence",
            rms=rms,
            peak=peak,
            frames=frames,
            measured_frames=measured,
            sample_rate=rate,
            channels=channels,
        )


def _valid(data):
    return (
        isinstance(data, dict)
        and data.get("method") == _METHOD
        and data.get("status") in ("measured", "silence")
        and all(
            type(data.get(k)) is int and data[k] > 0 for k in ("frames", "measured_frames", "sample_rate", "channels")
        )
        and data["measured_frames"] <= min(240000, data["frames"])
        and data["channels"] in (1, 2)
        and all(type(data.get(k)) in (int, float) and math.isfinite(data[k]) and data[k] >= 0 for k in ("rms", "peak"))
        and data["rms"] <= data["peak"]
        and (data["status"] == "silence") == (data["rms"] <= 1e-9)
    )


def make_audition_router(store):
    router = APIRouter()

    @router.get("/assets/{asset_id}/audition-level")
    def audition_level(asset_id: str):
        try:
            path = store.asset_path(asset_id)
            with store.connect() as db:
                row = db.execute("SELECT hash FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None or not re.fullmatch(r"[0-9a-f]{64}", row["hash"]):
                raise NotFound("Asset not found")
            cache = store.root / "audition-cache"
            with _lock:
                cache.mkdir(exist_ok=True)
                target = cache / f"{row['hash']}-{_METHOD}.json"
                if target.is_file() and target.stat().st_size <= 4096:
                    try:
                        data = json.loads(target.read_text())
                        if _valid(data):
                            os.utime(target, None)
                            return data
                    except (OSError, ValueError, TypeError):
                        pass
                data = _measure(path)
                temp = cache / ("." + uuid.uuid4().hex + ".tmp")
                try:
                    temp.write_text(json.dumps(data, allow_nan=False, separators=(",", ":")))
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
                entries = sorted(
                    (p for p in cache.glob(f"*-{_METHOD}.json") if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                )
                total = sum(p.stat().st_size for p in entries)
                for entry in entries:
                    if total <= _CACHE_LIMIT:
                        break
                    total -= entry.stat().st_size
                    entry.unlink(missing_ok=True)
                return data
        except NotFound as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(422, detail="Source listening level is not measurable") from exc

    return router
