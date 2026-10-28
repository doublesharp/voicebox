"""Durable generation requests; the queue retains ownership until inference returns."""

import copy
import json

from .storage import Conflict, NotFound, encode, now, uid
from ..utils.inference import serialized_inference


class StudioJobs:
    def __init__(self, store, synth=None, enqueue=None):
        self.store = store
        self.synth = synth or (lambda request: synthesize(request, store))
        if enqueue is None:
            from ..services.task_queue import enqueue_generation

            enqueue = enqueue_generation
        self.enqueue = enqueue
        with store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS job_requests (project_id TEXT, key TEXT, request TEXT, response TEXT, PRIMARY KEY(project_id,key))"
            )

    def submit(self, project_id, key, passage_ids, count, adopt):
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(passage_ids, list)
            or not passage_ids
            or len(set(passage_ids)) != len(passage_ids)
            or type(count) is not int
            or count not in (1, 2, 3)
            or type(adopt) is not bool
        ):
            raise ValueError("Invalid generation request")
        request = dict(passage_ids=passage_ids, count=count, adopt=adopt)
        scheduled = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM job_requests WHERE project_id=? AND key=?", (project_id, key)).fetchone()
            if old:
                if old["request"] != encode(request):
                    raise Conflict("idempotency key already used with different input")
                return json.loads(old["response"])
            project = self.store._project(db, project_id)
            passages = {p["id"]: p for p in project["document"]["passages"]}
            for ident in passage_ids:
                p = passages.get(ident)
                if not p:
                    raise NotFound("Passage not found in project")
                if p["locked"]:
                    raise ValueError("Passage is locked")
                for _ in range(count):
                    job_id = uid()
                    timestamp = now()
                    frozen = dict(passage=p, adopt=adopt)
                    db.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            project_id,
                            "generation",
                            "queued",
                            ident,
                            None,
                            None,
                            timestamp,
                            timestamp,
                            encode(frozen),
                        ),
                    )
                    scheduled.append(job_id)
            response = [j for j in self.store._project(db, project_id)["jobs"] if j["id"] in scheduled]
            db.execute(
                "INSERT INTO job_requests VALUES (?,?,?,?)", (project_id, key, encode(request), encode(response))
            )
        for job_id in scheduled:
            self.enqueue(job_id, self.run(job_id))
        return response

    def get(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT project_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise NotFound("Job not found")
            return next(j for j in self.store._project(db, row["project_id"])["jobs"] if j["id"] == job_id)

    def cancel(self, job_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise NotFound("Job not found")
            if row["status"] in ("queued", "running"):
                status = "cancelled" if row["status"] == "queued" else "cancelling"
                db.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (status, now(), job_id))
        return self.get(job_id)

    def retry(self, job_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise NotFound("Job not found")
            if row["status"] not in ("failed", "interrupted"):
                raise ValueError("Only failed or interrupted jobs can be retried")
            kind = row["kind"]
            if kind not in ("generation", "render"):
                raise ValueError("Unknown job kind")
            db.execute("UPDATE jobs SET status=?,error=NULL,updated_at=? WHERE id=?", ("queued", now(), job_id))
        if kind == "generation":
            self.enqueue(job_id, self.run(job_id))
        else:
            from .delivery import StudioDelivery

            delivery = StudioDelivery(self.store)
            delivery.schedule(delivery.run(job_id))
        return self.get(job_id)

    def recover_on_startup(self):
        """Resume acknowledged queued jobs; running work needs explicit retry.

        Call once per process after the legacy generation queue is initialized.
        """
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET status='interrupted',error='Server restarted; retry explicitly',updated_at=? WHERE status='running'",
                (now(),),
            )
            db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE status='cancelling'", (now(),))
            queued = db.execute("SELECT id,kind FROM jobs WHERE status='queued' ORDER BY created_at,id").fetchall()
        for row in queued:
            if row["kind"] == "generation":
                self.enqueue(row["id"], self.run(row["id"]))
            elif row["kind"] == "render":
                from .delivery import StudioDelivery

                delivery = StudioDelivery(self.store)
                delivery.schedule(delivery.run(row["id"]))

    def interrupt_on_startup(self):
        """Compatibility alias for startup integration."""
        self.recover_on_startup()

    async def run(self, job_id):
        import soundfile as sf

        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "queued":
                return
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), job_id))
            project_id = row["project_id"]
            frozen = json.loads(row["request"])
        temporary = self.store.asset_dir / (".generation-" + job_id)
        try:
            audio, rate = await self.synth(frozen)
            sf.write(str(temporary), audio, rate, format="WAV")
            with self.store.connect() as db:
                row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row or row["status"] != "running":
                    return
            asset = self.store.add_asset(project_id, temporary)
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row or row["status"] != "running":
                    return
                p = frozen["passage"]
                snapshot = p.get("voice_snapshot") or {}
                take_id = uid()
                db.execute(
                    "INSERT INTO takes VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        take_id,
                        project_id,
                        p["id"],
                        asset["id"],
                        p["text"],
                        snapshot.get("engine", "unknown"),
                        snapshot.get("model_size"),
                        now(),
                        encode(frozen),
                    ),
                )
                project = self.store._project(db, project_id)
                current = next((x for x in project["document"]["passages"] if x["id"] == p["id"]), None)
                if (
                    frozen["adopt"]
                    and current
                    and not current["locked"]
                    and current["input_revision"] == p["input_revision"]
                    and current["selection_token"] == p["selection_token"]
                ):
                    row = db.execute("SELECT undo FROM projects WHERE id=?", (project_id,)).fetchone()
                    undo = json.loads(row["undo"])
                    undo.append(
                        dict(
                            document=copy.deepcopy(project["document"]),
                            name=project["name"],
                            client_label=project["client_label"],
                        )
                    )
                    current["selected_take_id"] = take_id
                    current["selection_token"] += 1
                    self.store._remember_intents(db, project_id, project["document"])
                    db.execute(
                        "UPDATE projects SET document=?,undo=?,redo=?,revision=revision+1,updated_at=? WHERE id=?",
                        (encode(project["document"]), encode(undo), "[]", now(), project_id),
                    )
                db.execute(
                    "UPDATE jobs SET status='completed',result=?,updated_at=? WHERE id=?",
                    (encode({"take_id": take_id, "asset_id": asset["id"]}), now(), job_id),
                )
        except Exception as exc:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=? AND status='running'",
                    (str(exc), now(), job_id),
                )
        finally:
            temporary.unlink(missing_ok=True)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status='cancelling'",
                    (now(), job_id),
                )


