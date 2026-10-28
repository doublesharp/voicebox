import hashlib
import zipfile
import pytest
import numpy as np
import soundfile as sf


def test_archive_roundtrip_and_integrity(tmp_path):
    from backend.studio.archive import write_archive, read_archive
    from backend.tests.test_studio_archive_validation import fixture_project

    project, asset, audio = fixture_project(tmp_path)
    archive = tmp_path / "project.zip"
    write_archive(project, [asset], lambda _: audio, archive)
    result = read_archive(archive, tmp_path / "stage")
    assert result["project"] == project
    assert open(result["assets"][0]["path"], "rb").read() == audio.read_bytes()


def test_archive_rejects_traversal_before_extraction(tmp_path):
    from backend.studio.archive import read_archive

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../outside", "bad")
    with pytest.raises(ValueError):
        read_archive(archive, tmp_path / "stage")
    assert not (tmp_path / "outside").exists()


def test_archive_rejects_missing_checkpoint_asset_and_preserves_existing_output(tmp_path):
    from backend.studio.archive import write_archive

    out = tmp_path / "old.zip"
    out.write_bytes(b"old")
    project = {
        "document": {"schema_version": 1, "clips": []},
        "revisions": [{"document": {"schema_version": 1, "clips": [{"asset_id": "lost"}]}}],
    }
    with pytest.raises(ValueError):
        write_archive(project, [], lambda _: None, out)
    assert out.read_bytes() == b"old"


def test_archive_rejects_corrupt_asset_and_limits(tmp_path):
    import json
    from backend.studio.archive import read_archive

    manifest = {
        "schema_version": 1,
        "project": {"document": {"schema_version": 1, "clips": [{"asset_id": "a"}]}},
        "assets": [{"id": "a", "hash": "0" * 64, "size_bytes": 3, "archive_path": "assets/00000000"}],
    }
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("assets/00000000", b"bad")
    with pytest.raises(ValueError):
        read_archive(archive, tmp_path / "stage")
    assert not list((tmp_path / "stage").iterdir())
    with pytest.raises(ValueError):
        read_archive(archive, tmp_path / "stage", max_total_bytes=2)


def test_archive_rejects_symlink_and_duplicate_entries(tmp_path):
    from backend.studio.archive import read_archive
    import stat

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        info = zipfile.ZipInfo("assets/00000000")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "../escape")
    with pytest.raises(ValueError):
        read_archive(archive, tmp_path / "stage")
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("manifest.json", "{}")
            z.writestr("manifest.json", "{}")
    with pytest.raises(ValueError):
        read_archive(archive, tmp_path / "stage")
    assert not list((tmp_path / "stage").iterdir())
