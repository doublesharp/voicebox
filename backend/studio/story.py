"""Copy a legacy Story and its pinned audio into a new studio project."""

import hashlib
from uuid import NAMESPACE_URL, uuid5

import soundfile as sf

from .. import config
from ..database import Generation, GenerationVersion, Story, StoryItem
from .publication import discard_unregistered, publish_audio
from .storage import NotFound, document, encode, now, passage, uid
from .timing import nearest_sample
from .voice_identity import frozen_identity


def import_story(store, story_id, db, idempotency_key=None):
    request = None
    project_id = uid()
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("Story copy identity must be a nonempty string of at most 200 characters")
        request = dict(type="import_story", story_id=story_id, idempotency_key=idempotency_key)
        project_id = str(uuid5(NAMESPACE_URL, "voicebox:studio:story-copy:" + encode(request)))
        # Replay the committed response before looking at mutable/deleted legacy data.
        with store.connect() as target_db:
            prior = store._operation(target_db, project_id, "import_story", request)
            if prior is not None:
                return prior
    story = db.query(Story).filter_by(id=story_id).first()
    if not story:
        raise NotFound("Story not found")
    items = db.query(StoryItem).filter_by(story_id=story_id).order_by(StoryItem.start_time_ms, StoryItem.id).all()
    doc = document("", "en")
    doc["tracks"] = []
    tracks = {}
    prepared = []
    for item in items:
        generation = db.query(Generation).filter_by(id=item.generation_id).first()
        if not generation:
            raise ValueError("Story generation is missing")
        path = generation.audio_path
        if item.version_id:
            version = db.query(GenerationVersion).filter_by(id=item.version_id, generation_id=generation.id).first()
            if not version:
                raise ValueError("Story pinned version is missing")
            path = version.audio_path
        path = config.resolve_storage_path(path)
        if path is None or not path.is_file():
            raise ValueError("Story audio is missing")
        info = sf.info(str(path))
        if info.frames <= 0 or info.channels not in (1, 2):
            raise ValueError("Invalid Story audio")
        source_key = (item.track, generation.id, item.version_id)
        if source_key not in tracks:
            track = dict(id=uid(), name=f"Track {item.track + 1}", kind="voice", mute=False, solo=False, gain=1.0)
            identity = frozen_identity(
                {
                    "id": "",
                    "engine": generation.engine or "unknown",
                    "model_size": generation.model_size,
                    "request": {"legacy_generation_id": generation.id, "legacy_version_id": item.version_id},
                },
                lambda _: None,
            )
            track["voice_key"] = hashlib.sha256(encode(identity).encode()).hexdigest()
            tracks[source_key] = track
            doc["tracks"].append(track)
        p = passage(generation.text or "Imported audio", generation.language or "en")
        take_id = uid()
        asset_id = uid()
        p["selected_take_id"] = take_id
        doc["passages"].append(p)
        clip = dict(
            id=uid(),
            passage_id=p["id"],
            take_id=take_id,
            asset_id=asset_id,
            track_id=tracks[source_key]["id"],
            start_sample=nearest_sample(item.start_time_ms * 48),
            trim_start_sample=nearest_sample((item.trim_start_ms or 0) * 48),
            trim_end_sample=nearest_sample((item.trim_end_ms or 0) * 48),
            gain=item.volume if item.volume is not None else 1.0,
            fade_in_samples=0,
            fade_out_samples=0,
        )
        doc["clips"].append(clip)
        prepared.append((item, generation, path, info, p, take_id, asset_id, clip))
    if not tracks:
        doc["tracks"] = document("", "en")["tracks"]
    stamp = now()
    copied = []
    try:
        with store.connect() as target_db:
            target_db.execute("BEGIN IMMEDIATE")
            if request is not None:
                # The write lock arbitrates simultaneous retries after preparation.
                prior = store._operation(target_db, project_id, "import_story", request)
                if prior is not None:
                    return prior
            target_db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                (project_id, story.name, None, 0, stamp, stamp, encode(doc), "[]", "[]"),
            )
            for item, gen, path, info, p, take_id, asset_id, clip in prepared:
                target = store.asset_dir / asset_id
                publish_audio(
                    target_db,
                    store.asset_dir,
                    project_id,
                    path,
                    asset_id,
                    expected={"sample_rate": info.samplerate, "channels": info.channels, "frames": info.frames},
                )
                copied.append(target)
                take_request = {
                    "legacy_story_id": story_id,
                    "legacy_item_id": item.id,
                    "legacy_generation_id": gen.id,
                    "legacy_version_id": item.version_id,
                }
                target_db.execute(
                    "INSERT INTO takes VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        take_id,
                        project_id,
                        p["id"],
                        asset_id,
                        p["text"],
                        gen.engine or "unknown",
                        gen.model_size,
                        stamp,
                        encode(take_request),
                    ),
                )
                store._validate_clip(target_db, project_id, doc, clip)
            if request is not None:
                return store._record(target_db, project_id, "import_story", request)
            return store._project(target_db, project_id)
    except BaseException:
        if copied:
            discard_unregistered(target_db, copied)
        raise
