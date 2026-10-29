"""Regression checks for the bounded frontend test runner."""
import importlib.util
import os
from pathlib import Path
import sys
import time

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
