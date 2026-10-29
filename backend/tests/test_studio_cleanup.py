"""Recoverable cleanup uses real PCM, filesystem readback, and migrated SQLite."""

import copy
import sqlite3

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.cleanup import StudioCleanup, make_cleanup_router
from backend.studio.storage import Conflict, NotFound, StudioStore, encode, now, uid


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "original.wav"
    sf.write(source, np.full(4800, 0.1), 48000)
    store = StudioStore(tmp_path / "studio")
    project = store.create_project("Cleanup")
    asset = store.add_asset(project["id"], source)
    service = StudioCleanup(store)
    return store, service, project, asset, source


def request(service, project, asset, operation="cleanup"):
    preview = service.preview(project["id"])
    return service.cleanup(project["id"], operation, preview["preview_token"], [asset["id"]])


def test_preview_keeps_draft_checkpoint_history_takes_jobs_and_unknown_references(prepared):
    store, service, project, _, source = prepared
    assets = [store.add_asset(project["id"], source) for _ in range(8)]
    doc = copy.deepcopy(project["document"])
    protected = {asset["id"] for asset in assets[:7]}
    with store.connect() as db:
        doc["future_reference"] = assets[0]["id"]
        undo = [{"document": {"schema_version": 1, "asset_id": assets[1]["id"]}}]
        redo = [{"document": {"schema_version": 1, "reference_assets": [assets[2]["id"]]}}]
        db.execute(
            "UPDATE projects SET document=?,undo=?,redo=? WHERE id=?",
            (encode(doc), encode(undo), encode(redo), project["id"]),
        )
        revision = copy.deepcopy(doc)
        revision["extra"] = assets[3]["id"]
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?)",
            (uid(), project["id"], "Protected", now(), encode(revision), 0),
        )
        db.execute(
            "INSERT INTO takes VALUES (?,?,?,?,?,?,?,?,?)",
            (uid(), project["id"], "passage", assets[4]["id"], "Words", "fixture", None, now(), "{}"),
        )
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                uid(),
                project["id"],
                "render",
                "running",
                None,
                None,
                None,
                now(),
                now(),
                encode({"document": {"asset_id": assets[5]["id"]}}),
            ),
        )
        db.execute(
            "INSERT INTO operations VALUES (?,?,?,?)",
            (project["id"], "old", "{}", encode({"future": {assets[6]["id"]: "metadata"}})),
        )
    preview = service.preview(project["id"])
    assert not protected & {item["id"] for item in preview["candidates"]}
    assert assets[7]["id"] in {item["id"] for item in preview["candidates"]}
    assert preview["protected_count"] == 7
    with store.connect() as db:
        db.execute(
            "UPDATE projects SET undo=? WHERE id=?", (encode([{"document": {"schema_version": 999}}]), project["id"])
        )
    assert service.preview(project["id"])["candidates"] == []


def test_cleanup_restore_and_exact_operation_replay(prepared):
    store, service, project, asset, source = prepared
    preview = service.preview(project["id"])
    body = dict(operation_id="cleanup", preview_token=preview["preview_token"], asset_ids=[asset["id"]])
    result = service.cleanup(project["id"], **body)
    assert service.cleanup(project["id"], **body) == result
    assert store.get_project(project["id"])["assets"] == []
    assert not (store.asset_dir / asset["id"]).exists()
    receipt = result["receipts"][0]
    assert (service.trash_dir / receipt["id"]).read_bytes() == source.read_bytes()
    restored = service.restore(project["id"], "restore", [receipt["id"]])
    assert service.restore(project["id"], "restore", [receipt["id"]]) == restored
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert service.preview(project["id"])["trash"] == []
    with pytest.raises(Conflict):
        service.cleanup(project["id"], "cleanup", "changed", [asset["id"]])


def test_new_reference_invalidates_preview_before_any_copy(prepared):
    store, service, project, asset, source = prepared
    preview = service.preview(project["id"])
    store.command(project["id"], "place", 0, "insert_asset", {"asset_id": asset["id"], "kind": "music"})
    with pytest.raises(Conflict, match="references changed"):
        service.cleanup(project["id"], "cleanup", preview["preview_token"], [asset["id"]])
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert not (store.root / "cleanup-trash").exists()


