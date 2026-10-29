"""The shared output guard keeps caller-selected report roots exclusive."""

import os

import pytest

from scripts.portable_output import Output


def test_new_parent_directories_and_exclusive_utf8_artifacts(tmp_path):
    with Output(tmp_path / "new-parent/run") as output:
        output.write_text("report.txt", "finite audio: ✓")
        assert (output.path / "report.txt").read_text() == "finite audio: ✓"
        with pytest.raises(FileExistsError):
            output.write_text("report.txt", "overwrite")
        descriptor = output.fd
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "nested/file", "/absolute"])
def test_artifact_names_cannot_escape_directory(tmp_path, name):
    with Output(tmp_path / "run") as output:
        with pytest.raises(ValueError, match="single filename"):
            output.open(name)


def test_artifact_symlinks_cannot_overwrite_external_files(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    with Output(tmp_path / "run") as output:
        (output.path / "alias").symlink_to(outside)
        with pytest.raises(FileExistsError):
            output.write_text("alias", "overwrite")
    assert outside.read_text() == "preserve"


def test_parent_symlink_refusal_closes_directory_descriptor(tmp_path, monkeypatch):
    target = tmp_path / "outside"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)
    opened = []
    original_open = os.open

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {tracked_open})
    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(OSError):
        Output(tmp_path / "alias/run")
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not (target / "run").exists()


def test_open_directory_keeps_artifacts_in_original_after_path_replacement(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with Output(tmp_path / "run") as output:
        moved = tmp_path / "moved"
        output.path.rename(moved)
        output.path.symlink_to(outside, target_is_directory=True)
        output.write_text("report", "owned")
    assert (moved / "report").read_text() == "owned"
    assert not (outside / "report").exists()


def test_unsupported_safe_io_is_a_clear_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(ValueError, match="unsupported"):
        Output(tmp_path / "run")
    assert not (tmp_path / "run").exists()