@serialized_inference
async def synthesize(request, store):
    """Use the copied snapshot, never the mutable profile row."""
    from ..backends import (
        ensure_model_cached_or_raise,
        get_tts_backend_for_engine,
        load_engine_model,
        engine_needs_trim,
        engine_retries_runaway,
    )
    from ..utils.chunked_tts import generate_chunked
    from ..utils.audio import has_tts_runaway, trim_tts_output

    p = request["passage"]
    voice = p.get("voice_snapshot")
    if not voice:
        raise ValueError("Assign a voice before generation")
    engine = voice["engine"]
    size = voice.get("model_size") or "default"
    await ensure_model_cached_or_raise(engine, size)
    model = get_tts_backend_for_engine(engine)
    await load_engine_model(engine, size)
    if voice["voice_type"] == "preset":
        prompt = {k: voice[k] for k in ("voice_type", "preset_engine", "preset_voice_id")}
    elif voice["voice_type"] == "designed":
        prompt = {"voice_type": "designed", "design_prompt": voice["design_prompt"]}
    else:
        samples = voice.get("samples", [])
        if not samples:
            raise ValueError("Voice reference snapshot is missing")
        paths = [str(store.asset_path(s["asset_id"])) for s in samples]
        if len(paths) == 1:
            prompt, _ = await model.create_voice_prompt(paths[0], samples[0]["reference_text"], use_cache=False)
        else:
            import soundfile as sf

            combined, reference_text = await model.combine_voice_prompts(paths, [s["reference_text"] for s in samples])
            path = store.asset_dir / (".combined-" + uid())
            try:
                sf.write(str(path), combined, 24000, format="WAV")
                prompt, _ = await model.create_voice_prompt(str(path), reference_text, use_cache=False)
            finally:
                path.unlink(missing_ok=True)
    d = p["direction"]
    return await generate_chunked(
        model,
        p["text"],
        prompt,
        language=p["language"],
        seed=d["seed"],
        instruct=d["instruct"] or None,
        max_chunk_chars=d["max_chunk_chars"],
        crossfade_ms=d["crossfade_ms"],
        trim_fn=trim_tts_output if engine_needs_trim(engine) else None,
        runaway_detector=has_tts_runaway if engine_retries_runaway(engine) else None,
    )
