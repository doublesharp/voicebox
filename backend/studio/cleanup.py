"""Explicit, recoverable cleanup of unreferenced Studio-owned source audio."""

import hashlib
import json
import os
import stat
import uuid

import soundfile as sf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .publication import publish_audio
from .storage import Conflict, NotFound, encode, now, uid

POLICY = "Copies remain in recoverable trash until restored. No automatic or permanent purge."
MAX_BATCH_BYTES = 1024 * 1024 * 1024


class StalePreviewError(Conflict):
    pass


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("Invalid managed audio identity")
    try:
        valid = str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid managed audio identity")
    return value


def _owned_path(directory, name):
    _uuid(name)
    if directory.is_symlink():
        raise ValueError("Managed audio directory cannot be a symlink")
    path = directory / name
    if path.is_symlink():
        raise ValueError("Managed audio cannot be a symlink")
    return path


def _digest(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Managed audio must be a regular file")
        digest = hashlib.sha256()
        size = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _verify(path, metadata):
    if (
        path.is_symlink()
        or path.stat().st_size != metadata["size_bytes"]
        or _digest(path) != (metadata["hash"], metadata["size_bytes"])
    ):
        raise ValueError("Managed audio integrity mismatch; original files were preserved")
    try:
        info = sf.info(str(path))
    except (RuntimeError, OSError) as exc:
        raise ValueError("Managed audio is unreadable; files were preserved") from exc
    if (info.samplerate, info.channels, info.frames) != (
        metadata["sample_rate"],
        metadata["channels"],
        metadata["frames"],
    ):
        raise ValueError("Managed audio format mismatch")


def _copy_verified(source, target, metadata):
    """A failed copy removes only its own incomplete sibling; source stays put."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    created = good = False
    try:
        with os.fdopen(os.open(source, flags), "rb") as src:
            if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                raise ValueError("Managed audio must be a regular file")
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dest:
                created = True
                copied = 0
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    copied += len(block)
                    if copied > metadata["size_bytes"]:
                        raise ValueError("Managed audio size changed")
                    dest.write(block)
                dest.flush()
                os.fsync(dest.fileno())
        _verify(target, metadata)
        if os.name != "nt":
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        good = True
    finally:
        if created and not good:
            target.unlink(missing_ok=True)


def _references(value, asset_ids):
    """Conservatively keep ID-valued strings and keys, including unknown fields."""
    if isinstance(value, str):
        return {value} & asset_ids
    if isinstance(value, list):
        return set().union(*(_references(item, asset_ids) for item in value)) if value else set()
    if isinstance(value, dict):
        if "schema_version" in value and value["schema_version"] != 1:
            return set(asset_ids)
        for key, item in value.items():
            if (key == "asset_id" or key.endswith("_asset_id")) and item is not None and not isinstance(item, str):
                return set(asset_ids)
            if (
                (key == "reference_assets" or key.endswith("asset_ids"))
                and item is not None
                and (not isinstance(item, list) or any(not isinstance(ident, str) for ident in item))
            ):
                return set(asset_ids)
        return set(value) & asset_ids | _references(list(value.values()), asset_ids)
    return set()


class StudioCleanup:
    def __init__(self, store):
        self.store = store

    @property
    def trash_dir(self):
        directory = self.store.root / "cleanup-trash"
        if directory.is_symlink():
            raise ValueError("Cleanup trash cannot be a symlink")
        created = not directory.exists()
        directory.mkdir(exist_ok=True)
        if created and os.name != "nt":
            descriptor = os.open(directory.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return directory

    def _preview(self, db, project_id):
        project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if project is None:
            raise NotFound("Project not found")
        assets = [dict(row) for row in db.execute("SELECT * FROM assets WHERE project_id=? ORDER BY id", (project_id,))]
        ids = {item["id"] for item in assets}
        protected = set()
        # Other projects are scanned too: a surprising cross-project reference must
        # retain the source rather than turning malformed metadata into data loss.
        for row in db.execute("SELECT document,undo,redo FROM projects"):
            for raw in row:
                try:
                    value = json.loads(raw)
                    if isinstance(value, dict) and value.get("schema_version", 1) != 1:
                        protected.update(ids)
                    protected.update(_references(value, ids))
                except (TypeError, ValueError):
                    protected.update(ids)
        for table, columns in (
            ("revisions", ("document",)),
            ("takes", ("asset_id", "request")),
            ("jobs", ("request", "result")),
            ("deliverables", ("data",)),
            ("approvals", ("note",)),
            ("recording_acknowledgements", ("recordings",)),
            ("operations", ("request", "response")),
            ("job_requests", ("request", "response")),
            ("render_requests", ("request", "response")),
        ):
            for row in db.execute(f"SELECT {','.join(columns)} FROM {table}"):
                for column, raw in zip(columns, row, strict=True):
                    if raw is None:
                        continue
                    if column == "asset_id" or column == "note":
                        protected.update(_references(raw, ids))
                    else:
                        try:
                            protected.update(_references(json.loads(raw), ids))
                        except (TypeError, ValueError):
                            protected.update(ids)
        candidates = [item for item in assets if item["id"] not in protected]
        for item in candidates:
            if item["path"] != item["id"]:
                # Unknown physical layouts are protected, never guessed.
                protected.add(item["id"])
        candidates = [item for item in candidates if item["id"] not in protected]
        token = hashlib.sha256(
            encode(
                dict(
                    project=project_id, revision=project["revision"], candidates=candidates, protected=sorted(protected)
                )
            ).encode()
        ).hexdigest()
        trash = [
            dict(row)
            for row in db.execute(
                "SELECT id,asset_id,metadata,created_at FROM cleanup_trash WHERE project_id=? AND restored_at IS NULL ORDER BY created_at,id",
                (project_id,),
            )
        ]
        for item in trash:
            metadata = json.loads(item.pop("metadata"))
            item["size_bytes"] = metadata["size_bytes"]
        return dict(
            project_id=project_id,
            preview_token=token,
            candidates=[{k: item[k] for k in ("id", "hash", "size_bytes", "duration")} for item in candidates],
            protected_count=len(protected),
            trash=trash,
            policy=POLICY,
        )

    def preview(self, project_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._preview(db, project_id)

    def _request(self, operation_id, identifiers):
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
            raise ValueError("Cleanup operation identity is required")
        if (
            not isinstance(identifiers, list)
            or not 1 <= len(identifiers) <= 100
            or len(set(identifiers)) != len(identifiers)
        ):
            raise ValueError("Choose between one and 100 unique audio items")
        for ident in identifiers:
            _uuid(ident)

    def _record(self, db, project_id, operation_id, request, receipt):
        db.execute(
            "INSERT INTO operations VALUES (?,?,?,?)", (project_id, operation_id, encode(request), encode(receipt))
        )

    def cleanup(self, project_id, operation_id, preview_token, asset_ids):
        self._request(operation_id, asset_ids)
        request = dict(type="cleanup_audio", preview_token=preview_token, asset_ids=asset_ids)
        copied = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self.store._operation(db, project_id, operation_id, request)
            if previous is not None:
                return previous
            preview = self._preview(db, project_id)
            if preview_token != preview["preview_token"] or not set(asset_ids) <= {
                item["id"] for item in preview["candidates"]
            }:
                raise StalePreviewError("Storage references changed. Refresh the cleanup preview before confirming.")
            selected = [
                dict(db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (ident, project_id)).fetchone())
                for ident in asset_ids
            ]
            if sum(item["size_bytes"] for item in selected) > MAX_BATCH_BYTES:
                raise ValueError("Choose at most 1 GiB of audio in one cleanup batch")
            receipts = []
            for item in selected:
                source = _owned_path(self.store.asset_dir, item["path"])
                receipt_id, stamp = uid(), now()
                target = _owned_path(self.trash_dir, receipt_id)
                _copy_verified(source, target, item)
                # Complete verified copies intentionally survive uncertain commits.
                db.execute(
                    "INSERT INTO cleanup_trash VALUES (?,?,?,?,?,?,NULL)",
                    (receipt_id, project_id, item["id"], encode(item), receipt_id, stamp),
                )
                db.execute("DELETE FROM assets WHERE id=? AND project_id=?", (item["id"], project_id))
                copied.append((receipt_id, item))
                receipts.append(
                    dict(id=receipt_id, asset_id=item["id"], size_bytes=item["size_bytes"], created_at=stamp)
                )
            result = dict(
                operation_id=operation_id, project_id=project_id, action="cleanup", receipts=receipts, policy=POLICY
            )
            self._record(db, project_id, operation_id, request, result)
        self._remove_originals(project_id, copied)
        return result

    def _remove_originals(self, project_id, copied):
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for receipt_id, item in copied:
                    row = db.execute(
                        "SELECT 1 FROM cleanup_trash WHERE id=? AND project_id=? AND restored_at IS NULL",
                        (receipt_id, project_id),
                    ).fetchone()
                    if (
                        not row
                        or db.execute(
                            "SELECT 1 FROM assets WHERE id=? OR path=?", (item["id"], item["path"])
                        ).fetchone()
                    ):
                        continue
                    source = _owned_path(self.store.asset_dir, item["path"])
                    trash = _owned_path(self.trash_dir, receipt_id)
                    _verify(trash, item)
                    if source.exists():
                        _verify(source, item)
                        source.unlink()
        except Exception:
            # Cleanup remains recoverable; uncertain/unremovable originals stay.
            pass

    def restore(self, project_id, operation_id, receipt_ids):
        self._request(operation_id, receipt_ids)
        request = dict(type="restore_cleanup_audio", receipt_ids=receipt_ids)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self.store._operation(db, project_id, operation_id, request)
            if previous is not None:
                return previous
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise NotFound("Project not found")
            restored = []
            selected = []
            for receipt_id in receipt_ids:
                row = db.execute(
                    "SELECT * FROM cleanup_trash WHERE id=? AND project_id=?", (receipt_id, project_id)
                ).fetchone()
                if row is None:
                    raise NotFound("Cleanup receipt not found in this project")
                if row["restored_at"] is not None:
                    raise Conflict("This audio has already been restored; refresh storage")
                item = json.loads(row["metadata"])
                if item["id"] != row["asset_id"] or item["project_id"] != project_id or item["path"] != item["id"]:
                    raise ValueError("Invalid cleanup receipt metadata")
                selected.append((receipt_id, row, item))
            if sum(item["size_bytes"] for _, _, item in selected) > MAX_BATCH_BYTES:
                raise ValueError("Choose at most 1 GiB of audio in one restore batch")
            for receipt_id, row, item in selected:
                source = _owned_path(self.trash_dir, row["path"])
                _verify(source, item)
                target = _owned_path(self.store.asset_dir, item["id"])
                if db.execute("SELECT 1 FROM assets WHERE id=?", (item["id"],)).fetchone():
                    raise Conflict("Audio identity is already registered; refresh storage")
                if target.exists():
                    # A cleanup COMMIT may have lost its acknowledgement before the
                    # original could be removed. Verify and register, never overwrite.
                    _verify(target, item)
                    db.execute(
                        "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
                        tuple(
                            item[key]
                            for key in (
                                "id",
                                "project_id",
                                "hash",
                                "sample_rate",
                                "channels",
                                "frames",
                                "duration",
                                "size_bytes",
                                "path",
                            )
                        ),
                    )
                else:
                    publish_audio(db, self.store.asset_dir, project_id, source, item["id"], expected=item)
                db.execute("UPDATE cleanup_trash SET restored_at=? WHERE id=?", (now(), receipt_id))
                restored.append(dict(id=receipt_id, asset_id=item["id"]))
            result = dict(
                operation_id=operation_id, project_id=project_id, action="restore", receipts=restored, policy=POLICY
            )
            self._record(db, project_id, operation_id, request, result)
        self._remove_restored_backups(project_id, receipt_ids)
        return result

    def _remove_restored_backups(self, project_id, receipt_ids):
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for receipt_id in receipt_ids:
                    row = db.execute(
                        "SELECT * FROM cleanup_trash WHERE id=? AND project_id=? AND restored_at IS NOT NULL",
                        (receipt_id, project_id),
                    ).fetchone()
                    if row is None:
                        continue
                    item = json.loads(row["metadata"])
                    asset = db.execute(
                        "SELECT * FROM assets WHERE id=? AND project_id=?", (row["asset_id"], project_id)
                    ).fetchone()
                    if asset is None or asset["hash"] != item["hash"]:
                        continue
                    _verify(_owned_path(self.store.asset_dir, asset["path"]), item)
                    backup = _owned_path(self.trash_dir, row["path"])
                    if backup.exists():
                        _verify(backup, item)
                        backup.unlink()
        except Exception:
            # An extra verified backup is safer than deleting on uncertain readback.
            pass


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: StrictStr
    preview_token: StrictStr
    asset_ids: list[StrictStr] = Field(min_length=1, max_length=100)


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: StrictStr
    receipt_ids: list[StrictStr] = Field(min_length=1, max_length=100)


def make_cleanup_router(store):
    router = APIRouter()

    def call(method, project_id, **kwargs):
        try:
            service = StudioCleanup(store() if callable(store) else store)
            return getattr(service, method)(project_id, **kwargs)
        except StalePreviewError as exc:
            raise HTTPException(409, detail={"message": str(exc), "code": "storage_preview_changed"}) from exc
        except Conflict as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/storage")
    def preview(project_id: str):
        return call("preview", project_id)

    @router.post("/projects/{project_id}/storage/cleanup")
    def cleanup(project_id: str, body: CleanupRequest):
        return call("cleanup", project_id, **body.model_dump())

    @router.post("/projects/{project_id}/storage/restore")
    def restore(project_id: str, body: RestoreRequest):
        return call("restore", project_id, **body.model_dump())

    return router
