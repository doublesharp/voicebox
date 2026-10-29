"""Advisory recognized speech, immutable source identity and reviewable proposals.

Recognized word times describe the recognizer's words, never forced alignment of
supplied script. Reports do not change scripts, selected takes, or source audio.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from difflib import SequenceMatcher

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .storage import Conflict, NotFound

MAX_REPORTS = 16


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def _tokens(text):
    return re.findall(r"[\u3400-\u9fff]|[^\W_]+(?:['\u2019][^\W_]+)?", text, flags=re.UNICODE)


def _portable_request(value, assets, key=None):
    """Keep rendering settings/content while canonicalizing archive-local IDs."""
    if isinstance(value, dict):
        return {
            name: _portable_request(item, assets, name)
            for name, item in value.items()
            if name not in {"id", "selected_take_id", "take_id", "passage_id", "source_passage_id", "track_id"}
        }
    if isinstance(value, list):
        return [_portable_request(item, assets, key) for item in value]
    if key in {"asset_id", "reference_assets", "unavailable_references"} and isinstance(value, str):
        return assets.get(value, value)
    return value


def source_identity(project, take_id):
    take = next((value for value in project["takes"] if value["id"] == take_id), None)
    if take is None:
        raise NotFound("Take not found in this project")
    asset = next((value for value in project["assets"] if value["id"] == take["asset_id"]), None)
    passage = next((value for value in project["document"]["passages"] if value["id"] == take["passage_id"]), None)
    if asset is None or passage is None:
        raise NotFound("Take source is unavailable")
    # Content-derived identity survives portable archive ID remapping. Immutable
    # take metadata and request provenance still distinguish performances.
    source = {
        "asset_hash": asset["hash"],
        "take_text": take["text"],
        "take_engine": take["engine"],
        "take_model": take.get("model_size"),
        "request_sha256": _digest(
            _portable_request(take["request"], {item["id"]: item["hash"] for item in project["assets"]})
        ),
        "script_text": passage["text"],
        "language": passage["language"],
        "duration": asset["duration"],
    }
    return source, asset


def rekey_portable_reviews(before, after, old_assets, new_assets, identity_map=None):
    """Rebind validated reviews only during controlled archive transformations.

    Reference omission and owned-ID remapping change request representation.
    Keep its digest bound to the transformed request, while requiring the exact
    prior report identity and unchanged recorded audio, script and performance.
    """
    import copy

    identity_map = identity_map or {}
    old_documents = [before["document"], *(item["document"] for item in before.get("revisions", []))]
    new_documents = [after["document"], *(item["document"] for item in after.get("revisions", []))]
    for old_document, new_document in zip(old_documents, new_documents, strict=True):
        old_project = {**before, "assets": old_assets, "document": old_document}
        new_project = {**after, "assets": new_assets, "document": new_document}
        if not new_document.get("speech_reviews"):
            continue
        replacements = {}
        for take in before.get("takes", []):
            try:
                old_source, _ = source_identity(old_project, take["id"])
                new_source, _ = source_identity(new_project, identity_map.get(take["id"], take["id"]))
            except NotFound:
                continue
            if {k: v for k, v in old_source.items() if k != "request_sha256"} != {
                k: v for k, v in new_source.items() if k != "request_sha256"
            }:
                continue
            replacements[review_key(old_source)] = (old_source, new_source)
        for report in new_document.get("speech_reviews", []):
            replacement = replacements.get(report["source_key"])
            if replacement is None or report["source"] != replacement[0]:
                continue
            validate_report(report)
            report["source"] = copy.deepcopy(replacement[1])
            report["source_key"] = review_key(report["source"])


def review_key(source):
    return _digest(source)


def compare_speech(expected_text, recognized_text, words):
    expected, actual = _tokens(expected_text), _tokens(recognized_text)
    if len(expected) > 2000 or len(actual) > 2000:
        raise ValueError("Speech review supports at most 2000 text tokens")
    expected_keys, actual_keys = [word.casefold() for word in expected], [word.casefold() for word in actual]
    timed = [(token.casefold(), word) for word in words for token in _tokens(word["word"])]
    timing_matches = [token for token, _ in timed] == actual_keys
    differences = []
    for kind, start, end, found_start, found_end in SequenceMatcher(
        None, expected_keys, actual_keys, autojunk=False
    ).get_opcodes():
        if kind == "equal":
            continue
        label = {"delete": "omission", "replace": "substitution", "insert": "extra"}[kind]
        if kind == "insert" and any(word in expected_keys for word in actual_keys[found_start:found_end]):
            label = "repetition"
        interval = None
        if timing_matches and timed:
            left = timed[max(0, min(found_start, len(timed) - 1))][1]
            right = timed[max(0, min(max(found_start, found_end - 1), len(timed) - 1))][1]
            interval = {"start": left["start"], "end": right["end"]}
        differences.append(
            {
                "kind": label,
                "expected": " ".join(expected[start:end]),
                "recognized": " ".join(actual[found_start:found_end]),
                "expected_start": start,
                "expected_end": end,
                "recognized_start": found_start,
                "recognized_end": found_end,
                "audition": interval,
            }
        )
    return differences


def validate_report(report):
    required = {"version", "advisory", "source", "source_key", "recognized_text", "words", "differences", "recognizer"}
    if (
        not isinstance(report, dict)
        or set(report) != required
        or report["version"] != 1
        or report["advisory"] is not True
    ):
        raise ValueError("Invalid speech review report")
    source = report["source"]
    if (
        not isinstance(source, dict)
        or set(source)
        != {
            "asset_hash",
            "take_text",
            "take_engine",
            "take_model",
            "request_sha256",
            "script_text",
            "language",
            "duration",
        }
        or any(
            not isinstance(source.get(key), str) or len(source[key]) > 10000
            for key in ("asset_hash", "take_text", "take_engine", "request_sha256", "script_text", "language")
        )
        or not re.fullmatch(r"[0-9a-f]{64}", source["asset_hash"])
        or not re.fullmatch(r"[0-9a-f]{64}", source["request_sha256"])
        or (source["take_model"] is not None and not isinstance(source["take_model"], str))
        or type(source["duration"]) not in (int, float)
        or not math.isfinite(source["duration"])
        or not 0 < source["duration"] <= 120
        or report["source_key"] != review_key(source)
    ):
        raise ValueError("Invalid speech review source provenance")
    from ..services.transcribe import normalize_recognition

    normalized = normalize_recognition(
        {"text": report["recognized_text"], "words": report["words"]}, source["duration"]
    )
    if normalized["text"] != report["recognized_text"] or normalized["words"] != report["words"]:
        raise ValueError("Invalid normalized speech review")
    if report["differences"] != compare_speech(source["script_text"], normalized["text"], normalized["words"]):
        raise ValueError("Speech review differences do not match recognized text")
    identity = report["recognizer"]
    if (
        not isinstance(identity, dict)
        or not identity
        or len(identity) > 16
        or any(
            not isinstance(key, str)
            or len(key) > 100
            or (value is not None and (not isinstance(value, str) or len(value) > 200))
            for key, value in identity.items()
        )
    ):
        raise ValueError("Invalid recognizer identity")
    if len(json.dumps(report, allow_nan=False)) > 256 * 1024:
        raise ValueError("Speech review exceeds storage budget")


def validate_reviews(doc):
    reports = doc.get("speech_reviews", [])
    if not isinstance(reports, list) or len(reports) > MAX_REPORTS:
        raise ValueError("Invalid speech review history")
    for report in reports:
        validate_report(report)
    if len(json.dumps(reports, allow_nan=False)) > 1024 * 1024:
        raise ValueError("Speech review history exceeds storage budget")


def save_review(store, db, project_id, doc, payload):
    if not isinstance(payload, dict) or set(payload) != {"take_id", "report"}:
        raise ValueError("Expected take and speech review report")
    report = payload["report"]
    validate_report(report)
    project = store._project(db, project_id)
    project["document"] = doc
    source, _ = source_identity(project, payload["take_id"])
    if report["source"] != source:
        raise Conflict("Speech review source changed; recognize the current take again")
    reports = [value for value in doc.get("speech_reviews", []) if value["source_key"] != report["source_key"]]
    doc["speech_reviews"] = [*reports[-(MAX_REPORTS - 1) :], report]
    validate_reviews(doc)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    take_id: str = Field(min_length=1, max_length=200)


def make_speech_router(store):
    router = APIRouter()

    @router.post("/projects/{project_id}/speech-review")
    async def recognize(project_id: str, body: ReviewRequest):
        from ..services import transcribe
        from ..utils.async_work import run_blocking

        try:
            project = await run_blocking(store.get_project, project_id)
            if project["revision"] != body.expected_revision:
                raise Conflict("Save and reconcile the current project before speech review")
            source, asset = source_identity(project, body.take_id)
            if not 0 < asset["duration"] <= 120:
                raise ValueError("Speech review supports takes of at most 120 seconds")
            path = await run_blocking(store.asset_path, asset["id"])

            def verify_audio():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != source["asset_hash"]:
                    raise Conflict("Immutable source audio changed; restore the take before recognition")

            await run_blocking(verify_audio)
            result = await transcribe.recognize_words(path, asset["duration"], source["language"])
            current = await run_blocking(store.get_project, project_id)
            if current["revision"] != body.expected_revision or source_identity(current, body.take_id)[0] != source:
                raise Conflict("Speech review source changed while recognition was running")
            await run_blocking(verify_audio)
            report = {
                "version": 1,
                "advisory": True,
                "source": source,
                "source_key": review_key(source),
                "recognized_text": result["text"],
                "words": result["words"],
                "differences": compare_speech(source["script_text"], result["text"], result["words"]),
                "recognizer": result["recognizer"],
            }
            validate_report(report)
            return report
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "Local speech recognition failed; compare the take by listening") from exc

    return router
