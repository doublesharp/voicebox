"""Local recognition remains advisory and tied to the exact immutable source."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services import transcribe
from backend.studio.api import make_router
from backend.tests.test_studio_phrase_repairs import setup_project


def test_recognition_proposal_is_reviewable_and_does_not_adopt_or_rewrite(tmp_path, monkeypatch):
    store, project, _ = setup_project(tmp_path)

    async def recognize(*args, **kwargs):
        return {
            "text": "The correct name is Eva.",
            "words": [
                {"word": "The", "start": 0.0, "end": 0.1},
                {"word": "correct", "start": 0.1, "end": 0.3},
                {"word": "name", "start": 0.3, "end": 0.5},
                {"word": "is", "start": 0.5, "end": 0.6},
                {"word": "Eva.", "start": 0.6, "end": 0.9},
            ],
            "recognizer": {"backend": "fixture", "model_size": "base", "revision": None},
        }

    monkeypatch.setattr(transcribe, "recognize_words", recognize, raising=False)
    app = FastAPI()
    app.include_router(make_router(store, jobs=object()))
    response = TestClient(app).post(
        f"/studio/v1/projects/{project['id']}/speech-review",
        json={"take_id": "base", "expected_revision": project["revision"]},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["advisory"] is True
    assert report["differences"][0]["kind"] == "substitution"
    assert report["differences"][0]["expected"] == "Ada"
    assert report["words"][-1]["word"] == "Eva."
    assert store.get_project(project["id"]) == project


def proposal(store, project):
    from backend.studio.speech_review import compare_speech, review_key, source_identity

    source, _ = source_identity(project, "base")
    words = [
        {"word": "The", "start": 0.0, "end": 0.1},
        {"word": "correct", "start": 0.1, "end": 0.3},
        {"word": "name", "start": 0.3, "end": 0.5},
        {"word": "is", "start": 0.5, "end": 0.6},
        {"word": "Eva.", "start": 0.6, "end": 0.9},
    ]
    text = "The correct name is Eva."
    return {
        "version": 1,
        "advisory": True,
        "source": source,
        "source_key": review_key(source),
        "recognized_text": text,
        "words": words,
        "differences": compare_speech(source["script_text"], text, words),
        "recognizer": {"backend": "fixture", "model_size": "base", "revision": None},
    }


def test_saved_review_survives_reopen_maps_word_captions_and_rejects_stale_script(tmp_path):
    import copy

    import pytest

    from backend.studio.storage import Conflict, StudioStore

    store, before, passage_id = setup_project(tmp_path)
    report = proposal(store, before)
    saved = store.command(
        before["id"], "save-review", before["revision"], "set_speech_review", {"take_id": "base", "report": report}
    )
    assert saved["document"]["speech_reviews"] == [report]
    assert StudioStore(store.root).get_project(before["id"])["document"]["speech_reviews"] == [report]
    assert saved["document"]["passages"] == before["document"]["passages"]
    assert saved["document"]["clips"] == before["document"]["clips"]
    captioned = store.command(before["id"], "recognized-captions", saved["revision"], "recognize_captions", {})
    captions = captioned["document"]["captions"]
    assert captions["reviewed"] is False
    assert [cue["text"] for cue in captions["cues"]] == ["The", "correct", "name", "is", "Eva."]
    assert captions["cues"][-1]["start_sample"] == 28800
    # A repeated save replaces the same source report, never duplicates history.
    saved2 = store.command(
        before["id"],
        "save-review-again",
        captioned["revision"],
        "set_speech_review",
        {"take_id": "base", "report": report},
    )
    assert len(saved2["document"]["speech_reviews"]) == 1
    changed = store.command(
        before["id"],
        "change-script",
        saved2["revision"],
        "update_passage",
        {"passage_id": passage_id, "text": "A different current script."},
    )
    with pytest.raises(Conflict, match="changed"):
        store.command(
            before["id"],
            "stale-review",
            changed["revision"],
            "set_speech_review",
            {"take_id": "base", "report": report},
        )
    with pytest.raises(ValueError, match="current narration"):
        store.command(before["id"], "stale-words", changed["revision"], "recognize_captions", {})
    forged = copy.deepcopy(report)
    forged["differences"][0]["expected"] = "a forged script"
    with pytest.raises(ValueError, match="differences"):
        store.command(
            before["id"],
            "forged-review",
            changed["revision"],
            "set_speech_review",
            {"take_id": "base", "report": forged},
        )


def test_recognized_caption_mapping_keeps_only_complete_words_in_audible_trimmed_clips(tmp_path):
    import copy

    import pytest

    from backend.studio.captions import recognized_captions

    store, project, _ = setup_project(tmp_path)
    doc = copy.deepcopy(project["document"])
    doc["speech_reviews"] = [proposal(store, project)]
    doc["clips"][0].update(start_sample=48000, trim_start_sample=14400, trim_end_sample=9600)
    cues = recognized_captions({**project, "document": doc})["cues"]
    assert [cue["text"] for cue in cues] == ["name", "is"]
    assert cues[0]["start_sample"] == 48000
    doc["tracks"][0]["mute"] = True
    with pytest.raises(ValueError, match="No complete"):
        recognized_captions({**project, "document": doc})
    doc["tracks"][0]["mute"] = False
    doc["clips"][0]["take_id"] = None
    with pytest.raises(ValueError, match="reviewed take"):
        recognized_captions({**project, "document": doc})


def test_diff_distinguishes_omissions_repetitions_extra_words_and_unicode():
    from backend.studio.speech_review import compare_speech

    assert compare_speech("Ada has apples", "Ada apples", [])[0]["kind"] == "omission"
    assert compare_speech("Ada has apples", "Ada has has apples", [])[0]["kind"] == "repetition"
    assert compare_speech("Ada has apples", "Ada has ripe apples", [])[0]["kind"] == "extra"
    assert compare_speech("你好\uff0c世界\uff01", "你好世界", []) == []
    assert compare_speech("HELLO Ada!", "hello ada", []) == []


def test_route_rejects_stale_foreign_corrupt_and_changed_during_recognition(tmp_path, monkeypatch):
    store, project, pid = setup_project(tmp_path)
    app = FastAPI()
    app.include_router(make_router(store, jobs=object()))
    client = TestClient(app)
    path = f"/studio/v1/projects/{project['id']}/speech-review"
    called = []

    async def recognize(*args, **kwargs):
        called.append(True)
        store.command(
            project["id"],
            "during-recognition",
            project["revision"],
            "update_passage",
            {"passage_id": pid, "text": "Changed while running"},
        )
        return {"text": "", "words": [], "recognizer": {"backend": "fixture"}}

    monkeypatch.setattr(transcribe, "recognize_words", recognize)
    assert client.post(path, json={"take_id": "base", "expected_revision": 0}).status_code == 409
    assert client.post(path, json={"take_id": "foreign", "expected_revision": project["revision"]}).status_code == 404
    assert not called
    assert client.post(path, json={"take_id": "base", "expected_revision": project["revision"]}).status_code == 409
    current = store.get_project(project["id"])
    assert not current["document"].get("speech_reviews")
    store.asset_path(project["takes"][0]["asset_id"]).write_bytes(b"changed")
    assert client.post(path, json={"take_id": "base", "expected_revision": current["revision"]}).status_code == 409


def test_report_validation_refuses_malformed_provenance_words_and_history(tmp_path):
    import copy

    import pytest

    from backend.studio.speech_review import validate_report, validate_reviews

    store, project, _ = setup_project(tmp_path)
    report = proposal(store, project)
    for change in (
        {"version": 2},
        {"advisory": False},
        {"source_key": "0" * 64},
        {"recognizer": {}},
        {"recognized_text": "x" * 10001},
        {"words": [{"word": "bad", "start": float("nan"), "end": 0.5}]},
    ):
        with pytest.raises(ValueError, match="Invalid|exceeds"):
            validate_report({**report, **change})
    bad = copy.deepcopy(report)
    bad["source"]["asset_hash"] = "../other"
    with pytest.raises(ValueError, match="provenance"):
        validate_report(bad)
    with pytest.raises(ValueError, match="history"):
        validate_reviews({"speech_reviews": [report] * 17})


def test_local_recognition_errors_remain_actionable_without_a_persisted_result(tmp_path, monkeypatch):
    store, project, _ = setup_project(tmp_path)
    app = FastAPI()
    app.include_router(make_router(store, jobs=object()))
    client = TestClient(app)
    path = f"/studio/v1/projects/{project['id']}/speech-review"

    async def unavailable(*args, **kwargs):
        raise ValueError("Download the local model first")

    monkeypatch.setattr(transcribe, "recognize_words", unavailable)
    response = client.post(path, json={"take_id": "base", "expected_revision": project["revision"]})
    assert response.status_code == 422
    assert "Download" in response.json()["detail"]

    async def failed(*args, **kwargs):
        raise RuntimeError("native decoding failed")

    monkeypatch.setattr(transcribe, "recognize_words", failed)
    assert client.post(path, json={"take_id": "base", "expected_revision": project["revision"]}).status_code == 503
    assert not store.get_project(project["id"])["document"].get("speech_reviews")
    with store.connect() as db:
        db.execute("UPDATE assets SET duration=121 WHERE id=?", (project["takes"][0]["asset_id"],))
    assert client.post(path, json={"take_id": "base", "expected_revision": project["revision"]}).status_code == 422


def test_invalid_source_whitespace_and_unbounded_comparison_are_refused(tmp_path):
    import copy

    import pytest

    from backend.studio.speech_review import compare_speech, source_identity, validate_report
    from backend.studio.storage import NotFound

    store, project, _ = setup_project(tmp_path)
    with pytest.raises(NotFound, match="unavailable"):
        source_identity({**project, "assets": []}, "base")
    with pytest.raises(ValueError, match="2000"):
        compare_speech("word " * 2001, "word", [])
    report = proposal(store, project)
    changed = copy.deepcopy(report)
    changed["recognized_text"] = " " + changed["recognized_text"]
    with pytest.raises(ValueError, match="normalized"):
        validate_report(changed)
    with pytest.raises(ValueError, match="Expected take"):
        store.command(project["id"], "missing-report", project["revision"], "set_speech_review", {})
    with pytest.raises(ValueError, match="no payload"):
        store.command(
            project["id"], "bad-caption-payload", project["revision"], "recognize_captions", {"unknown": True}
        )


def test_speech_review_provenance_is_preserved_in_portable_archives(tmp_path):
    from backend.studio.archive import write_archive
    from backend.studio.portability import import_archive
    from backend.studio.speech_review import review_key, source_identity
    from backend.studio.storage import StudioStore

    store, project, _ = setup_project(tmp_path)
    from backend.studio.storage import encode

    # Replacement-phrase takes retain an original passage ID in provenance.
    with store.connect() as db:
        take = next(item for item in project["takes"] if item["id"] == "base")
        request = {**take["request"], "phrase_repair": {"source_passage_id": take["passage_id"]}}
        db.execute("UPDATE takes SET request=? WHERE id=?", (encode(request), "base"))
    project = store.get_project(project["id"])
    report = proposal(store, project)
    project = store.command(
        project["id"],
        "save-portable-review",
        project["revision"],
        "set_speech_review",
        {"take_id": "base", "report": report},
    )
    archive = tmp_path / "portable.zip"
    write_archive(project, project["assets"], store.asset_path, archive)
    imported = import_archive(StudioStore(tmp_path / "restored"), archive)
    assert imported["document"]["speech_reviews"] == [report]
    imported_base = next(
        take for take in imported["takes"] if take["asset_id"] == imported["document"]["clips"][0]["asset_id"]
    )
    assert review_key(source_identity(imported, imported_base["id"])[0]) == report["source_key"]
