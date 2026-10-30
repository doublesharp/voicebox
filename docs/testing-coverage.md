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

The consolidated candidate `bac3af1` passed **760 tests in 116 isolated files** and
the application TypeScript check, with no failed checks or missing coverage files.
All 117 checks completed and production source remained stable during the run.
This includes local AI preview, model management, speech review, shortcut-conflict
handling and existing Studio/upstream regressions.

Subsequent published profile and model-download UI changes passed 19 profile
and 31 model-management integration tests, TypeScript and production bundling
after merging. That focused run does
not establish a new whole-frontend coverage percentage; the table below belongs
to the consolidated source snapshot above.
A later server-session integration also passed 93 affected frontend tests,
TypeScript and a fresh production build, including preview-cancellation checks.

| Scope | Lines | Statements | Functions | Branches |
| --- | ---: | ---: | ---: | ---: |
| Whole frontend, all 248 source files | 74.81% | 73.05% | 66.63% | 65.91% |
| The 157 modules imported by tests | 80.49% | 78.52% | 73.60% | 70.52% |

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

## Rust desktop production coverage

LLVM coverage of Rust unit tests includes the executable lines in inline test
modules. Use `scripts/rust-production-coverage.py` to summarize LCOV after
excluding syntax explicitly gated by `#[cfg(test)]` or `#[cfg(all(test, ...))]`.
The helper uses `syn` to find Rust item spans instead of cutting off a file at
its first test module; production code can occur after those modules. Build the
helper and the desktop test target under a host-approved output root:

```sh
export VOICEBOX_RUST_COVERAGE_ROOT="/absolute/approved-build-root"
export TMPDIR="$VOICEBOX_RUST_COVERAGE_ROOT/tmp"
mkdir -p "$TMPDIR" "$VOICEBOX_RUST_COVERAGE_ROOT/reports"
export CARGO_TARGET_DIR="$VOICEBOX_RUST_COVERAGE_ROOT/helper"
cargo build --locked --manifest-path scripts/rust-test-spans/Cargo.toml
export CARGO_TARGET_DIR="$VOICEBOX_RUST_COVERAGE_ROOT/desktop"
TAURI_CONFIG='{"bundle":{"externalBin":[]}}' cargo llvm-cov \
  --manifest-path tauri/src-tauri/Cargo.toml --lib --bin voicebox --lcov \
  --output-path "$VOICEBOX_RUST_COVERAGE_ROOT/reports/desktop.lcov"
python3 scripts/rust-production-coverage.py \
  --lcov "$VOICEBOX_RUST_COVERAGE_ROOT/reports/desktop.lcov" \
  --root "$PWD" \
  --span-tool "$VOICEBOX_RUST_COVERAGE_ROOT/helper/debug/rust-test-spans" \
  > "$VOICEBOX_RUST_COVERAGE_ROOT/reports/production.json"
```

### Silent native audio coverage

On a macOS host with at least two working, uniquely identified output devices,
the ignored `native_silent` tests exercise actual CPAL streams and Tauri IPC
using silent samples. Run the standard command above first, then retain its
coverage profile while adding the native tests:

```sh
TAURI_CONFIG='{"bundle":{"externalBin":[]}}' cargo llvm-cov --no-clean \
  --manifest-path tauri/src-tauri/Cargo.toml --lib --bin voicebox --lcov \
  --output-path "$VOICEBOX_RUST_COVERAGE_ROOT/reports/desktop-with-native.lcov" \
  -- native_silent --ignored --test-threads=1
python3 scripts/rust-production-coverage.py \
  --lcov "$VOICEBOX_RUST_COVERAGE_ROOT/reports/desktop-with-native.lcov" \
  --root "$PWD" \
  --span-tool "$VOICEBOX_RUST_COVERAGE_ROOT/helper/debug/rust-test-spans" \
  > "$VOICEBOX_RUST_COVERAGE_ROOT/reports/production-with-native.json"
```

This is a hardware-dependent supplement to the standard result, not the default
CI coverage. It opens output streams on the host; the separate tests that play
audible samples or require a running backend are not selected by this filter.
Both LCOV runs must use the same checkout, target directory, and instrumentation.

The source tree must match the one used for the LCOV run; LCOV does not record
source hashes, so the reporter cannot verify that itself. LCOV and LLVM JSON
can count different mapped lines and should not be compared as identical
metrics. The result covers only source files and executable lines mapped by the
selected build, OS, and targets. It does not include uncompiled Windows/Linux
code, live device tests, or unmapped code. It excludes only syntax directly
marked by the supported test-only `cfg` forms; review other conditional and
macro-generated test code before treating the figure as complete.
