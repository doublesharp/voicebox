"""Reviewable caption timing against an immutable description of audible clips.

Estimates use whole clip intervals, never inferred word boundaries. A person must
correct trimmed wording/timing and review the final audio before export.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .storage import Conflict, NotFound, uid
from .timing import resampled_frames

MAX_CUES = 10000
MAX_SAMPLE = 48000 * 60 * 60 * 24


def _integer(value, label, *, minimum=0, maximum=MAX_SAMPLE):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {label}")
    return value


def _range(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label} range")
    start = _integer(value.get("start_sample"), f"{label} range")
    end = _integer(value.get("end_sample"), f"{label} range")
    if end <= start:
        raise ValueError(f"Invalid {label} range")


def _video(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"name", "frame_rate", "time_origin_sample", "duration_seconds"}:
        raise ValueError("Invalid reference video metadata")
    if not isinstance(value["name"], str) or not value["name"].strip() or len(value["name"]) > 255:
        raise ValueError("Invalid reference video name")
    fps = value["frame_rate"]
    if type(fps) not in (int, float) or not math.isfinite(fps) or not 1 <= fps <= 240:
        raise ValueError("Invalid reference video frame rate")
    _integer(value["time_origin_sample"], "video time origin", minimum=-MAX_SAMPLE)
    duration = value["duration_seconds"]
    if duration is not None and (
        type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 86400
    ):
        raise ValueError("Invalid reference video duration")
    return dict(value)


def _cues(values, doc):
    if not isinstance(values, list) or len(values) > MAX_CUES:
        raise ValueError("Invalid caption cues")
    ids = set()
    result = []
    for cue in values:
        if not isinstance(cue, dict) or set(cue) != {"id", "clip_id", "start_sample", "end_sample", "text"}:
            raise ValueError("Invalid caption cue")
        if not isinstance(cue["id"], str) or not cue["id"] or len(cue["id"]) > 200 or cue["id"] in ids:
            raise ValueError("Invalid or duplicate caption cue ID")
        ids.add(cue["id"])
        # A later clip deletion leaves an explicitly stale caption draft editable.
        if cue["clip_id"] is not None and (not isinstance(cue["clip_id"], str) or len(cue["clip_id"]) > 200):
            raise ValueError("Invalid caption source clip")
        _range(cue, "caption")
        text = cue["text"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 4000
            or "\r" in text
            or "\x00" in text
            or "-->" in text
            or re.search(r"\n\s*\n", text)
            or any(ord(char) < 32 and char not in "\n\t" for char in text)
        ):
            raise ValueError("Invalid caption text; use single line breaks inside a cue")
        result.append(dict(cue))
    return result


def validate_caption_document(doc, assets=None, takes=None):
    """Validate optional data on commands, archives, and restored documents."""
    if "video_reference" in doc:
        _video(doc["video_reference"])
    targets = doc.get("scene_targets", {})
    passages = {passage["id"] for passage in doc["passages"]}
    if not isinstance(targets, dict) or len(targets) > len(passages):
        raise ValueError("Invalid scene targets")
    for passage_id, target in targets.items():
        if passage_id not in passages or not isinstance(target, dict) or set(target) != {"start_sample", "end_sample"}:
            raise ValueError("Invalid scene target passage")
        _range(target, "scene target")
    captions = doc.get("captions")
    if captions is not None:
        if (
            not isinstance(captions, dict)
            or set(captions) != {"source_fingerprint", "reviewed", "cues"}
            or not isinstance(captions["source_fingerprint"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", captions["source_fingerprint"])
            or type(captions["reviewed"]) is not bool
        ):
            raise ValueError("Invalid caption provenance")
        _cues(captions["cues"], doc)


def _mapped(items):
    return items if isinstance(items, dict) else {item["id"]: item for item in items}


def _sources(doc, assets, takes):
    assets, takes = _mapped(assets), _mapped(takes)
    tracks = {track["id"]: track for track in doc["tracks"]}
    passages = {passage["id"]: passage for passage in doc["passages"]}
    solo = any(track.get("solo", False) and not track.get("mute", False) for track in tracks.values())
    active = {
        key
        for key, track in tracks.items()
        if not track.get("mute", False) and (not solo or track.get("solo", False)) and track.get("gain", 1) > 0
    }
    result = []
    for clip in doc["clips"]:
        if clip["track_id"] not in active or clip.get("gain", 1) <= 0:
            continue
        asset = assets.get(clip["asset_id"])
        if asset is None:
            raise ValueError("Caption source audio is unavailable")
        frames = resampled_frames(asset["frames"], asset["sample_rate"], doc["sample_rate"])
        length = frames - clip["trim_start_sample"] - clip["trim_end_sample"]
        if length <= 0:
            raise ValueError("Invalid caption source range")
        passage = passages.get(clip.get("passage_id"))
        take = takes.get(clip.get("take_id"))
        text = take["text"] if take else passage["text"] if passage else ""
        result.append({"clip": clip, "asset": asset, "take": take, "passage": passage, "text": text, "length": length})
    return result


def source_fingerprint(doc, assets, takes):
    """Use content/positions rather than IDs so portable identity remapping is safe."""
    assets, takes = _mapped(assets), _mapped(takes)
    tracks = {track["id"]: index for index, track in enumerate(doc["tracks"])}
    passages = {passage["id"]: passage for passage in doc["passages"]}
    sources = []
    # Include muted sources too: a mute/solo or gain edit must revoke prior review.
    for clip in doc["clips"]:
        asset = assets.get(clip["asset_id"])
        if asset is None:
            raise ValueError("Caption source audio is unavailable")
        take = takes.get(clip.get("take_id"))
        passage = passages.get(clip.get("passage_id"))
        sources.append(
            {
                "asset": {key: asset.get(key) for key in ("hash", "sample_rate", "channels", "frames")},
                "track": tracks.get(clip["track_id"]),
                "clip": {
                    key: clip[key]
                    for key in (
                        "start_sample",
                        "trim_start_sample",
                        "trim_end_sample",
                        "gain",
                        "fade_in_samples",
                        "fade_out_samples",
                    )
                },
                "source_text": take.get("text") if take else None,
                "script_text": passage.get("text") if passage else None,
            }
        )
    recipe = {
        "version": 1,
        "sample_rate": doc["sample_rate"],
        "channels": doc["channels"],
        "sources": sources,
        "tracks": [
            {key: value for key, value in track.items() if key not in ("id", "name", "voice_identity")}
            for track in doc["tracks"]
        ],
        "mix": doc["mix"],
    }
    return hashlib.sha256(
        json.dumps(recipe, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def estimate_captions(doc, assets, takes):
    cues = []
    for source in _sources(doc, assets, takes):
        if not source["text"].strip():
            continue
        clip = source["clip"]
        # Flatten paragraphs because blank lines delimit cues in subtitle files.
        text = " ".join(source["text"].split())
        cues.append(
            {
                "id": uid(),
                "clip_id": clip["id"],
                "start_sample": clip["start_sample"],
                "end_sample": clip["start_sample"] + source["length"],
                "text": text,
            }
        )
    cues.sort(key=lambda cue: (cue["start_sample"], cue["end_sample"]))
    return {"source_fingerprint": source_fingerprint(doc, assets, takes), "reviewed": False, "cues": cues}


def caption_status(doc, assets, takes):
    validate_caption_document(doc)
    fingerprint = source_fingerprint(doc, assets, takes)
    captions = doc.get("captions")
    current = bool(captions and captions["source_fingerprint"] == fingerprint)
    reviewed = bool(captions and captions["reviewed"])
    return {
        "source_fingerprint": fingerprint,
        "current": current,
        "reviewed": reviewed,
        "exportable": bool(current and reviewed and captions["cues"]),
        "captions": captions,
        "scene_timing": scene_timing(doc, assets, takes),
    }


def scene_timing(doc, assets, takes):
    assets, takes = _mapped(assets), _mapped(takes)
    sources = _sources(doc, assets, takes)
    result = []
    for passage_id, target in doc.get("scene_targets", {}).items():
        passage = next(p for p in doc["passages"] if p["id"] == passage_id)
        selected_take = takes.get(passage.get("selected_take_id"))
        selected_asset = assets.get(selected_take["asset_id"]) if selected_take else None
        placed = [source for source in sources if source["clip"].get("passage_id") == passage_id]
        rate = doc["sample_rate"]
        target_seconds = (target["end_sample"] - target["start_sample"]) / rate
        placed_seconds = sum(source["length"] for source in placed) / rate
        result.append(
            {
                "passage_id": passage_id,
                "target_seconds": target_seconds,
                "selected_seconds": selected_asset["duration"] if selected_asset else None,
                "placed_seconds": placed_seconds,
                "delta_seconds": placed_seconds - target_seconds if placed else None,
            }
        )
    return result


def apply_caption_command(store, db, project_id, doc, kind, payload):
    if kind == "set_video_reference":
        if set(payload) != {"reference"}:
            raise ValueError("Expected reference video metadata")
        doc["video_reference"] = _video(payload["reference"])
    elif kind == "set_scene_target":
        passage_id = payload.get("passage_id")
        if passage_id not in {passage["id"] for passage in doc["passages"]}:
            raise ValueError("Scene target passage is unavailable")
        targets = doc.setdefault("scene_targets", {})
        if payload.get("remove") is True:
            if set(payload) != {"passage_id", "remove"}:
                raise ValueError("Invalid scene target removal")
            targets.pop(passage_id, None)
        else:
            if set(payload) != {"passage_id", "start_sample", "end_sample"}:
                raise ValueError("Invalid scene target")
            _range(payload, "scene target")
            targets[passage_id] = {key: payload[key] for key in ("start_sample", "end_sample")}
    elif kind in ("estimate_captions", "set_captions"):
        project = store._project(db, project_id)
        assets, takes = project["assets"], project["takes"]
        if kind == "estimate_captions":
            if payload:
                raise ValueError("Caption estimates require no payload")
            doc["captions"] = estimate_captions(doc, assets, takes)
        else:
            candidate = {**doc, "captions": payload}
            validate_caption_document(candidate)
            if payload["source_fingerprint"] != source_fingerprint(doc, assets, takes):
                raise Conflict("Caption source changed; reload and review the current audio")
            doc["captions"] = {**payload, "cues": _cues(payload["cues"], doc)}
    else:
        raise ValueError("Unknown caption command")


def _timestamp(sample, rate, separator):
    milliseconds = (sample * 1000 + rate // 2) // rate
    hours, remaining = divmod(milliseconds, 3600000)
    minutes, remaining = divmod(remaining, 60000)
    seconds, ms = divmod(remaining, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{ms:03d}"


def export_captions(doc, assets, takes, format):  # noqa: A002 - transport calls it format.
    if format not in ("srt", "vtt"):
        raise ValueError("Unsupported caption format")
    status = caption_status(doc, assets, takes)
    if not status["current"]:
        raise ValueError("Caption source changed; review captions against the current audio")
    if not status["reviewed"]:
        raise ValueError("Captions require timing and wording review before export")
    if not doc["captions"]["cues"]:
        raise ValueError("Add caption cues before export")
    separator = "," if format == "srt" else "."
    blocks = []
    for index, cue in enumerate(sorted(doc["captions"]["cues"], key=lambda c: (c["start_sample"], c["end_sample"])), 1):
        start = _timestamp(cue["start_sample"], doc["sample_rate"], separator)
        end = _timestamp(cue["end_sample"], doc["sample_rate"], separator)
        if start == end:
            raise ValueError("Caption range rounds to zero milliseconds; lengthen the cue")
        blocks.append(f"{index}\n{start} --> {end}\n{html.escape(cue['text'], quote=False)}")
    return ("WEBVTT\n\n" if format == "vtt" else "") + "\n\n".join(blocks) + "\n"


def make_caption_router(store):
    router = APIRouter()

    def project_value(project_id):
        try:
            return store.get_project(project_id)
        except NotFound as exc:
            raise HTTPException(404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/captions")
    def status(project_id: str):
        project = project_value(project_id)
        try:
            return caption_status(project["document"], project["assets"], project["takes"])
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/captions/export")
    def export(
        project_id: str,
        caption_format: str = Query(default="srt", alias="format"),
        expected_fingerprint: str | None = Query(default=None, alias="source_fingerprint"),
    ):
        project = project_value(project_id)
        try:
            if expected_fingerprint is not None and expected_fingerprint != source_fingerprint(
                project["document"], project["assets"], project["takes"]
            ):
                raise HTTPException(409, detail="Caption source changed before download")
            value = export_captions(project["document"], project["assets"], project["takes"], caption_format)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return Response(
            value,
            media_type="text/vtt" if caption_format == "vtt" else "application/x-subrip",
            headers={"Content-Disposition": f'attachment; filename="captions.{caption_format}"'},
        )

    return router
