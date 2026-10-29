# Frontend testing and coverage

Run test files in separate Bun processes. Module mocks in the existing tests can
leak between files in a single process. The runner also checks TypeScript and
fails if production source changes during the run.

Choose absolute paths for disposable run output and an isolated coverage-tooling
installation, following the current host's storage policy:

```sh
export VOICEBOX_REPORTS_ROOT="/absolute/disposable-reports"
export VOICEBOX_COVERAGE_TOOLS="/absolute/coverage-tooling"
python3 scripts/test-frontend.py \
  --coverage \
  --instrumentation-root "$VOICEBOX_COVERAGE_TOOLS" \
  --output "$VOICEBOX_REPORTS_ROOT/new-coverage-run"
```

The output directory must be new, absolute, and have no `..` or symlink
components. The guard requires POSIX directory-relative no-follow IO; unsupported
hosts fail explicitly. Follow any host-specific mount requirement before running.
The instrumentation directory must contain `istanbul-lib-instrument@6.0.3` and
`istanbul-lib-coverage@3.2.2` in its own `node_modules`. These tools are installed
separately in the chosen tooling directory; they do not modify the application's
dependencies.
Use `--test <basename>` repeatedly for a focused run. Omit `--coverage` for a
normal regression run.

The Istanbul preload instruments the real production TypeScript before Bun
transpiles it. The merger seeds every `app/src` TypeScript module with zero
counters, then merges counters from all isolated processes. Files the tests
never import therefore remain uncovered in the whole-frontend result. Imported
module coverage is reported separately. Types and declarations without runtime
statements do not add executable lines.

Bun's built-in LCOV output is retained as a diagnostic option when
`--instrumentation-root` is omitted. Its unexecuted function ranges can include
comments, blank lines and closing braces. Merging those ranges across isolated
runs produces a different denominator from focused runs. Do not compare that
raw percentage directly with Istanbul or claim it is whole-app coverage.

## Measured scope, 29 October 2026

The final consolidated candidate passes **728 tests in 115 isolated files** and
the application TypeScript check, with no failed checks or missing coverage files.
All 116 checks completed and production source remained stable during the run.
This includes local AI preview, model management, speech review and existing
Studio/upstream regressions.

| Scope | Lines | Statements | Functions | Branches |
| --- | ---: | ---: | ---: | ---: |
| Whole frontend, all 248 source files | 74.52% | 72.76% | 66.27% | 65.65% |
| The 156 modules imported by tests | 80.31% | 78.35% | 73.31% | 70.36% |

Using the same Istanbul setup, the earlier 333-test suite covered 38.69% of
whole-frontend lines; the later 414-test/62-file integration covered 45.85%.
These are historical snapshots with different source denominators. The earlier
raw Bun 72% figure used imported modules and a different instrumentation method.

Some focused areas exceed 95% line coverage: API client 97.48%, profile hooks
97.14%, story hooks 100%, effects chain 97.64%, and audio utilities 100%.
The whole-frontend 95% target has not been reached. Untested screens and forms,
capture workflows, and platform integration remain significant gaps. Backend
and Rust coverage are not included in these percentages.

Four deterministic action-sequence suites exercise **690 actions across 12
seeded runs**. They cover composer drafts and settings, clip gestures, playback
source identity and stale callbacks, and effects editing through refetches.
The first three suites assert that each action class is actually reached.
These are bounded, reproducible fuzz-style tests, not long-running fuzz campaigns.

Real QueryClient, form, store, API client and component behavior is exercised
where possible. Network, waveform, recording, filesystem, and native boundaries
are mocked in component tests. Passing tests do not qualify live model quality,
OS permissions, physical devices, real database mutations, or native rendering;
those require separate runtime checks.
