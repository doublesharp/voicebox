"""Portable archives retain playback when only regeneration references are missing."""

import copy
import hashlib
import json
import zipfile

import numpy as np
import pytest
import soundfile as sf

from backend.studio.archive import read_archive, write_archive
from backend.studio.portability import import_archive
from backend.studio.storage import StudioStore
from backend.tests.test_studio_archive_validation import fixture_project


def prepared(tmp_path):
    project, audio, source = fixture_project(tmp_path)
    reference = tmp_path / "reference.wav"
    sf.write(reference, np.full(480, 0.2), 48000, subtype="PCM_24")
    ref = dict(
        audio,
        id="reference",
        hash=hashlib.sha256(reference.read_bytes()).hexdigest(),
        size_bytes=reference.stat().st_size,
        frames=480,
        duration=0.01,
    )
    p = project["document"]["passages"][0]
    p["voice_snapshot"] = dict(
        engine="qwen",
        model_size="1.7B",
        voice_type="cloned",
        samples=[dict(asset_id="reference", reference_text="This is the reference")],
    )
    project["takes"][0]["request"] = dict(passage=copy.deepcopy(p))
    project["revisions"] = [
        dict(
            id="checkpoint",
            name="Approved",
            created_at="now",
            project_revision=0,
            document=copy.deepcopy(project["document"]),
        )
    ]
    project["assets"] = [audio, ref]
    paths = {"audio": source, "reference": reference}
    archive = tmp_path / "all.zip"
    write_archive(project, project["assets"], paths.__getitem__, archive)
    return project, paths, archive


def rewrite_archive(source, target, *, omit=(), corrupt=(), mutate=None):
    with zipfile.ZipFile(source) as original:
        manifest = json.loads(original.read("manifest.json"))
        if mutate:
            mutate(manifest)
        with zipfile.ZipFile(target, "w") as result:
            for asset in manifest["assets"]:
                if asset["id"] not in omit:
                    data = b"broken" if asset["id"] in corrupt else original.read(asset["archive_path"])
                    result.writestr(asset["archive_path"], data)
            result.writestr("manifest.json", json.dumps(manifest))
    return target


def test_missing_reference_imports_and_reexports_playable_audio_into_clean_store(tmp_path):
    _, paths, archive = prepared(tmp_path)
    without_reference = rewrite_archive(archive, tmp_path / "missing-reference.zip", omit={"reference"})
    clean = StudioStore(tmp_path / "clean")
    restored = import_archive(clean, without_reference)
    assert len(restored["assets"]) == 1
    assert clean.asset_path(restored["assets"][0]["id"]).read_bytes() == paths["audio"].read_bytes()
    for doc in [restored["document"], restored["revisions"][0]["document"]]:
        voice = doc["passages"][0]["voice_snapshot"]
        assert voice["unavailable_references"] == [voice["samples"][0]["asset_id"]]
    assert restored["takes"][0]["request"]["passage"]["voice_snapshot"]["unavailable_references"]
    second = tmp_path / "second.zip"
    write_archive(restored, restored["assets"], clean.asset_path, second)
    third_store = StudioStore(tmp_path / "third")
    third = import_archive(third_store, second)
    assert len(third["assets"]) == 1
    assert third_store.asset_path(third["assets"][0]["id"]).read_bytes() == paths["audio"].read_bytes()


@pytest.mark.parametrize("fault", ["required", "corrupt-reference"])
def test_required_audio_and_present_corrupt_reference_are_rejected_before_publication(tmp_path, fault):
    _, _, archive = prepared(tmp_path)
    broken = rewrite_archive(
        archive,
        tmp_path / "broken.zip",
        omit={"audio"} if fault == "required" else (),
        corrupt={"reference"} if fault == "corrupt-reference" else (),
    )
    clean = StudioStore(tmp_path / "clean")
    with pytest.raises(ValueError, match="(?i)(audio|asset)"):
        import_archive(clean, broken)
    assert clean.list_projects() == []
    assert list(clean.asset_dir.iterdir()) == []


