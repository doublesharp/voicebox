"""Checkpoint reviews use the real migrated SQLite store and HTTP boundary."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.storage import StudioStore


def setup(tmp_path):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Review", script="Frozen text")
    project = store.checkpoint(project["id"], "checkpoint", 0, "First")
    revision = project["revisions"][0]["id"]
    app = FastAPI()
    # The root-owned registration is intentionally exercised here through its public factory.
    from backend.studio import reviews

    app.include_router(reviews.make_review_router(lambda: store), prefix="/studio/v1")
    return store, project, revision, TestClient(app)


def test_review_endpoint_is_persistent_and_independent_of_draft(tmp_path):
    store, project, revision, client = setup(tmp_path)
    path = f"/studio/v1/projects/{project['id']}/reviews/{revision}"
    original = store.get_project(project["id"])["revisions"]
    body = dict(
        operation_id="save",
        expected_version=0,
        note="Keep " + revision,
        checklist=[dict(id="pacing", label="Pacing checked", checked=False)],
    )
    result = client.post(path, json=body)
    assert result.status_code == 200
    saved = result.json()["review"]
    assert saved["version"] == 1
    assert saved["note"] == body["note"]
    assert client.post(path, json=body).json()["review"] == saved
    assert client.post(path, json={**body, "note": "changed"}).status_code == 409
    assert client.get(path).json()["review"] == saved
    assert store.get_project(project["id"])["revisions"] == original
    from backend.studio.reviews import StudioReviews

    assert StudioReviews(StudioStore(store.root)).get(project["id"], revision) == saved


def test_concurrent_review_cas_and_atomic_approval_snapshot(tmp_path):
    store, project, revision, client = setup(tmp_path)
    from backend.studio.reviews import StudioReviews
    from backend.studio.storage import Conflict

    reviews = StudioReviews(store)

    def write(n):
        try:
            return reviews.save(project["id"], revision, f"save-{n}", 0, f"Note {n}", [])
        except Conflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, [1, 2]))
    assert sum(r is not None for r in results) == 1
    frozen = reviews.get(project["id"], revision)
    approved = reviews.approve(project["id"], revision, "approve", 1)
    assert frozen["note"] in approved["project"]["approvals"][0]["note"]
    reviews.save(project["id"], revision, "later", 1, "Later", [])
    assert reviews.approve(project["id"], revision, "approve", 1) == approved
    with pytest.raises(Conflict):
        reviews.approve(project["id"], revision, "stale-approval", 1)
    assert "Later" not in store.get_project(project["id"])["approvals"][0]["note"]


def test_review_invalid_types_and_foreign_revision_rejected(tmp_path):
    store, project, revision, client = setup(tmp_path)
    foreign = store.create_project("Other")
    path = f"/studio/v1/projects/{foreign['id']}/reviews/{revision}"
    body = dict(operation_id="x", expected_version=0, note="x", checklist=[])
    assert client.post(path, json=body).status_code == 404
    path = f"/studio/v1/projects/{project['id']}/reviews/{revision}"
    for change in [
        dict(note=42),
        dict(expected_version=True),
        dict(checklist=[dict(id="a", label="A", checked="yes")]),
    ]:
        assert client.post(path, json={**body, **change}).status_code == 422
    assert client.post(path + "/approvals", json=dict(operation_id="a", expected_version=0)).status_code == 409


def test_migration_four_backup_and_failure_rollback(tmp_path, monkeypatch):
    from backend.studio import migrations

    store = StudioStore(tmp_path / "old")
    store.create_project("Keep")
    with store.connect() as db:
        db.execute("DROP TABLE checkpoint_reviews")
        db.execute("DROP TABLE review_operations")
        db.execute("PRAGMA user_version=3")
    monkeypatch.setitem(migrations.MIGRATIONS, 4, (*migrations.MIGRATIONS[4], "INSERT INTO absent VALUES (1)"))
    with pytest.raises(sqlite3.OperationalError):
        StudioStore(store.root)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='checkpoint_reviews'").fetchall()
    backups = list((store.root / "backups").glob("studio-v3-*.sqlite"))
    assert backups
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT name FROM projects").fetchone()[0] == "Keep"


def test_complete_archive_roundtrip_preserves_review_text_and_frozen_approvals(tmp_path):
    from backend.studio.archive import write_archive
    from backend.studio.portability import import_archive
    from backend.studio.review_schema import validate_reviews
    from backend.studio.reviews import StudioReviews

    source = StudioStore(tmp_path / "source")
    project = source.create_project("Portable review", script="Immutable words")
    import wave

    audio = tmp_path / "owned.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\x01\x00" * 100)
    asset = source.add_asset(project["id"], audio)
    project = source.command(project["id"], "place", 0, "insert_asset", {"asset_id": asset["id"], "kind": "music"})
    project = source.checkpoint(project["id"], "checkpoint", project["revision"], "Reviewed")
    revision = project["revisions"][0]["id"]
    reviews = StudioReviews(source)
    note = "Literal identity " + revision + " must stay in text"
    reviews.save(project["id"], revision, "save", 0, note, [dict(id="timing", label=revision, checked=False)])
    reviews.approve(project["id"], revision, "approve", 1)
    project = source.get_project(project["id"])
    project["reviews"] = reviews.export(project["id"])
    validate_reviews(project)
    archive = tmp_path / "complete.zip"
    write_archive(project, project["assets"], source.asset_path, archive)
    destination = StudioStore(tmp_path / "destination")
    imported = import_archive(destination, archive)
    new_revision = imported["revisions"][0]["id"]
    assert new_revision != revision
    assert imported["assets"][0]["hash"] == asset["hash"]
    assert destination.asset_path(imported["assets"][0]["id"]).read_bytes() == audio.read_bytes()
    record = StudioReviews(destination).get(imported["id"], new_revision)
    assert record["note"] == note
    assert record["checklist"][0]["label"] == revision
    assert record["version"] == 1
    assert imported["approvals"][0]["note"] == project["approvals"][0]["note"]
    assert imported["approvals"][0]["revision_id"] == new_revision


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(revision_id="foreign"),
        lambda r: r.update(version=True),
        lambda r: r.update(note=12),
        lambda r: r.update(checklist=[dict(id="x", label="X", checked=1)]),
        lambda r: r.update(checklist=[dict(id="x", label="X", checked=True)] * 2),
    ],
)
def test_malformed_archive_reviews_abort_before_project_publication(tmp_path, mutation):
    import json
    import zipfile

    from backend.studio.archive import write_archive
    from backend.studio.portability import import_archive

    source = StudioStore(tmp_path / "source")
    p = source.create_project("Bad review")
    p = source.checkpoint(p["id"], "c", 0, "Checkpoint")
    record = dict(revision_id=p["revisions"][0]["id"], version=1, note="Text", checklist=[], updated_at="now")
    p["reviews"] = [record]
    archive = tmp_path / "valid.zip"
    write_archive(p, [], lambda _: None, archive)
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("manifest.json"))
    mutation(manifest["project"]["reviews"][0])
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
    target = StudioStore(tmp_path / "target")
    keep = target.create_project("Keep")
    with pytest.raises(ValueError, match="[Rr]eview"):
        import_archive(target, bad)
    assert [p["id"] for p in target.list_projects()] == [keep["id"]]
    with target.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM checkpoint_reviews").fetchone()[0] == 0
    assert not list(target.root.glob(".import-*"))


def test_review_restore_database_failure_rolls_back_entire_import(tmp_path, monkeypatch):
    from backend.studio import portability
    from backend.studio.archive import write_archive

    source = StudioStore(tmp_path / "source")
    p = source.create_project("Atomic")
    p = source.checkpoint(p["id"], "c", 0, "Checkpoint")
    p["reviews"] = [dict(revision_id=p["revisions"][0]["id"], version=1, note="Text", checklist=[], updated_at="now")]
    archive = tmp_path / "source.zip"
    write_archive(p, [], lambda _: None, archive)
    target = StudioStore(tmp_path / "target")
    original = portability.restore_reviews

    def fail(db, project_id, records):
        original(db, project_id, records)
        raise sqlite3.OperationalError("after review registration")

    monkeypatch.setattr(portability, "restore_reviews", fail)
    with pytest.raises(sqlite3.OperationalError, match="review registration"):
        portability.import_archive(target, archive)
    assert target.list_projects() == []
    with target.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM checkpoint_reviews").fetchone()[0] == 0


def test_failed_request_receipt_rolls_back_review_and_approval_together(tmp_path):
    store, project, revision, _ = setup(tmp_path)
    from backend.studio.reviews import StudioReviews

    reviews = StudioReviews(store)
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON review_operations BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="receipt unavailable"):
        reviews.save(project["id"], revision, "save", 0, "Retry me", [])
    assert reviews.get(project["id"], revision)["version"] == 0
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_receipt")
    reviews.save(project["id"], revision, "save", 0, "Retry me", [])
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON review_operations BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="receipt unavailable"):
        reviews.approve(project["id"], revision, "approve", 1)
    assert store.get_project(project["id"])["approvals"] == []
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_receipt")
    assert len(reviews.approve(project["id"], revision, "approve", 1)["project"]["approvals"]) == 1


def test_v3_upgrade_preserves_old_operation_replay_and_verified_snapshot(tmp_path):
    store = StudioStore(tmp_path / "v3")
    project = store.create_project("Keep replay")
    committed = store.command(project["id"], "old-operation", 0, "update_project", {"name": "Renamed"})
    with store.connect() as db:
        db.execute("DROP TABLE checkpoint_reviews")
        db.execute("DROP TABLE review_operations")
        db.execute("PRAGMA user_version=3")
    upgraded = StudioStore(store.root)
    assert upgraded.command(project["id"], "old-operation", 0, "update_project", {"name": "Renamed"}) == committed
    with upgraded.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    backup = next((store.root / "backups").glob("studio-v3-*.sqlite"))
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT operation_id FROM operations").fetchone()[0] == "old-operation"
