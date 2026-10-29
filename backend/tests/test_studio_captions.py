"""Caption export stays tied to the audible, edited Studio arrangement."""

import copy

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.storage import Conflict, StudioStore, uid


def test_caption_status_is_available_for_existing_projects(tmp_path):
    store = StudioStore(tmp_path / "studio")
    project = store.create_project("A film", script="A short introduction")
    app = FastAPI()
    app.include_router(make_router(store, jobs=object()))
    response = TestClient(app).get(f"/studio/v1/projects/{project['id']}/captions")
    assert response.status_code == 200
    assert response.json()["exportable"] is False


def test_caption_commands_persist_export_review_and_guard_changed_audio(tmp_path):
    store = StudioStore(tmp_path / "studio")
    project = store.create_project("A film", script="An introduction")
    source = tmp_path / "speech.wav"
    sf.write(source, np.full(96000, 0.2), 48000)
    asset = store.add_asset(project["id"], source)
    project = store.command(project["id"], uid(), 0, "insert_asset", {"asset_id": asset["id"], "kind": "voice"})
    app = FastAPI()
    app.include_router(make_router(store, jobs=object()))
    client = TestClient(app)
    path = f"/studio/v1/projects/{project['id']}"
    fingerprint = client.get(path + "/captions").json()["source_fingerprint"]
    caption_data = {
        "source_fingerprint": fingerprint,
        "reviewed": True,
        "cues": [
            {
                "id": uid(),
                "clip_id": project["document"]["clips"][0]["id"],
                "start_sample": 0,
                "end_sample": 96000,
                "text": "An introduction",
            }
        ],
    }
    saved = client.post(
        path + "/commands",
        json={
            "operation_id": uid(),
            "expected_revision": project["revision"],
            "type": "set_captions",
            "payload": caption_data,
        },
    )
    assert saved.status_code == 200
    assert StudioStore(store.root).get_project(project["id"])["document"]["captions"] == caption_data
    assert (
        client.get(path + "/captions/export", params={"format": "vtt", "source_fingerprint": "0" * 64}).status_code
        == 409
    )
    exported = client.get(path + "/captions/export", params={"format": "vtt", "source_fingerprint": fingerprint})
    assert exported.status_code == 200
    assert exported.text == "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nAn introduction\n"
    changed = client.post(
        path + "/commands",
        json={
            "operation_id": uid(),
            "expected_revision": saved.json()["revision"],
            "type": "update_clip",
            "payload": {"clip_id": project["document"]["clips"][0]["id"], "trim_start_sample": 48000},
        },
    )
    assert changed.status_code == 200
    assert client.get(path + "/captions").json()["exportable"] is False
    assert client.get(path + "/captions/export").status_code == 422
    restored = client.post(
        path + "/commands",
        json={"operation_id": uid(), "expected_revision": changed.json()["revision"], "type": "undo", "payload": {}},
    )
    assert restored.status_code == 200
    assert client.get(path + "/captions").json()["exportable"] is True


def arranged(tmp_path):
    store = StudioStore(tmp_path / "studio")
    project = store.create_project("A film", script="Hello <world> & friends")
    source = tmp_path / "speech.wav"
    sf.write(source, np.full(96000, 0.2), 48000)
    asset = store.add_asset(project["id"], source)
    project = store.command(
        project["id"], uid(), project["revision"], "insert_asset", {"asset_id": asset["id"], "kind": "voice"}
    )
    document = copy.deepcopy(project["document"])
    clip = document["clips"][0]
    clip.update(passage_id=document["passages"][0]["id"], start_sample=48000, trim_start_sample=24000)
    return store, project, document, {asset["id"]: asset}


def test_estimates_use_trimmed_audible_clips_and_require_review(tmp_path):
    from backend.studio.captions import caption_status, estimate_captions, export_captions

    _, _, doc, assets = arranged(tmp_path)
    doc["captions"] = estimate_captions(doc, assets, {})
    cue = doc["captions"]["cues"][0]
    assert (cue["start_sample"], cue["end_sample"]) == (48000, 120000)
    assert cue["text"] == "Hello <world> & friends"
    assert doc["captions"]["reviewed"] is False
    assert caption_status(doc, assets, {})["current"] is True
    with pytest.raises(ValueError, match="review"):
        export_captions(doc, assets, {}, "srt")
    doc["captions"]["reviewed"] = True
    assert (
        export_captions(doc, assets, {}, "srt")
        == "1\n00:00:01,000 --> 00:00:02,500\nHello &lt;world&gt; &amp; friends\n"
    )
    assert export_captions(doc, assets, {}, "vtt").startswith("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\n")