def test_copy_or_registration_failure_preserves_registered_original(prepared, monkeypatch):
    import backend.studio.cleanup as cleanup

    store, service, project, asset, source = prepared
    original = cleanup._copy_verified

    def fail(*_args):
        raise OSError("destination full")

    monkeypatch.setattr(cleanup, "_copy_verified", fail)
    with pytest.raises(OSError, match="destination full"):
        request(service, project, asset)
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    monkeypatch.setattr(cleanup, "_copy_verified", original)
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER deny_cleanup BEFORE INSERT ON cleanup_trash BEGIN SELECT RAISE(ABORT, 'registration failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="registration failed"):
        request(service, project, asset)
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert service.preview(project["id"])["trash"] == []


def lose_commit(store, monkeypatch, predicate):
    connect = store.connect
    lost = []

    class Uncertain:
        def __init__(self):
            self.db = connect()

        def __enter__(self):
            return self.db.__enter__()

        def __exit__(self, kind, error, tb):
            uncertain = kind is None and self.db.total_changes > 0 and not lost and predicate(self.db)
            result = self.db.__exit__(kind, error, tb)
            if uncertain:
                lost.append(True)
                raise OSError("commit acknowledgement lost")
            return result

    monkeypatch.setattr(store, "connect", Uncertain)
    return connect


def test_uncertain_cleanup_keeps_original_and_replays_then_restores_without_overwrite(prepared, monkeypatch):
    store, service, project, asset, source = prepared
    preview = service.preview(project["id"])
    body = dict(operation_id="cleanup", preview_token=preview["preview_token"], asset_ids=[asset["id"]])
    connect = lose_commit(
        store, monkeypatch, lambda db: db.execute("SELECT COUNT(*) FROM cleanup_trash").fetchone()[0] == 1
    )
    with pytest.raises(OSError, match="acknowledgement lost"):
        service.cleanup(project["id"], **body)
    monkeypatch.setattr(store, "connect", connect)
    assert (store.asset_dir / asset["id"]).read_bytes() == source.read_bytes()
    result = service.cleanup(project["id"], **body)
    assert store.get_project(project["id"])["assets"] == []
    service.restore(project["id"], "restore", [result["receipts"][0]["id"]])
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()


def test_uncertain_restore_keeps_registered_audio_and_exact_receipt(prepared, monkeypatch):
    store, service, project, asset, source = prepared
    receipt = request(service, project, asset)["receipts"][0]
    connect = lose_commit(store, monkeypatch, lambda db: db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1)
    with pytest.raises(OSError, match="acknowledgement lost"):
        service.restore(project["id"], "restore", [receipt["id"]])
    monkeypatch.setattr(store, "connect", connect)
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    replay = service.restore(project["id"], "restore", [receipt["id"]])
    assert replay["receipts"][0]["asset_id"] == asset["id"]


def test_restore_corrupt_trash_and_cross_project_receipt_are_rejected(prepared):
    store, service, project, asset, source = prepared
    receipt = request(service, project, asset)["receipts"][0]
    other = store.create_project("Other")
    with pytest.raises(NotFound):
        service.restore(other["id"], "wrong", [receipt["id"]])
    (service.trash_dir / receipt["id"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        service.restore(project["id"], "restore", [receipt["id"]])
    assert store.get_project(project["id"])["assets"] == []
    assert source.is_file()


def test_symlink_source_cannot_be_cleaned(prepared):
    store, service, project, asset, source = prepared
    path = store.asset_dir / asset["id"]
    path.unlink()
    path.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        request(service, project, asset)
    assert source.is_file()
    assert len(store.get_project(project["id"])["assets"]) == 1


def test_http_factory_is_lazy_strict_and_replayable(prepared):
    store, service, project, asset, _ = prepared
    called = []
    app = FastAPI()
    app.include_router(make_cleanup_router(lambda: called.append(True) or store), prefix="/studio/v1")
    assert called == []
    client = TestClient(app)
    path = f"/studio/v1/projects/{project['id']}/storage"
    preview = client.get(path).json()
    body = dict(operation_id="cleanup", preview_token=preview["preview_token"], asset_ids=[asset["id"]])
    assert client.post(path + "/cleanup", json={**body, "asset_ids": [True]}).status_code == 422
    first = client.post(path + "/cleanup", json=body)
    assert first.status_code == 200
    assert client.post(path + "/cleanup", json=body).json() == first.json()
    receipt = first.json()["receipts"][0]["id"]
    assert client.post(path + "/restore", json={"operation_id": "restore", "receipt_ids": [receipt]}).status_code == 200


def test_migration_five_uses_verified_v4_backup(prepared):
    from backend.studio import migrations

    store, _, project, _, _ = prepared
    with store.connect() as db:
        db.execute("DROP TABLE cleanup_trash")
        db.execute("PRAGMA user_version=4")
    StudioStore(store.root)
    assert list((store.root / "backups").glob("studio-v4-*.sqlite"))
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION
        assert db.execute("SELECT name FROM projects WHERE id=?", (project["id"],)).fetchone()[0] == "Cleanup"


def test_uncertain_cleanup_reopens_cleanly_and_restores_from_verified_trash(prepared, monkeypatch):
    store, service, project, asset, source = prepared
    preview = service.preview(project["id"])
    connect = lose_commit(
        store, monkeypatch, lambda db: db.execute("SELECT COUNT(*) FROM cleanup_trash").fetchone()[0] == 1
    )
    with pytest.raises(OSError, match="acknowledgement lost"):
        service.cleanup(project["id"], "cleanup", preview["preview_token"], [asset["id"]])
    monkeypatch.setattr(store, "connect", connect)
    reopened = StudioStore(store.root)
    service = StudioCleanup(reopened)
    assert not (reopened.asset_dir / asset["id"]).exists()
    assert list((store.root / "recovery").glob("*/*"))
    receipt = service.preview(project["id"])["trash"][0]
    service.restore(project["id"], "restore", [receipt["id"]])
    assert reopened.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert not (service.trash_dir / receipt["id"]).exists()


def test_copy_verification_rejects_changed_audio_and_removes_only_partial_trash(prepared):
    from backend.studio.cleanup import _copy_verified

    store, service, _, asset, source = prepared
    target = service.trash_dir / uid()
    with pytest.raises(ValueError, match="integrity"):
        _copy_verified(source, target, {**asset, "hash": "0" * 64})
    assert not target.exists()
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()


def test_unknown_reference_shape_and_foreign_project_references_protect_audio(prepared):
    store, service, project, asset, _ = prepared
    other = store.create_project("Foreign")
    doc = copy.deepcopy(other["document"])
    doc["future"] = asset["id"]
    with store.connect() as db:
        db.execute("UPDATE projects SET document=? WHERE id=?", (encode(doc), other["id"]))
    assert service.preview(project["id"])["candidates"] == []
    doc["future"] = None
    doc["future_asset_ids"] = "unknown shape"
    with store.connect() as db:
        db.execute("UPDATE projects SET document=? WHERE id=?", (encode(doc), other["id"]))
    assert service.preview(project["id"])["candidates"] == []


def test_migration_five_failure_rolls_back_and_keeps_v4_snapshot(prepared, monkeypatch):
    from backend.studio import migrations

    store, _, project, _, _ = prepared
    with store.connect() as db:
        db.execute("DROP TABLE cleanup_trash")
        db.execute("PRAGMA user_version=4")
    monkeypatch.setitem(migrations.MIGRATIONS, 5, (*migrations.MIGRATIONS[5], "INSERT INTO missing VALUES (1)"))
    with pytest.raises(sqlite3.OperationalError):
        StudioStore(store.root)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='cleanup_trash'").fetchall()
    backup = next((store.root / "backups").glob("studio-v4-*.sqlite"))
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT name FROM projects WHERE id=?", (project["id"],)).fetchone()[0] == "Cleanup"


def test_sync_failure_keeps_registered_original_and_cleans_only_incomplete_copy(prepared, monkeypatch):
    import backend.studio.cleanup as cleanup

    store, service, project, asset, source = prepared
    # Create the owned directory before injecting a file/directory sync error.
    assert service.trash_dir.is_dir()

    def failed_sync(_descriptor):
        raise OSError("simulated sync failure")

    monkeypatch.setattr(cleanup.os, "fsync", failed_sync)
    with pytest.raises(OSError, match="sync failure"):
        request(service, project, asset)
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert not list(service.trash_dir.iterdir())


def test_restore_registration_failure_keeps_recoverable_trash(prepared):
    store, service, project, asset, source = prepared
    receipt = request(service, project, asset)["receipts"][0]
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER deny_restore BEFORE INSERT ON assets BEGIN SELECT RAISE(ABORT, 'restore registration failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="restore registration failed"):
        service.restore(project["id"], "restore", [receipt["id"]])
    assert (service.trash_dir / receipt["id"]).read_bytes() == source.read_bytes()
    assert not (store.asset_dir / asset["id"]).exists()
    assert len(service.preview(project["id"])["trash"]) == 1
