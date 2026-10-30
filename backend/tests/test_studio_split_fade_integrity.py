"""Splitting a clip must preserve the sound of the timeline."""

import numpy as np
import pytest
import soundfile as sf

from backend.studio.rendering import render_project
from backend.studio.storage import StudioStore


def render(store, project, output):
    assets = {asset["id"]: asset for asset in project["assets"]}
    result = render_project(project["document"], assets, store.asset_path, output, processing="dry")
    samples, rate = sf.read(result["files"][0]["path"], always_2d=True)
    assert rate == 48000
    return samples


@pytest.mark.parametrize("fade_in,fade_out,split", [
    (0, 0, 25),
    (80, 0, 25),
    (0, 80, 75),
    (80, 80, 50),
])
def test_splitting_preserves_rendered_audio(tmp_path, monkeypatch, fade_in, fade_out, split):
    from backend.studio import rendering

    monkeypatch.setattr(rendering, "_measure", lambda *args, **kwargs: {"status": "unavailable"})
    store = StudioStore(tmp_path / "studio")
    project = store.create_project("Narration")
    source = tmp_path / "source.wav"
    sf.write(source, np.full(100, 0.25), 48000, subtype="FLOAT")
    asset = store.add_asset(project["id"], source)
    project = store.command(project["id"], "insert", 0, "insert_asset", {"asset_id": asset["id"]})
    clip = project["document"]["clips"][0]
    project = store.command(project["id"], "fades", 1, "update_clip", {
        "clip_id": clip["id"], "fade_in_samples": fade_in, "fade_out_samples": fade_out,
    })
    before = render(store, project, tmp_path / "before")
    project = store.command(project["id"], "split", 2, "split_clip", {
        "clip_id": clip["id"], "at_sample": split,
    })
    after = render(store, project, tmp_path / "after")
    assert len(project["document"]["clips"]) == 2
    np.testing.assert_allclose(after, before, atol=1e-6)


def test_repeated_splits_undo_redo_and_noop_edits_preserve_audio(tmp_path, monkeypatch):
    from backend.studio import rendering

    monkeypatch.setattr(rendering, "_measure", lambda *args, **kwargs: {"status": "unavailable"})
    store = StudioStore(tmp_path / "studio")
    project = store.create_project("Narration")
    source = tmp_path / "source.wav"
    sf.write(source, np.linspace(0.1, 0.5, 150), 48000, subtype="FLOAT")
    asset = store.add_asset(project["id"], source)

    def command(kind, payload):
        nonlocal project
        project = store.command(project["id"], f"op-{project['revision']}", project["revision"], kind, payload)

    command("insert_asset", {"asset_id": asset["id"]})
    clip_id = project["document"]["clips"][0]["id"]
    command("update_clip", {"clip_id": clip_id, "start_sample": 10, "trim_start_sample": 20,
                            "trim_end_sample": 10, "fade_in_samples": 100, "fade_out_samples": 100})
    before = render(store, project, tmp_path / "before")
    command("split_clip", {"clip_id": clip_id, "at_sample": 50})
    right_id = next(c["id"] for c in project["document"]["clips"] if c["id"] != clip_id)
    command("split_clip", {"clip_id": right_id, "at_sample": 90})
    np.testing.assert_allclose(render(store, project, tmp_path / "split"), before, atol=1e-6)
    command("undo", {})
    command("redo", {})
    np.testing.assert_allclose(render(store, project, tmp_path / "redo"), before, atol=1e-6)
    middle = next(c for c in project["document"]["clips"] if c["id"] == right_id)
    command("update_clip", {"clip_id": right_id, "trim_start_sample": middle["trim_start_sample"],
                            "fade_in_samples": middle["fade_in_samples"]})
    np.testing.assert_allclose(render(store, project, tmp_path / "noop"), before, atol=1e-6)
    from backend.studio.archive import write_archive
    from backend.studio.portability import import_archive

    archive = tmp_path / "project.zip"
    write_archive(project, project["assets"], store.asset_path, archive)
    restored_store = StudioStore(tmp_path / "restored")
    restored = import_archive(restored_store, archive)
    np.testing.assert_allclose(render(restored_store, restored, tmp_path / "restored-render"), before, atol=1e-6)


@pytest.mark.parametrize("metadata", [
    {"fade_in_total_samples": 20},
    {"fade_in_offset_samples": 5},
    {"fade_in_total_samples": 20, "fade_in_offset_samples": -1},
    {"fade_in_total_samples": 20, "fade_in_offset_samples": 20},
    {"fade_in_total_samples": 101, "fade_in_offset_samples": 1},
    {"fade_in_total_samples": 20, "fade_in_offset_samples": 5},
])
def test_malformed_fade_envelopes_are_rejected(metadata):
    from backend.studio.fades import fade_envelopes

    clip = {"fade_in_samples": 10, "fade_out_samples": 0, **metadata}
    with pytest.raises(ValueError, match="Invalid clip fade envelope"):
        fade_envelopes(clip, 50, 100)
