"""Bounded, sample-domain comping into new immutable takes."""

import copy
import json

import numpy as np
import soundfile as sf

from .publication import publish_audio
from .storage import NotFound, encode, now, uid
from .timing import resampled_frames

MAX_SEGMENTS = 32
MAX_SECONDS = 1800
BLOCK = 65536


def compose_take(store, db, project_id, doc, payload, published):
    """Publish an alternative under the command transaction; never select it."""
    passage = next((p for p in doc["passages"] if p["id"] == payload.get("passage_id")), None)
    if passage is None:
        raise NotFound("Passage not found in project")
    if passage["locked"]:
        raise ValueError("Passage is locked")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 100000:
        raise ValueError("A transcript of the composed audio is required (at most 100000 characters)")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise ValueError(f"Choose between 1 and {MAX_SEGMENTS} source phrases")
    fade = payload.get("crossfade_samples", 0)
    rate = doc["sample_rate"]
    if type(fade) is not int or not 0 <= fade <= rate // 2:
        raise ValueError("Crossfade must be between 0 and 500 milliseconds")
    sources = []
    length = 0
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"take_id", "start_sample", "end_sample"}:
            raise ValueError("A phrase needs a take and its start/end sample range")
        if not isinstance(segment["take_id"], str) or not segment["take_id"]:
            raise ValueError("A source take ID is required")
        start, end = segment["start_sample"], segment["end_sample"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("Invalid phrase source range")
        row = db.execute(
            "SELECT t.*, a.path, a.hash, a.sample_rate, a.channels, a.frames FROM takes t "
            "JOIN assets a ON a.id=t.asset_id WHERE t.id=? AND t.project_id=? AND t.passage_id=?",
            (segment["take_id"], project_id, passage["id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Source take not found for passage")
        if end > resampled_frames(row["frames"], row["sample_rate"], rate):
            raise ValueError("Phrase range exceeds source audio")
        if len(segments) > 1 and fade * 2 > end - start:
            raise ValueError("Crossfade exceeds half a source phrase; shorten the crossfade")
        length += end - start
        if length > rate * MAX_SECONDS:
            raise ValueError(f"Composed audio may contain at most {MAX_SECONDS // 60} minutes of source audio")
        path = store.asset_dir / row["path"]
        info = sf.info(str(path))
        if (info.samplerate, info.channels, info.frames) != (row["sample_rate"], row["channels"], row["frames"]):
            raise ValueError("Source audio metadata changed")
        sources.append((segment, row, path, info))

    from .rendering import _source_range

    def read(source, start, end):
        audio = _source_range(source[2], source[3], rate, start, end)
        return np.repeat(audio, 2, axis=1) if audio.shape[1] == 1 else audio

    temporary = store.asset_dir / (".composition-" + uid())
    asset_id = uid()
    target = store.asset_dir / asset_id
    registered = False
    try:
        tail = None
        with sf.SoundFile(str(temporary), "w", samplerate=rate, channels=2, format="WAV", subtype="FLOAT") as output:
            for index, source in enumerate(sources):
                start, end = source[0]["start_sample"], source[0]["end_sample"]
                if index and fade:
                    head = read(source, start, start + fade)
                    weight = np.linspace(0, 1, fade)[:, None]
                    output.write(tail * (1 - weight) + head * weight)
                    start += fade
                stop = end - fade if fade and index < len(sources) - 1 else end
                for offset in range(start, stop, BLOCK):
                    output.write(read(source, offset, min(stop, offset + BLOCK)))
                tail = read(source, stop, end) if stop < end else None
        expected_frames = length - fade * (len(sources) - 1)
        asset = publish_audio(
            db,
            store.asset_dir,
            project_id,
            temporary,
            asset_id,
            expected={"frames": expected_frames, "sample_rate": rate, "channels": 2},
        )
        registered = True
        take_id = uid()
        frozen = copy.deepcopy(passage)
        frozen["text"] = text.strip()
        # Use the first source voice identity for track compatibility; every source
        # snapshot and hash remains visible in composition provenance.
        first_request = json.loads(sources[0][1]["request"])
        if "voice_snapshot" in first_request.get("passage", {}):
            frozen["voice_snapshot"] = first_request["passage"]["voice_snapshot"]
        composition = {
            "method": "linear_crossfade_v1",
            "sample_rate": rate,
            "crossfade_samples": fade,
            "segments": [
                {**segment, "source_hash": row["hash"], "asset_id": row["asset_id"], "text": row["text"]}
                for segment, row, _, _ in sources
            ],
        }
        request = {"passage": frozen, "composition": composition, "adopt": False}
        db.execute(
            "INSERT INTO takes VALUES (?,?,?,?,?,?,?,?,?)",
            (
                take_id,
                project_id,
                passage["id"],
                asset["id"],
                text.strip(),
                "composite",
                None,
                now(),
                encode(request),
            ),
        )
        published.append(target)
    except BaseException:
        if registered:
            target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
