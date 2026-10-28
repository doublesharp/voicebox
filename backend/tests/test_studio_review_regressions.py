"""Regressions reproduced from the independent S1 reviews."""

import asyncio

import numpy as np
import pytest
import soundfile as sf

from backend.studio.storage import StudioStore
from backend.studio.jobs import StudioJobs


def fixture_capabilities(voice):
    return {"languages": ["en"], "expressive_tags": [], "supports_instruct": False, "supports_seed": False}


def setup(tmp_path, script="One"):
    def voice(store, project_id, profile_id):
        return {"engine": "fixture", "voice_type": "preset", "preset_engine": "fixture", "preset_voice_id": profile_id}

    store = StudioStore(tmp_path, voice_resolver=voice)
    project = store.create_project("Review", script=script)
    return store, project


async def generate(store, project, ids, adopt=True):
    pending = []

    async def synth(request):
        return np.full(100, 0.1, dtype="float32"), 1000

    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: pending.append(coro)
    )
    jobs.submit(project["id"], "generate", ids, 1, adopt)
    for coro in pending:
        await coro
    return store.get_project(project["id"])


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("update_passage", {"text": "Changed"}),
        ("delete_passage", {}),
        ("select_take", {"take_id": "missing"}),
        ("assign_voice", {"profile_id": "voice"}),
    ],
)
def test_locked_passage_cannot_be_mutated(tmp_path, kind, payload):
    store, p = setup(tmp_path)
    pid = p["document"]["passages"][0]["id"]
    store.command(p["id"], "lock", 0, "set_lock", {"passage_id": pid, "locked": True})
    payload = {**payload, **({"passage_ids": [pid]} if kind == "assign_voice" else {"passage_id": pid})}
    with pytest.raises(ValueError, match="locked"):
        store.command(p["id"], "mutation", 1, kind, payload)
    assert store.get_project(p["id"])["revision"] == 1


@pytest.mark.asyncio
async def test_deleted_restored_passage_never_reuses_adoption_intent(tmp_path):
    store, p = setup(tmp_path)
    pid = p["document"]["passages"][0]["id"]
    p = store.checkpoint(p["id"], "checkpoint", 0, "Original")
    rid = p["revisions"][0]["id"]
    p = store.command(p["id"], "restore1", 0, "restore_revision", {"revision_id": rid})
    pending = []

    async def synth(request):
        return np.full(100, 0.1, dtype="float32"), 1000

    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: pending.append(coro)
    )
    jobs.submit(p["id"], "job", [pid], 1, True)
    p = store.command(p["id"], "delete", p["revision"], "delete_passage", {"passage_id": pid})
    store.command(p["id"], "restore2", p["revision"], "restore_revision", {"revision_id": rid})
    await pending.pop()
    assert store.get_project(p["id"])["document"]["passages"][0]["selected_take_id"] is None


@pytest.mark.asyncio
async def test_automatic_adoption_is_the_next_undoable_selection(tmp_path):
    store, p = setup(tmp_path)
    pid = p["document"]["passages"][0]["id"]
    p = store.command(p["id"], "edit", 0, "update_passage", {"passage_id": pid, "text": "Edited"})
    p = await generate(store, p, [pid])
    p = store.command(p["id"], "undo", p["revision"], "undo", {})
    assert p["document"]["passages"][0]["selected_take_id"] is None
    assert p["document"]["passages"][0]["text"] == "Edited"
    assert len(p["takes"]) == 1


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("update_passage", {"direction": {"crossfade_ms": 10001}}),
        ("update_passage", {"direction": {"max_chunk_chars": 100001}}),
        ("update_mix", {"target_lufs": 0}),
        ("update_mix", {"max_true_peak_dbtp": 2}),
    ],
)
def test_committed_inputs_are_accepted_by_archive_validation(tmp_path, kind, payload):
    store, p = setup(tmp_path)
    if kind == "update_passage":
        payload = {**payload, "passage_id": p["document"]["passages"][0]["id"]}
    with pytest.raises(ValueError):
        store.command(p["id"], "invalid", 0, kind, payload)
    assert store.get_project(p["id"])["revision"] == 0


@pytest.mark.asyncio
async def test_incomplete_narration_cannot_be_queued_for_delivery(tmp_path):
    from backend.studio.delivery import StudioDelivery

    store, p = setup(tmp_path, "One\n\nTwo")
    pid = p["document"]["passages"][0]["id"]
    p = await generate(store, p, [pid])
    p = store.command(p["id"], "insert", p["revision"], "insert_passages", {"passage_ids": [pid]})
    p = store.checkpoint(p["id"], "checkpoint", p["revision"], "Incomplete")
    scheduled = []
    delivery = StudioDelivery(store, schedule=lambda coro: scheduled.append(coro))
    try:
        with pytest.raises(ValueError, match="narration"):
            delivery.submit(p["id"], "render", p["revisions"][0]["id"], "mix", "wav")
    finally:
        for coro in scheduled:
            coro.close()


def test_archive_identifier_remapping_preserves_user_strings(tmp_path):
    from backend.tests.test_studio_archive_validation import fixture_project
    from backend.studio.archive import write_archive
    from backend.studio.portability import import_archive

    project, asset, source = fixture_project(tmp_path)
    project["id"] = "Hello"
    project["name"] = "Hello"
    project["takes"][0]["engine"] = "Hello"
    archive = tmp_path / "archive.zip"
    write_archive(project, [asset], lambda _: source, archive)
    imported = import_archive(StudioStore(tmp_path / "clean"), archive)
    assert imported["id"] != "Hello"
    assert imported["name"] == "Hello"
    assert imported["document"]["passages"][0]["text"] == "Hello"
    assert imported["takes"][0]["text"] == "Hello" and imported["takes"][0]["engine"] == "Hello"


