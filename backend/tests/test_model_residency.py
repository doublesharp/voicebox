"""Model residency is a TTS lifecycle boundary, not a system RAM guarantee."""

import asyncio
import threading

import numpy as np
import pytest

from backend import backends


class Backend:
    def __init__(self, size="default"):
        self.model_size = size
        self.loaded = False
        self.loads = 0
        self.unloads = 0

    def is_loaded(self):
        return self.loaded

    def _is_model_cached(self, size="default"):
        return True

    async def load_model(self, size="default"):
        self.model_size = size
        self.loaded = True
        self.loads += 1

    load_model_async = load_model

    def unload_model(self):
        self.loaded = False
        self.unloads += 1


@pytest.fixture
def engines(monkeypatch):
    from backend.services import model_residency as residency

    monkeypatch.setattr(residency, "model_residency", residency.ModelResidency())
    monkeypatch.setattr(residency, "_estimated_weights", lambda config: config.size_mb)
    models = {"qwen": Backend("1.7B"), "moss_tts": Backend(), "kokoro": Backend()}
    monkeypatch.setattr(backends, "_tts_backends", models)
    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda engine: models[engine])
    return models


@pytest.mark.asyncio
async def test_default_switch_evicts_other_tts_model(engines):
    await backends.load_engine_model("qwen", "1.7B")
    assert engines["qwen"].is_loaded()
    await backends.load_engine_model("moss_tts")
    assert not engines["qwen"].is_loaded(), "Switching models retained the previous TTS engine without a bound"
    assert engines["moss_tts"].is_loaded()


def manager():
    from backend.services.model_residency import model_residency

    return model_residency


def cfg(name):
    return backends.get_model_config(name)


@pytest.mark.asyncio
async def test_estimated_budget_and_engine_limit_lru(engines):
    await manager().update_policy(2, 6000)
    await backends.load_engine_model("qwen", "1.7B")
    await backends.load_engine_model("moss_tts")
    assert engines["qwen"].loaded
    assert engines["moss_tts"].loaded
    await backends.load_engine_model("kokoro")
    assert not engines["qwen"].loaded
    assert engines["moss_tts"].loaded
    assert engines["kokoro"].loaded
    await manager().update_policy(1, 6000)
    assert not engines["moss_tts"].loaded
    assert engines["kokoro"].loaded


@pytest.mark.asyncio
async def test_budget_refusal_has_no_eviction_or_policy_side_effect(engines):
    await backends.load_engine_model("moss_tts")
    await manager().update_policy(1, 1000)
    with pytest.raises(ValueError, match="exceeds"):
        await backends.load_engine_model("qwen", "1.7B")
    assert engines["moss_tts"].loaded
    assert engines["moss_tts"].unloads == 0
    assert engines["qwen"].loads == 0
    assert manager().selected_model == "moss-tts"
    with pytest.raises(ValueError, match="exceeds"):
        await manager().update_policy(1, 512)
    assert manager().policy.estimated_weight_budget_mb == 1000


@pytest.mark.parametrize(
    ("count", "budget"), [(0, 1000), (5, 1000), (True, 1000), (1, 511), (1, 65537), (1, True), (1, 1.5)]
)
def test_invalid_limits_rejected(count, budget):
    from backend.services.model_residency import ResidencyPolicy

    with pytest.raises(ValueError, match="integer"):
        ResidencyPolicy(count, budget)


@pytest.mark.asyncio
async def test_cached_only_weights_warm_no_voice_or_history(engines, monkeypatch):
    engine = engines["moss_tts"]
    monkeypatch.setattr(engine, "_is_model_cached", lambda: False)
    with pytest.raises(ValueError, match="Download"):
        await manager().prewarm(cfg("moss-tts"))
    assert engine.loads == 0
    monkeypatch.setattr(engine, "_is_model_cached", lambda: True)
    result = await manager().prewarm(cfg("moss-tts"))
    assert result["mode"] == "weights_only"
    assert result["audio_saved"] is False
    assert result["last_voice_prewarm"] is None
    assert result["persistence"] == "session"


@pytest.mark.asyncio
async def test_exact_voice_warm_and_failure_restore_previous_model(engines, monkeypatch):
    await backends.load_engine_model("qwen", "1.7B")
    seen = []

    async def prepare():
        seen.append("exact-profile")
        return {"speaker": "exact-profile"}

    async def generate(text, prompt, language, seed):
        assert prompt == {"speaker": "exact-profile"}
        assert text == "Your selected voice is ready."
        return np.ones(20, dtype=np.float32), 24000

    monkeypatch.setattr(engines["moss_tts"], "generate", generate, raising=False)
    result = await manager().prewarm(cfg("moss-tts"), profile_id="exact-profile", prepare_prompt=prepare)
    assert seen == ["exact-profile"]
    assert result["mode"] == "voice"
    assert result["last_voice_prewarm"]["profile_id"] == "exact-profile"

    async def fail():
        raise RuntimeError("reference encoding failed")

    with pytest.raises(RuntimeError, match="encoding failed"):
        await manager().prewarm(cfg("qwen-tts-1.7B"), profile_id="other-profile", prepare_prompt=fail)
    assert engines["moss_tts"].loaded
    assert not engines["qwen"].loaded
    assert manager().selected_model == "moss-tts"
    assert manager().last_voice_prewarm == result["last_voice_prewarm"]


