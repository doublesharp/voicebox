# CUDA C0 qualification — 2026-10-30

This report covers batch C0 of the CUDA handoff in
[plans/LOCAL_AI_FOLLOWUPS.md](plans/LOCAL_AI_FOLLOWUPS.md): trustworthy
measurement tooling on an NVIDIA host and qualification of the existing
Chatterbox Turbo conditioning cache on CUDA. Batches C1 (accelerated Qwen) and
C2 (quantization and packaging) were not attempted. Raw reports, logs and audio
are retained outside Git on the CUDA host.

## Host and runtime

| Item | Value |
| --- | --- |
| OS | Windows 11 Pro 10.0.26200, AMD64, native (not WSL) |
| GPU | 1 × NVIDIA GeForce RTX 5070 Laptop GPU, 8151 MiB, compute capability 12.0 |
| Driver | 610.88 |
| Host RAM | 31.7 GiB |
| Runtime | Python 3.12.10, torch 2.11.0+cu128 (`torch.version.cuda` 12.8, `torch.version.hip` null), chatterbox-tts 0.1.7, transformers 4.57.3, huggingface-hub 0.36.2, numpy 1.26.4 |
| Precision | float32 model tensors (BF16 is supported by the GPU but not used by this adapter) |
| Checkpoint | `ResembleAI/chatterbox-turbo` revision `749d1c1a46eb10492095d68fbcf55691ccf137cd` (same revision as the CPU check) |
| Measured source | Clean tree whose `scripts/` and `backend/` are byte-identical to this series (recorded as commit `d9ca118`, a pre-reorder hash of the same code on `doublesharp` `76f2114`) |

The host is a hybrid-graphics laptop. Desktop applications held 0.8 to 1.7 GiB
of VRAM at 2 to 12% utilization before runs, so no measurement used an idle
GPU. The task used a dedicated virtual environment and scratch root; the shared
build environment and system drivers were not changed. References were two
synthetic Kokoro preset voices (`af_heart` 6.28 s, `am_michael` 7.22 s, 24 kHz
PCM), not personal recordings. All numbers below come from one set of runs of
that code; earlier runs from intermediate commits are superseded.

## Tooling changes

- `scripts/portable_output.py` supports native Windows. Every directory from
  the drive root to the run directory is pinned by a handle that denies delete
  sharing and is verified from the handle not to be a reparse point; the run
  handle's final path must equal the requested path. Directories it creates and
  artifacts get an owner-only protected ACL (current user and SYSTEM), matching
  POSIX 0700/0600. Artifacts use `CREATE_NEW` without following reparse points,
  and names containing `:` (NTFS streams) are refused. POSIX behavior is
  otherwise unchanged.
- `scripts/benchmark-local-tts.py` requires `--device` (`mlx`, `cpu` or
  `cuda[:N]`), records device identity, the selected GPU, tensor dtypes and
  per-stage telemetry (schema 2), and fails a run whose loaded tensors are not
  on the requested device. `cpu` hides every GPU (`CUDA_VISIBLE_DEVICES=-1`;
  an empty value does not hide GPUs on Windows). It admits `chatterbox_turbo`
  with a complete local asset policy and verifies the loaded revision.
- The offline guard refuses creation of any Internet socket, because asyncio's
  Windows Proactor loop connects and sends through calls that raise no audited
  connect or send event. The only exception is CPython's internal `socketpair`
  fallback, which every Windows event loop uses for its loopback self-pipe.
  DNS lookups stay refused. Child processes, native extensions and UNC paths
  are not covered by an in-process audit hook; the offline environment
  variables remain the defense there.
- `scripts/device_telemetry.py` records identity from `nvidia-smi` and torch,
  refuses ROCm/HIP or unavailable CUDA, and measures wall time, process CPU
  time, RSS/peak RSS and CUDA allocated/reserved current and peak per stage.
  CUDA synchronizes only at stage boundaries.
- `scripts/qualify-turbo-conditioning.py` drives the real Turbo backend and
  model through ordered checks. Spies only count calls and observe tensor
  devices, storage pointers and timing; inference is never replaced. Checks
  are `gating` (decide the exit status) or `recorded` (measurements the brief
  says to record rather than assume). `--lifecycle-cycles N` adds a gating
  load/prepare/generate/unload loop.

## Turbo conditioning cache on CUDA