@pytest.mark.asyncio
async def test_cast_passages_are_inserted_on_separate_voice_tracks(tmp_path):
    store, p = setup(tmp_path, "One\n\nTwo")
    ids = [x["id"] for x in p["document"]["passages"]]
    for index, pid in enumerate(ids):
        p = store.command(
            p["id"],
            f"cast{index}",
            p["revision"],
            "assign_voice",
            {"passage_ids": [pid], "profile_id": f"voice{index}"},
        )
    p = await generate(store, p, ids)
    p = store.command(p["id"], "insert", p["revision"], "insert_passages", {"passage_ids": ids})
    assert len({c["track_id"] for c in p["document"]["clips"]}) == 2


def test_uploaded_audio_can_be_inserted_as_an_undoable_clip(tmp_path):
    store, p = setup(tmp_path, script="")
    source = tmp_path / "music.wav"
    sf.write(source, np.full(4800, 0.1), 48000)
    asset = store.add_asset(p["id"], source)
    p = store.command(
        p["id"],
        "insert",
        0,
        "insert_asset",
        {"asset_id": asset["id"], "start_sample": 24000, "kind": "music", "name": "Bed"},
    )
    assert p["document"]["clips"][0]["asset_id"] == asset["id"]
    assert p["document"]["clips"][0]["start_sample"] == 24000
    p = store.command(p["id"], "undo", p["revision"], "undo", {})
    assert p["document"]["clips"] == [] and len(p["assets"]) == 1


@pytest.mark.asyncio
async def test_earlier_recording_acknowledgement_survives_delivery_and_archive(tmp_path):
    from backend.studio.delivery import StudioDelivery
    from backend.studio.archive import write_archive
    from backend.studio.portability import import_archive

    store, p = setup(tmp_path / "studio")
    pid = p["document"]["passages"][0]["id"]
    p = await generate(store, p, [pid])
    p = store.command(p["id"], "insert", p["revision"], "insert_passages", {"passage_ids": [pid]})
    p = store.command(p["id"], "edit", p["revision"], "update_passage", {"passage_id": pid, "text": "New words"})
    p = store.checkpoint(p["id"], "checkpoint", p["revision"], "Earlier recording")
    work = []
    delivery = StudioDelivery(store, schedule=lambda coro: work.append(coro))
    with pytest.raises(ValueError, match="Stale"):
        delivery.submit(p["id"], "blocked", p["revisions"][0]["id"], "mix", "wav")
    delivery.submit(p["id"], "accepted", p["revisions"][0]["id"], "mix", "wav", allow_stale_audio=True)
    await work.pop()
    p = store.get_project(p["id"])
    recording = p["recording_acknowledgements"][0]["recordings"][0]
    assert (recording["text"], recording["script_text"], recording["stale"]) == ("One", "New words", True)
    assert p["deliverables"][0]["recordings"][0]["text"] == "One"
    assert p["deliverables"][0]["filename"].startswith("Review-Earlier-recording-")
    archive = tmp_path / "earlier.zip"
    write_archive(p, p["assets"], store.asset_path, archive)
    restored = import_archive(StudioStore(tmp_path / "clean"), archive)
    ack = restored["recording_acknowledgements"][0]
    assert ack["revision_id"] == restored["revisions"][0]["id"]
    assert ack["recordings"][0]["take_id"] == restored["takes"][0]["id"]
    assert ack["recordings"][0]["text"] == "One"


@pytest.mark.asyncio
async def test_replacing_linked_clip_routes_the_new_frozen_voice_atomically(tmp_path):
    store, p = setup(tmp_path, "One\n\nTwo")
    ids = [passage["id"] for passage in p["document"]["passages"]]
    p = store.command(p["id"], "alice", p["revision"], "assign_voice", {"passage_ids": ids, "profile_id": "alice"})
    p = await generate(store, p, ids)
    p = store.command(p["id"], "insert", p["revision"], "insert_passages", {"passage_ids": ids})
    original = {c["passage_id"]: dict(c) for c in p["document"]["clips"]}
    p = store.command(p["id"], "bob", p["revision"], "assign_voice", {"passage_ids": [ids[0]], "profile_id": "bob"})
    pending = []

    async def synth(request):
        return np.full(100, 0.2, dtype="float32"), 1000

    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: pending.append(coro)
    )
    jobs.submit(p["id"], "bob-take", [ids[0]], 1, True)
    await pending.pop()
    p = store.get_project(p["id"])
    new_take = p["document"]["passages"][0]["selected_take_id"]
    p = store.command(
        p["id"],
        "replace",
        p["revision"],
        "select_take",
        {"passage_id": ids[0], "take_id": new_take, "replace_clips": True},
    )
    clips = {c["passage_id"]: c for c in p["document"]["clips"]}
    assert clips[ids[0]]["track_id"] != clips[ids[1]]["track_id"]
    assert clips[ids[0]]["start_sample"] == original[ids[0]]["start_sample"]
    assert clips[ids[1]] == original[ids[1]]
    p = store.command(p["id"], "undo-replace", p["revision"], "undo", {})
    assert p["document"]["clips"] == list(original.values())
