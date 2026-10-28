"""Publish verified immutable audio under the caller's Studio write transaction."""

import hashlib
import os
from pathlib import Path

import soundfile as sf


def publish_audio(db, asset_dir, project_id, source, asset_id, expected=None):
    if not db.in_transaction:
        raise RuntimeError("Audio publication requires a Studio write transaction")
    source, asset_dir = Path(source), Path(asset_dir)
    target = asset_dir / asset_id
    staged = asset_dir / (".publish-" + asset_id)
    staged_created = published = False
    try:
        digest = hashlib.sha256()
        with source.open("rb") as src, staged.open("xb") as dest:
            staged_created = True
            for block in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(block)
                dest.write(block)
            dest.flush()
            os.fsync(dest.fileno())
        copied_digest = hashlib.sha256()
        with staged.open("rb") as copied:
            for block in iter(lambda: copied.read(1024 * 1024), b""):
                copied_digest.update(block)
        if digest.digest() != copied_digest.digest():
            raise ValueError("Copied audio hash mismatch")
        try:
            info = sf.info(str(staged))
        except (RuntimeError, OSError) as exc:
            raise ValueError("Invalid audio file") from exc
        if info.frames <= 0 or info.samplerate <= 0 or info.channels not in (1, 2):
            raise ValueError("Empty or invalid audio")
        asset = dict(
            id=asset_id,
            hash=digest.hexdigest(),
            sample_rate=info.samplerate,
            channels=info.channels,
            frames=info.frames,
            duration=info.frames / info.samplerate,
            size_bytes=staged.stat().st_size,
        )
        for key in ("hash", "sample_rate", "channels", "frames", "duration", "size_bytes"):
            if expected is None or key not in expected:
                continue
            matches = abs(asset[key] - expected[key]) <= 1e-9 if key == "duration" else asset[key] == expected[key]
            if not matches:
                raise ValueError(f"Copied audio {key} mismatch")
        if os.path.lexists(target):
            raise FileExistsError(f"Asset destination already exists: {target}")
        staged.rename(target)
        published = True
        if os.name != "nt":
            descriptor = os.open(asset_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        db.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
            (
                asset_id,
                project_id,
                asset["hash"],
                asset["sample_rate"],
                asset["channels"],
                asset["frames"],
                asset["duration"],
                asset["size_bytes"],
                asset_id,
            ),
        )
        return asset
    except BaseException:
        if staged_created:
            staged.unlink(missing_ok=True)
        if published:
            target.unlink(missing_ok=True)
        raise


def discard_unregistered(db, paths):
    """Keep audio if a commit succeeded despite an uncertain return status."""
    for path in paths:
        if not db.execute("SELECT 1 FROM assets WHERE path=?", (path.name,)).fetchone():
            path.unlink(missing_ok=True)
