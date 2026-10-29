"""Frozen dependency closure and caller-owned output destinations."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import build_binary

ROOT = Path(__file__).resolve().parents[2]
REVISION = "8b7bcc9341b3b4ef3a3a58ba1338a7d85ff133eb"


def values(args, flag):
    return [args[index + 1] for index, value in enumerate(args[:-1]) if value == flag]


def capture(**kwargs):
    with (
        patch("build_binary.PyInstaller.__main__.run") as run,
        patch("build_binary.os.chdir"),
        patch("build_binary.platform.system", return_value="Linux"),
    ):
        build_binary.build_server(**kwargs)
        return run.call_args.args[0]


def test_cpu_freeze_includes_low_level_moss_runtime_native_wheels_and_metadata():
    args = capture()
    assert {"backend.backends.moss_tts_backend", "ort_cpu_runtime"} <= set(values(args, "--hidden-import"))
    assert {"onnxruntime", "sentencepiece"} <= set(values(args, "--collect-all"))
    assert {"onnxruntime", "sentencepiece", "moss-tts-nano"} <= set(values(args, "--copy-metadata"))
    assert "onnx_tts_runtime" not in values(args, "--hidden-import")


def test_output_paths_are_caller_owned_and_resolved_before_chdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VOICEBOX_PYINSTALLER_DISTPATH", str(tmp_path / "env-dist"))
    args = capture(distpath="dist", workpath="work", specpath="spec")
    for flag, name in [("--distpath", "dist"), ("--workpath", "work"), ("--specpath", "spec")]:
        assert values(args, flag) == [str(tmp_path / name)]
    args = capture()
    assert values(args, "--distpath") == [str(tmp_path / "env-dist")]


def test_shim_uses_same_output_options_without_adding_ml_dependency_tree(tmp_path):
    with patch("build_binary.PyInstaller.__main__.run") as run, patch("build_binary.os.chdir"):
        build_binary.build_shim(distpath=tmp_path / "dist", workpath=tmp_path / "work", specpath=tmp_path / "spec")
    args = run.call_args.args[0]
    assert values(args, "--specpath") == [str(tmp_path / "spec")]
    assert "ort_cpu_runtime" not in values(args, "--hidden-import")


@pytest.mark.parametrize("relative", ["justfile", ".github/workflows/release.yml", "Dockerfile"])
def test_install_routes_pin_source_and_disable_upstream_torch_dependency_resolution(relative):
    text = (ROOT / relative).read_text()
    installs = [line for line in text.splitlines() if "MOSS-TTS-Nano.git@" in line]
    assert installs, f"No MOSS special install in {relative}"
    assert all("--no-deps" in line and REVISION in line for line in installs)
    if relative == "justfile":
        assert len(installs) == 2, "Unix and Windows setup must both install MOSS"


def test_requirements_include_only_reachable_moss_dependencies():
    text = (ROOT / "backend/requirements.txt").read_text()
    assert "sentencepiece==0.2.1" in text
    assert "onnxruntime>=1.20" in text
    assert not any("MOSS-TTS-Nano.git@" in line and not line.startswith("#") for line in text.splitlines())


def test_checked_in_spec_retains_moss_native_and_license_metadata():
    text = (ROOT / "backend/voicebox-server.spec").read_text()
    for value in [
        "backend.backends.moss_tts_backend",
        "ort_cpu_runtime",
        "collect_all('onnxruntime')",
        "collect_all('sentencepiece')",
        "copy_metadata('onnxruntime')",
        "copy_metadata('sentencepiece')",
        "copy_metadata('moss-tts-nano')",
    ]:
        assert value in text


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sidecar wrapper")
@pytest.mark.parametrize("override", [None, "caller-output"])
def test_unix_wrapper_resolves_dist_override_before_changing_directory(tmp_path, override):
    project = tmp_path / "project"
    (project / "backend").mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    python = tools / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\nfrom pathlib import Path\n"
        "if sys.argv[1:2]==['-c']:sys.exit(0)\n"
        "name='voicebox-mcp' if '--shim' in sys.argv else 'voicebox-server'\n"
        "root=Path(os.environ.get('VOICEBOX_PYINSTALLER_DISTPATH','dist'));root.mkdir(parents=True,exist_ok=True)\n"
        "(root/name).write_bytes(b'fixture');(root/name).chmod(0o755)\n"
        "(root/'destinations.json').write_text(json.dumps({k:os.environ.get(k) for k in ['VOICEBOX_PYINSTALLER_WORKPATH','VOICEBOX_PYINSTALLER_SPECPATH']}))\n"
    )
    python.chmod(0o755)
    rustc = tools / "rustc"
    rustc.write_text("#!/bin/sh\nprintf 'fixture-triple\\n'\n")
    rustc.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "VOICEBOX_PYINSTALLER_WORKPATH": "caller-work",
        "VOICEBOX_PYINSTALLER_SPECPATH": "caller-spec",
    }
    env.pop("VOICEBOX_PYINSTALLER_DISTPATH", None)
    if override:
        env["VOICEBOX_PYINSTALLER_DISTPATH"] = override
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/build-server.sh")],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = project / override if override else project / "backend/dist"
    assert (expected / "voicebox-server").read_bytes() == b"fixture"
    assert json.loads((expected / "destinations.json").read_text()) == {
        "VOICEBOX_PYINSTALLER_WORKPATH": str(project / "caller-work"),
        "VOICEBOX_PYINSTALLER_SPECPATH": str(project / "caller-spec"),
    }
    assert (project / "tauri/src-tauri/binaries/voicebox-server-fixture-triple").read_bytes() == b"fixture"
    assert (project / "tauri/src-tauri/binaries/voicebox-mcp-fixture-triple").read_bytes() == b"fixture"
