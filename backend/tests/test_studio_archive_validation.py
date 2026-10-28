"""Validate the project graph at the portable archive boundary."""

import copy
import hashlib
import json
import zipfile

import numpy as np
import pytest
import soundfile as sf

from backend.studio.archive import read_archive, write_archive


def fixture_project(tmp_path):
    source = tmp_path / "source.wav"
    sf.write(source, np.full(48, 0.1), 48000, subtype="PCM_24")
    asset = {
        "id": "audio",
        "hash": hashlib.sha256(source.read_bytes()).hexdigest(),
        "size_bytes": source.stat().st_size,
        "sample_rate": 48000,
        "channels": 1,
        "frames": 48,
        "duration": 0.001,
    }
    passage = {
        "id": "passage",
        "text": "Hello",
        "language": "en",
        "profile_id": None,
        "voice_snapshot": None,
        "direction": {"instruct": "", "seed": None, "max_chunk_chars": 300, "crossfade_ms": 0},
        "input_revision": 0,
        "selection_token": 0,
        "selected_take_id": "take",
        "locked": False,
    }
    clip = {
        "id": "clip",
        "passage_id": "passage",
        "take_id": "take",
        "asset_id": "audio",
        "track_id": "voice",
        "start_sample": 0,
        "trim_start_sample": 0,
        "trim_end_sample": 0,
        "gain": 1.0,
        "fade_in_samples": 0,
        "fade_out_samples": 0,
    }
    document = {
        "schema_version": 1,
        "sample_rate": 48000,
        "channels": 2,
        "language": "en",
        "passages": [passage],
        "tracks": [{"id": "voice", "name": "Voice", "kind": "voice", "mute": False, "solo": False, "gain": 1}],
        "clips": [clip],
        "mix": {"normalize": False, "target_lufs": -16, "max_true_peak_dbtp": -1},
    }
    project = {
        "id": "project",
        "name": "Project",
        "document": document,
        "takes": [
            {
                "id": "take",
                "passage_id": "passage",
                "asset_id": "audio",
                "text": "Hello",
                "request": {},
                "engine": "fixture",
                "created_at": "2026-10-28T00:00:00Z",
            }
        ],
        "revisions": [],
        "approvals": [],
    }
    return project, asset, source


@pytest.mark.parametrize(
    "fault",
    [
        "missing_take",
        "missing_track",
        "duplicate_passage",
        "boolean_schema",
        "missing_voice_reference",
        "invalid_range",
    ],
)
def test_writer_rejects_invalid_project_graph_without_replacing_archive(tmp_path, fault):
    project, asset, source = fixture_project(tmp_path)
    doc = project["document"]
    if fault == "missing_take":
        doc["passages"][0]["selected_take_id"] = "missing"
    elif fault == "missing_track":
        doc["clips"][0]["track_id"] = "missing"
    elif fault == "duplicate_passage":
        doc["passages"].append(copy.deepcopy(doc["passages"][0]))
    elif fault == "boolean_schema":
        doc["schema_version"] = True
    elif fault == "missing_voice_reference":
        doc["passages"][0]["voice_snapshot"] = {"reference_assets": ["missing"]}
    else:
        doc["clips"][0]["trim_end_sample"] = 49
    output = tmp_path / "approved.zip"
    output.write_bytes(b"approved")
    with pytest.raises(ValueError):
        write_archive(project, [asset], lambda _: source, output)
    assert output.read_bytes() == b"approved"


def test_import_rejects_dangling_take_before_publishing_assets(tmp_path):
    project, asset, source = fixture_project(tmp_path)
    project["document"]["passages"][0]["selected_take_id"] = "missing"
    manifest = {"schema_version": 1, "project": project, "assets": [{**asset, "archive_path": "assets/00000000"}]}
    archive = tmp_path / "invalid.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("manifest.json", json.dumps(manifest))
        output.write(source, "assets/00000000")
    stage = tmp_path / "stage"
    with pytest.raises(ValueError):
        read_archive(archive, stage)
    assert not list(stage.iterdir())


def test_declared_audio_metadata_must_match_the_recording(tmp_path):
    project, asset, source = fixture_project(tmp_path)
    asset["sample_rate"] = 24000
    with pytest.raises(ValueError):
        write_archive(project, [asset], lambda _: source, tmp_path / "bad.zip")


def test_complete_project_and_checkpoint_roundtrip(tmp_path):
    project, asset, source = fixture_project(tmp_path)
    project["revisions"] = [
        {
            "id": "revision",
            "name": "Approved",
            "document": copy.deepcopy(project["document"]),
            "created_at": "2026-10-28T00:00:00Z",
            "project_revision": 0,
        }
    ]
    project["approvals"] = [{"id": "approval", "revision_id": "revision", "created_at": "2026-10-28T00:00:00Z"}]
    archive = tmp_path / "valid.zip"
    write_archive(project, [asset], lambda _: source, archive)
    restored = read_archive(archive, tmp_path / "stage")
    assert restored["project"] == project
    assert hashlib.sha256(open(restored["assets"][0]["path"], "rb").read()).hexdigest() == asset["hash"]


@pytest.mark.parametrize(
    "fault", ["missing_direction", "string_direction", "bad_chunk", "bad_seed", "string_voice", "incomplete_preset"]
)
@pytest.mark.parametrize("checkpoint", [False, True])
def test_archive_rejects_corrupt_generation_inputs(tmp_path, fault, checkpoint):
    project, asset, source = fixture_project(tmp_path)
    doc = copy.deepcopy(project["document"])
    if checkpoint:
        project["revisions"] = [
            {"id": "rev", "name": "Saved", "created_at": "2026-10-28T00:00:00Z", "project_revision": 0, "document": doc}
        ]
    else:
        project["document"] = doc
    passage = doc["passages"][0]
    if fault == "missing_direction":
        passage.pop("direction")
    elif fault == "string_direction":
        passage["direction"] = "bad"
    elif fault == "bad_chunk":
        passage["direction"]["max_chunk_chars"] = 0
    elif fault == "bad_seed":
        passage["direction"]["seed"] = True
    elif fault == "string_voice":
        passage["voice_snapshot"] = "bad"
    else:
        passage["voice_snapshot"] = {"voice_type": "preset", "engine": "kokoro"}
    with pytest.raises(ValueError):
        write_archive(project, [asset], lambda _: source, tmp_path / "bad.zip")


@pytest.mark.parametrize("rate,channels", [(96000, 2), (48000, 1)])
def test_import_rejects_project_formats_the_studio_cannot_preview(tmp_path, rate, channels):
    project, asset, source = fixture_project(tmp_path)
    project["document"]["sample_rate"] = rate
    project["document"]["channels"] = channels
    manifest = {"schema_version": 1, "project": project, "assets": [{**asset, "archive_path": "assets/00000000"}]}
    archive = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("manifest.json", json.dumps(manifest))
        output.write(source, "assets/00000000")
    stage = tmp_path / "stage"
    with pytest.raises(ValueError, match="48 kHz stereo"):
        read_archive(archive, stage)
    assert not list(stage.iterdir())
