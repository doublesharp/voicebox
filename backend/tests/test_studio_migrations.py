"""Upgrade and publication recovery operate on real SQLite and owned files."""

import sqlite3
import uuid

import numpy as np
import pytest
import soundfile as sf

from backend.studio.storage import StudioStore


def test_existing_unversioned_database_is_backed_up_and_upgraded(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Existing", script="Keep these words")
    with store.connect() as db:
        db.execute("PRAGMA user_version=0")
    reopened = StudioStore(tmp_path)
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] > 0
    assert reopened.get_project(project["id"])["document"] == project["document"]
    backups = list((tmp_path / "backups").glob("studio-v0-*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM projects").fetchone()[0] == "Existing"


def test_newer_schema_is_rejected_without_changing_database(tmp_path):
    store = StudioStore(tmp_path)
    store.create_project("Future")
    with store.connect() as db:
        db.execute("PRAGMA user_version=999")
    before = store.db_path.read_bytes()
    with pytest.raises(ValueError, match="newer"):
        StudioStore(tmp_path)
    assert store.db_path.read_bytes() == before


def test_startup_quarantines_unpublished_audio_but_preserves_registered_assets(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Recovery")
    source = tmp_path / "source.wav"
    sf.write(source, np.full(100, 0.1), 24000)
    asset = store.add_asset(project["id"], source)
    orphan = store.asset_dir / str(uuid.uuid4())
    orphan.write_bytes(source.read_bytes())
    reopened = StudioStore(tmp_path)
    assert reopened.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert not orphan.exists()
    recovered = list((tmp_path / "recovery").rglob(orphan.name))
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == source.read_bytes()


def test_backup_failure_leaves_unversioned_database_unchanged(tmp_path, monkeypatch):
    from backend.studio import migrations

    store = StudioStore(tmp_path)
    store.create_project("Keep")
    with store.connect() as db:
        db.execute("PRAGMA user_version=0")
    before = store.db_path.read_bytes()

    def fail_backup(*args):
        raise OSError("backup unavailable")

    monkeypatch.setattr(migrations, "_backup", fail_backup)
    with pytest.raises(OSError, match="backup unavailable"):
        StudioStore(tmp_path)
    assert store.db_path.read_bytes() == before


def test_failed_migration_rolls_back_all_structural_changes(tmp_path, monkeypatch):
    from backend.studio import migrations

    store = StudioStore(tmp_path)
    project = store.create_project("Keep")
    with store.connect() as db:
        db.execute("PRAGMA user_version=0")
    monkeypatch.setitem(
        migrations.MIGRATIONS,
        1,
        (
            "CREATE TABLE migration_probe (id INTEGER)",
            "INSERT INTO missing_table VALUES (1)",
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        StudioStore(tmp_path)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='migration_probe'").fetchone()
        assert db.execute("SELECT name FROM projects WHERE id=?", (project["id"],)).fetchone()[0] == "Keep"
    assert len(list((tmp_path / "backups").glob("studio-v0-*.sqlite"))) == 1


def test_registration_failure_preserves_source_and_existing_audio(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Audio")
    source = tmp_path / "source.wav"
    sf.write(source, np.full(100, 0.1), 24000)
    original = source.read_bytes()
    asset = store.add_asset(project["id"], source)
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_asset BEFORE INSERT ON assets BEGIN SELECT RAISE(ABORT, 'no registration'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="no registration"):
        store.add_asset(project["id"], source)
    assert source.read_bytes() == original
    assert store.asset_path(asset["id"]).read_bytes() == original
    assert [p.name for p in store.asset_dir.iterdir()] == [asset["id"]]
    assert len(store.get_project(project["id"])["assets"]) == 1


def test_recovery_waits_for_active_publication(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store = StudioStore(tmp_path)
    project = store.create_project("Concurrent")
    source = tmp_path / "source.wav"
    sf.write(source, np.full(100, 0.1), 24000)
    started, release, reopening = threading.Event(), threading.Event(), threading.Event()
    real_info = sf.info

    def paused_info(path):
        started.set()
        assert release.wait(10)
        return real_info(path)

    monkeypatch.setattr(sf, "info", paused_info)

    def reopen():
        reopening.set()
        return StudioStore(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        publisher = pool.submit(store.add_asset, project["id"], source)
        assert started.wait(10)
        other = pool.submit(reopen)
        assert reopening.wait(10)
        try:
            assert not other.done()
        finally:
            release.set()
        asset = publisher.result(timeout=10)
        reopened = other.result(timeout=10)
    assert reopened.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert not (tmp_path / "recovery").exists()


def test_upgrade_preserves_history_approvals_intents_and_asset_records(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("History", script="A passage")
    source = tmp_path / "source.wav"
    sf.write(source, np.full(100, 0.1), 24000)
    asset = store.add_asset(project["id"], source)
    with store.connect() as db:
        db.execute("UPDATE projects SET undo='[1]', redo='[2]' WHERE id=?", (project["id"],))
        db.execute("INSERT INTO operations VALUES (?,?,?,?)", (project["id"], "op", "{}", "{}"))
        db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?)", ("rev", project["id"], "checkpoint", "time", "{}", 3))
        db.execute("INSERT INTO approvals VALUES (?,?,?,?,?)", ("approval", project["id"], "rev", "note", "time"))
        db.execute("INSERT INTO passage_intents VALUES (?,?,?)", (project["id"], "passage", 17))
        db.execute("PRAGMA user_version=0")
        tables = ("projects", "operations", "revisions", "approvals", "passage_intents", "assets")
        before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}
    reopened = StudioStore(tmp_path)
    with reopened.connect() as db:
        assert {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables} == before
    with sqlite3.connect(next((tmp_path / "backups").glob("studio-v0-*.sqlite"))) as db:
        assert {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before
    assert reopened.asset_path(asset["id"]).read_bytes() == source.read_bytes()


def test_recovery_quarantines_staging_without_touching_other_files(tmp_path):
    store = StudioStore(tmp_path)
    staged = store.asset_dir / (".publish-" + str(uuid.uuid4()))
    staged.write_bytes(b"partial")
    other = store.asset_dir / ".generation-in-progress"
    other.write_bytes(b"active")
    StudioStore(tmp_path)
    assert not staged.exists()
    assert next((tmp_path / "recovery").rglob(staged.name)).read_bytes() == b"partial"
    assert other.read_bytes() == b"active"


def test_failed_rename_rolls_back_staged_copy(tmp_path, monkeypatch):
    from pathlib import Path

    store = StudioStore(tmp_path)
    project = store.create_project("Rename failure")
    source = tmp_path / "source.wav"
    sf.write(source, np.full(100, 0.1), 24000)
    contents = source.read_bytes()
    real_rename = Path.rename

    def fail_publish(path, target):
        if path.name.startswith(".publish-"):
            raise OSError("rename failed")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError, match="rename failed"):
        store.add_asset(project["id"], source)
    assert source.read_bytes() == contents
    assert list(store.asset_dir.iterdir()) == []
    assert store.get_project(project["id"])["assets"] == []


def test_service_request_tables_are_migrated_and_preserve_old_identities(tmp_path):
    from backend.studio.migrations import CURRENT_VERSION

    store = StudioStore(tmp_path)
    with store.connect() as db:
        for table in ("job_requests", "render_requests"):
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
            db.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", ("project", "retry-key", "{}", "[]"))
        db.execute("PRAGMA user_version=1")
    reopened = StudioStore(tmp_path)
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        for table in ("job_requests", "render_requests"):
            assert tuple(db.execute(f"SELECT * FROM {table}").fetchone()) == (
                "project",
                "retry-key",
                "{}",
                "[]",
            )
    with sqlite3.connect(next((tmp_path / "backups").glob("studio-v1-*.sqlite"))) as snapshot:
        assert snapshot.execute("PRAGMA user_version").fetchone()[0] == 1
        assert snapshot.execute("SELECT key FROM job_requests").fetchone()[0] == "retry-key"
