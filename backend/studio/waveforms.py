"""Bounded, source-derived waveform envelopes for studio assets."""

import json
import math
import os
import re
import threading
import uuid

from fastapi import APIRouter, HTTPException, Query

from .storage import NotFound

_CACHE_LIMIT = 64 * 1024 * 1024
_BLOCK = 65536
_lock = threading.Lock()


def _envelope(path, bins):
    # Imported only when requested; router construction does not load audio libraries.
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(path) as audio:
        frames, rate, channels = len(audio), audio.samplerate, audio.channels
        if frames <= 0 or rate <= 0 or channels <= 0:
            raise ValueError("Source audio is empty or invalid")
        low = np.full(bins, np.inf)
        high = np.full(bins, -np.inf)
        offset = 0
        while offset < frames:
            data = audio.read(min(max(1, _BLOCK // channels), frames - offset), dtype="float32", always_2d=True)
            if not len(data) or not np.isfinite(data).all():
                raise ValueError("Source audio is incomplete or invalid")
            stop = offset + len(data)
            first = offset * bins // frames
            last = (stop - 1) * bins // frames
            for index in range(first, last + 1):
                start = max(offset, (index * frames + bins - 1) // bins)
                end = min(stop, ((index + 1) * frames + bins - 1) // bins)
                if start < end:
                    part = data[start - offset : end - offset]
                    low[index] = min(low[index], float(part.min()))
                    high[index] = max(high[index], float(part.max()))
            offset = stop
        peaks = [[float(lo), float(hi)] if math.isfinite(lo) else [0.0, 0.0] for lo, hi in zip(low, high)]
        return dict(sample_rate=rate, frames=frames, channels=channels, peaks=peaks)


def make_waveform_router(store):
    router = APIRouter()

    @router.get("/assets/{asset_id}/waveform")
    def waveform(asset_id: str, bins: int = Query(default=512, ge=32, le=2048)):
        try:
            # Lookup and file existence are checked even on a cache hit.
            path = store.asset_path(asset_id)
            with store.connect() as db:
                row = db.execute("SELECT hash FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None or not re.fullmatch(r"[0-9a-f]{64}", row["hash"]):
                raise NotFound("Asset not found")
            cache = store.root / "waveform-cache"
            key = f"{row['hash']}-{bins}.json"
            with _lock:
                cache.mkdir(exist_ok=True)
                target = cache / key
                if target.is_file():
                    try:
                        data = json.loads(target.read_text())
                        if len(data["peaks"]) == bins:
                            os.utime(target, None)
                            return data
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                data = _envelope(path, bins)
                encoded = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
                temp = cache / ("." + uuid.uuid4().hex + ".tmp")
                try:
                    temp.write_bytes(encoded)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
                entries = sorted(
                    (p for p in cache.iterdir() if re.fullmatch(r"[0-9a-f]{64}-\d+\.json", p.name) and p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                )
                total = sum(p.stat().st_size for p in entries)
                for entry in entries:
                    if total <= _CACHE_LIMIT:
                        break
                    size = entry.stat().st_size
                    entry.unlink(missing_ok=True)
                    total -= size
                return data
        except NotFound as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(422, detail="Source audio unavailable or invalid") from exc

    return router
