"""Bounded audio envelopes cached independently of full playback buffers."""

import hashlib
import json
import math
import os
import re
import subprocess
import threading
import uuid
from contextlib import contextmanager

import numpy as np
import soundfile as sf

from .ffmpeg import find_ffmpeg

_LIMIT = 64 * 1024 * 1024
_BLOCK = 65536
_lock = threading.Lock()
_keys = {}
_workers = threading.BoundedSemaphore(2)


@contextmanager
def _key_lock(key):
    with _lock:
        guard, users = _keys.get(key, (threading.Lock(), 0))
        _keys[key] = guard, users + 1
    try:
        with guard:
            yield
    finally:
        with _lock:
            _, users = _keys[key]
            if users == 1:
                del _keys[key]
            else:
                _keys[key] = guard, users - 1


def _identity(path):
    stat = path.stat()
    return str(path.resolve()), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _decoded_blocks(path):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise ValueError("This source requires ffmpeg for waveform decoding")
    with subprocess.Popen(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "24000", "-f", "f32le", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            while block := process.stdout.read(_BLOCK * 4):
                if len(block) % 4:
                    raise ValueError("Audio source ended within a sample")
                yield np.frombuffer(block, dtype="<f4")[:, None]
            if process.wait():
                raise ValueError("Audio waveform decoding failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _envelope(path, bins):
    source = None
    try:
        source = sf.SoundFile(path)
    except (RuntimeError, OSError):
        # Formats not handled by libsndfile are decoded in bounded pipe blocks.
        # Counting first keeps peak bins and duration tied to actual samples.
        frames = sum(len(block) for block in _decoded_blocks(path))
        rate, channels = 24000, 1
        blocks = _decoded_blocks(path)
    else:
        frames, rate, channels = len(source), source.samplerate, source.channels

        def read():
            with source:
                while True:
                    data = source.read(max(1, _BLOCK // channels), dtype="float32", always_2d=True)
                    if len(data) == 0:
                        break
                    yield data

        blocks = read()
    if frames <= 0 or rate <= 0 or channels <= 0:
        if source is not None:
            source.close()
        raise ValueError("Audio source is empty or invalid")
    low, high = np.full(bins, np.inf), np.full(bins, -np.inf)
    offset = 0
    for data in blocks:
        if not np.isfinite(data).all():
            raise ValueError("Audio source contains invalid samples")
        stop = offset + len(data)
        for index in range(offset * bins // frames, min(bins, (stop - 1) * bins // frames + 1)):
            begin = max(offset, (index * frames + bins - 1) // bins)
            end = min(stop, ((index + 1) * frames + bins - 1) // bins)
            if begin < end:
                part = data[begin - offset : end - offset]
                low[index] = min(low[index], float(part.min()))
                high[index] = max(high[index], float(part.max()))
        offset = stop
    if offset != frames:
        raise ValueError("Audio source changed or ended during waveform decoding")
    peaks = [[float(lo), float(hi)] if math.isfinite(lo) else [0.0, 0.0] for lo, hi in zip(low, high, strict=False)]
    return dict(frames=frames, sample_rate=rate, channels=channels, peaks=peaks)


def waveform(path, cache, bins=2048):
    """Cache by source file revision; never trust an entry after source deletion."""
    for _attempt in range(2):
        identity = _identity(path)
        key = hashlib.sha256(repr((identity, bins)).encode()).hexdigest() + ".json"
        with _key_lock((str(cache), key)):
            with _lock:
                cache.mkdir(parents=True, exist_ok=True)
                target = cache / key
                if target.is_file():
                    try:
                        data = json.loads(target.read_text())
                        if len(data["peaks"]) == bins and data["frames"] > 0 and data["sample_rate"] > 0:
                            os.utime(target, None)
                            return data
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
            with _workers:
                data = _envelope(path, bins)
            if _identity(path) != identity:
                continue
            temporary = cache / ("." + uuid.uuid4().hex)
            try:
                temporary.write_text(json.dumps(data, separators=(",", ":"), allow_nan=False))
                with _lock:
                    os.replace(temporary, target)
                    entries = sorted(
                        (
                            path
                            for path in cache.iterdir()
                            if re.fullmatch(r"[0-9a-f]{64}\.json", path.name) and path.is_file()
                        ),
                        key=lambda p: p.stat().st_mtime,
                    )
                    total = sum(p.stat().st_size for p in entries)
                    for entry in entries:
                        if total <= _LIMIT:
                            break
                        total -= entry.stat().st_size
                        entry.unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)
            return data
    raise ValueError("Audio source is changing; retry its waveform")
