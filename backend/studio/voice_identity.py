"""Conservative frozen identities shared by track routing and stem preflight."""

import copy
import json


def frozen_identity(take, asset_hash):
    request = take.get("request") or {}
    if isinstance(request, str):
        request = json.loads(request)
    voice = copy.deepcopy(request.get("passage", {}).get("voice_snapshot"))
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