def test_fingerprint_rejects_audio_script_arrangement_and_mix_changes(tmp_path):
    from backend.studio.captions import caption_status, estimate_captions, export_captions

    _, _, doc, assets = arranged(tmp_path)
    doc["captions"] = {**estimate_captions(doc, assets, {}), "reviewed": True}
    for mutate in (
        lambda d: d["clips"][0].update(trim_start_sample=0),
        lambda d: d["clips"][0].update(start_sample=96000),
        lambda d: d["passages"][0].update(text="The script changed"),
        lambda d: d["tracks"][0].update(mute=True),
        lambda d: d["clips"][0].update(gain=0),
        lambda d: d["tracks"][0].update(effects_chain=[{"type": "Reverb", "enabled": True}]),
    ):
        changed = copy.deepcopy(doc)
        mutate(changed)
        assert caption_status(changed, assets, {})["current"] is False
        with pytest.raises(ValueError, match="changed"):
            export_captions(changed, assets, {}, "vtt")
    changed_assets = copy.deepcopy(assets)
    next(iter(changed_assets.values()))["hash"] = "0" * 64
    assert caption_status(doc, changed_assets, {})["current"] is False


def test_duplicate_clips_make_separate_cues_and_mute_solo_match_render(tmp_path):
    from backend.studio.captions import estimate_captions

    _, _, doc, assets = arranged(tmp_path)
    doc["clips"].append({**doc["clips"][0], "id": uid(), "start_sample": 180000})
    doc["tracks"].append({"id": "music", "name": "Music", "kind": "music", "mute": False, "solo": True, "gain": 1})
    assert estimate_captions(doc, assets, {})["cues"] == []
    doc["tracks"][-1]["mute"] = True
    assert len(estimate_captions(doc, assets, {})["cues"]) == 2


def test_manual_cues_have_strict_ranges_and_preserve_subtitle_boundaries(tmp_path):
    from backend.studio.captions import apply_caption_command, caption_status, estimate_captions

    store, project, doc, assets = arranged(tmp_path)
    doc["captions"] = estimate_captions(doc, assets, {})
    with store.connect() as db:
        payload = {**doc["captions"], "reviewed": True}
        payload["cues"] = [{**payload["cues"][0], "text": "A corrected phrase\nwith a second line"}]
        apply_caption_command(store, db, project["id"], doc, "set_captions", payload)
        assert caption_status(doc, assets, {})["exportable"] is True
        invalid = copy.deepcopy(payload)
        invalid["cues"][0]["end_sample"] = invalid["cues"][0]["start_sample"]
        with pytest.raises(ValueError, match="range"):
            apply_caption_command(store, db, project["id"], doc, "set_captions", invalid)
        invalid = copy.deepcopy(payload)
        invalid["cues"][0]["text"] = "Inject\n\n00:00:01 --> 00:00:02"
        with pytest.raises(ValueError, match="caption text"):
            apply_caption_command(store, db, project["id"], doc, "set_captions", invalid)
        with pytest.raises(Conflict, match="changed"):
            apply_caption_command(
                store, db, project["id"], doc, "set_captions", {**payload, "source_fingerprint": "a" * 64}
            )


def test_scene_targets_and_external_video_metadata_validate_and_undo(tmp_path):
    from backend.studio.captions import apply_caption_command, scene_timing

    store, project, doc, assets = arranged(tmp_path)
    pid = doc["passages"][0]["id"]
    with store.connect() as db:
        apply_caption_command(
            store,
            db,
            project["id"],
            doc,
            "set_scene_target",
            {"passage_id": pid, "start_sample": 48000, "end_sample": 96000},
        )
        apply_caption_command(
            store,
            db,
            project["id"],
            doc,
            "set_video_reference",
            {
                "reference": {
                    "name": "film.mp4",
                    "frame_rate": 23.976,
                    "time_origin_sample": 4800,
                    "duration_seconds": 10,
                }
            },
        )
        assert doc["video_reference"]["name"] == "film.mp4"
        status = scene_timing(doc, assets, {})[0]
        assert status["target_seconds"] == 1
        assert status["placed_seconds"] == 1.5
        assert status["delta_seconds"] == 0.5
        with pytest.raises(ValueError, match="frame rate"):
            apply_caption_command(
                store,
                db,
                project["id"],
                doc,
                "set_video_reference",
                {
                    "reference": {
                        "name": "film",
                        "frame_rate": float("nan"),
                        "time_origin_sample": 0,
                        "duration_seconds": None,
                    }
                },
            )
