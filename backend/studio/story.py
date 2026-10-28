"""Copy a legacy Story and its pinned audio into a new studio project."""

import soundfile as sf
from .. import config
from ..database import Story, StoryItem, Generation, GenerationVersion
from .storage import NotFound, document, encode, now, passage, uid
from .publication import discard_unregistered, publish_audio
from .timing import nearest_sample


def import_story(store, story_id, db):
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
        if item.track not in tracks:
            track = dict(id=uid(), name=f"Track {item.track + 1}", kind="voice", mute=False, solo=False, gain=1.0)
            tracks[item.track] = track
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
            track_id=tracks[item.track]["id"],
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
    project_id = uid()
    stamp = now()
    copied = []
    try:
        with store.connect() as target_db:
            target_db.execute("BEGIN IMMEDIATE")
            target_db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                (project_id, story.name, None, 0, stamp, stamp, encode(doc), "[]", "[]"),
            )
            for item, gen, path, info, p, take_id, asset_id, clip in prepared:
                target = store.asset_dir / asset_id
                publish_audio(
                    target_db, store.asset_dir, project_id, path, asset_id,
                    expected={"sample_rate": info.samplerate, "channels": info.channels, "frames": info.frames},
                )
                copied.append(target)
                request = {
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
                        encode(request),
                    ),
                )
                store._validate_clip(target_db, project_id, doc, clip)
            return store._project(target_db, project_id)
    except BaseException:
        if copied:
            discard_unregistered(target_db, copied)
        raise
