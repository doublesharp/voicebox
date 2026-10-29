"""Publication uncertainty and unsupported stateful plugin boundaries."""

import pytest
import soundfile as sf

from backend.studio.delivery import StudioDelivery
from backend.studio.storage import StudioStore
from backend.tests.test_studio_review_regressions import generate, setup


@pytest.mark.asyncio
async def test_lost_successful_render_commit_keeps_registered_audio(tmp_path, monkeypatch):
    store, project = setup(tmp_path)
    pid = project["document"]["passages"][0]["id"]
    project = await generate(store, project, [pid])
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": [pid]})
    project = store.checkpoint(project["id"], "cp", project["revision"], "Ready")
    pending = []
    delivery = StudioDelivery(store, schedule=pending.append)
    job = delivery.submit(project["id"], "render", project["revisions"][0]["id"], "mix", "wav", processing="dry")
    connect = store.connect
    lost = False

    class Uncertain:
        def __init__(self):
            self.db = connect()

        def __enter__(self):
            return self.db.__enter__()

        def __exit__(self, kind, error, tb):
            nonlocal lost
            completed = self.db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "completed"
            uncertain = kind is None and completed and self.db.total_changes > 0 and not lost
            result = self.db.__exit__(kind, error, tb)
            if uncertain:
                lost = True
                raise OSError("successful commit acknowledgement lost")
            return result

    monkeypatch.setattr(store, "connect", Uncertain)
    await pending.pop()
    assert lost
    saved = store.get_project(project["id"])
    assert saved["jobs"][0]["status"] == "completed"
    for item in saved["deliverables"]:
        path, _ = delivery.path(item["id"])
        assert sf.info(path).frames > 0


def test_enabled_pitch_is_rejected_before_document_commit(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Pitch")
    track = project["document"]["tracks"][0]["id"]
    with pytest.raises(ValueError, match="(?i)pitch.*(unsupported|available|supported|disabled)"):
        store.command(
            project["id"],
            "pitch",
            0,
            "update_track",
            {
                "track_id": track,
                "effects_chain": [{"type": "pitch_shift", "params": {"semitones": 3}}],
            },
        )
    assert store.get_project(project["id"])["revision"] == 0
