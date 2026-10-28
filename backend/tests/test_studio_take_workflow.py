import copy

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.archive import _validate_document, write_archive
from backend.studio.portability import import_archive
from backend.studio.storage import Conflict, StudioStore, encode, now


def setup_takes(tmp_path):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Takes", script="Recorded words")
    pid = project["document"]["passages"][0]["id"]
    for ident, frames, level in (("long", 96000, 0.4), ("short", 24000, 0.1)):
        path = tmp_path / (ident + ".wav")
        sf.write(path, np.full(frames, level), 48000, subtype="FLOAT")
        asset = store.add_asset(project["id"], path)
        with store.connect() as db:
            db.execute(
                "INSERT INTO takes VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    project["id"],
                    pid,
                    asset["id"],
                    "Recorded words",
                    "test",
                    None,
                    now(),
                    encode({"passage": copy.deepcopy(project["document"]["passages"][0])}),
                ),
            )
    return store, store.get_project(project["id"]), pid


def command(store, project, kind, payload, op=None):
    return store.command(project["id"], op or str(project["revision"]) + kind, project["revision"], kind, payload)


def test_annotations_persist_undo_checkpoint_archive_and_do_not_change_take(tmp_path):
    store, project, pid = setup_takes(tmp_path)
    original = copy.deepcopy(project["takes"])
    payload = {"take_id": "long", "name": "Client pick", "note": "short"}
    updated = command(store, project, "annotate_take", payload, "name")
    assert updated["document"]["take_annotations"]["long"] == {"name": payload["name"], "note": payload["note"]}
    assert command(store, project, "annotate_take", payload, "name") == updated
    with pytest.raises(Conflict):
        command(store, project, "annotate_take", {**payload, "note": "Changed"}, "name")
    with pytest.raises(Conflict):
        command(store, project, "annotate_take", payload, "stale")
    undone = command(store, updated, "undo", {})
    assert undone["document"].get("take_annotations", {}) == {}
    redone = command(store, undone, "redo", {})
    saved = store.checkpoint(project["id"], "checkpoint", redone["revision"], "Chosen")
    assert StudioStore(store.root).get_project(project["id"])["document"] == saved["document"]
    assert saved["takes"] == original
    archive = tmp_path / "takes.zip"
    write_archive(saved, saved["assets"], store.asset_path, archive)
    reopened = import_archive(StudioStore(tmp_path / "clean"), archive)
    annotations = reopened["document"]["take_annotations"]
    assert "long" not in annotations
    assert list(annotations.values()) == [{"name": "Client pick", "note": "short"}]
    assert reopened["revisions"][0]["document"]["take_annotations"] == annotations


@pytest.mark.parametrize(
    "payload",
    [
        {"take_id": "missing", "name": "No"},
        {"take_id": "long", "name": "x" * 121},
        {"take_id": "long", "note": "x" * 4001},
        {"take_id": "long", "note": None},
    ],
)
def test_bad_annotation_does_not_acknowledge_or_change_revision(tmp_path, payload):
    store, project, _ = setup_takes(tmp_path)
    app = FastAPI()
    app.include_router(make_router(store))
    response = TestClient(app).post(
        f"/studio/v1/projects/{project['id']}/commands",
        json={
            "operation_id": "bad",
            "expected_revision": 0,
            "type": "annotate_take",
            "payload": payload,
        },
    )
    assert response.status_code in (404, 422)
    assert store.get_project(project["id"])["revision"] == 0


