"""Session-only TTS residency limits and explicit cached-asset prewarming.

The weight budget is an estimate from declared/cached assets, never a RAM cap.
All mutations share the physical inference owner with generation and shutdown.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .inference_scheduler import check_inference_cancelled, serialized_inference

WARMUP_TEXT = "Your selected voice is ready."


@dataclass(frozen=True)
class ResidencyPolicy:
    max_resident_engines: int = 1
    estimated_weight_budget_mb: int = 16384

    def __post_init__(self):
        for value, lower, upper, name in (
            (self.max_resident_engines, 1, 4, "max_resident_engines"),
            (self.estimated_weight_budget_mb, 512, 65536, "estimated_weight_budget_mb"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{name} must be an integer from {lower} to {upper}")


def _backends():
    from .. import backends

    return backends


def _model_config(engine, size):
    configs = [c for c in _backends().get_tts_model_configs() if c.engine == engine]
    if len(configs) == 1 and engine not in {"qwen", "qwen_custom_voice", "tada"}:
        return configs[0]
    for config in configs:
        if config.model_size == size:
            return config
    raise ValueError(f"Unknown TTS model selection: {engine}/{size}")


def _loaded():
    result = []
    for engine, backend in tuple(_backends()._tts_backends.items()):
        if not backend.is_loaded():
            continue
        size = getattr(backend, "_current_model_size", None) or getattr(backend, "model_size", "default")
        result.append((_model_config(engine, size), backend))
    return result


def _estimated_weights(config):
    """Use actual cached weight bytes where larger than the declared estimate."""
    from huggingface_hub import snapshot_download

    declared = max(1, int(config.size_mb))
    total = 0
    seen = set()
    repos = [
        (config.hf_repo_id, config.revision),
        *((repo, config.aux_revisions.get(repo)) for repo in config.aux_hf_repo_ids),
    ]
    for repo, revision in repos:
        try:
            root = Path(snapshot_download(repo, revision=revision, local_files_only=True))
        except (OSError, ValueError):
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".safetensors", ".bin", ".pt", ".data", ".onnx"} or not path.is_file():
                continue
            identity = path.resolve()
            if identity not in seen:
                seen.add(identity)
                total += path.stat().st_size
    return max(declared, math.ceil(total / 1_000_000))


def _cached(config):
    backend = _backends().get_tts_backend_for_engine(config.engine)
    # Every local backend exposes this check; size-specific engines receive the
    # actual selected variant, while single-model engines use their default.
    if config.engine in {"qwen", "qwen_custom_voice", "qwen_voice_design", "tada"}:
        return backend._is_model_cached(config.model_size)
    return backend._is_model_cached()


class ModelResidency:
    def __init__(self):
        self.policy = ResidencyPolicy()
        self.selected_model = None
        self.last_voice_prewarm = None
        self._clock = 0
        self._last_used = {}
        self._costs = {}

    def _evict(self, config, backend):
        # Do not clear global caches belonging to other loaded models. Each
        # owning backend disposes of its CPU/device prompt state on unload.
        backend.unload_model()
        from ..utils import cache

        formats = {
            "qwen": "qwen-voice-clone-v2",
            "chatterbox_turbo": "chatterbox-turbo-conditionals-v1",
        }
        owned_format = formats.get(config.engine)
        for key, payload in tuple(cache._memory_cache.items()):
            if (owned_format and isinstance(payload, dict) and payload.get("format") == owned_format) or (
                config.engine == "luxtts" and key.startswith("luxtts_")
            ):
                cache._memory_cache.pop(key, None)
        self._last_used.pop(config.engine, None)
        self._costs.pop(config.engine, None)
        if self.last_voice_prewarm and self.last_voice_prewarm["model_name"] == config.model_name:
            self.last_voice_prewarm = None

    async def _plan(self, target, policy):
        loaded = _loaded()
        configs = {c.engine: c for c, _ in loaded}
        if target is not None:
            configs[target.engine] = target
        costs = await asyncio.to_thread(lambda: {engine: _estimated_weights(c) for engine, c in configs.items()})
        protected = target or next((c for c, _ in loaded if c.model_name == self.selected_model), None)
        if protected and costs[protected.engine] > policy.estimated_weight_budget_mb:
            raise ValueError("Selected model exceeds the estimated weight budget; increase the budget first")
        candidates = sorted(
            [(c, b) for c, b in loaded if protected is None or c.engine != protected.engine],
            key=lambda pair: (self._last_used.get(pair[0].engine, 0), pair[0].engine),
        )
        evictions = []
        while len(configs) > policy.max_resident_engines or sum(costs.values()) > policy.estimated_weight_budget_mb:
            if not candidates:
                raise ValueError("The requested residency policy cannot retain the selected model")
            config, backend = candidates.pop(0)
            evictions.append((config, backend))
            configs.pop(config.engine)
            costs.pop(config.engine)
        return evictions, costs

    @serialized_inference
    async def load_model(self, config, operation):
        evictions, costs = await self._plan(config, self.policy)
        for old, backend in evictions:
            self._evict(old, backend)
        try:
            await operation()
        except BaseException:
            if not any(c.model_name == self.selected_model for c, _ in _loaded()):
                self.selected_model = None
            raise
        self._clock += 1
        self._last_used[config.engine] = self._clock
        self._costs = costs
        if self.selected_model != config.model_name:
            self.last_voice_prewarm = None
        self.selected_model = config.model_name

    @serialized_inference
    async def update_policy(self, max_resident_engines, estimated_weight_budget_mb):
        policy = ResidencyPolicy(max_resident_engines, estimated_weight_budget_mb)
        evictions, costs = await self._plan(None, policy)
        for config, backend in evictions:
            self._evict(config, backend)
        self.policy = policy
        self._costs = costs
        return self.status()

    def status(self):
        loaded = _loaded()
        loaded_names = {c.model_name for c, _ in loaded}
        return {
            "policy": asdict(self.policy),
            "persistence": "session",
            "budget_basis": "Maximum of declared size and cached weight bytes; not a runtime RAM limit",
            "selected_model": self.selected_model if self.selected_model in loaded_names else None,
            "resident_models": [
                {
                    "model_name": c.model_name,
                    "engine": c.engine,
                    "estimated_weight_mb": self._costs.get(c.engine, max(1, c.size_mb)),
                }
                for c, _ in loaded
            ],
            "last_voice_prewarm": self.last_voice_prewarm
            if self.last_voice_prewarm and self.last_voice_prewarm["model_name"] in loaded_names
            else None,
        }

    @serialized_inference
    async def prewarm(self, config, *, profile_id=None, prepare_prompt=None, language="en"):
        from ..utils.hf_offline_patch import force_offline_if_cached

        registry = _backends()
        if not await asyncio.to_thread(_cached, config):
            raise ValueError("Download the complete model assets before prewarming")
        if profile_id and prepare_prompt is None:
            raise ValueError("Voice prewarming requires the exact selected profile")
        previous = [c for c, _ in _loaded()]
        previous_selected = self.selected_model
        previous_warm = self.last_voice_prewarm
        try:
            # Cached loads have their own guard too. This outer window covers
            # lazily fetched tokenizer/voice assets during the short synthesis.
            with force_offline_if_cached(True, "TTS prewarm"):
                await registry.load_engine_model(config.engine, config.model_size)
                check_inference_cancelled()
                if profile_id:
                    prompt = await prepare_prompt()
                    check_inference_cancelled()
                    backend = registry.get_tts_backend_for_engine(config.engine)
                    audio, sample_rate = await backend.generate(WARMUP_TEXT, prompt, language=language, seed=0)
                    check_inference_cancelled()
                    import numpy as np

                    samples = np.asarray(audio)
                    if not samples.size or not np.isfinite(samples).all() or sample_rate <= 0:
                        raise RuntimeError("Voice prewarm returned invalid audio")
                    del audio, samples
                    self.last_voice_prewarm = {"model_name": config.model_name, "profile_id": profile_id}
                return {"mode": "voice" if profile_id else "weights_only", "audio_saved": False, **self.status()}
        except BaseException as error:
            backend = registry.get_tts_backend_for_engine(config.engine)
            if not any(c.model_name == config.model_name for c in previous) and backend.is_loaded():
                self._evict(config, backend)
            self.last_voice_prewarm = None
            # Ordinary failures restore prior cached models. Cancellation does
            # no extra inference: a subsequent selected-model load recovers.
            if not isinstance(error, asyncio.CancelledError):
                for old in sorted(previous, key=lambda c: c.model_name == previous_selected):
                    if await asyncio.to_thread(_cached, old):
                        await registry.load_engine_model(old.engine, old.model_size)
                self.last_voice_prewarm = previous_warm
            self.selected_model = (
                previous_selected if any(c.model_name == previous_selected for c, _ in _loaded()) else None
            )
            raise


model_residency = ModelResidency()
