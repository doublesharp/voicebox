"""Phrase edits publish new performances without mutating their source takes."""

import hashlib

import numpy as np
import pytest
import soundfile as sf

from backend.studio.storage import Conflict, NotFound, StudioStore, encode, now


def setup_project(tmp_path):
    store = StudioStore(tmp_path / "studio")
    project = store.create_project("Repairs", script="The correct name is Ada.")
    passage = project["document"]["passages"][0]
    for ident, signal, rate in (
        ("base", np.full(48000, 0.2), 48000),
        ("phrase", np.full(12000, -0.4), 24000),
    ):
        path = tmp_path / f"{ident}.wav"
        sf.write(path, signal, rate, subtype="FLOAT")
        asset = store.add_asset(project["id"], path)
        with store.connect() as db:
            db.execute(
                "INSERT INTO takes VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    project["id"],
                    passage["id"],
                    asset["id"],
                    passage["text"],
                    "test",
                    None,
                    now(),
                    encode({"passage": passage}),
                ),
            )
    project = store.command(
        project["id"],
        "select",
        0,
        "select_take",
        {
            "passage_id": passage["id"],
            "take_id": "base",
        },
    )
    project = store.command(
        project["id"],
        "place",
        1,
        "insert_passages",
        {
            "passage_ids": [passage["id"]],
        },
    )
    return store, project, passage["id"]


def repair_payload(passage_id, crossfade=0):
    return {
        "passage_id": passage_id,
        "text": "The correct name is Ada.",
        "crossfade_samples": crossfade,
        "segments": [
            {"take_id": "base", "start_sample": 0, "end_sample": 12000},
            {"take_id": "phrase", "start_sample": 0, "end_sample": 24000},
            {"take_id": "base", "start_sample": 36000, "end_sample": 48000},
        ],
    }


def test_splice_publishes_new_take_and_preserves_original_selection_timeline_and_sources(tmp_path):
    store, before, pid = setup_project(tmp_path)
    hashes = {a["id"]: hashlib.sha256(store.asset_path(a["id"]).read_bytes()).hexdigest() for a in before["assets"]}
    result = store.command(before["id"], "repair", before["revision"], "compose_take", repair_payload(pid))
    assert len(result["takes"]) == 3
    composed = next(t for t in result["takes"] if t["id"] not in ("base", "phrase"))
    audio, rate = sf.read(store.asset_path(composed["asset_id"]), always_2d=True)
    assert (rate, audio.shape) == (48000, (48000, 2))
    np.testing.assert_allclose(audio[:12000], 0.2, atol=1e-6)
    # Polyphase interpolation has small pass-band ripple around the DC level.
    np.testing.assert_allclose(audio[12500:35500], -0.4, atol=3e-4)
    np.testing.assert_allclose(audio[36000:], 0.2, atol=1e-6)
    assert result["document"] == before["document"]
    assert composed["request"]["composition"]["segments"][1]["source_hash"]
    assert composed["text"] == before["document"]["passages"][0]["text"]
    for asset_id, digest in hashes.items():
        assert hashlib.sha256(store.asset_path(asset_id).read_bytes()).hexdigest() == digest
    # A lost response can be replayed without publishing another take.
    assert store.command(before["id"], "repair", before["revision"], "compose_take", repair_payload(pid)) == result
    assert len(store.get_project(before["id"])["takes"]) == 3


