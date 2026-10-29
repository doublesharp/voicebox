"""Portable frozen voice identity retains the track's mix recipe on reuse."""

import numpy as np
import pytest
import soundfile as sf

from backend.studio.archive import write_archive
from backend.studio.jobs import StudioJobs
from backend.studio.portability import import_archive
from backend.studio.storage import StudioStore
from backend.tests.test_studio_review_regressions import fixture_capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_track", [False, True])
async def test_reference_omission_and_id_remap_preserve_voice_track_recipe(tmp_path, empty_track):
    voice = {}
    source = StudioStore(tmp_path / "source", voice_resolver=lambda *args: voice)
    project = source.create_project("Portable voice", script="Same performance")
    reference_path = tmp_path / "reference.wav"
    sf.write(reference_path, np.full(480, 0.2), 48000)
    reference = source.add_asset(project["id"], reference_path)
    voice.update(
        engine="fixture", voice_type="cloned", samples=[{"asset_id": reference["id"], "reference_text": "Sample"}]
    )
    pid = project["document"]["passages"][0]["id"]
    project = source.command(project["id"], "voice", 0, "assign_voice", {"passage_ids": [pid], "profile_id": "voice"})
    pending = []

    async def synth(_):
        return np.full(1000, 0.1, dtype="float32"), 48000

    jobs = StudioJobs(
        source, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda _, coro: pending.append(coro)
    )
    jobs.submit(project["id"], "takes", [pid], 2, True)
    for job in pending:
        await job
    project = source.get_project(project["id"])
    project = source.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": [pid]})
    tid = project["document"]["tracks"][0]["id"]
    recipe = [{"type": "gain", "params": {"gain_db": -6}}]
    project = source.command(
        project["id"],
        "mix",
        project["revision"],
        "update_track",
        {"track_id": tid, "gain": 0.4, "effects_chain": recipe},
    )
    if empty_track:
        project = source.command(
            project["id"],
            "remove",
            project["revision"],
            "delete_clip",
            {"clip_id": project["document"]["clips"][0]["id"]},
        )
    project = source.checkpoint(project["id"], "cp", project["revision"], "Kept recipe")
    archive = tmp_path / "portable.zip"
    write_archive(project, project["assets"], source.asset_path, archive, include_regeneration_references=False)
    clean = StudioStore(tmp_path / "clean")
    imported = import_archive(clean, archive)
    original = imported["document"]["tracks"][0]
    passage = imported["document"]["passages"][0]
    if empty_track:
        updated = clean.command(imported["id"], "reuse", 0, "insert_passages", {"passage_ids": [passage["id"]]})
    else:
        alternative = next(t for t in imported["takes"] if t["id"] != passage["selected_take_id"])
        updated = clean.command(
            imported["id"],
            "replace",
            0,
            "select_take",
            {"passage_id": passage["id"], "take_id": alternative["id"], "replace_clips": True},
        )
    clip = updated["document"]["clips"][0]
    if not empty_track:
        assert clip["take_id"] == alternative["id"]
    assert clip["track_id"] == original["id"]
    assert len(updated["document"]["tracks"]) == 1
    assert updated["document"]["tracks"][0]["gain"] == 0.4
    assert updated["document"]["tracks"][0]["effects_chain"] == recipe
