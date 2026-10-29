"""MOSS is discoverable and preserves explicit model, profile and runtime identity."""

from types import SimpleNamespace

import pytest

from backend import backends
from backend.models import GenerationRequest
from backend.services import profiles
from backend.studio.capabilities import capability_for_config


def test_moss_registry_is_pinned_and_matches_runtime():
    cfg = backends.get_model_config("moss-tts")
    assert cfg is not None, "CPU ONNX engine is missing from the model manager"
    from backend.backends.moss_tts_backend import CODEC_REPO, CODEC_REVISION, TTS_REPO, TTS_REVISION, MossTTSBackend

    assert cfg.engine == "moss_tts"
    assert cfg.hf_repo_id == TTS_REPO
    assert cfg.revision == TTS_REVISION
    assert cfg.aux_hf_repo_ids == (CODEC_REPO,)
    assert cfg.aux_revisions == {CODEC_REPO: CODEC_REVISION}
    runtime = backends.get_tts_backend_for_engine("moss_tts")
    assert isinstance(runtime, MossTTSBackend)
    assert runtime._get_model_path("default") == cfg.hf_repo_id
    identity = capability_for_config(cfg)["registry_identity"]
    assert identity["weights_revision"] == TTS_REVISION
    assert "MossTTSBackend" in identity["adapter"]
    assert capability_for_config(cfg)["supports_instruct"] is False


def test_moss_generation_and_clone_profile_are_accepted():
    assert GenerationRequest(profile_id="voice", text="CPU speech.", engine="moss_tts").engine == "moss_tts"
    profiles.validate_profile_engine(SimpleNamespace(voice_type="cloned", preset_engine=None), "moss_tts")


def test_moss_does_not_claim_presets_or_unsupported_app_languages():
    cfg = backends.get_model_config("moss-tts")
    assert cfg is not None
    assert set(cfg.languages) <= set(["zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it", "he", "ar", "da", "el", "fi", "hi", "ms", "nl", "no", "pl", "sv", "sw", "tr"])
    with pytest.raises(ValueError, match="Preset"):
        profiles.validate_profile_engine(
            SimpleNamespace(id="preset", voice_type="preset", preset_engine="kokoro", preset_voice_id="af_heart"),
            "moss_tts",
        )
