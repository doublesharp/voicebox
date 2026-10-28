import pytest
from backend.studio.storage import StudioStore, Conflict


def test_project_commands_persist_and_replay_original_result(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Narration", script="First\n\nSecond")
    assert [p["text"] for p in project["document"]["passages"]] == ["First", "Second"]
    passage = project["document"]["passages"][0]
    request = dict(
        operation_id="edit-1",
        expected_revision=0,
        type="update_passage",
        payload={"passage_id": passage["id"], "text": "Changed"},
    )
    edited = store.command(project["id"], **request)
    assert edited["revision"] == 1
    store.command(
        project["id"],
        operation_id="edit-2",
        expected_revision=1,
        type="set_lock",
        payload={"passage_id": passage["id"], "locked": True},
    )
    assert store.command(project["id"], **request) == edited
    with pytest.raises(Conflict):
        store.command(project["id"], **{**request, "payload": {"passage_id": passage["id"], "text": "Different"}})
    reopened = StudioStore(tmp_path).get_project(project["id"])
    assert reopened["revision"] == 2
    assert reopened["document"]["passages"][0]["locked"]
    undone = store.command(project["id"], operation_id="undo", expected_revision=2, type="undo", payload={})
    assert not undone["document"]["passages"][0]["locked"]
    assert undone["document"]["passages"][0]["selection_token"] > passage["selection_token"]


def test_checkpoint_approval_and_foreign_revision_are_isolated(tmp_path):
    from backend.studio.storage import NotFound

    store = StudioStore(tmp_path)
    a = store.create_project("A", script="A")
    b = store.create_project("B", script="B")
    a = store.checkpoint(a["id"], "checkpoint", 0, "Review")
    revision = a["revisions"][0]
    a = store.approve(a["id"], "approve", revision["id"], "OK")
    assert a["approvals"][0]["revision_id"] == revision["id"]
    with pytest.raises(NotFound):
        store.command(b["id"], "restore", 0, "restore_revision", {"revision_id": revision["id"]})
    with pytest.raises(NotFound):
        store.approve(b["id"], "approve", revision["id"])


def test_story_import_copies_pinned_audio_and_preserves_placement(tmp_path):
    import numpy as np
    import soundfile as sf
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from backend.database.models import Base, Story, StoryItem, Generation, GenerationVersion
    from backend.studio.story import import_story

    audio = tmp_path / "legacy.wav"
    sf.write(audio, np.zeros(1000, dtype="float32"), 1000)
    legacy = create_engine("sqlite:///" + str(tmp_path / "legacy.sqlite"))
    Base.metadata.create_all(legacy)
    with Session(legacy) as db:
        db.add_all(
            [
                Story(id="s", name="Story"),
                Generation(id="g", profile_id="p", text="Hello", language="en", audio_path=str(audio), engine="qwen"),
                GenerationVersion(id="v", generation_id="g", label="pin", audio_path=str(audio)),
                StoryItem(
                    id="i",
                    story_id="s",
                    generation_id="g",
                    version_id="v",
                    start_time_ms=500,
                    track=2,
                    trim_start_ms=100,
                    trim_end_ms=100,
                    volume=0.5,
                ),
            ]
        )
        db.commit()
        store = StudioStore(tmp_path / "studio")
        project = import_story(store, "s", db)
        clip = project["document"]["clips"][0]
        assert (clip["start_sample"], clip["trim_start_sample"], clip["trim_end_sample"], clip["gain"]) == (
            24000,
            4800,
            4800,
            0.5,
        )
        audio.unlink()
        assert store.asset_path(clip["asset_id"]).is_file()
        assert db.query(StoryItem).count() == 1
