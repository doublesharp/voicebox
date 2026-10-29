# Qwen MLX Base reference codec cache

Bounded, in-memory CPU codec reuse applies to `MLXTTSBackend.generate` and
`MLXTTSBackend.generate_stream`.
Different target chunks/takes may share reference codec codes on the same
loaded model. The caller's current reference transcript and target text still
reach the model on every call. `create_voice_prompt(..., use_cache=False)` also
disables codec-cache reads and writes for that returned prompt.

## Installed API audit

Audited **mlx-audio 0.4.1**, MLX 0.32.4,
without a dependency change or downloads:

- `tts/models/qwen3_tts/qwen3_tts.py:862`: `Model.generate` accepts `ref_audio`
  and `ref_text`, but has no pre-encoded reference parameter. Its Base path
  loads/resamples/averages file audio through `mlx_audio.utils.load_audio`
  (`utils.py:519`) and calls `_generate_icl`.
- `qwen3_tts.py:499`: `_prepare_icl_generation_inputs` reshapes the normalized
  waveform to `[1, 1, samples]`, calls `self.speech_tokenizer.encode(ref_audio)`
  at line 536, and evaluates the returned codes. It separately combines the
  **current** transcript and target text into text conditioning and extracts
  the speaker embedding. Caching this whole preparation or its KV state
  across different target texts would be incorrect.
- `speech_tokenizer.py:1081`: `Qwen3TTSSpeechTokenizer.encode(self, audio)`
  delegates to `encoder_model.encode(audio)`, returning integer
  `[batch, quantizers, time]` codes. No encode options are accepted.
- `speech_tokenizer.py:1036`: the underlying encoder resets its audio and
  transformer caches at the start of each encode. Encoding is independent of
  the transcript/target and does not sample randomness. Subsequent misses
  reset state normally even after a hit skipped encoding.

The shim accepts only the exact audited upstream Base model, speech tokenizer,
encoder classes, package version and encoder argument names. Unsupported
versions/classes, instance overrides, keyword/extra encoder arguments, input
forms, and oversized/nonfinite inputs bypass reuse and keep upstream behavior.

## Ownership, keys and lifecycle

`backend/utils/mlx_reference_cache.py` wraps only `codec.encode` temporarily
inside a worker-side context. The wrapper disappears on success, error or
generator closure. The cache holds weak model/codec references, not bound
methods or model-retaining cycles. It never stores waveforms, paths, transcripts,
speaker embeddings, text embeddings, prefill state or KV state.

Keys hash the **actual encoder input**, after upstream audio normalization,
including its exact float32 dtype, shape and contents. Keys also include codec
sample rate/downsample settings and quantizer counts. Replacing file contents
at the same path therefore misses; identical normalized input from another
path can reuse codes. Transcript changes do not reuse text conditioning.

Values contain immutable CPU bytes, exact integer dtype/shape and an integrity
digest. Every hit reconstructs and evaluates a fresh MLX array on the worker;
caller mutations cannot alter stored codes. Defaults are **8 entries / 8 MiB**
of persistent code payloads, with LRU eviction. Each generation's staged values
have the same independent bounds. Inputs over **16 MiB** bypass hashing/copying.
Cache metadata is also bounded by the entry count. Nothing is written to disk.

The cache binds to each actual loaded model/codec lifetime. Load, unload and
size changes invalidate the scope. A lock protects CPU bookkeeping only;
unload can invalidate an epoch during encoding without waiting for MLX. Old
in-flight contexts cannot repopulate it. Generation publishes staged codes only
after successful completion. Cancellation invalidates pending publication;
later requests can reuse the still-loaded model's new epoch. Corrupt entries
miss and re-encode; encoding failures propagate through the existing cloning
error path with no replacement speaker. MLX load, seed, materialization, encode
and restore remain on the single MLX worker.

## Native-stream integration

