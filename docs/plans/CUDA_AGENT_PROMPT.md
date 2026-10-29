# CUDA implementation agent prompt

Implement and verify NVIDIA CUDA improvements for Voicebox. Work in bounded
batches with branches/worktrees, behavioral RED/GREEN tests, runtime coverage,
real GPU benchmarks, updated docs and an independent correctness review. Return
integration-ready commits and evidence. The requester has an Apple Silicon Mac
and cannot validate CUDA themselves. This is an implementation task: do not stop
at research or a performance proposal when a compatible improvement can be
implemented and verified.

## 1. Establish the exact source and hardware

Repository: https://github.com/doublesharp/voicebox.git . Use an existing checkout
if provided. Read all applicable AGENTS.md instructions and local skills. Do not
discard unrelated edits. The required local AI baseline is merge
`fe2f8394961c7f85293cbede6d79c487d6e9cc86`; documentation baseline is
`a614d08afbe1d142fec62cef3d395eed27b7e05b`. A normal clone of the remote branch
may not include these local commits. Do not implement against an older baseline.

An incremental `voicebox-cuda-handoff.bundle` may be supplied with this prompt.
Verify its SHA-256 against the separately supplied manifest. From an existing
repository containing prerequisite commit
`a5632679f8e37bdd71b4a99d78929902b58ff6df`, import it as follows, substituting the
actual bundle path (not an untrusted shell-interpolated string):

```bash
git bundle verify /path/to/voicebox-cuda-handoff.bundle
git fetch /path/to/voicebox-cuda-handoff.bundle refs/heads/docs/local-ai-followup-plan-20261029:refs/remotes/voicebox-handoff/local-ai-followups
git merge-base --is-ancestor fe2f8394961c7f85293cbede6d79c487d6e9cc86 refs/remotes/voicebox-handoff/local-ai-followups
```

Create your own `improve/cuda-local-ai-<date>` worktree/branch from that imported
ref using the active harness's worktree procedure. If the baseline is already
present without a bundle, use a source ref containing it. If prerequisites are
missing, fetch the fork's history where authorized or report the exact missing
commit; continue the independent hardware/dependency audit while source transfer
is resolved. Never reset an existing `doublesharp` or remote-tracking branch.

Read these repo-relative files first:

- `docs/local-ai-implementation-2026-10-29.md`
- `docs/plans/LOCAL_AI_FOLLOWUPS.md`
- `docs/content/docs/developer/local-ai-benchmarks.mdx`
- `docs/chatterbox-turbo-conditioning.md`
- `backend/services/inference_scheduler.py`
- `backend/utils/native_stream.py` and `backend/utils/streaming_audio.py`
- `backend/backends/pytorch_backend.py`, `qwen_custom_voice_backend.py`,
  `qwen_voice_design_backend.py`, `chatterbox_turbo_backend.py`, and the registry
- `backend/routes/generations.py` and relevant regression tests
- Dependency/build files and `.agents/skills/add-tts-engine/SKILL.md`; if adding
  an adapter/runtime, complete its Phase 0 audit and read the full referenced guide.

Record OS/architecture, GPU name/count/VRAM/compute capability, driver,
`torch.__version__`, `torch.version.cuda`, `torch.version.hip`, actual available
device and chosen dtype. Use `nvidia-smi` plus actual Torch tensor/model device
checks. CUDA API availability alone can also describe ROCm: require NVIDIA for
this qualification. Use driver-compatible official Torch wheels in task-owned
environments; do not change system drivers or shared environments. Treat GPUs
with insufficient memory or unsupported BF16 as explicit hardware constraints.

Follow the CUDA host's approved scratch-volume policy. Do not assume this Mac's
`/Volumes/Scratch` exists on Linux or Windows; do not silently substitute system
temporary directories. If applicable instructions require a specific mount,
stop disposable-output work when it is missing. Put environments, models, build
outputs, audio, logs and coverage under the approved task-owned scratch root.
Set TMPDIR/TEMP and Python/library cache paths explicitly. Check free capacity
before downloads/builds and budget simultaneous workers. Download only the
checkpoints/codecs needed for the current batch, at pinned revisions.

## 2. Batch C0: measurement and existing Turbo CUDA qualification

First establish a reliable baseline before optimization.

