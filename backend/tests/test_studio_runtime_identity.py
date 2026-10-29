"""Frozen identity follows the engine's adapter and installed engine library."""

import importlib.metadata

import pytest

from backend import backends
from backend.studio.capabilities import capability_for_config
from backend.studio.jobs import synthesize
from backend.utils import platform_detect


def selected(monkeypatch, engine, backend):
    monkeypatch.setattr(platform_detect, "get_backend_type", lambda: backend)
    monkeypatch.setattr(backends, "get_backend_type", lambda: backend)
    return next(config for config in backends.get_tts_model_configs() if config.engine == engine)


@pytest.mark.parametrize("runtime,adapter", [
    ("mlx", "qwen_mlx_styles_backend.QwenMLXCustomVoiceBackend"),
    ("pytorch", "qwen_custom_voice_backend.QwenCustomVoiceBackend"),
])
def test_custom_voice_records_its_actual_platform_adapter(monkeypatch, runtime, adapter):
    config = selected(monkeypatch, "qwen_custom_voice", runtime)
    identity = capability_for_config(config)["registry_identity"]
    assert identity["backend"] == runtime
    assert identity["adapter"].endswith(adapter)


def test_selected_engine_package_changes_the_frozen_registry_revision(monkeypatch):
    config = selected(monkeypatch, "chatterbox", "pytorch")
    versions = {"chatterbox-tts": "1"}
    monkeypatch.setattr(importlib.metadata, "version", lambda package: versions.get(package, "same"))
    before = capability_for_config(config)
    versions["chatterbox-tts"] = "2"
    after = capability_for_config(config)
    assert before["registry_identity"] != after["registry_identity"]
    assert before["registry_revision"] != after["registry_revision"]


@pytest.mark.asyncio
async def test_changed_engine_runtime_is_refused_before_model_access(monkeypatch):
    config = selected(monkeypatch, "chatterbox", "pytorch")
    versions = {"chatterbox-tts": "1"}
    monkeypatch.setattr(importlib.metadata, "version", lambda package: versions.get(package, "same"))
    capabilities = capability_for_config(config)
    voice = {"engine": config.engine, "model_size": config.model_size, "voice_type": "cloned"}
    versions["chatterbox-tts"] = "2"

    async def no_model_access(*args):
        raise AssertionError("Model access preceded the frozen-runtime check")

    monkeypatch.setattr(backends, "ensure_model_cached_or_raise", no_model_access)
    with pytest.raises(ValueError, match="Frozen model identity"):
        await synthesize({"passage": {"voice_snapshot": voice}, "capabilities": capabilities}, None)
