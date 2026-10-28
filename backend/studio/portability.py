"""Bridge validated archives into studio-owned identifiers and files."""

import copy
import shutil
import soundfile as sf
from .storage import encode, now, uid


def import_archive(store, archive_path):
    from .archive import read_archive

    staging = store.root / (".import-" + uid())
    staging.mkdir()
    copied = []
    try:
        payload = read_archive(archive_path, staging)
        project = payload["project"]
        assets = payload["assets"]
        for asset in assets:
            info = sf.info(asset["path"])
            if (info.samplerate, info.channels, info.frames) != (
                asset.get("sample_rate"),
                asset.get("channels"),
                asset.get("frames"),
            ):
                raise ValueError("Asset audio metadata mismatch")
            if abs(asset.get("duration", -1) - info.frames / info.samplerate) > 1e-9:
                raise ValueError("Asset duration mismatch")
        mapping = {project["id"]: uid()}
        for asset in assets:
            mapping[asset["id"]] = uid()
        for section in ("takes", "revisions", "approvals"):
            for item in project.get(section, []):
                mapping[item["id"]] = uid()
        for item in project.get("recording_acknowledgements", []):
            mapping[item["id"]] = uid()
        for take in project.get("takes", []):
            mapping.setdefault(take["passage_id"], uid())
        docs = [project["document"]] + [r["document"] for r in project.get("revisions", [])]
        for doc in docs:
            for section in ("passages", "tracks", "clips"):
                for item in doc[section]:
                    mapping.setdefault(item["id"], uid())

        def ref(value):
            return mapping.get(value, value) if isinstance(value, str) else value

        def voice(value):
            if not isinstance(value, dict):
                return value
            for sample in value.get("samples", []):
                sample["asset_id"] = ref(sample["asset_id"])
            if "reference_assets" in value:
                value["reference_assets"] = [ref(x) for x in value["reference_assets"]]
            return value

        def passage(value):
            value["id"] = ref(value["id"])
            value["selected_take_id"] = ref(value.get("selected_take_id"))
            voice(value.get("voice_snapshot"))
            return value

        def document(value):
            for item in value["passages"]:
                passage(item)
            for track in value["tracks"]:
                track["id"] = ref(track["id"])
                if track.get("voice_key", "").startswith(("{", "null")):
                    import json

                    track["voice_key"] = encode(voice(json.loads(track["voice_key"])))
            for clip in value["clips"]:
                for key in ("id", "asset_id", "take_id", "passage_id", "track_id"):
                    clip[key] = ref(clip.get(key))

        imported = copy.deepcopy(project)
        imported["id"] = ref(imported["id"])
        document(imported["document"])
        for take in imported.get("takes", []):
            for key in ("id", "asset_id", "passage_id"):
                take[key] = ref(take[key])
            if isinstance(take.get("request", {}).get("passage"), dict):
                passage(take["request"]["passage"])
        for revision in imported.get("revisions", []):
            revision["id"] = ref(revision["id"])
            document(revision["document"])
        for item in imported.get("approvals", []):
            item["id"] = ref(item["id"])
            item["revision_id"] = ref(item["revision_id"])
        for item in imported.get("recording_acknowledgements", []):
            item["id"] = ref(item["id"])
            item["revision_id"] = ref(item["revision_id"])
            for recording in item["recordings"]:
                for key in ("clip_id", "take_id"):
                    recording[key] = ref(recording[key])
        project_id = imported["id"]
        stamp = now()
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    imported["name"],
                    imported.get("client_label"),
                    0,
                    stamp,
                    stamp,
                    encode(imported["document"]),
                    "[]",
                    "[]",
                ),
            )
            for asset in assets:
                ident = mapping[asset["id"]]
                target = store.asset_dir / ident
                with open(asset["path"], "rb") as source, target.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
                copied.append(target)
                db.execute(
                    "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        project_id,
                        asset["hash"],
                        asset["sample_rate"],
                        asset["channels"],
                        asset["frames"],
                        asset["duration"],
                        asset["size_bytes"],
                        ident,
                    ),
                )
            for take in imported.get("takes", []):
                db.execute(
                    "INSERT INTO takes VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        take["id"],
                        project_id,
                        take["passage_id"],
                        take["asset_id"],
                        take["text"],
                        take["engine"],
                        take.get("model_size"),
                        take["created_at"],
                        encode(take.get("request", {})),
                    ),
                )
            for revision in imported.get("revisions", []):
                db.execute(
                    "INSERT INTO revisions VALUES (?,?,?,?,?,?)",
                    (
                        revision["id"],
                        project_id,
                        revision["name"],
                        revision["created_at"],
                        encode(revision["document"]),
                        revision["project_revision"],
                    ),
                )
            for approval in imported.get("approvals", []):
                if not db.execute(
                    "SELECT 1 FROM revisions WHERE id=? AND project_id=?", (approval["revision_id"], project_id)
                ).fetchone():
                    raise ValueError("Approval revision missing")
                db.execute(
                    "INSERT INTO approvals VALUES (?,?,?,?,?)",
                    (approval["id"], project_id, approval["revision_id"], approval.get("note"), approval["created_at"]),
                )
            for item in imported.get("recording_acknowledgements", []):
                db.execute(
                    "INSERT INTO recording_acknowledgements VALUES (?,?,?,?,?)",
                    (item["id"], project_id, item["revision_id"], item["created_at"], encode(item["recordings"])),
                )
            for doc in [imported["document"]] + [r["document"] for r in imported.get("revisions", [])]:
                for clip in doc["clips"]:
                    store._validate_clip(db, project_id, doc, clip)
                for p in doc["passages"]:
                    if (
                        p.get("selected_take_id")
                        and not db.execute(
                            "SELECT 1 FROM takes WHERE id=? AND project_id=? AND passage_id=?",
                            (p["selected_take_id"], project_id, p["id"]),
                        ).fetchone()
                    ):
                        raise ValueError("Invalid selected take")
            return store._project(db, project_id)
    except BaseException:
        for path in copied:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