def test_comp_crossfades_each_join_and_undo_keeps_the_alternative(tmp_path):
    store, project, pid = setup_project(tmp_path)
    result = store.command(project["id"], "comp", project["revision"], "compose_take", repair_payload(pid, 240))
    # Query by identity instead of relying on record ordering.
    take = next(t for t in result["takes"] if t["id"] not in ("base", "phrase"))
    audio, rate = sf.read(store.asset_path(take["asset_id"]), always_2d=True)
    assert len(audio) == 48000 - 2 * 240
    assert np.max(np.abs(np.diff(audio[:, 0]))) < 0.01
    chosen = store.command(
        project["id"],
        "use-comp",
        result["revision"],
        "select_take",
        {
            "passage_id": pid,
            "take_id": take["id"],
            "replace_clips": True,
        },
    )
    restored = store.command(project["id"], "undo", chosen["revision"], "undo", {})
    assert restored["document"]["passages"][0]["selected_take_id"] == "base"
    assert restored["document"]["clips"][0]["take_id"] == "base"
    assert len(restored["takes"]) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"segments": []},
        {"segments": [{"take_id": [], "start_sample": 0, "end_sample": 5}]},
        {"segments": [{"take_id": "base", "start_sample": -1, "end_sample": 5}]},
        {"segments": [{"take_id": "base", "start_sample": 0, "end_sample": 48001}]},
        {"segments": [{"take_id": "base", "start_sample": 1, "end_sample": 1}]},
        {"crossfade_samples": True},
        {"crossfade_samples": 10000},
        {"text": ""},
    ],
)
def test_invalid_repairs_publish_nothing(tmp_path, change):
    store, project, pid = setup_project(tmp_path)
    files = set(store.asset_dir.iterdir())
    with pytest.raises(ValueError, match="Choose|phrase|source|Phrase|Crossfade|transcript|take"):
        store.command(project["id"], "bad", project["revision"], "compose_take", {**repair_payload(pid), **change})
    assert len(store.get_project(project["id"])["takes"]) == 2
    assert set(store.asset_dir.iterdir()) == files


def test_locked_stale_or_foreign_take_cannot_be_composed(tmp_path):
    store, project, pid = setup_project(tmp_path)
    other = store.create_project("Other", script="Other voice")
    with pytest.raises(NotFound):
        store.command(other["id"], "foreign", 0, "compose_take", repair_payload(other["document"]["passages"][0]["id"]))
    locked = store.command(project["id"], "lock", project["revision"], "set_lock", {"passage_id": pid, "locked": True})
    with pytest.raises(Conflict):
        store.command(project["id"], "stale", project["revision"], "compose_take", repair_payload(pid))
    with pytest.raises(ValueError, match="locked"):
        store.command(project["id"], "locked", locked["revision"], "compose_take", repair_payload(pid))


def test_late_command_failure_discards_the_uncommitted_audio(tmp_path, monkeypatch):
    store, project, pid = setup_project(tmp_path)
    files = set(store.asset_dir.iterdir())

    def fail(*args, **kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(store, "_record", fail)
    with pytest.raises(RuntimeError, match="disk failure"):
        store.command(project["id"], "repair", project["revision"], "compose_take", repair_payload(pid))
    assert len(store.get_project(project["id"])["takes"]) == 2
    assert set(store.asset_dir.iterdir()) == files


@pytest.mark.parametrize("take_scope", ["all", "used"])
def test_composed_provenance_survives_archive_import_and_reexport(tmp_path, take_scope):
    from backend.studio.archive import write_archive
    from backend.studio.archive_scope import required_references
    from backend.studio.portability import import_archive

    store, project, pid = setup_project(tmp_path)
    project = store.command(project["id"], "comp", project["revision"], "compose_take", repair_payload(pid))
    take = next(t for t in project["takes"] if t["id"] not in ("base", "phrase"))
    project = store.command(
        project["id"],
        "use",
        project["revision"],
        "select_take",
        {
            "passage_id": pid,
            "take_id": take["id"],
            "replace_clips": True,
        },
    )
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    write_archive(project, project["assets"], store.asset_path, first, take_scope=take_scope)
    imported = import_archive(store, first)
    assert required_references(imported) <= {a["id"] for a in imported["assets"]}
    write_archive(imported, imported["assets"], store.asset_path, second, take_scope=take_scope)
    reopened = import_archive(store, second)
    assert len(reopened["document"]["clips"]) == 1
