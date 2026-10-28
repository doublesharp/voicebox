"""Numbered, transactional Studio schema upgrades and verified snapshots."""

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

CURRENT_VERSION = 3

# Version 1 adopts the previously unversioned schema without replacing existing tables.
MIGRATIONS = {
    1: (
        "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, client_label TEXT, revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, document TEXT NOT NULL, undo TEXT NOT NULL, redo TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS operations (project_id TEXT NOT NULL, operation_id TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(project_id, operation_id));",
        "CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, document TEXT NOT NULL, project_revision INTEGER NOT NULL);",
        "CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision_id TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, hash TEXT NOT NULL, sample_rate INTEGER NOT NULL, channels INTEGER NOT NULL, frames INTEGER NOT NULL, duration REAL NOT NULL, size_bytes INTEGER NOT NULL, path TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS takes (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, passage_id TEXT NOT NULL, asset_id TEXT NOT NULL, text TEXT NOT NULL, engine TEXT NOT NULL, model_size TEXT, created_at TEXT NOT NULL, request TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, passage_id TEXT, error TEXT, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, request TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS deliverables (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, data TEXT NOT NULL, path TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS passage_intents (project_id TEXT NOT NULL, passage_id TEXT NOT NULL, epoch INTEGER NOT NULL, PRIMARY KEY(project_id,passage_id));",
        "CREATE TABLE IF NOT EXISTS recording_acknowledgements (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision_id TEXT NOT NULL, created_at TEXT NOT NULL, recordings TEXT NOT NULL);",
    ),
    2: (
        "CREATE TABLE IF NOT EXISTS job_requests (project_id TEXT, key TEXT, request TEXT, response TEXT, PRIMARY KEY(project_id,key));",
        "CREATE TABLE IF NOT EXISTS render_requests (project_id TEXT, key TEXT, request TEXT, response TEXT, PRIMARY KEY(project_id,key));",
    ),
    3: (
        "CREATE TABLE IF NOT EXISTS job_attempts (job_id TEXT NOT NULL, attempt INTEGER NOT NULL, phase TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, PRIMARY KEY(job_id, attempt));",
        "CREATE TABLE IF NOT EXISTS job_phase_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, phase TEXT NOT NULL, created_at TEXT NOT NULL);",
    ),
}


def migrate(db_path):
    db_path = Path(db_path)
    existed = db_path.exists()
    if existed:
        # Reject before acquiring a write transaction or creating managed directories.
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as read_db:
            version = read_db.execute("PRAGMA user_version").fetchone()[0]
        _check_version(version)
    db = sqlite3.connect(db_path, timeout=30)
    try:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        _check_version(version)
        if version < CURRENT_VERSION:
            if existed:
                _backup(db_path, version)
            for number in range(version + 1, CURRENT_VERSION + 1):
                for statement in MIGRATIONS[number]:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={number}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _check_version(version):
    if version > CURRENT_VERSION:
        raise ValueError(
            f"Studio database schema {version} is newer than supported version {CURRENT_VERSION}. "
            "Open it with a newer Voicebox version, or restore a compatible backup after saving a copy of this database."
        )


def _backup(db_path, version):
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    destination = backup_dir / f"studio-v{version}-{uuid.uuid4()}.sqlite"
    try:
        # A separate reader sees the snapshot protected by the caller's write lock.
        with (
            closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(destination)) as snapshot,
        ):
            source.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Studio backup integrity check failed")
            if snapshot.execute("PRAGMA user_version").fetchone()[0] != version:
                raise sqlite3.DatabaseError("Studio backup version mismatch")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
