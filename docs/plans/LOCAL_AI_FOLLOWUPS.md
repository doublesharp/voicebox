# Local AI follow-ups: Mac work and CUDA handoff

Prepared 2026-10-29. Planning deliverable; these follow-ups are not implemented by
this document. Implementation baseline is local `doublesharp` at
`a614d08afbe1d142fec62cef3d395eed27b7e05b`, including accepted local AI merge
`fe2f8394961c7f85293cbede6d79c487d6e9cc86`.

The prior batches passed 274 tests with 975/986 changed executable lines traced
(98.88%). They exercised real MLX synthesis and Turbo CPU conditioning/synthesis.
They did not qualify CUDA, desktop streaming playback, listening quality or
frozen builds. See [completed work](../local-ai-implementation-2026-10-29.md).

## Work we can implement and verify on this Apple Silicon Mac

| Batch | Deliverable | Acceptance here | Main responsibility |
| --- | --- | --- | --- |
| M1: playback | Connect incremental API audio to desktop playback, with bounded buffering, one continuous output stream, stop/replacement/device-change handling and underrun recovery. | Real desktop playback starts before synthesis completes; measure submission to first audible output separately from first model frame. Verify disconnect/repeated cancellation and retain buffered mastering behavior. | App playback code, Tauri audio lifecycle, focused backend/API integration. |
| M2: MLX efficiency | Trial explicit Qwen 8-bit variants, then 4-bit; record exact weight/codec revisions, prewarm the selected model and enforce a model-residency budget. Optionally compare a GGML/Metal runtime in isolation. | Matched BF16/quantized corpus, seeds and references; report latency, peak/steady unified memory, repeated unload behavior and listening artifacts. Preserve BF16 default until quality is accepted. | MLX adapters, model variant metadata, runtime identity and Mac benchmarks. |
| M3: CPU cloning | Audit and compare Pocket TTS, Chatterbox Nano and MOSS-TTS-Nano against existing LuxTTS/Turbo; consider Phonon only for presets. Integrate one qualifying candidate at a time. | Complete dependencies/codecs, offline real CPU synthesis, reference identity, pronunciation, sample-rate/channel conversion and frozen inference. Unsupported features must not be advertised. | Separate engine adapter/test/docs tracks; serialized registry and packaging integration. |
| M4: production quality | Add generated-speech verification, word alignment, caption timing and assisted phrase repair; improve reference-segment selection and engine-specific pronunciation controls. | Known omissions/repetitions/substitutions and timing fixtures; real local ASR/alignment runs; preserve the script and original take; user auditions repairs. ASR disagreement is advisory and alignment is not proof every word was spoken. | Studio services/UI, local ASR adapters and editing tests. |
| M5: recognition and management | Evaluate CPU/MLX ASR/streaming transcription where supported, optional refinement that preserves meaning, and persisted revision/license/runtime metadata. | Names, numbers, multilingual speech and hotkey-stop latency; offline and packaged checks on this Mac. Mark unsupported backends explicitly. | Transcription/refinement paths, provenance and model manager. |

M1 and an isolated M3 dependency/model trial can run in parallel. M2 shares MLX
runtime and model metadata with M3, so schedule their real GPU runs separately
and serialize registry/capability changes. M4 depends on a chosen local ASR and
aligner: start with bounded existing-runtime trials before changing defaults.
M5 metadata can proceed alongside UI work. Within each batch, use RED/GREEN
behavioral tests, scoped runtime coverage, documentation and bounded independent
review before merging accepted commits into `doublesharp`.

All local worktrees, temporary files, model downloads, native builds, audio,
coverage and raw evidence follow the mounted `/Volumes/Scratch/voicebox/`
layout. Run the resource preflight when budget assumptions change. Use separate
environments for model experiments; no shared environment dependency upgrades.
Read `.agents/skills/add-tts-engine/SKILL.md` and its full guide before adding an
engine, including the Phase 0 dependency audit and frozen-inference gates.

## Work assigned to a CUDA machine

| Batch | Deliverable | Hardware-dependent acceptance |
| --- | --- | --- |
| C0: trustworthy measurements | Confirm NVIDIA CUDA execution; adapt the offline benchmark's Scratch-root handling for the host; record device/driver/runtime and baseline timing/memory; validate existing Turbo conditioning reuse on CUDA. | Real device residency, CPU-owned cache payloads, memory/disk cache hits, speaker switching, cancellation/unload and finite audio on NVIDIA. No silent CPU fallback. |
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
Its output guard hardcodes `/Volumes/Scratch`; this must be deliberately adapted
for the CUDA host without weakening exclusive writes or symlink protections.
Turbo benchmarking needs its own admitted asset policy, not a validation bypass.

## Dependency and capability decisions

- NVIDIA is required to prove CUDA kernels, graph capture/replay, VRAM behavior
  and the packaged CUDA runtime. None of those acceptance claims can come from
  this Mac's fake device tests.
- MLX quantization and Metal-native experiments do not require CUDA. CPU cloning,
  verification, alignment, editing and provenance can be developed here.
- New models are candidates, not automatic defaults. Model-specific dependency,
  license, quality and packaging audits still decide adoption.
- Accelerated implementations need not produce bit-identical arrays to a stock
  runtime with different kernels. Test deterministic behavior within a fixed
  runtime, preserve requested speaker/text/settings, compare speech errors and
  listen to matched samples. The prior Turbo CPU parity result is a specific
  observation, not a universal GPU contract.

## Source handoff

The local `doublesharp` and the last observed `origin/doublesharp` have diverged;
the latter does not establish that the local AI commits are present. Do not
reset either branch or assume the remote branch contains this work.

A local incremental Git bundle accompanies this plan at
`/Volumes/Scratch/voicebox/reports/local-ai-followup-plan-20261029/voicebox-cuda-handoff.bundle`.
It exports `refs/heads/docs/local-ai-followup-plan-20261029` above prerequisite
commit `a5632679f8e37bdd71b4a99d78929902b58ff6df`. It contains committed code and
these instructions; it excludes local dirty packaging edits, model weights,
credentials and Scratch runtime evidence. A SHA-256 manifest is alongside it.
Transfer the bundle and prompt to the CUDA host using your normal source-transfer
process. The prompt describes verification/import. No push, external upload or
remote execution was performed to prepare this handoff.

## Primary references checked for this plan

- [MLX quantization](https://github.com/Blaizzy/mlx-audio/blob/main/docs/guides/quantization.md): evaluate real voice fidelity and total runtime memory.
- [Pocket CPU runtime](https://github.com/kyutai-labs/pocket-tts) and [MOSS Nano ONNX path](https://github.com/OpenMOSS/MOSS-TTS-Nano): CPU experiments are available without an NVIDIA device.
- [Faster Qwen runtime](https://github.com/andimarafioti/faster-qwen3-tts) and [dependency manifest](https://github.com/andimarafioti/faster-qwen3-tts/blob/main/pyproject.toml): its current Torch path uses CUDA graphs; default dependencies include Transformers 5 and `qwen-tts-hf`, which occupies the same Python package namespace as `qwen-tts`. Voicebox currently caps Transformers at 4.57.6. Re-audit exact pinned revisions before installation.