@pytest.mark.asyncio
async def test_repeated_cancel_drains_before_eviction_and_later_load_recovers(engines, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    async def prepare():
        return {"speaker": "exact-profile"}

    async def generate(*args, **kwargs):
        def blocked():
            entered.set()
            release.wait(5)
            return np.ones(20, dtype=np.float32), 24000

        return await asyncio.to_thread(blocked)

    monkeypatch.setattr(engines["moss_tts"], "generate", generate, raising=False)
    warm = asyncio.create_task(manager().prewarm(cfg("moss-tts"), profile_id="exact-profile", prepare_prompt=prepare))
    assert await asyncio.to_thread(entered.wait, 2)
    warm.cancel()
    await asyncio.sleep(0)
    warm.cancel()
    switch = asyncio.create_task(backends.load_engine_model("qwen", "1.7B"))
    await asyncio.sleep(0.03)
    assert not warm.done()
    assert not switch.done()
    assert engines["moss_tts"].unloads == 0
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await warm
    await switch
    assert engines["qwen"].loaded
    assert not engines["moss_tts"].loaded
    assert manager().last_voice_prewarm is None


@pytest.mark.asyncio
async def test_eviction_drops_only_identifiable_owning_cpu_cache(engines):
    from backend.utils import cache

    original = dict(cache._memory_cache)
    try:
        cache._memory_cache.clear()
        cache._memory_cache["qwen"] = {"format": "qwen-voice-clone-v2", "items": []}
        cache._memory_cache["turbo"] = {"format": "chatterbox-turbo-conditionals-v1"}
        await backends.load_engine_model("qwen", "1.7B")
        await backends.load_engine_model("moss_tts")
        assert "qwen" not in cache._memory_cache
        assert "turbo" in cache._memory_cache
    finally:
        cache._memory_cache.clear()
        cache._memory_cache.update(original)


@pytest.mark.asyncio
async def test_model_management_load_callable_uses_residency(engines):
    await backends.get_model_load_func(cfg("qwen-tts-1.7B"))()
    await backends.get_model_load_func(cfg("moss-tts"))()
    assert not engines["qwen"].loaded
    assert engines["moss_tts"].loaded


@pytest.mark.asyncio
async def test_single_model_engine_accepts_legacy_generation_size_default(engines):
    await backends.load_engine_model("moss_tts", "1.7B")
    assert engines["moss_tts"].loaded
    assert manager().selected_model == "moss-tts"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["qwen", "qwen_custom_voice", "tada"])
async def test_multiple_size_engines_reject_unknown_selection(engines, engine):
    with pytest.raises(ValueError, match="Unknown TTS model selection"):
        await backends.load_engine_model(engine, "unknown")


@pytest.mark.asyncio
async def test_cancelled_warm_does_not_publish_success_or_keep_new_model(engines, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    async def prepare():
        return {"speaker": "exact"}

    async def generate(*args, **kwargs):
        def blocked():
            entered.set()
            release.wait(5)
            return np.ones(10, dtype=np.float32), 48000

        return await asyncio.to_thread(blocked)

    monkeypatch.setattr(engines["moss_tts"], "generate", generate, raising=False)
    operation = asyncio.create_task(manager().prewarm(cfg("moss-tts"), profile_id="exact", prepare_prompt=prepare))
    assert await asyncio.to_thread(entered.wait, 2)
    operation.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not engines["moss_tts"].loaded
    assert manager().last_voice_prewarm is None


@pytest.mark.asyncio
async def test_none_size_normalizes_in_public_and_internal_load_hooks(engines):
    await backends.load_engine_model("moss_tts", None)
    assert engines["moss_tts"].loaded
    await backends._load_engine_model_unmanaged("moss_tts", None)
    assert engines["moss_tts"].loads == 2


@pytest.mark.asyncio
async def test_failed_cached_model_load_restores_prior_selection(engines, monkeypatch):
    await backends.load_engine_model("qwen", "1.7B")

    async def fail():
        raise RuntimeError("cached runtime failed to load")

    monkeypatch.setattr(engines["moss_tts"], "load_model", fail)
    with pytest.raises(RuntimeError, match="failed to load"):
        await manager().prewarm(cfg("moss-tts"))
    assert engines["qwen"].loaded
    assert not engines["moss_tts"].loaded
    assert manager().selected_model == "qwen-tts-1.7B"
