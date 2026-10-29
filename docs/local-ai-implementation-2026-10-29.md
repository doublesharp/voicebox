# Local AI improvements — 2026-10-29

This report records the earlier existing-engine integration and the subsequent
local AI continuation. Source revisions and final distribution checks remain
separate from the bounded observations below.
Source behavior, model execution, listening quality, CUDA, native playback, and
frozen-build qualification are separate evidence domains.

## Current continuation

- **Desktop speech preview:** “Preview speech” plays the current script at normal
  speed without effects or loudness normalization and does not save a generation.
  “Generate” retains finished audio, mastering and export. Incremental PCM uses
  the existing native playback owner with a bounded queue. Engines requiring
  whole-utterance guard/trim checks still wait for their validated utterance.
- **Explicit MLX variants:** Qwen Base 1.7B adds pinned `1.7B-8bit` and
  `1.7B-4bit` selections on Apple Silicon. BF16 remains the default. Real cached
  synthesis produced finite audio for all three variants and unload returned
  active MLX memory near its starting level. The quantized variants remain
  experimental: listening fidelity, a controlled matched corpus and reliable
  performance distributions have not been established. Style engines retain
  their existing BF16 variants.
- **CPU cloning:** MOSS-TTS-Nano uses its pinned low-level ONNX CPU runtime,
  companion codec and actual encoded-reference cache. It preserves the shared
  Torch/Transformers stack and returns finite mono 48 kHz audio. Offline English
  synthesis and reference-switch isolation passed. Pocket TTS and Chatterbox
  Nano remain deferred for the specific access/dependency reasons in the
  [CPU audit](content/docs/developer/cpu-tts-audit.mdx).
- **Studio speech review:** Phrase repairs can recognize a retained take using
  cached local Whisper, save advisory omissions/substitutions/repetitions and
  audition the suspected interval. “Prepare replacement phrase” fills editable
  controls; generation, composition and adoption remain separate user actions.
  The original script, take and audio remain available. Reports carry content,
  request, script, language and recognizer provenance; stale results are rejected
  and portable archive import retains their identity.
- **Recognized captions and reference selection:** “Use recognized word timings”
  creates an unreviewed caption draft from saved current speech reviews. These
  timings describe recognized words, not forced alignment of the supplied script
  or proof that every word was spoken. Reference Quality ranks short waveform
  intervals and auditions the unchanged original; level heuristics do not detect
  speaker identity or sentence boundaries. Optional refinement skips disabled or
  unnecessary work and conservatively rejects obvious content loss.
- **Model management:** Server model controls expose cached-only prewarming and
  session residency limits. The budget estimates model-weight bytes; it is not
  a runtime RAM cap and resets on restart. Model status and Studio provenance
  carry pinned revisions, licenses and actual runtime identity where available;
  unknown metadata remains unknown.

A real CustomVoice 0.6B request reached the native CPAL output callback before
HTTP generation finished. This establishes the production HTTP/native queue
path, not first audible output, physical hotplug or the packaged Tauri journey.
Cached Whisper Base produced ten word intervals from explicitly synthetic speech;
its number formatting and punctuation artifacts reinforce the advisory label.
The audited mlx-audio 0.4.1 word-timing head-name mismatch is handled by a scoped
compatibility alias without editing the installed library.

An intermediate frozen backend passed clean-cache MOSS download/load and Qwen
4-bit synthesis. MOSS ordinary generation exposed a model-selection defect that
was repaired in source; final rebuilt-artifact synthesis/recovery and packaged UI
qualification must be checked against the final artifact before distribution.
Windows/Linux devices, signing/notarization, blind listening and CUDA remain
separate gates. See the [remaining work](plans/LOCAL_AI_FOLLOWUPS.md).

## Earlier integration

Delivered to `doublesharp` in merge commit
`fe2f8394961c7f85293cbede6d79c487d6e9cc86`, after independent round 2
**ACCEPT** of candidate `3960bf65c5c610915c795603c7f02ce750ef5ddf`. The reviewer
reran 60 focused tests successfully and matched all 11 production source hashes
to the supplied coverage artifact. The merged tree matched that candidate exactly;
preexisting target checkout edits and links were verified unchanged.

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
  headers retain their existing contracts. The earlier integration added
  backend/API streaming; the continuation above connects desktop preview.
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

Real offline adapter synthesis passed on Apple Silicon:

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
audio were retained outside Git in task-owned run reports.
No raw evidence or downloaded model weights belong in Git.

## Remaining research qualification

Pocket TTS and Chatterbox Nano remain deferred lightweight cloning candidates;
Ming 0.5B and VoxCPM2 remain quality/control candidates. MOSS-TTS-Nano is now
implemented as described above. Deferred engines still need compatible runtime,
license, real synthesis and frozen-build acceptance. Qwen CUDA graph/fused-kernel
acceleration and CUDA quantization remain separate trials; the explicit MLX
quantized variants do not qualify NVIDIA behavior. Turbo
conditioning reuse applies before device restoration and can benefit CUDA, but
no NVIDIA measurements or CUDA qualification were available during this run. The
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
`merge-coverage/changed-source-coverage.json` in the retained external report directory.

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
scoped changes. Unrelated development and local packaging edits were excluded
from this implementation. Source, logs and run reports were retained locally
outside Git; this report does not imply external publication of raw evidence.
