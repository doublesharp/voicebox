#!/usr/bin/env python3
"""Run Bun files in isolated processes and merge their scoped LCOV line hits.

Isolation is intentional: Bun's module mocks can leak between test files.
Unimported source files are listed explicitly, never treated as covered.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace


def source_snapshot(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "app/src").rglob("*"))
        if p.is_file() and p.suffix in (".ts", ".tsx")
    }


def merge_lines(paths, root, source_files):
    modules = {}
    for path in paths:
        current = None
        for line in path.read_text().splitlines():
            if line.startswith("SF:"):
                name = Path(line[3:])
                if name.is_absolute():
                    try:
                        name = name.relative_to(root)
                    except ValueError:
                        current = None
                        continue
                current = name.as_posix()
                if current not in source_files:
                    current = None
                    continue
                modules.setdefault(current, {})
            elif current and line.startswith("DA:"):
                number, hits, *_ = line[3:].split(",")
                number = int(number)
                modules[current][number] = modules[current].get(number, 0) + int(hits)
    rows = []
    for name, lines in sorted(modules.items()):
        hit = sum(value > 0 for value in lines.values())
        rows.append({
            "file": name,
            "coveredLines": hit,
            "instrumentedLines": len(lines),
            "linePercent": round(100 * hit / len(lines), 2) if lines else None,
            "uncoveredLines": sorted(number for number, count in lines.items() if count == 0),
        })
    found = sum(row["instrumentedLines"] for row in rows)
    hit = sum(row["coveredLines"] for row in rows)
    return {
        "scope": "Imported, instrumented app/src modules; union of line hits across isolated runs.",
        "coveredLines": hit,
        "instrumentedLines": found,
        "linePercent": round(100 * hit / found, 2) if found else None,
        "instrumentedAppSourceFiles": len(rows),
        "allAppSourceFiles": len(source_files),
        "unimportedSourceFiles": sorted(set(source_files) - set(modules)),
        "functionPercent": None,
        "branchPercent": None,
        "limitations": [
            "Unimported modules are excluded from the line percentage, not counted as covered.",
            "Bun LCOV lacks function identities and branch counters for an exact merged percentage.",
            "Mocked services and native APIs do not establish live backend, model or device behavior.",
        ],
        "files": rows,
    }


def run_check(command, *, cwd, env, timeout):
    """Bound each subprocess and make timeout a failed check, with captured output."""
    try:
        return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as error:
        def decoded(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        return SimpleNamespace(returncode=124, stdout=decoded(error.stdout),
                               stderr=decoded(error.stderr) + f"\nTimed out after {timeout:.1f}s\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True, help="New directory under Scratch")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--instrumentation-root", type=Path,
                        help="Scratch tooling directory with Istanbul instrument 6.0.3 and coverage 3.2.2")
    parser.add_argument("--test", action="append", help="Explicit app/tests basename; repeat to select files")
    parser.add_argument("--timeout-seconds", type=float, default=30,
                        help="Maximum time for one check (default: 30 seconds)")
    parser.add_argument("--max-seconds", type=float, default=180,
                        help="Maximum total check time (default: 180 seconds)")
    args = parser.parse_args()
    if args.timeout_seconds <= 0 or args.max_seconds <= 0:
        parser.error("Timeouts must be positive")
    scratch = Path("/Volumes/Scratch")
    if not scratch.is_mount():
        parser.error("Scratch is not mounted; refusing to write disposable output elsewhere")
    root = args.source_root.resolve()
    output = args.output.resolve()
    if not output.is_relative_to(scratch / "voicebox") or output.exists():
        parser.error("--output must be a new directory under /Volumes/Scratch/voicebox")
    tests = sorted((root / "app/tests").glob("*.test.*"))
    if args.test:
        tests = [p for p in tests if p.name in args.test]
        if {p.name for p in tests} != set(args.test):
            parser.error("At least one selected test file does not exist")
    if not tests:
        parser.error("No test files found")
    before = source_snapshot(root)
    output.mkdir(parents=True)
    temporary = output / "tmp"
    temporary.mkdir()
    env = {**os.environ, "TMPDIR": str(temporary)}
    if args.instrumentation_root:
        if not args.coverage:
            parser.error("--instrumentation-root requires --coverage")
        env.update(VOICEBOX_COVERAGE_TOOLS=str(args.instrumentation_root.resolve()),
                   VOICEBOX_COVERAGE_SOURCE=str(root))
    checks = [("typecheck", ["bun", "run", "typecheck"], root / "app")]
    for test in tests:
        command = ["bun", "test"]
        if args.instrumentation_root:
            command.extend(["--preload", str(root / "scripts/coverage-preload.ts")])
        elif args.coverage:
            command.extend(["--coverage", "--coverage-reporter=lcov", "--coverage-dir", str(output / test.stem)])
        checks.append((test.stem, [*command, str(test)], root))
    results = []
    deadline = time.monotonic() + args.max_seconds
    budget_exhausted = False
    for name, command, cwd in checks:
        start = time.monotonic()
        remaining = deadline - start
        if remaining <= 0:
            budget_exhausted = True
            break
        run_env = {**env, "VOICEBOX_COVERAGE_FILE": str(output / f"{name}.coverage.json")}
        result = run_check(command, cwd=cwd, env=run_env, timeout=min(args.timeout_seconds, remaining))
        log = result.stdout + result.stderr
        (output / f"{name}.log").write_text(log)
        count = re.search(r"(\d+) pass", log)
        results.append({
            "check": name,
            "exitCode": result.returncode,
            "seconds": round(time.monotonic() - start, 2),
            "tests": int(count[1]) if count else None,
        })
    after = source_snapshot(root)
    stable = before == after
    summary = {
        "sourceDigest": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        "sourceStableDuringRun": stable,
        "testFiles": len(tests),
        "passingTests": sum(row["tests"] or 0 for row in results),
        "failedChecks": [row["check"] for row in results if row["exitCode"]] + (["suite-time-budget"] if budget_exhausted else []),
        "checksCompleted": len(results),
        "results": results,
    }
    if args.instrumentation_root:
        missing = [p.stem for p in tests if not (output / f"{p.stem}.coverage.json").is_file()]
        summary["missingCoverageFiles"] = missing
        merge = run_check(["bun", str(root / "scripts/coverage-merge.ts"), str(root),
                           str(output), str(args.instrumentation_root.resolve())],
                          cwd=root, env=env, timeout=args.timeout_seconds)
        (output / "coverage-merge.log").write_text(merge.stderr)
        if merge.returncode:
            summary["failedChecks"].append("coverage-merge")
        else:
            summary.update(json.loads(merge.stdout))
    elif args.coverage:
        missing = [p.stem for p in tests if not (output / p.stem / "lcov.info").is_file()]
        summary["missingCoverageFiles"] = missing
        summary.update(merge_lines(sorted(output.glob("*/lcov.info")), root, before))
    (output / "source-hashes.json").write_text(json.dumps(before, indent=2) + "\n")
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in ("results", "files", "unimportedSourceFiles")}))
    return 1 if summary["failedChecks"] or not stable or summary.get("missingCoverageFiles") else 0


if __name__ == "__main__":
    sys.exit(main())
