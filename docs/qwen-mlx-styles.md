# Qwen style engines on Apple Silicon

`qwen_custom_voice` and `qwen_voice_design` follow the same platform selection
as Qwen Base: MLX on functional Apple Silicon installations, existing PyTorch
adapters elsewhere. Registry downloads, cache readiness, model inventory, and
Studio runtime identity select the matching adapter and repository. MLX uses
`mlx-community/Qwen3-TTS-12Hz-{size}-CustomVoice-bf16` (1.7B/0.6B) and
`mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16`. No quantized default is added.

The implementation follows the installed **mlx-audio 0.4.1** source:
`tts.utils.load` eagerly loads weights by default, and the Qwen model exposes
`generate_custom_voice(text, speaker, language, instruct)` and
`generate_voice_design(text, instruct, language)`, yielding audio results.
CustomVoice forwards the profile's preset speaker (default Ryan) and request
instruction. VoiceDesign keeps the profile description first and appends request
direction, matching PyTorch. Language codes map to Qwen's full language names;
unknown codes use `auto`. Explicit seeds, including zero, seed `mlx.core.random`
on the inference worker. Seed forwarding does not establish cross-backend or
cross-version bitwise reproducibility.

Loading, inference, seed changes, and NumPy audio materialization use the existing
single MLX executor. The adapter's operation lock stays held until physical work
finishes even after repeated cancellation. Unload drops the published model
inline; a generation already holding it can finish and then drains the cache.
Unload during loading prevents late publication. Load errors, a mismatched
checkpoint type, missing generation methods, or missing text/speech tokenizers
never publish readiness. The module does not import MLX's native runtime at import
or construction time.

These models do not clone reference audio. MLX rejects reference-bearing or
incompatible voice prompts rather than substituting a preset or designed voice.
Normal preset/designed profile prompts retain their existing defaults. Empty
audio result streams and inconsistent sample rates are errors.

`backend/tests/test_qwen_mlx_styles.py` exercises these application contracts with
offline CPU fakes, local cache fixtures, and the actual shared executor.
On 2026-10-29, real offline synthesis also passed for CustomVoice 0.6B and
VoiceDesign 1.7B on Apple Silicon, using mlx-audio 0.4.1 and BF16 checkpoints.
Both produced finite 24 kHz native audio frames. CustomVoice 1.7B, listening
quality, cross-backend/repeated-seed waveform equality, packaged macOS, and real
non-Mac PyTorch synthesis remain separate gates. See
[local AI implementation](local-ai-implementation-2026-10-29.md).

The optional `generate_stream` adapter method emits model-native frames through a
bounded worker bridge. Use it through incremental preview when no whole-output
trim or runaway validation is required. It does not change the buffered export
or mastering contract. Model-frame arrival is not device playback latency.
