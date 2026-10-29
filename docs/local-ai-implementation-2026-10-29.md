# Local AI improvements — 2026-10-29

This is the repository working view of Coordinate work
`work_63df15bc-24a9-4144-a668-678954461549`, implementation plan
`artifact_a3fb6e56-b90e-4312-89f5-0eb03f30d483`. Integration target: `doublesharp`.
Source behavior, model execution, listening quality, CUDA, native playback, and
frozen-build qualification are separate evidence domains.

## Existing-engine improvements

- Qwen CustomVoice and VoiceDesign select MLX on Apple Silicon, matching download,
  readiness, and Studio runtime identities. Existing non-Mac PyTorch adapters and
  BF16 defaults remain in place. See [adapter contracts](qwen-mlx-styles.md).
- Qwen Base and both MLX style adapters expose optional native audio frames.
  Inference, seeding, materialization, and generator closure stay on the shared
  MLX worker. The two-frame queue applies backpressure. Closing or cancelling a
  stream drains the physical worker before releasing adapter ownership.
- Incremental preview uses native frames when the adapter supports them and no
  full-utterance trimming or runaway check is configured. Codec frames remain
  consecutive; only text-utterance joins crossfade. Short first frames retain
  enough head audio for the requested join. Rate changes and invalid mono/finite
  samples fail explicitly. Guarded engines retain buffered validation, so Qwen
  Base's existing preview runaway protection is preserved.
- Buffered generation, final normalization/effects, saved takes, and exported WAV
  headers retain their existing contracts. This change adds backend/API streaming;
  it does not claim new streaming playback in the desktop UI.
- Qwen Base reuses bounded CPU reference codec codes on the same loaded model,
  across different target texts in both buffered and native generation. Keys use
  actual normalized audio; unload, bypass and failures invalidate or skip reuse.
  Speaker embeddings and text prefill remain fresh. See [cache audit](qwen-mlx-reference-cache.md).
- Chatterbox Turbo reuses validated CPU voice conditioning across requests and
  chunks, restoring isolated device storage for every generation and clearing
  request speaker state afterward. See [Turbo verification](chatterbox-turbo-conditioning.md).
- The [offline benchmark runner](content/docs/developer/local-ai-benchmarks.mdx)
  records pinned asset/source provenance, cold/warm timing, RTF, percentiles and
  failed partial reports without downloading or contacting providers.
- Model inventory scans use the active Hugging Face cache root explicitly,
  including after the configured root changes. Previously the scanner could
  retain its import-time default and inspect a different cache.

## Verification

The native preview regressions first reproduced ignored native frames, missing
within-utterance rate validation, and premature worker closure. An earlier
streaming, style, API-processing, scheduler, and lifecycle run passed 118 tests.
Threaded coverage.py line tracing covered all 172 changed executable streaming
lines, including the complete 67-line worker bridge. This is scoped line coverage;
it is not whole-backend or branch coverage.

Real offline adapter synthesis passed on Apple M2 Max / 96 GiB unified memory:

| Adapter | Checkpoint revision | Observation |
| --- | --- | --- |
| CustomVoice 0.6B BF16 | `6415d95f88be018ff9e46813119dc3bc12261328` | 16 native frames; buffered synthesis also completed |
| VoiceDesign 1.7B BF16 | `7d3824abff87e49756bb0f83fb5411de75d160c4` | 13 native frames |
| Base 1.7B BF16 | `a6eb4f68e4b056f1215157bb696209bc82a6db48` | 10 native frames with an explicitly synthetic reference |

All outputs were finite 24 kHz mono audio. The Base reference was generated with
macOS Samantha; no human voice or personal recording was used. These are bounded
smoke checks, not blind listening, clone-similarity, controlled performance, or
first-audible-device measurements. Initial kernels and runtime state differed
between runs, so their timings do not establish comparative speedups.

Raw commands, line coverage, checkpoint provenance, memory observations, logs and
audio remain under `/Volumes/Scratch/voicebox/reports/local-ai-20261029/`.
No raw evidence or downloaded model weights belong in Git.

## Remaining research qualification

Pocket TTS, Chatterbox Nano and MOSS-TTS-Nano remain lightweight cloning candidates;
Ming 0.5B and VoxCPM2 remain quality/control candidates. They are not registered or
advertised by this change. Each needs its own dependency audit, compatible runtime,
checkpoint-specific license check, real synthesis, and frozen-build acceptance.
Qwen CUDA graphs/fused kernels/quantization are also separate trials. Turbo
conditioning reuse applies before device restoration and can benefit CUDA, but
no NVIDIA measurements or CUDA qualification were available on this Mac. The
MLX routing, codec reuse and streaming changes are Apple Silicon improvements.

## Integration evidence

The final combined deterministic suite passed **274 tests**. Actual threaded
line tracing covered **975 / 986 changed executable source lines (98.88%)**
against the `doublesharp` baseline `a563267`. All changed native-stream bridge,
style adapter, MLX backend, endpoint closure and audio-joining lines executed.
The remaining 11 lines are defensive reference-cache paths (9), Turbo’s
non-Tensor audio conversion fallback (1), and the benchmark CLI’s `__main__`
entry point (1), exercised in subprocess checks outside this tracer. This is changed-source line coverage, not a claim about
whole-backend coverage, branch coverage or native hardware qualification.
Exact command: the existing Python environment ran the retained
`merge-coverage/verify-merge.py` harness with `timid=True` and thread tracing,
covering 14 focused backend suites plus the benchmark suite. Raw results are
`merge-coverage.log`, `merge-coverage/coverage.json` and
`merge-coverage/changed-source-coverage.json` under the Scratch report root.

Real Turbo CPU encoding and synthesis passed with memory/disk reuse and exact
seeded parity against bypass. Initial preparation was 3.21 seconds versus
1.52 ms from memory and 3.84 ms from disk in one bounded run. This demonstrates
skipped reference work, not a hardware-wide speedup claim.

Both direct and native MLX Base checks observed encoder calls **1, 1, 0, 0**
for bypass/miss/hit/different-text hit and exact same-text fixed-seed parity.
The direct warm miss/hit finished in about 4.21 seconds each, so no reliable
end-to-end speedup is claimed for codec reuse. CPU cache payload was 5,120 bytes.

Independent review reproduced a preview disconnect releasing scheduler ownership
before native closure. The endpoint now explicitly closes the iterator within
ownership; RED regressions proved early admission of unload/another engine,
and GREEN checks cover repeated cancellation and drain ordering. Native cache
regressions also cover partial close, failure, unload and repeated cancellation.

The merge candidate starts from `doublesharp` and contains only this task's
scoped changes. Unrelated newer development commits and the target checkout's
local packaging edits were excluded from this implementation. Coordinate's
latest source/log checkpoint was rejected by automatic approval review because
external export was not explicitly authorized; local source and Scratch proof
remain the current integration record.
