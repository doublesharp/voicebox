"""Revision-local review CAS, exact operation replay, and frozen local approval."""

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from .review_schema import approval_note, validate_review_content, validate_reviews
from .storage import Conflict, NotFound, encode, now, uid


class StudioReviews:
    def __init__(self, store):
        self.store = store

    def _revision(self, db, project_id, revision_id):
        if not db.execute("SELECT 1 FROM revisions WHERE project_id=? AND id=?", (project_id, revision_id)).fetchone():
            raise NotFound("Review checkpoint not found")

    def _get(self, db, project_id, revision_id):
        self._revision(db, project_id, revision_id)
        row = db.execute(
            "SELECT revision_id,version,note,checklist,updated_at FROM checkpoint_reviews WHERE project_id=? AND revision_id=?",
            (project_id, revision_id),
        ).fetchone()
        if row is None:
            return dict(revision_id=revision_id, version=0, note="", checklist=[], updated_at=None)
        record = dict(row)
        record["checklist"] = json.loads(record["checklist"])
        return record

    def get(self, project_id, revision_id):
        with self.store.connect() as db:
            return self._get(db, project_id, revision_id)

    def export(self, project_id):
        with self.store.connect() as db:
            self.store._project(db, project_id)
            return [
                self._get(db, project_id, r[0])
                for r in db.execute(
                    "SELECT revision_id FROM checkpoint_reviews WHERE project_id=? ORDER BY revision_id", (project_id,)
                ).fetchall()
            ]

    def _mutate(self, project_id, revision_id, operation_id, expected_version, action, note="", checklist=None):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
            raise ValueError("Review operation identity required")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Invalid review version")
        request = dict(action=action, revision_id=revision_id, expected_version=expected_version)
        if action == "save":
            validate_review_content(note, checklist)
            request.update(note=note, checklist=checklist)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT request,response FROM review_operations WHERE project_id=? AND operation_id=?",
                (project_id, operation_id),
            ).fetchone()
            if prior:
                if prior["request"] != encode(request):
                    raise Conflict("Review operation identity reused with different content")
                return json.loads(prior["response"])
            review = self._get(db, project_id, revision_id)
            if review["version"] != expected_version or (action == "approve" and expected_version == 0):
                raise Conflict("Review changed; save and acknowledge this review before approval", review["version"])
            if action == "save":
                review = dict(
                    revision_id=revision_id,
                    version=expected_version + 1,
                    note=note,
                    checklist=checklist,
                    updated_at=now(),
                )
                db.execute(
                    "INSERT INTO checkpoint_reviews VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,revision_id) DO UPDATE SET version=excluded.version,note=excluded.note,checklist=excluded.checklist,updated_at=excluded.updated_at",
                    (project_id, revision_id, review["version"], note, encode(checklist), review["updated_at"]),
                )
                response = {"review": review}
            else:
                # Existing local approval format; no project document or checkpoint is rewritten.
                db.execute(
                    "INSERT INTO approvals VALUES (?,?,?,?,?)",
                    (uid(), project_id, revision_id, approval_note(review), now()),
                )
                response = {"review": review, "project": self.store._project(db, project_id)}
            db.execute(
                "INSERT INTO review_operations VALUES (?,?,?,?)",
                (project_id, operation_id, encode(request), encode(response)),
            )
            return response

    def save(self, project_id, revision_id, operation_id, expected_version, note, checklist):
        return self._mutate(project_id, revision_id, operation_id, expected_version, "save", note, checklist)

    def approve(self, project_id, revision_id, operation_id, expected_version):
        return self._mutate(project_id, revision_id, operation_id, expected_version, "approve")


def restore_reviews(db, project_id, records):
    """Caller owns the archive transaction; validation precedes every insertion."""
    revisions = [dict(id=r[0]) for r in db.execute("SELECT id FROM revisions WHERE project_id=?", (project_id,))]
    validate_reviews(dict(reviews=records, revisions=revisions))
    for record in records:
        db.execute(
            "INSERT INTO checkpoint_reviews VALUES (?,?,?,?,?,?)",
            (
                project_id,
                record["revision_id"],
                record["version"],
                record["note"],
                encode(record["checklist"]),
                record["updated_at"],
            ),
        )


class SaveReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: StrictStr
    expected_version: StrictInt
    note: StrictStr
    checklist: list


class ApproveReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: StrictStr
    expected_version: StrictInt


def make_review_router(store):
    """Unprefixed router; accepts a store or the root API's lazy store callable."""
    router = APIRouter()

    def call(method, *args, **kwargs):
        try:
            return getattr(StudioReviews(store() if callable(store) else store), method)(*args, **kwargs)
        except NotFound as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, detail=dict(message=str(exc), current_revision=exc.current_revision)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/reviews/{revision_id}")
    def get(project_id: str, revision_id: str):
        return {"review": call("get", project_id, revision_id)}

    @router.post("/projects/{project_id}/reviews/{revision_id}")
    def save(project_id: str, revision_id: str, body: SaveReview):
        return call("save", project_id, revision_id, **body.model_dump())

    @router.post("/projects/{project_id}/reviews/{revision_id}/approvals")
    def approve(project_id: str, revision_id: str, body: ApproveReview):
        return call("approve", project_id, revision_id, **body.model_dump())

    return router