def test_explicit_used_scope_preserves_checkpoint_selected_takes_and_prunes_only_unused(tmp_path):
    project, paths, _ = prepared(tmp_path)
    original = copy.deepcopy(project)
    audio = project["assets"][0]
    for ident in ("second", "unused"):
        metadata = dict(audio, id=ident)
        project["assets"].append(metadata)
        paths[ident] = paths["audio"]
        project["takes"].append(dict(project["takes"][0], id=ident, asset_id=ident))
    project["document"]["passages"][0]["selected_take_id"] = "second"
    project["document"]["take_annotations"] = {"unused": {"name": "An alternative", "note": "Not used"}}
    before = copy.deepcopy(project)
    target = tmp_path / "used.zip"
    write_archive(
        project, project["assets"], paths.__getitem__, target, take_scope="used", include_regeneration_references=False
    )
    restored = read_archive(target, tmp_path / "used-stage")
    assert {t["id"] for t in restored["project"]["takes"]} == {"take", "second"}
    assert {a["id"] for a in restored["assets"]} == {"audio", "second"}
    assert restored["project"]["document"]["take_annotations"] == {}
    assert restored["project"]["revisions"] == [
        {
            **original["revisions"][0],
            "document": {
                **original["revisions"][0]["document"],
                "passages": [
                    {
                        **original["revisions"][0]["document"]["passages"][0],
                        "voice_snapshot": {
                            **original["revisions"][0]["document"]["passages"][0]["voice_snapshot"],
                            "unavailable_references": ["reference"],
                        },
                    }
                ],
            },
        }
    ]
    assert restored["scope"]["take_scope"] == "used"
    assert restored["scope"]["excluded_take_count"] == 1
    assert restored["scope"]["excluded_asset_count"] == 2
    assert restored["scope"]["missing_reference_count"] == 1
    assert project == before
    clean = StudioStore(tmp_path / "used-clean")
    imported = import_archive(clean, target)
    assert imported["document"]["archive_scope"]["excluded_take_count"] == 1
    assert len(imported["revisions"]) == 1
    assert len(imported["takes"]) == 2


def test_missing_reference_blocks_generation_before_capability_or_engine_resolution(tmp_path):
    from backend.studio.jobs import StudioJobs

    _, _, archive = prepared(tmp_path)
    broken = rewrite_archive(archive, tmp_path / "playback-only.zip", omit={"reference"})
    clean = StudioStore(tmp_path / "clean")
    restored = import_archive(clean, broken)
    pending, resolved = [], []
    jobs = StudioJobs(
        clean,
        enqueue=lambda ident, coro: pending.append(coro),
        capability_resolver=lambda voice: resolved.append(voice),
    )
    with pytest.raises(ValueError, match="regeneration references are unavailable"):
        jobs.submit(restored["id"], "regenerate", [restored["document"]["passages"][0]["id"]], 1, False)
    assert pending == []
    assert resolved == []
    assert clean.get_project(restored["id"])["jobs"] == []


@pytest.mark.parametrize(
    "marker", [None, "reference", ["reference", "reference"], ["audio"], ["unknown"], ["reference"]]
)
def test_unavailable_reference_markers_cannot_hide_present_or_required_audio(tmp_path, marker):
    _, _, archive = prepared(tmp_path)
    target = rewrite_archive(
        archive,
        tmp_path / "marker.zip",
        mutate=lambda manifest: manifest["project"]["document"]["passages"][0]["voice_snapshot"].update(
            unavailable_references=marker
        ),
    )
    with pytest.raises(ValueError, match="(?i)(reference|sample)"):
        read_archive(target, tmp_path / "marker-stage")


def test_playback_audio_used_as_reference_is_never_optional(tmp_path):
    project, paths, _ = prepared(tmp_path)
    for passage in [
        project["document"]["passages"][0],
        project["revisions"][0]["document"]["passages"][0],
        project["takes"][0]["request"]["passage"],
    ]:
        passage["voice_snapshot"]["samples"][0]["asset_id"] = "audio"
    target = tmp_path / "shared.zip"
    write_archive(project, project["assets"], paths.__getitem__, target, include_regeneration_references=False)
    restored = read_archive(target, tmp_path / "shared-stage")
    assert "audio" in {a["id"] for a in restored["assets"]}
    assert "unavailable_references" not in restored["project"]["document"]["passages"][0]["voice_snapshot"]
    missing = rewrite_archive(target, tmp_path / "missing-required.zip", omit={"audio"})
    with pytest.raises(ValueError, match="Required playback audio"):
        read_archive(missing, tmp_path / "missing-required-stage")


def test_unknown_reference_field_cannot_be_excused_by_optional_sample_identity(tmp_path):
    project, paths, _ = prepared(tmp_path)
    project["custom"] = {"required_asset_id": "reference"}
    target = tmp_path / "custom.zip"
    write_archive(project, project["assets"], paths.__getitem__, target)
    missing = rewrite_archive(target, tmp_path / "custom-missing.zip", omit={"reference"})
    with pytest.raises(ValueError, match="Required playback audio"):
        read_archive(missing, tmp_path / "custom-stage")


def test_frozen_job_snapshots_are_marked_without_rewriting_text(tmp_path):
    project, paths, _ = prepared(tmp_path)
    passage = copy.deepcopy(project["document"]["passages"][0])
    passage["text"] = "reference"
    project["jobs"] = [
        dict(id="job", request=dict(passage=passage)),
        dict(id="render", request=dict(document=copy.deepcopy(project["document"]))),
    ]
    target = tmp_path / "jobs.zip"
    write_archive(project, project["assets"], paths.__getitem__, target, include_regeneration_references=False)
    restored = read_archive(target, tmp_path / "jobs-stage")["project"]
    assert restored["jobs"][0]["request"]["passage"]["text"] == "reference"
    assert restored["jobs"][0]["request"]["passage"]["voice_snapshot"]["unavailable_references"] == ["reference"]
    assert restored["jobs"][1]["request"]["document"]["passages"][0]["voice_snapshot"]["unavailable_references"] == [
        "reference"
    ]
