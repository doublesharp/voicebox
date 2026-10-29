import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database import Base, ProfileSample, VoiceProfile
from backend.studio import voices
from backend.studio.storage import StudioStore


def test_qwen_snapshot_uses_available_variant_and_freezes_explicit_choice(tmp_path, monkeypatch):
    from backend.backends import base

    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.sqlite"))
    Base.metadata.create_all(engine)
    source = tmp_path / "reference.wav"
    sf.write(source, np.full(2400, 0.1), 24000)
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Narrator", language="en", voice_type="cloned", default_engine="qwen"))
        db.add(ProfileSample(id="sample", profile_id="voice", audio_path=str(source), reference_text="Hello"))
        db.commit()
    monkeypatch.setattr(voices, "get_db", lambda: iter([Session(engine)]))
    monkeypatch.setattr(base, "is_model_cached", lambda repo, **kwargs: "0.6B" in repo)
    store = StudioStore(tmp_path / "studio")
    p = store.create_project("Voice")
    snapshot = voices.snapshot_voice(store, p["id"], "voice")
    assert snapshot["model_size"] == "0.6B"
    assert snapshot["capabilities"]["registry_identity"]["model_size"] == "0.6B"
    assert snapshot["capabilities"]["supports_instruct"] is False
    explicit = voices.snapshot_voice(store, p["id"], "voice", model_size="1.7B")
    assert explicit["model_size"] == "1.7B"
    with pytest.raises(ValueError, match="variant"):
        voices.snapshot_voice(store, p["id"], "voice", model_size="missing")


def test_readiness_requires_exact_primary_and_auxiliary_manifests(monkeypatch):
    from types import SimpleNamespace

    from backend.backends import base
    from backend.studio.voices import _ready

    cfg = SimpleNamespace(
        hf_repo_id="example/tts",
        revision="a" * 40,
        required_files=("model.onnx",),
        aux_hf_repo_ids=["example/codec"],
        aux_revisions={"example/codec": "b" * 40},
        aux_required_files={"example/codec": ("codec.onnx",)},
    )
    calls = []

    def cached(repo, **kwargs):
        calls.append((repo, kwargs))
        return kwargs.get("revision") == ("a" * 40 if repo == cfg.hf_repo_id else "b" * 40)

    monkeypatch.setattr(base, "is_model_cached", cached)
    assert _ready(cfg)
    assert calls == [
        ("example/tts", {"revision": "a" * 40, "required_files": ["model.onnx"]}),
        ("example/codec", {"revision": "b" * 40, "required_files": ["codec.onnx"]}),
    ]
    cfg.aux_revisions["example/codec"] = "c" * 40
    assert not _ready(cfg)