The worker-side `frames()` iterator wraps the entire `model.generate` loop in
`_reference_cache.encoding(...)`. Both buffered and native calls honor the
`_mlx_reference_cache=False` bypass marker returned by
`create_voice_prompt(..., use_cache=False)`. The shim does not apply to style
adapters. Operation ownership remains held through generator closure and the
physical worker's final drain. Early close, failure and unload do not publish
staged codes. Tests cover early close with repeated cancellation, failure,
unload, cross-text reuse and cache bypass.

The HTTP preview endpoint explicitly closes its audio iterator inside scheduler
ownership. A disconnected or stalled send cannot release admission while the
native worker is still running. Endpoint regressions queue an unload or another
engine behind a blocked native worker and exercise repeated cancellation.

## Verification and limits

The initial backend regression was RED: two different target texts with the
same reference called the encoder twice (`2 != 1`). Deterministic tests use a
tiny fake model and **real MLX arrays on the worker**. They cover content and
transcript changes, bypass/options, bounds/LRU, fresh dtype/shape restoration,
mutation isolation, corrupt entries, weak ownership, model/codec identity,
reload/size changes, failures, cancellation and cross-thread unload during an
in-flight encode. Existing affinity and unload regressions run alongside them.
Coverage uses the existing tools package with `timid=True, concurrency=["thread"]`
against the changed source, including the worker closures. Final result:
**44 passed**; new helper **156/165 lines (94.55%)**, added executable backend
lines **16/16 (100%)**, combined changed source **172/181 (95.03%)**. Exact suite (set `VOICEBOX_WORK_DIR` to an approved absolute disposable directory):

```sh
python -m pytest -q \
  backend/tests/test_mlx_reference_cache.py \
  backend/tests/test_mlx_thread_affinity.py \
  backend/tests/test_mlx_unload_clears_cache.py \
  backend/tests/test_prompt_cache_reuse.py \
  --basetemp="$VOICEBOX_WORK_DIR/pytest-reference-cache" \
  -p no:cacheprovider
```

The new helper/tests pass Ruff. The existing backend retains its baseline's
11 Ruff findings; unrelated formatting/import cleanup is outside Track D.

One bounded real check loaded cached Base1.7B BF16 revision
`a6eb4f68e4b056f1215157bb696209bc82a6db48` offline and used the supplied synthetic
reference/transcript. Seed 42 and `max_tokens=16` bounded four short generations:
uncached, cache miss, same-text hit, different-text hit. Actual upstream encoder
call counts were **1, 1, 0, 0**. The first three outputs were exactly equal
(30,720 samples); the different-text output differed while reusing the codes.
Actual codes were `uint32 [1, 16, 80]`, **5,120 CPU bytes**. A Python profile hook
observed the original upstream encoder without patching its implementation.
All observed encoder calls ran on the same MLX worker.

The warm encoding itself took about **60 ms**. The warm miss and hit both took
about **4.21 seconds** to finish the short non-streaming output. This single
run establishes codec reuse and seeded parity, **not a demonstrated end-to-end
latency gain**, streaming time-to-first-audio, speech quality, other models,
CUDA behavior, or a hardware matrix. Speaker embedding, reference file loading,
and text-conditioned prefill still run each time. The real check used the
loaded backend's cache context directly around `model.generate(max_tokens=16)`;
backend `generate` integration is covered by deterministic tests. Upstream
printed a tokenizer-regex warning; no dependency/config changes were made.

Run provenance, logs, audio and coverage were retained outside Git. All runs
set `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` and explicitly redirected temporary
files and bytecode to task-owned disposable storage. No shared environment or
model cache was modified. Coverage used `timid=True` and thread tracing against
the actual changed source, including worker closures.

Parent integration also exercised the actual native Base adapter offline, using
seed 42, the same checkpoint/reference and `max_tokens=16` on the loaded model.
Encoder calls were **1, 1, 0, 0** for bypass, miss, same-text hit and different-text
hit. Bypass/miss/hit returned exactly equal seeded audio; changing target text
changed the output. Each run emitted four native frames and retained only
**5,120 bytes** of CPU codes. This bounded run is evidence of integration and
parity, not a statistically established speedup. Its run report was retained outside Git.
