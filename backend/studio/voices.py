"""Snapshot legacy voice metadata and copy reference audio into studio storage."""

from .. import config
from ..database import ProfileSample, VoiceProfile, get_db
from ..services.profiles import default_engine_for_profile, validate_profile_engine
from .capabilities import capability_for_config
from .storage import NotFound


def _ready(cfg):
    from ..backends.base import is_model_cached

    try:
        return is_model_cached(
            cfg.hf_repo_id,
            revision=getattr(cfg, "revision", None),
            required_files=list(getattr(cfg, "required_files", ())),
        ) and all(
            is_model_cached(
                repo,
                revision=getattr(cfg, "aux_revisions", {}).get(repo),
                required_files=list(getattr(cfg, "aux_required_files", {}).get(repo, ())),
            )
            for repo in cfg.aux_hf_repo_ids
        )
    except Exception:
        return False


def snapshot_voice(store, project_id, profile_id, model_size=None):
    db = next(get_db())
    try:
        profile = db.query(VoiceProfile).filter_by(id=profile_id).first()
        if not profile:
            raise NotFound("Voice profile not found")
        engine = default_engine_for_profile(profile)
        validate_profile_engine(profile, engine)
        snapshot = {
            key: getattr(profile, key, None)
            for key in ("id", "name", "voice_type", "preset_engine", "preset_voice_id", "design_prompt")
        }
        snapshot["voice_type"] = snapshot["voice_type"] or "cloned"
        snapshot["engine"] = engine
        from ..backends import get_tts_model_configs

        variants = [cfg for cfg in get_tts_model_configs() if cfg.engine == engine]
        if not variants:
            raise ValueError("No compatible engine variant is registered")
        if model_size is not None:
            selected = next((cfg for cfg in variants if cfg.model_size == model_size), None)
            if selected is None:
                raise ValueError("Unknown compatible model variant")
        else:
            selected = next((cfg for cfg in variants if _ready(cfg)), variants[0])
        snapshot["model_size"] = selected.model_size
        snapshot["capabilities"] = capability_for_config(selected)
        snapshot["samples"] = []
        if snapshot["voice_type"] == "cloned":
            samples = db.query(ProfileSample).filter_by(profile_id=profile_id).all()
            if not samples:
                raise ValueError("Voice profile has no reference samples")
            for sample in samples:
                path = config.resolve_storage_path(sample.audio_path)
                if path is None or not path.is_file():
                    raise ValueError("Voice reference audio is missing")
                asset = store.add_asset(project_id, path)
                snapshot["samples"].append({"asset_id": asset["id"], "reference_text": sample.reference_text})
        return snapshot
    finally:
        db.close()


def engines(commercial_only=False):
    """Return registered variants; commercial_only excludes unknown and restricted terms."""
    from ..backends import get_tts_model_configs

    result = []
    for cfg in get_tts_model_configs():
        caps = capability_for_config(cfg)
        if commercial_only and caps["commercial_use"] != "allowed":
            continue
        result.append(dict(id=cfg.engine, name=cfg.display_name, model_size=cfg.model_size, ready=_ready(cfg), **caps))
    return result
