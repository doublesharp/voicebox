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
unload, worker ownership, cancellation and failures. These checks do not qualify
real Turbo encoder output, synthesis quality/latency, GPU restoration or frozen
packaging. Those gates require existing Turbo weights and the target platform;
no model download is needed for this test suite.
