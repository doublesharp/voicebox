# Local AI follow-ups: Apple Silicon work and CUDA handoff

This is the maintained implementation and qualification view. A completed
source slice does not establish listening, device, packaged or CUDA acceptance.
Use the current integration branch and record the exact source revision.
The [implementation report](../local-ai-implementation-2026-10-29.md) records the
prior accepted local AI work; record the exact source revision for each new run.

The prior batches passed 274 tests with 975/986 changed executable lines traced
(98.88%). They exercised real MLX synthesis and Turbo CPU conditioning/synthesis.
They did not qualify CUDA, desktop streaming playback, listening quality or
frozen builds. See [completed work](../local-ai-implementation-2026-10-29.md).

## Apple Silicon implementation and remaining qualification

| Batch | Implemented | Verified boundary | Remaining work |
| --- | --- | --- | --- |
| M1: playback | Desktop “Preview speech” connects incremental API PCM to the native playback owner with bounded buffering, replacement/stop ownership and validated fallback for guarded utterances. | Real CustomVoice request reached the native output callback before HTTP EOF; deterministic lifecycle/queue regressions pass. | Final packaged Tauri UI journey, physical device changes and first-audible measurement; Windows/Linux device qualification. |
| M2: MLX efficiency | Explicit pinned Qwen Base 1.7B 8-bit/4-bit variants, BF16 default, session residency limits and cached prewarming. | Real finite BF16/8-bit/4-bit synthesis and memory recovery after unload; pinned asset and ownership checks. | Blind listening, controlled same-corpus performance/memory distributions and wider lifecycle/hardware qualification. Quantized variants remain experimental. |
| M3: CPU cloning | MOSS-TTS-Nano low-level ONNX CPU adapter with pinned codec, reference-content cache, registry/UI selection and packaging hooks. | Offline English synthesis, encoded-reference reuse and speaker-state isolation; comparison with existing Turbo was observational. | Final rebuilt frozen ordinary-generation/recovery checks, listening and multilingual quality. Pocket TTS access/NumPy compatibility and Chatterbox Nano dependency conflicts remain explicit deferrals. |
| M4: production quality | Persisted advisory take/script comparisons, recognized-word caption drafts, explicit phrase-repair preparation/audition and ranked original-reference interval auditions. | Real cached Whisper word timings, immutable source/revision checks, archive identity and component workflows. | Rendered/browser journeys and listening. Recognized timing is not supplied-script forced alignment; ASR agreement does not certify speech. |
| M5: recognition and management | Existing local recognizer exposes detailed words; optional refinement skips disabled/unneeded work and guards clear name/number/negation/content loss; model identity and session residency are surfaced. | Real cached Whisper recognition plus deterministic refusal, cancellation, metadata and cleanup checks. | Multilingual/error-corpus and packaged recognition/refinement qualification; streaming dictation/hotkey latency remains separate. No new ASR runtime is claimed. |

The [implementation report](../local-ai-implementation-2026-10-29.md) and
[CPU audit](../content/docs/developer/cpu-tts-audit.mdx) distinguish source,
fixture, real-model and artifact evidence. Final artifact receipts supersede
intermediate builds. Serialize real GPU work and shared registry/model changes;
use behavioral RED/GREEN, actual changed-source coverage and independent review
for remaining implementation batches.

Place worktrees, temporary files, model downloads, native builds, audio, coverage
and raw evidence under task-owned storage approved for the current host. Follow
any required mount policy; do not assume a particular volume name. Run the
resource preflight when budget assumptions change. Use separate
environments for model experiments; no shared environment dependency upgrades.
Read `.agents/skills/add-tts-engine/SKILL.md` and its full guide before adding an
engine, including the Phase 0 dependency audit and frozen-inference gates.

## Work assigned to a CUDA machine

