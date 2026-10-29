# Frontend testing and coverage

Run test files in separate Bun processes. Module mocks in the existing tests can
leak between files in a single process. The runner also checks TypeScript and
fails if production source changes during the run.

```sh
python3 scripts/test-frontend.py \
  --coverage \
  --instrumentation-root /Volumes/Scratch/voicebox/tmp/coverage-instrumentation-20261029 \
  --output /Volumes/Scratch/voicebox/reports/new-coverage-run
```

The output directory must be new and under mounted Scratch. The instrumentation
directory must contain `istanbul-lib-instrument@6.0.3` and
`istanbul-lib-coverage@3.2.2` in its own `node_modules`. These tools are installed
separately on Scratch; they do not modify the application's dependencies.
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

The merged DoubleSharp source passes **414 tests in 62 files**, with no failed
tests or missing coverage files. Source remained stable during the run. This
includes the upstream reliability and audio preview regressions already on the
DoubleSharp branch.

| Scope | Lines | Statements | Functions | Branches |
| --- | ---: | ---: | ---: | ---: |
| Whole frontend, all 230 source files | 45.85% | 44.95% | 42.02% | 39.77% |
| The 101 modules imported by tests | 79.14% | 76.92% | 72.06% | 67.47% |

Measured with the same Istanbul setup, the original 333-test suite covered
38.69% of whole-frontend lines and 70.12% of lines in its 96 imported modules.
Before the upstream merge, the installed UI snapshot passed 373 tests in 56
files and covered 44.35% of whole-frontend lines (549 additional covered lines).
The merged source adds two production files and the upstream tests. The earlier
raw Bun 72% figure used an imported-module scope and a different denominator.

Some focused areas exceed 95% line coverage: API client 99.56%, profile hooks
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
