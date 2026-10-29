"""Mutation snapshots remain replayable without repeated checkpoint documents."""

import json

from backend.studio.storage import StudioStore, encode


def test_mutation_receipt_stores_checkpoint_metadata_and_replays_original_full_response(tmp_path):
    store = StudioStore(tmp_path)
    p = store.create_project("Original", script="A named performance.")
    p = store.checkpoint(p["id"], "checkpoint-one", p["revision"], "One")
    request = dict(
        operation_id="rename", expected_revision=p["revision"], type="update_project", payload={"name": "Renamed"}
    )
    saved = store.command(p["id"], **request)
    assert saved["revisions"][0]["document"]["passages"][0]["text"] == "A named performance."
    with store.connect() as db:
        receipt = json.loads(db.execute("SELECT response FROM operations WHERE operation_id='rename'").fetchone()[0])
    compact = receipt.get("project", receipt)
    assert all("document" not in revision for revision in compact["revisions"]), (
        "journal repeated immutable checkpoint documents"
    )
    store.checkpoint(p["id"], "checkpoint-two", saved["revision"], "Two")
    assert store.command(p["id"], **request) == saved


def test_edit_trims_legacy_history_and_preserves_recent_undo_redo_and_checkpoints(tmp_path):
    store = StudioStore(tmp_path)
    p = store.create_project("Original", script="Still here")
    p = store.checkpoint(p["id"], "checkpoint", p["revision"], "Kept")
    legacy = [dict(document=p["document"], name=f"Old {i}", client_label=None) for i in range(150)]
    with store.connect() as db:
        db.execute("UPDATE projects SET undo=? WHERE id=?", (encode(legacy), p["id"]))
    edited = store.command(p["id"], "edit", 0, "update_project", {"name": "Latest"})
    with store.connect() as db:
        states = json.loads(db.execute("SELECT undo FROM projects WHERE id=?", (p["id"],)).fetchone()[0])
    assert len(states) <= 100, "undo snapshots grew without a retention limit"
    undone = store.command(p["id"], "undo", edited["revision"], "undo", {})
    assert undone["name"] == "Original"
    redone = store.command(p["id"], "redo", undone["revision"], "redo", {})
    assert redone["name"] == "Latest"
    assert redone["revisions"] == p["revisions"]


def test_edit_bounds_history_bytes_while_retaining_most_recent_snapshot(tmp_path):
    store = StudioStore(tmp_path)
    p = store.create_project("Original")
    legacy = [dict(document=p["document"], name=str(i) + "x" * (2 * 1024 * 1024), client_label=None) for i in range(6)]
    with store.connect() as db:
        db.execute("UPDATE projects SET undo=? WHERE id=?", (encode(legacy), p["id"]))
    edited = store.command(p["id"], "edit", 0, "update_project", {"name": "Latest"})
    with store.connect() as db:
        raw = db.execute("SELECT undo FROM projects WHERE id=?", (p["id"],)).fetchone()[0]
    assert len(raw.encode()) <= 8 * 1024 * 1024, "undo rewrite exceeds its byte budget"
    assert json.loads(raw)[-1]["name"] == "Original"
    assert store.command(p["id"], "undo", edited["revision"], "undo", {})["name"] == "Original"