`--lifecycle-cycles 10` on `cuda:0`: 17 of 17 gating checks passed, and both
recorded checks were bit-exact. No backend production change was needed. An
earlier runner version failed two checks because its own spies kept the model
alive; that runner defect was fixed with a regression test.

| Check | Evidence |
| --- | --- |
| Memory and disk hits skip preparation | Miss 2.54 s; memory hit 5.8 ms and disk hit 4.8 ms (107,985-byte entry), both with no `prepare_conditionals` call |
| Cached payload is CPU-owned | All 7 cached tensors on CPU in memory and on disk |
| Restoration uses fresh storage | 7 tensors restored on `cuda:0`, cached tensors unchanged after generation, conditioning cleared. Across devices the device check carries isolation; pointer comparison only matters on CPU |
| Identity misses | Content replaced at the same path and transcript change miss; a changed model identity string misses (simulated, not a second revision) |
| Built-in voice and failure | Empty prompt generates the built-in voice; a missing reference raises with no inference and no substitute speaker |
| Two speakers | Distinct keys; speaker embedding cosine 0.593 between A and B |
| Bypass | `use_cache=False` encodes once per generation and leaves memory and disk entries unchanged |
| Unload and reload | Allocated VRAM 4.47 GB before unload, 20 MB after; reload resident on `cuda:0` and hits disk without encoding |
| Cancellation | In-flight generation cancelled; the queued request's backend work started 0.4 ms after the cancelled worker finished, then hit the cache and generated |
| Lifecycle, 10 cycles | Every reload hit disk; allocated VRAM 28 MiB (reserved 44 MiB) after every unload |
| Output | Finite mono 24 kHz WAVs, 3.66 to 4.04 s, no clipping |
| Parity (recorded) | Cached vs bypassed generation at seed 42 bit-exact (max abs difference 0.0) |
| Repeats (recorded) | Cached pair and bypassed pair each bit-exact |

Host memory is not returned after unload. Across the ten cycles, RSS after
unload rose from 6,267 MiB to 6,436 MiB (about 15 to 19 MiB per cycle) and peak
working set from 9,830 MiB to 9,998 MiB. Whether this growth is bounded, and
its allocator-level cause, were not investigated.

## Benchmark baseline

Turbo, CUDA, reference A, seed 42, “Hello from Voicebox. This speech was
generated locally.”, 12 warm repeats, three runs. Every CUDA generation
returned 82,560 samples (3.44 s).

| Stage | Result |
| --- | --- |
| Load | 23.3 to 24.6 s; 4206 MiB allocated |
| Reference preparation (cache miss) | 2.41 to 2.69 s |
| First generation | 4.26 to 4.56 s |
| Generation peak | 4318 to 4328 MiB allocated, 4440 MiB reserved; RSS 4.3 GiB; peak working set 6.23 GiB (during load) |
| Warm generation, slow mode | 3.05 to 4.57 s (`wall_audio_rtf` 0.89 to 1.33), 28 of 36 repeats |
| Warm generation, fast mode | 1.24 to 1.34 s (`wall_audio_rtf` 0.36 to 0.39), 8 of 36 repeats |

Warm timing is bimodal with identical output, so a single p50 is misleading.
Two runs stayed in the slow mode for all 12 repeats; the other switched to the
fast mode at its fifth repeat and stayed there. In the slow mode process CPU
time is close to wall time (3.4 to 5.2 s) and the GPU sat in P2 at about
2.8 GHz with 14 to 21% utilization, so host work, not GPU compute, set the
pace. In the fast mode CPU time was about 3.5 times wall time, so host work ran
on several threads at once. What switches the mode is not known; Windows power
or scheduling state is a guess, not a finding. An earlier set of runs at an
intermediate commit showed the same two modes.

A CPU run of the same configuration recorded `cuda_available: false`
(`CUDA_VISIBLE_DEVICES=-1`), kept all tensors on CPU and took 6.4 to 7.1 s per
generation of 3.2 s audio (RTF about 2.2), with 38 to 40 s of CPU time per
generation.

These numbers come from one laptop GPU with a busy desktop and small samples.
They are a baseline for later C1 comparisons, not a performance claim.

## Unqualified

Listening quality, first-model-frame and audible latency, other NVIDIA GPUs,
Linux, frozen builds, the cause of the slow and fast host modes, and whether
host memory growth across reloads is bounded remain open. Symlink refusal paths
were exercised only where the account can create symlinks, which this Windows
account cannot; junction variants exercise the same reparse-point refusals.
