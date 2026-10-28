"""HTTP interface for the independent studio store."""

import zipfile
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from .jobs import StudioJobs
from .storage import Conflict, NotFound, uid


class Create(BaseModel):
    name: str
    client_label: str | None = None
    script: str = ""
    language: str = "en"


class Command(BaseModel):
    operation_id: str
    expected_revision: int
    type: str
    payload: dict


class Revision(BaseModel):
    operation_id: str
    expected_revision: int
    name: str


class Approval(BaseModel):
    operation_id: str
    revision_id: str
    note: str | None = None


class Render(BaseModel):
    idempotency_key: str
    revision_id: str
    kind: str
    format: str
    allow_stale_audio: bool = False


class Generate(BaseModel):
    idempotency_key: str
    passage_ids: list[str]
    count: int = Field(ge=1, le=3)
    adopt: bool
    commercial_only: bool = False


def make_router(store, jobs=None):
    router = APIRouter(prefix="/studio/v1")
    from .preview import make_preview_router
    from .waveforms import make_waveform_router
    from .audition import make_audition_router

    router.include_router(make_preview_router(store))
    router.include_router(make_waveform_router(store))
    router.include_router(make_audition_router(store))
    jobs = jobs or StudioJobs(store)

    def get_delivery():
        from .delivery import StudioDelivery

        return StudioDelivery(store)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Conflict as exc:
            raise HTTPException(409, detail={"message": str(exc), "current_revision": exc.current_revision}) from exc
        except NotFound as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except (ValueError, TypeError, zipfile.BadZipFile) as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @router.get("/projects")
    def projects():
        return {"items": store.list_projects()}

    @router.post("/projects")
    def create(body: Create):
        return call(store.create_project, **body.model_dump())

    @router.get("/projects/{project_id}")
    def project(project_id: str):
        return call(store.get_project, project_id)

    @router.post("/projects/{project_id}/commands")
    def command(project_id: str, body: Command):
        return call(store.command, project_id, **body.model_dump())

    @router.post("/projects/{project_id}/revisions")
    def revision(project_id: str, body: Revision):
        return call(store.checkpoint, project_id, **body.model_dump())

    @router.post("/projects/{project_id}/approvals")
    def approval(project_id: str, body: Approval):
        return call(store.approve, project_id, **body.model_dump())

    @router.get("/engines")
    def get_engines(commercial_only: bool = False):
        from .voices import engines

        return {"items": engines(commercial_only=commercial_only)}

    @router.post("/projects/{project_id}/generation-jobs", status_code=202)
    async def generate(project_id: str, body: Generate):
        return {
            "jobs": call(
                jobs.submit, project_id, body.idempotency_key, body.passage_ids, body.count, body.adopt,
                commercial_only=body.commercial_only,
            )
        }

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return call(jobs.get, job_id)

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return call(jobs.cancel, job_id)

    @router.post("/jobs/{job_id}/retry")
    async def retry(job_id: str):
        return call(jobs.retry, job_id)

    @router.post("/projects/{project_id}/render-jobs", status_code=202)
    async def render(project_id: str, body: Render):
        return {"job": call(get_delivery().submit, project_id, **body.model_dump())}

    @router.get("/deliverables/{ident}/audio")
    @router.get("/deliverables/{ident}/download")
    def deliverable(ident: str):
        path, meta = call(get_delivery().path, ident)
        return FileResponse(
            path,
            media_type="audio/mpeg" if meta["format"] == "mp3" else "audio/wav",
            filename=meta.get("filename", "studio." + meta["format"]),
        )

    @router.get("/projects/{project_id}/archive")
    def archive(project_id: str):
        from .archive import write_archive

        project = call(store.get_project, project_id)
        target = store.root / (".archive-" + uid() + ".zip")
        try:
            call(write_archive, project, project["assets"], store.asset_path, target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return FileResponse(
            target,
            media_type="application/zip",
            filename="studio.zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @router.post("/imports/archive")
    async def import_project(file: UploadFile = File(...)):
        from .portability import import_archive

        path = store.root / (".archive-upload-" + uid())
        try:
            with path.open("xb") as dest:
                while chunk := await file.read(1024 * 1024):
                    if dest.tell() + len(chunk) > 1024 * 1024 * 1024:
                        raise HTTPException(413, detail="Archive is too large")
                    dest.write(chunk)
            return await run_in_threadpool(call, import_archive, store, path)
        finally:
            path.unlink(missing_ok=True)
            await file.close()

    @router.post("/imports/story/{story_id}")
    def import_legacy_story(story_id: str):
        from ..database import get_db
        from .story import import_story

        db = next(get_db())
        try:
            return call(import_story, store, story_id, db)
        finally:
            db.close()

    @router.get("/assets/{asset_id}/audio")
    def audio(asset_id: str):
        return FileResponse(call(store.asset_path, asset_id), media_type="audio/wav")

    @router.post("/projects/{project_id}/assets")
    async def upload(project_id: str, file: UploadFile = File(...)):
        path = store.root / (".upload-" + uid())
        try:
            with path.open("xb") as dest:
                while chunk := await file.read(1024 * 1024):
                    if dest.tell() + len(chunk) > 200 * 1024 * 1024:
                        raise HTTPException(413, detail="Audio file is too large")
                    dest.write(chunk)
            return await run_in_threadpool(call, store.add_asset, project_id, path)
        finally:
            path.unlink(missing_ok=True)
            await file.close()

    return router
