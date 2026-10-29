"""Portable graph scope and explicit, schema-owned missing regeneration references."""

import copy


def asset_references(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if (key == "asset_id" or key.endswith("_asset_id")) and item is not None:
                if not isinstance(item, str) or not item:
                    raise ValueError("invalid asset reference")
                found.add(item)
            elif (key in ("asset_ids", "reference_assets") or key.endswith("_asset_ids")) and item is not None:
                if not isinstance(item, list) or any(not isinstance(x, str) or not x for x in item):
                    raise ValueError("invalid asset references")
                found.update(item)
            else:
                found.update(asset_references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(asset_references(item))
    return found


def documents(project):
    yield project["document"]
    for revision in project.get("revisions", []):
        yield revision["document"]
    for job in project.get("jobs", []):
        request = job.get("request") or {}
        if isinstance(request, dict) and isinstance(request.get("document"), dict):
            yield request["document"]


def voice_snapshots(project):
    seen = set()
    for doc in documents(project):
        for passage in doc.get("passages", []):
            voice = passage.get("voice_snapshot")
            if isinstance(voice, dict) and id(voice) not in seen:
                seen.add(id(voice))
                yield voice
    for item in [*project.get("takes", []), *project.get("jobs", [])]:
        request = item.get("request") or {}
        passage = request.get("passage") if isinstance(request, dict) else None
        if isinstance(passage, dict) and isinstance(passage.get("voice_snapshot"), dict):
            voice = passage["voice_snapshot"]
            if id(voice) not in seen:
                seen.add(id(voice))
                yield voice


def sample_ids(voice):
    if voice.get("voice_type") != "cloned":
        return set()
    samples = voice.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("invalid cloned voice samples")
    found = set()
    for sample in samples:
        if (
            not isinstance(sample, dict)
            or not isinstance(sample.get("asset_id"), str)
            or not sample["asset_id"]
            or not isinstance(sample.get("reference_text"), str)
        ):
            raise ValueError("invalid voice sample")
        found.add(sample["asset_id"])
    return found


def unavailable_references(voice):
    missing = voice.get("unavailable_references", [])
    if (
        not isinstance(missing, list)
        or len(missing) > 10000
        or any(not isinstance(item, str) or not item for item in missing)
        or len(set(missing)) != len(missing)
    ):
        raise ValueError("invalid unavailable regeneration references")
    if missing and not set(missing).issubset(sample_ids(voice)):
        raise ValueError("unavailable regeneration reference is not a cloned voice sample")
    return set(missing)


def _without_sample_references(project, *, all_samples=False):
    result = copy.deepcopy(project)
    for voice in voice_snapshots(result):
        omitted = sample_ids(voice) if all_samples else unavailable_references(voice)
        for sample in voice.get("samples", []) if voice.get("voice_type") == "cloned" else []:
            if sample.get("asset_id") in omitted:
                sample.pop("asset_id")
        if isinstance(voice.get("reference_assets"), list):
            voice["reference_assets"] = [ident for ident in voice["reference_assets"] if ident not in omitted]
    return result


def required_references(project):
    """All references except explicitly unavailable schema-owned voice samples."""
    return asset_references(_without_sample_references(project))


def optional_reference_ids(project):
    samples = set().union(*(sample_ids(voice) for voice in voice_snapshots(project)))
    # An audio asset used anywhere outside those sample fields remains required.
    return samples - asset_references(_without_sample_references(project, all_samples=True))


def validate_reference_markers(project, assets):
    for voice in voice_snapshots(project):
        samples = sample_ids(voice)
        missing = unavailable_references(voice)
        if samples - set(assets) != missing:
            raise ValueError("missing voice reference must be explicitly unavailable")
        if missing.intersection(assets):
            raise ValueError("unavailable voice reference is present")


def mark_missing_references(project, missing):
    """Mutate only known snapshot references, never user text or arbitrary IDs."""
    missing = set(missing)
    if not missing.issubset(optional_reference_ids(project)):
        raise ValueError("Required playback audio is missing")
    for voice in voice_snapshots(project):
        unavailable = unavailable_references(voice) | (sample_ids(voice) & missing)
        if unavailable:
            voice["unavailable_references"] = sorted(unavailable)
    if isinstance(project.get("assets"), list):
        project["assets"] = [asset for asset in project["assets"] if asset["id"] not in missing]


def validate_scope(scope):
    if scope is None:
        return None  # Version-one archives created before scope recording remain readable.
    if (
        not isinstance(scope, dict)
        or set(scope)
        != {
            "schema_version",
            "take_scope",
            "include_regeneration_references",
            "checkpoints",
            "edit_history",
            "job_history",
            "excluded_take_count",
            "excluded_asset_count",
            "missing_reference_count",
        }
        or type(scope.get("schema_version")) is not int
        or scope["schema_version"] != 1
        or scope.get("take_scope") not in ("all", "used")
        or type(scope.get("include_regeneration_references")) is not bool
        or scope.get("checkpoints") != "all"
        or scope.get("edit_history") != "not_included"
        or scope.get("job_history") != "provenance_only"
        or any(
            type(scope.get(key)) is not int or scope[key] < 0
            for key in (
                "excluded_take_count",
                "excluded_asset_count",
                "missing_reference_count",
            )
        )
    ):
        raise ValueError("invalid archive scope")
    return copy.deepcopy(scope)


def prepare_export(project, assets, *, take_scope="all", include_regeneration_references=True):
    if take_scope not in ("all", "used") or type(include_regeneration_references) is not bool:
        raise ValueError("invalid archive export scope")
    result = copy.deepcopy(project)
    original_takes = result.get("takes", [])
    if take_scope == "used":
        used = set()
        for doc in documents(result):
            used.update(p["selected_take_id"] for p in doc.get("passages", []) if p.get("selected_take_id"))
            used.update(c["take_id"] for c in doc.get("clips", []) if c.get("take_id"))
        for acknowledgement in result.get("recording_acknowledgements", []):
            used.update(r["take_id"] for r in acknowledgement.get("recordings", []) if r.get("take_id"))
        result["takes"] = [take for take in original_takes if take["id"] in used]
        for doc in documents(result):
            if "take_annotations" in doc:
                doc["take_annotations"] = {
                    ident: note for ident, note in doc["take_annotations"].items() if ident in used
                }
    available = {asset["id"] for asset in assets}
    omitted = set()
    if not include_regeneration_references:
        omitted = optional_reference_ids(result) & available
        mark_missing_references(result, omitted)
    kept = available - omitted
    if take_scope == "used":
        kept &= required_references(result)
    selected = [copy.deepcopy(asset) for asset in assets if asset["id"] in kept]
    if isinstance(result.get("assets"), list):
        result["assets"] = [asset for asset in result["assets"] if asset["id"] in kept]
    scope = dict(
        schema_version=1,
        take_scope=take_scope,
        include_regeneration_references=include_regeneration_references,
        checkpoints="all",
        edit_history="not_included",
        job_history="provenance_only",
        excluded_take_count=len(original_takes) - len(result.get("takes", [])),
        excluded_asset_count=len(assets) - len(selected),
        missing_reference_count=len(set().union(*(unavailable_references(v) for v in voice_snapshots(result)))),
    )
    return result, selected, scope
