"""Quantized variants retain identity, explicit selection and platform boundaries."""


import pytest
from pydantic import ValidationError

from backend import backends
from backend.backends.mlx_backend import MLXTTSBackend
from backend.models import GenerationRequest


@pytest.mark.parametrize("bits", [8, 4])
def test_mlx_variant_routes_matching_download_and_generation_model(monkeypatch, bits):
    monkeypatch.setattr(backends, "get_backend_type", lambda: "mlx")
    size = f"1.7B-{bits}bit"
    config = next(
        (item for item in backends.get_tts_model_configs() if item.engine == "qwen" and item.model_size == size), None
    )
    assert config is not None, "Explicit quantized MLX variant is missing from model inventory"
    assert config.hf_repo_id == f"mlx-community/Qwen3-TTS-12Hz-1.7B-Base-{bits}bit"
    assert config.model_name == f"qwen-tts-{size}"
    assert MLXTTSBackend(size)._get_model_path(size) == config.hf_repo_id
    request = GenerationRequest(profile_id="voice", text="A sample.", engine="qwen", model_size=size)
    assert request.model_size == size


def test_non_mlx_registry_does_not_advertise_mlx_precision(monkeypatch):
    monkeypatch.setattr(backends, "get_backend_type", lambda: "pytorch")
    assert not any(item.model_size.endswith("bit") for item in backends.get_tts_model_configs())


@pytest.mark.parametrize("engine", ["chatterbox_turbo", "qwen_custom_voice", "qwen_voice_design"])
def test_precision_request_does_not_silently_change_another_engine(engine):
    with pytest.raises(ValidationError, match="Qwen Base"):
        GenerationRequest(profile_id="voice", text="A sample.", engine=engine, model_size="1.7B-8bit")


@pytest.mark.asyncio
async def test_profile_resolved_engine_cannot_silently_ignore_precision(monkeypatch):
    def unexpected(_):
        pytest.fail("Unsupported precision reached a different engine")

    monkeypatch.setattr(backends, "get_tts_backend_for_engine", unexpected)
    with pytest.raises(ValueError, match="Qwen Base"):
        await backends.load_engine_model("kokoro", "1.7B-4bit")
