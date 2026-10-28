from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import numpy as np
import soundfile as sf
import pytest

from backend import backends
from backend.database import Base, VoiceProfile, ProfileSample
from backend.studio.storage import StudioStore
from backend.studio import voices


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