def test_compact_projection_preserves_legacy_receipts_and_default_full_api(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.studio.api import make_router

    store = StudioStore(tmp_path)
    p = store.create_project("Original", script="Frozen")
    p = store.checkpoint(p["id"], "checkpoint", 0, "Named")
    request = dict(operation_id="rename", expected_revision=0, type="update_project", payload={"name": "Renamed"})
    full = store.command(p["id"], **request)
    # Simulate a pre-upgrade receipt, whose exact full response is retained.
    with store.connect() as db:
        db.execute("UPDATE operations SET response=? WHERE operation_id='rename'", (encode(full),))
    app = FastAPI()
    app.include_router(make_router(store))
    with TestClient(app) as client:
        url = f"/studio/v1/projects/{p['id']}/commands"
        assert client.post(url, json=request).json() == full
        compact = client.post(url, json=request, headers={"X-Studio-Projection": "compact"}).json()
        assert compact["revision"] == full["revision"]
        assert compact["document"] == full["document"]
        assert all("document" not in item for item in compact["revisions"])
        assert client.post(url, json=request).json() == full
    with store.connect() as db:
        assert db.execute("SELECT response FROM operations WHERE operation_id='rename'").fetchone()[0] == encode(full)


def test_new_compact_mutations_replay_across_projection_modes(tmp_path):
    store = StudioStore(tmp_path)
    p = store.create_project("Original", script="Frozen")
    p = store.checkpoint(p["id"], "checkpoint", 0, "Named", compact=True)
    assert "document" not in p["revisions"][0]
    request = dict(operation_id="edit", expected_revision=0, type="update_project", payload={"name": "Renamed"})
    compact = store.command(p["id"], **request, compact=True)
    assert "document" not in compact["revisions"][0]
    full = store.command(p["id"], **request)
    assert full["revisions"][0]["document"]["passages"][0]["text"] == "Frozen"
    assert store.command(p["id"], **request, compact=True) == compact
    approved = store.approve(p["id"], "approval", p["revisions"][0]["id"], compact=True)
    assert "document" not in approved["revisions"][0]
    assert (
        store.approve(p["id"], "approval", p["revisions"][0]["id"])["revisions"][0]["document"]
        == full["revisions"][0]["document"]
    )


def test_newest_oversized_snapshot_remains_undoable_and_replaces_older_history(tmp_path, monkeypatch):
    from backend.studio import storage

    monkeypatch.setattr(storage, "UNDO_MAX_BYTES", 500)
    store = StudioStore(tmp_path)
    p = store.create_project("Original", script="Long document " * 100)
    first = store.command(p["id"], "one", 0, "update_project", {"name": "First"})
    second = store.command(p["id"], "two", first["revision"], "update_project", {"name": "Second"})
    with store.connect() as db:
        states = json.loads(db.execute("SELECT undo FROM projects WHERE id=?", (p["id"],)).fetchone()[0])
    assert len(states) == 1
    assert states[0]["name"] == "First"
    undone = store.command(p["id"], "undo", second["revision"], "undo", {})
    assert undone["name"] == "First"
    assert store.command(p["id"], "redo", undone["revision"], "redo", {})["name"] == "Second"


def test_review_approval_receipt_omits_checkpoint_documents_and_preserves_full_replay(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.studio.reviews import make_review_router

    store = StudioStore(tmp_path)
    p = store.create_project("Review", script="Frozen words")
    p = store.checkpoint(p["id"], "checkpoint", 0, "Named")
    revision = p["revisions"][0]["id"]
    app = FastAPI()
    app.include_router(make_review_router(store), prefix="/studio/v1")
    path = f"/studio/v1/projects/{p['id']}/reviews/{revision}"
    with TestClient(app) as client:
        assert (
            client.post(
                path, json=dict(operation_id="review", expected_version=0, note="Keep", checklist=[])
            ).status_code
            == 200
        )
        body = dict(operation_id="approval", expected_version=1)
        full = client.post(path + "/approvals", json=body).json()
        assert full["project"]["revisions"][0]["document"] == p["revisions"][0]["document"]
        with store.connect() as db:
            raw = json.loads(
                db.execute("SELECT response FROM review_operations WHERE operation_id='approval'").fetchone()[0]
            )
        snapshot = raw["project"].get("project", raw["project"])
        assert all("document" not in checkpoint for checkpoint in snapshot["revisions"]), (
            "review journal duplicated immutable checkpoint documents"
        )
        store.checkpoint(p["id"], "later", 0, "Later")
        assert client.post(path + "/approvals", json=body).json() == full
        compact = client.post(path + "/approvals", json=body, headers={"X-Studio-Projection": "compact"}).json()
        assert compact["review"] == full["review"]
        assert "document" not in compact["project"]["revisions"][0]
        assert len(compact["project"]["revisions"]) == 1
        # Existing full review receipts remain stored and replayed unchanged.
        with store.connect() as db:
            db.execute("UPDATE review_operations SET response=? WHERE operation_id='approval'", (encode(full),))
        assert client.post(path + "/approvals", json=body).json() == full
        assert (
            "document"
            not in client.post(path + "/approvals", json=body, headers={"X-Studio-Projection": "compact"}).json()[
                "project"
            ]["revisions"][0]
        )
        with store.connect() as db:
            assert db.execute("SELECT response FROM review_operations WHERE operation_id='approval'").fetchone()[
                0
            ] == encode(full)


def test_missing_checkpoint_fails_closed_for_compact_receipt_full_replay(tmp_path):
    import pytest

    from backend.studio.storage import NotFound

    store = StudioStore(tmp_path)
    p = store.create_project("Original", script="Frozen")
    p = store.checkpoint(p["id"], "checkpoint", 0, "Named", compact=True)
    request = dict(operation_id="edit", expected_revision=0, type="update_project", payload={"name": "Renamed"})
    compact = store.command(p["id"], **request, compact=True)
    # There is no supported checkpoint-deletion operation. If storage is
    # damaged externally, never substitute the current document into replay.
    with store.connect() as db:
        db.execute("DELETE FROM revisions WHERE project_id=?", (p["id"],))
    assert store.command(p["id"], **request, compact=True) == compact
    with pytest.raises(NotFound, match="checkpoint"):
        store.command(p["id"], **request)
