"""Conservative frozen identities shared by track routing and stem preflight."""

import copy
import json


def frozen_identity(take, asset_hash):
    request = take.get("request") or {}
    if isinstance(request, str):
        request = json.loads(request)
    if not isinstance(request, dict):
        raise ValueError("Invalid frozen generation request")
    passage = request.get("passage", {})
    if not isinstance(passage, dict):
        raise ValueError("Invalid frozen passage snapshot")
    voice = copy.deepcopy(passage.get("voice_snapshot"))
    if voice is not None and not isinstance(voice, dict):
        raise ValueError("Invalid frozen voice snapshot")
    if voice and any(voice.get(key) for key in ("id", "preset_voice_id", "design_prompt", "samples")):
        voice.pop("name", None)
        for sample in voice.get("samples", []):
            sample["asset_id"] = asset_hash(sample["asset_id"]) or sample["asset_id"]
        return voice
    source = request.get("legacy_generation_id")
    version = request.get("legacy_version_id")
    if isinstance(source, str) and source and (version is None or isinstance(version, str)):
        # Same pinned recording is known; do not infer identity across unrelated
        # generations merely because their current profile or samples match.
        return {
            "legacy_generation_id": source,
            "legacy_version_id": version,
            "engine": take.get("engine"),
            "model_size": take.get("model_size"),
        }
    return {"unknown_take_id": take["id"]}


def rekey_portable_tracks(before, after, old_assets, new_assets, identity_map=None, *, bind_unkeyed=False):
    """Rebind opaque voice keys after reference omission or owned-ID remapping.

    Only unambiguous frozen identities are rebound; all mix controls stay intact.
    """
    import hashlib

    identity_map = identity_map or {}
    old_hashes = {asset["id"]: asset["hash"] for asset in old_assets}
    new_hashes = {asset["id"]: asset["hash"] for asset in new_assets}
    old_takes = {take["id"]: take for take in before.get("takes", [])}
    new_takes = {take["id"]: take for take in after.get("takes", [])}

    def key(take, hashes):
        value = frozen_identity(take, hashes.get)
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    changes = {}
    for old_id, take in old_takes.items():
        new = new_takes.get(identity_map.get(old_id, old_id))
        if new is not None:
            old_key, new_key = key(take, old_hashes), key(new, new_hashes)
            changes.setdefault(old_key, set()).add(new_key)
    if not bind_unkeyed and not any(values != {old_key} for old_key, values in changes.items()):
        return
    documents = [after["document"], *(r["document"] for r in after.get("revisions", []))]
    for document in documents:
        for track in document["tracks"]:
            if track.get("kind") != "voice":
                continue
            clips = [clip for clip in document["clips"] if clip["track_id"] == track["id"]]
            pinned = {key(new_takes[clip["take_id"]], new_hashes) for clip in clips if clip.get("take_id") in new_takes}
            if clips:
                if (
                    (track.get("voice_key") or bind_unkeyed)
                    and len(pinned) == 1
                    and all(clip.get("take_id") in new_takes for clip in clips)
                ):
                    track["voice_key"] = next(iter(pinned))
            else:
                replacement = changes.get(track.get("voice_key"), set())
                if len(replacement) == 1:
                    track["voice_key"] = next(iter(replacement))
