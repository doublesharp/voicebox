# Chatterbox Turbo conditioning reuse

The Turbo backend reuses **encoded voice conditioning**, rather than merely
caching reference paths. `create_voice_prompt()` keeps the `ref_audio`/`ref_text`
dictionary interface; its hit flag is true only for a validated tensor payload.
Legacy path-only prompts also work and resolve the current audio content on
each generation. A missing requested reference raises instead of choosing a
different voice.

The implementation targets installed `chatterbox-tts` 0.1.7:

- `prepare_conditionals(path, exaggeration=0.0, norm_loudness=True)` matches
  `generate()` defaults; the preparation API itself defaults exaggeration to 0.5.
- `Conditionals` holds a `T3Cond` and the S3Gen reference dictionary. CPU clones
  of their tensors are saved in a versioned, `weights_only=True` compatible
  `.prompt` dictionary. Cached T3 speech embeddings must be absent because
  inference computes/mutates them. Runtime objects are never pickled.
- The key includes audio bytes, transcript, engine/format, HF snapshot revision,
  installed Chatterbox version, conditioning windows, token limit and preparation
  settings. Changing any identity creates a miss. Invalid schema, tensor shape,
  dtype, length, token range or non-finite values invalidate that entry.
- CPU payloads use the existing prompt cache and its atomic disk writer. Fresh
  storage is cloned before device restoration, including on CPU, so upstream
  mutation cannot change cached speaker tensors. Backend unload drops the CPU
  built-in voice snapshot; shared service unload still clears the shared memory
  cache. Disk entries remain reusable.
- Encoding, restoration, speaker assignment and generation run on a worker under
  the existing serialized inference scheduler. Cancellation drains the physical
  worker before another request enters. Request conditioning is cleared on
  success and failure. An empty prompt explicitly restores the loaded built-in
  voice; a model without one raises.
- `use_cache=False` returns metadata without preparing conditioning. Generation
  then encodes each call without reading or writing encoded cache entries.

`backend/tests/test_chatterbox_turbo_conditioning_cache.py` exercises the async
backend and current chunk pipeline with real installed `Conditionals`/`T3Cond`
classes, real CPU tensors and disk serialization, and a deterministic model
double. It covers reuse, voice switching, corruption, invalidation, bypass,
unload, worker ownership, cancellation and failures. These deterministic checks establish adapter/cache behavior. The separate real
CPU check below establishes actual encoder/synthesis compatibility; GPU
restoration, listening quality and frozen packaging remain separate gates. No
model download is needed for the deterministic test suite.

## Real CPU verification

A bounded offline check used `ResembleAI/chatterbox-turbo` revision
`749d1c1a46eb10492095d68fbcf55691ccf137cd`, Chatterbox 0.1.7 and PyTorch 2.15.0
on Apple M2 Max CPU with six Torch threads. The 6.35-second reference was macOS
Samantha synthetic speech, not a personal recording. Actual conditioning
creation took 3.21 seconds on the initial miss; the next memory hit took
1.52 ms and a hit after clearing the memory cache took 3.84 ms. Both hits skipped
the encoder. Cached and bypassed generation of “Hello from Voicebox.” with seed
42 produced exactly equal finite mono 24 kHz arrays (36,960 samples).

Generation took 13.07 seconds cached and 15.97 seconds bypassed in this one
sequence. This pair does not establish a general latency percentage or GPU
performance: process/kernel warmup and run order were not controlled. CUDA/XPU,
other references, listening fidelity and frozen packaging remain unqualified.
Raw provenance, timings and audio are on Scratch under
`reports/local-ai-20261029/turbo-runtime/`; the backend was unloaded afterward.