The existing CLI `scripts/benchmark-local-tts.py` admits Qwen Base, CustomVoice
and VoiceDesign only. It pins assets, refuses missing caches, blocks networking
during inference and records partial failure reports. It currently hardcodes
`/Volumes/Scratch` and calls complete-array generation. Adapt or extend it for
an explicit approved host scratch root, required CUDA device selection and CUDA
telemetry, preserving exclusive/no-symlink writes and existing Mac behavior.
Never invent a first-frame measurement from complete-array elapsed time. Add
streaming measurements only when a real frame boundary exists. Turbo requires
an audited complete asset-admission policy or a separate equally strict runner.

Record separately: import/init, model load, reference preparation, graph/compile
initialization, first generation, steady-state generation, first model audio,
end-to-end output drain, peak/resident CPU RAM and CUDA allocated/reserved VRAM.
Use wall-clock completion measurements that account for CUDA's asynchronous
execution; synchronize only at valid boundaries, never force synchronization in
the production per-token loop. Distinguish first model frame from HTTP arrival
and first audible device output. Use Voicebox's `wall_audio_rtf = wall_s /
audio_s` convention (lower is better), not upstream's occasionally inverted RTF.

Qualify the already implemented Chatterbox Turbo conditioning cache on NVIDIA:
actual memory/disk hits skip preparation; cached tensors remain CPU-owned;
device restoration uses fresh storage; changing references/content/transcript
or model identity misses appropriately; built-in voice restoration works;
failures never substitute a different speaker. Cover two distinct authorized
reference samples, cache bypass, reference replacement, repeated requests,
unload/reload, cancellation and subsequent engine admission. Test fixed-seed
cache/bypass parity within the same supported runtime; record any non-bit-exact
behavior and its cause rather than assuming the prior CPU result applies.
Do not silently route the test to CPU. Fix concrete GPU issues with regression
coverage; do not rewrite the working cache without evidence.

## 3. Batch C1: implement opt-in accelerated Qwen on CUDA

Start with Base 0.6B; qualify 1.7B if VRAM permits. Extend to CustomVoice and
VoiceDesign only for modes actually supported and tested by the candidate.

Audit the current upstream runtime and pin the tested commit/version:
https://github.com/andimarafioti/faster-qwen3-tts . Compare the stock adapter
with CUDA-graph acceleration. Treat supported SDPA/FlashAttention or compilation
changes as separate experiments so their benefit and compatibility can be
attributed. Do not install every optimization or assume custom kernels beat
the stock path. No custom CUDA kernel rewrite is required absent a measured need.

Dependency boundary is mandatory: at planning time the upstream default
manifest requires Transformers >=5.15.1,<6, Hugging Face Hub >=1.5,<2 and
`qwen-tts-hf`, which exports `qwen_tts`. Voicebox currently requires
Transformers <=4.57.6 and uses upstream `qwen-tts`. Re-audit these facts before
installing. Never install both `qwen_tts` distributions together or upgrade the
shared application stack to make an experiment pass. Start in a separate
environment. If the dependency conflict remains, integrate an explicit local
worker/process boundary with task-owned dependencies and lifecycle management,
or another demonstrated compatible pinned solution. Do not expose a new hosted
service or leave an external demo server as the app integration.

Implement a usable explicit opt-in runtime/configuration with truthful registry,
cache/readiness and provenance identity. Keep current defaults and non-CUDA
imports operational. Unsupported device/model/voice/language combinations must
fail clearly before rendering audio, or use a documented user-selected fallback
whose actual runtime is recorded. Never silently report the optimized runtime
while executing the stock/CPU path. Preserve requested ICL cloning semantics;
do not obtain a speedup by switching to x-vector-only cloning, lowering token
caps, changing sampling/precision or shortening output without recording that
as a separate behavior change. Do not advertise instructions for Qwen Base.

For qualified native streaming, reuse the existing frame bridge where applicable
or an equivalently bounded local-worker transport. Preserve mono finite frames,
consistent sample rate, consecutive codec audio, utterance-only crossfades,
backpressure and explicit closure. Keep existing full-utterance runaway/trim
checks where required; bypassing them is not a latency optimization. Physical
worker shutdown/drain must remain inside scheduler ownership after disconnect,
stalled send, stop, repeated cancellation, load/unload or worker error. Retain
full-file normalization/effects/export behavior. Do not modify desktop playback
or the MLX adapters in this track.

Test graph capture/warmup/replay, variable text/reference lengths, two speakers,
model/size changes, fresh seed behavior across repeated replays, max sequence
bounds, OOM/capture failure recovery and unload/reload without stale pointers or
retained model state. A graph's fixed buffers must not freeze RNG, return a
previous request's audio or leak speaker conditioning across callers.

## 4. Batch C2: precision, memory and packaged inference

After a working C1 baseline, trial supported 8-bit first, then 4-bit paths if
this hardware/runtime actually supports them. Record which components are
quantized and which codecs/caches stay at higher precision. Keep quantized
variants explicit; use matched reference/text/language/sampling and compare
voice similarity, speech errors and listening samples. Do not promise an
8/4-bit speedup or a memory ratio from weight size alone. Reject variants that
fail the stated use case rather than making them defaults.

Package the accepted CUDA path for the host's actual OS using the existing build
workflow. Include worker runtime/dependencies if isolation is needed and account
for installed size. Run the packaged artifact offline through real checkpoint
load, cloning synthesis, streaming/stop if supported, unload/reload and recovery.
`--help` or mocked PyInstaller arguments alone are not frozen-inference proof.
Qualify Linux on Linux or native Windows on native Windows; WSL does not qualify
native Windows, and one GPU does not establish a hardware matrix. Record any
packaging blocker exactly and keep the runtime explicitly experimental until
that acceptance gate is cleared.

## 5. Verification and benchmark discipline

- Capture behavioral RED before each testable production change, then GREEN.
  Add focused tests for device refusal, configuration/registry identity, offline
  asset admission, versioned cache isolation, frame validation and lifecycle
  failures. Run the affected existing ownership/disconnect/cache regressions.
- Instrument actual changed production source, including worker code where
  possible. Aim for >=95% changed executable line coverage; explain every gap.
  Exercise critical failure/ownership branches. Report CPU fixture, real CUDA,
  subprocess and frozen coverage separately; source-only fakes are not GPU proof.
- Fix a matched evaluation corpus before comparing: short, medium and paragraph
  text; names/numbers/punctuation; at least two references and multiple seeds;
  supported languages and styles only. Use the same precise assets and decoding
  settings. Record output duration and token/early-stop limits to detect cheating
  through truncated or changed speech. Use synthetic or explicitly authorized
  references; do not harvest personal voices or upload recordings.
- Run both baselines and candidates on this same idle GPU, warming each runtime
  with complete utterances. Separate cold setup from warm use. Alternate run
  order, run at least ten warm samples for the principal comparison, report
  p50/p95 and raw counts, and note small-sample limitations. Run memory/long-form
  and at least ten load/generate/unload cycles separately. Never benchmark two
  GPU workers concurrently or compare a warm candidate with a cold baseline.
- Different kernels/precision may not be bit-identical. Test reproducibility
  within each fixed runtime and compare decoded speech, ASR error signals,
  duration, finite output, clipping, omissions/repetitions and speaker identity.
  Export matched audio and a blind audition manifest. If no listening review
  occurs, state that quality remains unqualified and keep the path opt-in.
- Adopt only optimizations whose improvement is repeatable and whose lifecycle
  and target quality gates pass. Report unsuccessful experiments honestly. If
  no accelerated candidate qualifies, still deliver verified C0 tooling/fixes
  and the exact compatibility or quality blocker; do not call CUDA acceleration
  implemented merely because an upstream demo runs.

## 6. Ownership, review and handoff

Keep CUDA-specific adapters/helpers/tests and a CUDA report in focused commits.
Avoid unrelated cleanup. Keep shared registry, scheduler, benchmark, requirements
and build-file changes minimal and in separate commits: the Mac owner is working
on MLX/CPU/UI tracks. Coordinate shared-file ownership through the existing task
mechanism; do not launch overlapping writers. Parallelize independent source
review, deterministic tests and docs only; serialize expensive GPU execution.
Use one independent reviewer after verification, with at most two scoped
material-fix rechecks. Fix small findings yourself.

Return branch name, base/head hashes, commit list and integration order; exact
setup/test/coverage/benchmark/build commands; GPU/driver/runtime/dependency and
checkpoint revisions; baseline/candidate timing, VRAM and quality results;
real-vs-fake and packaged-vs-development proof; unresolved gates and rollback/
opt-out behavior. Keep short maintained summaries in docs and all raw logs,
JSON, traces, audio and build artifacts on approved scratch storage, never Git.
Provide patches/bundle for the integration owner to transfer. Do not push,
release, deploy or merge into `doublesharp` from this CUDA track. The Mac owner
will review and run non-CUDA regressions before merging your accepted commits.

Do not expand this task into Pocket/MOSS/Nano integrations, ASR, Studio editing,
MLX quantization, voice conversion or a full-duplex agent. Those belong to the
separate local plan. Do not claim Mac/Metal/CPU or other NVIDIA cards were tested
when they were not available.
