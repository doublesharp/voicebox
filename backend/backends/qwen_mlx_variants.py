"""Explicit optional MLX Base variants; BF16 remains the default."""

QWEN_MLX_QUANTIZED_VARIANTS = {
    "1.7B-8bit": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
    "1.7B-4bit": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-4bit",
}

QWEN_MLX_QUANTIZED_REVISIONS = {
    "1.7B-8bit": "e7dd0585652209fa0d7783659aad4e8a324de11c",
    "1.7B-4bit": "37e955a1deb861c088ae5f3a67043185f3d1a60c",
}
QWEN_MLX_REQUIRED_FILES = (
    "config.json",
    "generation_config.json",
    "merges.txt",
    "vocab.json",
    "tokenizer_config.json",
    "preprocessor_config.json",
    "model.safetensors",
    "model.safetensors.index.json",
    "speech_tokenizer/config.json",
    "speech_tokenizer/configuration.json",
    "speech_tokenizer/model.safetensors",
    "speech_tokenizer/preprocessor_config.json",
)
