"""The shared output guard keeps caller-selected report roots exclusive."""

import os
import subprocess

import pytest

from scripts.portable_output import Output

WINDOWS = os.name == "nt"
posix_only = pytest.mark.skipif(WINDOWS, reason="exercises POSIX dirfd/O_NOFOLLOW semantics or os.symlink")
windows_only = pytest.mark.skipif(not WINDOWS, reason="exercises Win32 junctions and directory pins")


def test_new_parent_directories_and_exclusive_utf8_artifacts(tmp_path):
    with Output(tmp_path / "new-parent/run") as output:
        output.write_text("report.txt", "finite audio: ✓")
        assert (output.path / "report.txt").read_text(encoding="utf-8") == "finite audio: ✓"
        with pytest.raises(FileExistsError):
            output.write_text("report.txt", "overwrite")
        descriptor = output.fd
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_root_shape_and_existing_run_refusals(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        Output("relative/run")
    with pytest.raises(ValueError, match="absolute"):
        Output(tmp_path / ".." / "run")
    with pytest.raises(ValueError, match="fresh subdirectory"):
        Output(tmp_path.anchor)
    Output(tmp_path / "run").close()
    with pytest.raises(ValueError, match="already exists"):
        Output(tmp_path / "run")


def test_close_twice_raises_like_a_closed_descriptor(tmp_path):
    output = Output(tmp_path / "run")
    output.close()
    with pytest.raises(OSError):
        output.close()


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "nested/file", "/absolute", "report.txt:stream", "C:stream"])
def test_artifact_names_cannot_escape_directory_or_name_an_alternate_stream(tmp_path, name):
    with Output(tmp_path / "run") as output:
        with pytest.raises(ValueError, match="single filename"):
            output.open(name)


@posix_only
def test_artifact_symlinks_cannot_overwrite_external_files(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    with Output(tmp_path / "run") as output:
        (output.path / "alias").symlink_to(outside)
        with pytest.raises(FileExistsError):
            output.write_text("alias", "overwrite")
    assert outside.read_text() == "preserve"


@posix_only
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


@posix_only
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


@posix_only
def test_unsupported_safe_io_is_a_clear_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(ValueError, match="unsupported"):
        Output(tmp_path / "run")
    assert not (tmp_path / "run").exists()


def junction(target, link):
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def symlink_or_skip(target, link, **kwargs):
    try:
        link.symlink_to(target, **kwargs)
    except OSError as exc:
        pytest.skip(f"this account cannot create symlinks (winerror {exc.winerror})")


@windows_only
def test_junction_in_parent_is_refused_and_pins_are_released(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    junction(target, tmp_path / "alias")
    with pytest.raises(ValueError, match="reparse point"):
        Output(tmp_path / "alias/run")
    assert not (target / "run").exists()
    (tmp_path / "alias").rmdir()
    tmp_path.rename(tmp_path.with_name(tmp_path.name + "-free"))


@windows_only
def test_symlink_in_parent_is_refused(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    symlink_or_skip(target, tmp_path / "alias", target_is_directory=True)
    with pytest.raises(ValueError, match="reparse point"):
        Output(tmp_path / "alias/run")
    assert not (target / "run").exists()


@windows_only
def test_junction_or_file_at_artifact_name_is_neither_followed_nor_overwritten(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    with Output(tmp_path / "run") as output:
        junction(target, output.path / "alias")
        (output.path / "plain").write_bytes(b"preserve")
        with pytest.raises(OSError):
            output.write_text("alias", "overwrite")
        with pytest.raises(FileExistsError):
            output.write_text("plain", "overwrite")
    assert list(target.iterdir()) == []
    assert (tmp_path / "run/plain").read_bytes() == b"preserve"


@windows_only
def test_dangling_symlink_at_artifact_name_is_not_followed(tmp_path):
    never = tmp_path / "never"
    with Output(tmp_path / "run") as output:
        symlink_or_skip(never, output.path / "dangle")
        with pytest.raises(OSError):
            output.write_text("dangle", "owned")
    assert not never.exists()


@windows_only
def test_open_run_directory_and_ancestors_cannot_be_moved_or_replaced(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with Output(tmp_path / "parent/run") as output:
        with pytest.raises(OSError):
            output.path.rename(tmp_path / "moved")
        with pytest.raises(OSError):
            (tmp_path / "parent").rename(tmp_path / "parent-moved")
        with pytest.raises(OSError):
            os.replace(other, output.path)
        with pytest.raises(OSError):
            os.replace(other, tmp_path / "parent")
        output.write_text("report", "owned")
    assert (tmp_path / "parent/run/report").read_text() == "owned"
    output.path.rename(tmp_path / "moved")
    assert (tmp_path / "moved/report").read_text() == "owned"


def icacls(path):
    text = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    lines = text.replace(str(path), "", 1).splitlines()
    return sorted(line.strip() for line in lines[: lines.index("")] if line.strip())


@windows_only
def test_created_directories_and_artifacts_are_owner_only_and_ancestors_keep_their_acl(tmp_path):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    inherited = icacls(ancestor)
    assert any("(I)" in entry for entry in inherited), "a plain mkdir inherits its parent's ACL"
    with Output(ancestor / "parent/run") as output:
        output.write_text("report.json", "{}")
    assert icacls(ancestor) == inherited
    owner_only = {"nt authority\\system", f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}".lower()}
    for path in (ancestor / "parent", ancestor / "parent/run", ancestor / "parent/run/report.json"):
        entries = icacls(path)
        assert {entry.split(":(")[0].lower() for entry in entries} == owner_only, entries
        assert not any("(I)" in entry for entry in entries), entries
        assert all(":(OI)(CI)(F)" in entry for entry in entries) == path.is_dir(), entries
    assert (ancestor / "parent/run/report.json").read_text() == "{}"


@windows_only
def test_win32_failures_surface_as_oserror_without_creating_anything(tmp_path, monkeypatch):
    from scripts import portable_output

    with pytest.raises(OSError):
        Output(tmp_path / "bad<name" / "run")
    monkeypatch.setattr(portable_output._advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", lambda *args: 0)
    with pytest.raises(OSError):
        Output(tmp_path / "run")
    assert not (tmp_path / "run").exists()
    monkeypatch.setattr(portable_output._advapi32, "OpenProcessToken", lambda *args: 0)
    portable_output._current_user_sid.cache_clear()
    try:
        with pytest.raises(OSError):
            Output(tmp_path / "run")
    finally:
        portable_output._current_user_sid.cache_clear()
    assert not (tmp_path / "run").exists()


@windows_only
def test_substituted_drive_is_refused_because_the_pinned_path_resolves_elsewhere(tmp_path):
    free = next((f"{letter}:" for letter in "ZYXWVUTSRQ" if not os.path.exists(f"{letter}:\\")), None)
    if free is None or subprocess.run(["subst", free, str(tmp_path)], capture_output=True).returncode != 0:
        pytest.skip("no free drive letter to subst")
    try:
        with pytest.raises(ValueError, match="resolves"):
            Output(f"{free}\\run")
    finally:
        subprocess.run(["subst", free, "/D"], capture_output=True, check=True)
    tmp_path.rename(tmp_path.with_name(tmp_path.name + "-free"))