def test_short_replacement_is_explicit_atomic_and_undoable(tmp_path):
    store, project, pid = setup_takes(tmp_path)
    project = command(store, project, "select_take", {"passage_id": pid, "take_id": "long"})
    project = command(store, project, "insert_passages", {"passage_ids": [pid], "start_sample": 777})
    clip = project["document"]["clips"][0]
    project = command(
        store, project, "update_clip", {"clip_id": clip["id"], "trim_start_sample": 30000, "fade_in_samples": 10000}
    )
    before = copy.deepcopy(project)
    with pytest.raises(ValueError, match="Invalid clip range"):
        command(store, project, "select_take", {"passage_id": pid, "take_id": "short", "replace_clips": True})
    assert store.get_project(project["id"]) == before
    kept = command(store, project, "select_take", {"passage_id": pid, "take_id": "short", "replace_clips": False})
    assert kept["document"]["clips"] == before["document"]["clips"]
    updated = command(
        store, kept, "select_take", {"passage_id": pid, "take_id": "short", "replace_clips": True, "reset_trims": True}
    )
    clip = updated["document"]["clips"][0]
    assert (clip["start_sample"], clip["trim_start_sample"], clip["trim_end_sample"], clip["fade_in_samples"]) == (
        777,
        0,
        0,
        10000,
    )
    assert clip["take_id"] == "short"
    assert command(store, updated, "undo", {})["document"]["clips"] == before["document"]["clips"]


def test_annotation_api_partial_update_restoration_and_project_isolation(tmp_path):
    store, project, _ = setup_takes(tmp_path)
    app = FastAPI()
    app.include_router(make_router(store))
    client = TestClient(app)
    url = f"/studio/v1/projects/{project['id']}/commands"
    body = {
        "operation_id": "name",
        "expected_revision": 0,
        "type": "annotate_take",
        "payload": {"take_id": "long", "name": "Keep"},
    }
    first = client.post(url, json=body)
    assert first.status_code == 200
    first = first.json()
    saved = store.checkpoint(project["id"], "cp", first["revision"], "Named")
    response = client.post(
        url,
        json={
            **body,
            "operation_id": "note",
            "expected_revision": saved["revision"],
            "payload": {"take_id": "long", "note": "Later note"},
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["document"]["take_annotations"]["long"] == {"name": "Keep", "note": "Later note"}
    restored = command(store, updated, "restore_revision", {"revision_id": saved["revisions"][0]["id"]})
    assert restored["document"]["take_annotations"]["long"] == {"name": "Keep", "note": ""}
    other = store.create_project("Other")
    foreign = client.post(f"/studio/v1/projects/{other['id']}/commands", json=body)
    assert foreign.status_code == 404
    assert store.get_project(other["id"])["revision"] == 0


def test_reset_clears_only_fades_that_no_longer_fit_and_preserves_routing(tmp_path):
    store, project, pid = setup_takes(tmp_path)
    project = command(store, project, "select_take", {"passage_id": pid, "take_id": "long"})
    project = command(store, project, "insert_passages", {"passage_ids": [pid], "start_sample": 123})
    original_clip = project["document"]["clips"][0]
    with store.connect() as db:
        db.execute(
            "UPDATE takes SET request=? WHERE id='short'",
            (
                encode(
                    {
                        "passage": {
                            "voice_snapshot": {
                                "name": "Different frozen voice",
                                "engine": "test",
                                "voice_type": "preset",
                                "preset_voice_id": "other",
                            }
                        }
                    }
                ),
            ),
        )
    project = command(
        store,
        project,
        "update_clip",
        {"clip_id": original_clip["id"], "fade_in_samples": 48000, "fade_out_samples": 1000, "gain": 0.7},
    )
    selected = command(
        store,
        project,
        "select_take",
        {"passage_id": pid, "take_id": "short", "replace_clips": True, "reset_trims": True},
    )
    clip = selected["document"]["clips"][0]
    assert clip["start_sample"] == 123
    assert clip["gain"] == 0.7
    assert clip["fade_in_samples"] == 0
    assert clip["fade_out_samples"] == 1000
    assert clip["track_id"] != original_clip["track_id"]
    assert (
        next(t for t in selected["document"]["tracks"] if t["id"] == clip["track_id"])["name"]
        == "Different frozen voice"
    )


def test_archive_validates_annotation_identity_and_shape(tmp_path):
    store, project, _ = setup_takes(tmp_path)
    assets = {a["id"]: a for a in project["assets"]}
    takes = {t["id"]: t for t in project["takes"]}
    for annotations in ({"foreign": {"name": "A", "note": "B"}}, {"long": {"name": 1, "note": "B"}}, []):
        with pytest.raises(ValueError, match="annotation"):
            _validate_document({**project["document"], "take_annotations": annotations}, assets, takes)
