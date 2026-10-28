"""Imports publish verified audio together with their project transaction."""

import sqlite3
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database.models import Base, Generation, Story, StoryItem
from backend.studio.archive import write_archive
from backend.studio.portability import import_archive
from backend.studio.storage import StudioStore
from backend.studio.story import import_story


@pytest.fixture(params=["story", "archive"])
def imported_audio(request, tmp_path):
    source = tmp_path / "original.wav"
    sf.write(source, np.full(2400, 0.1), 24000)
    store = StudioStore(tmp_path / "destination")
    existing = store.create_project("Existing")
    asset = store.add_asset(existing["id"], source)
    legacy = None
    if request.param == "archive":
        author = StudioStore(tmp_path / "author")
        project = author.create_project("Imported")
        author.add_asset(project["id"], source)
        author.add_asset(project["id"], source)
        project = author.get_project(project["id"])
        archive = tmp_path / "project.zip"
        write_archive(project, project["assets"], author.asset_path, archive)

        def import_project():
            return import_archive(store, archive)
    else:
        legacy = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(legacy)
        with Session(legacy) as db:
            db.add_all(
                [
                    Story(id="story", name="Imported"),
                    Generation(id="generation", profile_id="profile", text="Words", audio_path=str(source)),
                    StoryItem(id="item", story_id="story", generation_id="generation", start_time_ms=0),
                    StoryItem(id="second", story_id="story", generation_id="generation", start_time_ms=100),
                ]
            )
            db.commit()

        def import_project():
            with Session(legacy) as db:
                return import_story(store, "story", db)

    yield store, source, existing, asset, import_project
    if legacy is not None:
        legacy.dispose()


def affect_owned_writes(monkeypatch, store, write):
    original_open = Path.open

    class Writer:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, block):
            return write(self.file, block)

    def open_file(path, mode="r", *args, **kwargs):
        file = original_open(path, mode, *args, **kwargs)
        return Writer(file) if path.parent == store.asset_dir and "x" in mode else file

    monkeypatch.setattr(Path, "open", open_file)


def assert_original_preserved(store, source, project, asset):
    assert [item["id"] for item in store.list_projects()] == [project["id"]]
    assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
    assert sorted(path.name for path in store.asset_dir.iterdir()) == [asset["id"]]


def test_interrupted_copy_leaves_no_partial_published_audio(imported_audio, monkeypatch):
    store, source, project, asset, import_project = imported_audio

    def interrupted(file, block):
        file.write(block[:64])
        raise OSError("copy interrupted")

    affect_owned_writes(monkeypatch, store, interrupted)
    with pytest.raises(OSError, match="copy interrupted"):
        import_project()
    assert_original_preserved(store, source, project, asset)


def test_changed_copy_is_rejected_before_import_publication(imported_audio, monkeypatch):
    store, source, project, asset, import_project = imported_audio

    def corrupted(file, block):
        return file.write(block[:-1] + bytes([block[-1] ^ 1]))

    affect_owned_writes(monkeypatch, store, corrupted)
    with pytest.raises(ValueError, match="hash"):
        import_project()
    assert_original_preserved(store, source, project, asset)


def test_later_asset_registration_failure_rolls_back_the_whole_import(imported_audio):
    store, source, project, asset, import_project = imported_audio
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_later_asset BEFORE INSERT ON assets "
            "WHEN (SELECT COUNT(*) FROM assets)>1 "
            "BEGIN SELECT RAISE(ABORT, 'no registration'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="no registration"):
        import_project()
    assert_original_preserved(store, source, project, asset)


def test_uncertain_commit_keeps_audio_that_was_actually_registered(imported_audio, monkeypatch):
    store, source, _, _, import_project = imported_audio
    connect = store.connect

    class UncertainCommit:
        def __init__(self):
            self.db = connect()

        def __enter__(self):
            return self.db.__enter__()

        def __exit__(self, kind, error, traceback):
            self.db.__exit__(kind, error, traceback)
            if kind is None:
                raise OSError("commit acknowledgement lost")

    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", UncertainCommit)
        with pytest.raises(OSError, match="acknowledgement lost"):
            import_project()
    assert len(store.list_projects()) == 2
    imported = next(item for item in store.list_projects() if item["name"] == "Imported")
    assets = store.get_project(imported["id"])["assets"]
    assert len(assets) == 2
    for asset in assets:
        assert store.asset_path(asset["id"]).read_bytes() == source.read_bytes()