| Batch | Deliverable | Hardware-dependent acceptance |
| --- | --- | --- |
| C0: trustworthy measurements | Confirm NVIDIA CUDA execution; choose explicit approved benchmark output/cache roots; record device/driver/runtime and baseline timing/memory; validate existing Turbo conditioning reuse on CUDA. | Real device residency, CPU-owned cache payloads, memory/disk cache hits, speaker switching, cancellation/unload and finite audio on NVIDIA. No silent CPU fallback. |
| C1: accelerated Qwen | Implement an explicit opt-in CUDA runtime, comparing the stock path with CUDA-graph acceleration and supported attention/compilation options. Add native audio frames for qualified modes. | Measured same-hardware benefit with matched weights/sampling, separate graph-build/cold/warm timings, no speaker/RNG/lifecycle regressions and explicit unsupported-model behavior. |
| C2: memory and distribution | Trial compatible 8/4-bit GPU paths after C1, then package and run offline on the actual CUDA host OS. | Report precision/quality tradeoffs and VRAM, not just weight size. Frozen load plus synthesis and recovery must run on CUDA. One OS/GPU does not qualify other platforms. |

The complete implementation brief is [CUDA_AGENT_PROMPT.md](CUDA_AGENT_PROMPT.md).
The CUDA owner returns focused commits and raw evidence. The Mac integration
owner merges them after non-CUDA regressions and conflict review. The CUDA agent
does not change Mac playback, MLX adapters or the CPU-engine backlog. Shared
registry, dependency, scheduler and benchmark edits must remain small and in
separate commits to make integration reviewable.

The current benchmark admits only Qwen Base, CustomVoice and VoiceDesign. It
uses the complete-array API and does not measure first-model-frame latency.
Its output/cache roots are explicit caller choices. Safe output requires POSIX
directory-relative no-follow IO; native Windows currently fails explicitly and
needs a separately verified safe-write implementation. Preserve exclusive writes
and symlink protections when extending platform support.
Turbo benchmarking needs its own admitted asset policy, not a validation bypass.

## Dependency and capability decisions

- NVIDIA is required to prove CUDA kernels, graph capture/replay, VRAM behavior
  and the packaged CUDA runtime. None of those acceptance claims can come from
  CPU fixture tests.
- MLX quantization and Metal-native experiments do not require CUDA. CPU cloning,
  verification, alignment, editing and provenance can be developed on supported
  CPU/Apple Silicon hosts.
- New models are candidates, not automatic defaults. Model-specific dependency,
  license, quality and packaging audits still decide adoption.
- Accelerated implementations need not produce bit-identical arrays to a stock
  runtime with different kernels. Test deterministic behavior within a fixed
  runtime, preserve requested speaker/text/settings, compare speech errors and
  listen to matched samples. The prior Turbo CPU parity result is a specific
  observation, not a universal GPU contract.

## Source handoff

Agree an integration ref and record its exact commit before handing off work.
Verify the required adapters, cache helpers and benchmark are present; a remote
branch name alone does not establish source identity. Never reset unrelated work.
Use the project's authorized source-transfer process. If an incremental Git
bundle is provided, verify its separately supplied SHA-256 manifest and
`git bundle verify` result, then import its explicitly supplied ref into a new
remote-tracking ref. Do not hardcode a developer's bundle path or timestamped ref.

Keep model weights, credentials, raw runtime evidence and unrelated dirty files
out of source handoffs. Transfer run evidence separately only when authorized.
The implementation prompt describes the required return evidence and integration
ownership.

## Primary references checked for this plan

- [MLX quantization](https://github.com/Blaizzy/mlx-audio/blob/main/docs/guides/quantization.md): evaluate real voice fidelity and total runtime memory.
- [Pocket CPU runtime](https://github.com/kyutai-labs/pocket-tts) and [MOSS Nano ONNX path](https://github.com/OpenMOSS/MOSS-TTS-Nano): CPU experiments are available without an NVIDIA device.
- [Faster Qwen runtime](https://github.com/andimarafioti/faster-qwen3-tts) and [dependency manifest](https://github.com/andimarafioti/faster-qwen3-tts/blob/main/pyproject.toml): its current Torch path uses CUDA graphs; default dependencies include Transformers 5 and `qwen-tts-hf`, which occupies the same Python package namespace as `qwen-tts`. Voicebox currently caps Transformers at 4.57.6. Re-audit exact pinned revisions before installation.
