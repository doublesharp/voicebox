"""Regression checks for the bounded frontend test runner."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "test-frontend.py"
SPEC = importlib.util.spec_from_file_location("test_frontend_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_hung_check_fails_with_timeout_and_preserves_output():
    start = time.monotonic()
    result = runner.run_check(
        [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(10)"],
        cwd=MODULE_PATH.parent, env=os.environ.copy(), timeout=0.3,
    )
    assert result.returncode == 124
    assert "started" in result.stdout
    assert "Timed out" in result.stderr
    assert time.monotonic() - start < 5


def test_successful_check_preserves_exit_and_output():
    result = runner.run_check(
        [sys.executable, "-c", "print('finished')"],
        cwd=MODULE_PATH.parent, env=os.environ.copy(), timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "finished"


def test_cli_runs_with_explicit_output_without_special_mount(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "app/tests").mkdir(parents=True)
    (root / "app/src").mkdir()
    (root / "app/tests/example.test.ts").write_text("test fixture")
    (root / "app/src/main.ts").write_text("export const value = 1;")
    output = tmp_path / "reports" / "run"
    monkeypatch.setattr(Path, "is_mount", lambda _: False)
    monkeypatch.setattr(sys, "argv", [str(MODULE_PATH), "--source-root", str(root), "--output", str(output)])
    monkeypatch.setattr(runner, "run_check", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="1 pass", stderr=""))
    assert runner.main() == 0
    assert (output / "summary.json").is_file()
    with pytest.raises(SystemExit):
        runner.main()


def test_cli_refuses_output_through_a_symlink(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "app/tests").mkdir(parents=True)
    (root / "app/tests/example.test.ts").write_text("test fixture")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "alias").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(sys, "argv", [str(MODULE_PATH), "--source-root", str(root), "--output", str(tmp_path / "alias/run")])
    with pytest.raises(SystemExit):
        runner.main()
    assert not (outside / "run").exists()


@pytest.mark.parametrize("option,value", [("--timeout-seconds", "nan"), ("--max-seconds", "inf"), ("--max-seconds", "0")])
def test_invalid_time_budget_is_refused_before_output(tmp_path, monkeypatch, option, value):
    output = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", [str(MODULE_PATH), "--output", str(output), option, value])
    with pytest.raises(SystemExit):
        runner.main()
    assert not output.exists()


def test_instrumentation_requires_coverage_before_creating_output(tmp_path, monkeypatch):
    output = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", [str(MODULE_PATH), "--output", str(output), "--instrumentation-root", str(tmp_path)])
    with pytest.raises(SystemExit):
        runner.main()
    assert not output.exists()


def test_failed_coverage_merge_retains_diagnostics_and_failed_report(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "app/tests").mkdir(parents=True)
    (root / "app/src").mkdir()
    (root / "app/tests/example.test.ts").write_text("test fixture")
    output = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", [str(MODULE_PATH), "--source-root", str(root), "--output", str(output),
                                      "--coverage", "--instrumentation-root", str(tmp_path / "tools")])

    def check(command, **kwargs):
        if any(str(value).endswith("coverage-merge.ts") for value in command):
            return SimpleNamespace(returncode=1, stdout="", stderr="invalid coverage input")
        return SimpleNamespace(returncode=0, stdout="1 pass", stderr="")

    monkeypatch.setattr(runner, "run_check", check)
    assert runner.main() == 1
    assert (output / "coverage-merge.log").read_text() == "invalid coverage input"
    summary = json.loads((output / "summary.json").read_text())
    assert summary["failedChecks"] == ["coverage-merge"]
    assert summary["missingCoverageFiles"] == ["example.test"]
