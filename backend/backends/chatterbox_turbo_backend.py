"""
Chatterbox Turbo TTS backend implementation.

Wraps ChatterboxTurboTTS from chatterbox-tts for fast, English-only
voice cloning with paralinguistic tag support ([laugh], [cough], etc.).
Forces CPU on macOS due to known MPS tensor issues.
"""

import asyncio
import logging
import threading
from importlib.metadata import version
from pathlib import Path
from typing import ClassVar, Optional

import numpy as np

from ..services.inference_scheduler import serialized_inference
from ..utils import cache
from .base import (
    combine_voice_prompts as _combine_voice_prompts,
    empty_device_cache,
    get_torch_device,
    is_model_cached,
    manual_seed,
    model_load_progress,
    patch_chatterbox_f32,
)

logger = logging.getLogger(__name__)

CHATTERBOX_TURBO_HF_REPO = "ResembleAI/chatterbox-turbo"

# prepare_conditionals in chatterbox-tts 0.1.7, matching generate's defaults
# (prepare_conditionals itself defaults exaggeration to 0.5, generate to 0.0).
_CONDITIONING_FORMAT = "chatterbox-turbo-conditionals-v1"
_EXAGGERATION = 0.0
_NORM_LOUDNESS = True
_T3_FIELDS = {"speaker_emb", "clap_emb", "cond_prompt_speech_tokens", "cond_prompt_speech_emb", "emotion_adv"}
_GEN_FIELDS = {"prompt_token", "prompt_token_len", "prompt_feat", "prompt_feat_len", "embedding"}


def _cpu_fields(fields: dict) -> dict:
    """Detach storage too: upstream inference mutates T3Cond in place on CPU."""
    import torch

    return {
        name: value.detach().cpu().clone() if isinstance(value, torch.Tensor) else value
        for name, value in fields.items()
    }


def _snapshot_conditionals(conds) -> dict:
    return {"t3": _cpu_fields(vars(conds.t3)), "gen": _cpu_fields(conds.gen)}


def _restore_conditionals(payload: dict, device: str):
    from chatterbox.models.t3.modules.cond_enc import T3Cond
    from chatterbox.tts_turbo import Conditionals

    # Conditionals.to mutates its inputs; never hand it cached storage.
    return Conditionals(T3Cond(**_cpu_fields(payload["t3"])), _cpu_fields(payload["gen"])).to(device)


def _valid_conditioning(payload, key: str) -> bool:
    """Accept the raw encoded 0.1.7 schema, never path metadata or device state."""
    import torch

    if not isinstance(payload, dict) or set(payload) != {"format", "key", "t3", "gen"}:
        return False
    if payload["format"] != _CONDITIONING_FORMAT or payload["key"] != key:
        return False
    t3, gen = payload["t3"], payload["gen"]
    if not isinstance(t3, dict) or set(t3) != _T3_FIELDS or not isinstance(gen, dict) or set(gen) != _GEN_FIELDS:
        return False

    def tensor(value, shape, *, integer=False):
        if not isinstance(value, torch.Tensor) or value.device.type != "cpu" or value.layout != torch.strided:
            return False
        if value.ndim != len(shape) or any(
            want is not None and got != want for got, want in zip(value.shape, shape, strict=True)
        ):
            return False
        if not 0 < value.numel() <= 1_000_000:
            return False
        if integer:
            return value.dtype in (torch.int32, torch.int64) and bool((value >= 0).all())
        return value.is_floating_point() and bool(torch.isfinite(value).all())

    if not tensor(t3["speaker_emb"], (1, 256)) or not tensor(t3["emotion_adv"], (1, 1, 1)):
        return False
    if t3["clap_emb"] is not None or t3["cond_prompt_speech_emb"] is not None:
        return False
    tokens = t3["cond_prompt_speech_tokens"]
    if not tensor(tokens, (1, None), integer=True) or tokens.shape[1] > 375 or bool((tokens >= 6561).any()):
        return False
    tokens, features = gen["prompt_token"], gen["prompt_feat"]
    if not tensor(tokens, (1, None), integer=True) or tokens.shape[1] > 250 or bool((tokens >= 6561).any()):
        return False
    if not tensor(features, (1, None, 80)) or features.shape[1] > 500:
        return False
    if not tensor(gen["embedding"], (1, 192)) or not tensor(gen["prompt_token_len"], (1,), integer=True):
        return False
    if gen["prompt_token_len"].item() != tokens.shape[1] or features.shape[1] // 2 != tokens.shape[1]:
        return False
    length = gen["prompt_feat_len"]
    return length is None or (tensor(length, (1,), integer=True) and 0 < length.item() <= features.shape[1])


# The files ChatterboxTurboTTS.from_local() reads, as of chatterbox-tts 0.1.7:
# the three weight files, the GPT-2 tokenizer files AutoTokenizer needs, and
# the built-in voice. The load runs with HF offline mode forced when this
# reports cached, so a partial snapshot must not count as cached -- if upstream
# starts reading another file, add it here too.
_TURBO_WEIGHT_FILES = [
    "t3_turbo_v1.safetensors",
    "s3gen_meanflow.safetensors",
    "ve.safetensors",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "conds.pt",
]


class ChatterboxTurboTTSBackend:
    """Chatterbox Turbo TTS backend — fast, English-only, with paralinguistic tags."""

    # Class-level lock for torch.load monkey-patching
    _load_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self):
        self.model = None
        self.model_size = "default"
        self._device = None
        self._model_load_lock = asyncio.Lock()
        self._conditioning_model_id = CHATTERBOX_TURBO_HF_REPO
        self._default_conditioning = None
        self._default_captured = False

    def _get_device(self) -> str:
        return get_torch_device(force_cpu_on_mac=True, allow_xpu=True)

    def is_loaded(self) -> bool:
        return self.model is not None

    def _get_model_path(self, model_size: str = "default") -> str:
        return CHATTERBOX_TURBO_HF_REPO

    def _is_model_cached(self, model_size: str = "default") -> bool:
        return is_model_cached(CHATTERBOX_TURBO_HF_REPO, required_files=_TURBO_WEIGHT_FILES)

    async def load_model(self, model_size: str = "default") -> None:
        """Load the Chatterbox Turbo model."""
        if self.model is not None:
            return
        async with self._model_load_lock:
            if self.model is not None:
                return
            await asyncio.to_thread(self._load_model_sync)

    def _load_model_sync(self):
        """Synchronous model loading."""
        model_name = "chatterbox-turbo"
        is_cached = self._is_model_cached()

        with model_load_progress(model_name, is_cached):
            device = self._get_device()
            self._device = device
            logger.info(f"Loading Chatterbox Turbo TTS on {device}...")

            import torch
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            from huggingface_hub import snapshot_download

            local_path = snapshot_download(
                repo_id=CHATTERBOX_TURBO_HF_REPO,
                token=None,
                allow_patterns=["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"],
            )

            if device == "cpu":
                _orig_torch_load = torch.load

                def _patched_load(*args, **kwargs):
                    kwargs.setdefault("map_location", "cpu")
                    return _orig_torch_load(*args, **kwargs)

                with ChatterboxTurboTTSBackend._load_lock:
                    torch.load = _patched_load
                    try:
                        model = ChatterboxTurboTTS.from_local(local_path, device)
                    finally:
                        torch.load = _orig_torch_load
            else:
                model = ChatterboxTurboTTS.from_local(local_path, device)

            patch_chatterbox_f32(model)
            # snapshot_download returns a revision-specific snapshot directory.
            self._conditioning_model_id = f"{CHATTERBOX_TURBO_HF_REPO}@{Path(local_path).name}"
            self.model = model

        logger.info("Chatterbox Turbo TTS loaded successfully")

    def unload_model(self) -> None:
        """Unload model to free memory."""
        self._default_conditioning = None
        self._default_captured = False
        self._conditioning_model_id = CHATTERBOX_TURBO_HF_REPO
        if self.model is not None:
            device = self._device
            del self.model
            self.model = None
            self._device = None
            empty_device_cache(device)
            logger.info("Chatterbox Turbo unloaded")

    def _capture_default(self):
        if not self._default_captured:
            conds = self.model.conds
            self._default_conditioning = _snapshot_conditionals(conds) if conds is not None else None
            self._default_captured = True
            self.model.conds = None

    def _conditioning_key(self, audio_path: str, reference_text: str) -> str:
        namespace = (
            f"{_CONDITIONING_FORMAT}:{self._conditioning_model_id}:chatterbox-tts-{version('chatterbox-tts')}:"
            f"exaggeration={_EXAGGERATION}:norm_loudness={_NORM_LOUDNESS}:enc=240000:dec=240000:tokens=375"
        )
        return cache.get_cache_key(audio_path, reference_text, namespace=namespace)

    def _encoded_conditioning(self, audio_path: str, reference_text: str, use_cache: bool):
        """Called only by a worker holding serialized physical inference ownership."""
        import torch

        key = self._conditioning_key(audio_path, reference_text)
        if use_cache:
            payload = cache.get_cached_voice_prompt(key)
            if _valid_conditioning(payload, key):
                return payload, True
            if payload is not None:
                # The shared utility has no per-key invalidation API. Invalidate
                # only our content-addressed entry, including failed encodes.
                cache._memory_cache.pop(key, None)
                (cache._get_cache_dir() / f"{key}.prompt").unlink(missing_ok=True)

        try:
            self.model.conds = None
            with torch.inference_mode():
                self.model.prepare_conditionals(
                    audio_path,
                    exaggeration=_EXAGGERATION,
                    norm_loudness=_NORM_LOUDNESS,
                )
            payload = {"format": _CONDITIONING_FORMAT, "key": key, **_snapshot_conditionals(self.model.conds)}
            if not _valid_conditioning(payload, key):
                raise RuntimeError("Invalid Chatterbox Turbo voice conditioning")
            # Do not publish conditioning if a reference was replaced during encoding.
            if key != self._conditioning_key(audio_path, reference_text):
                raise RuntimeError("Reference audio changed while encoding Chatterbox Turbo conditioning")
            if use_cache:
                cache.cache_voice_prompt(key, payload)
            return payload, False
        finally:
            self.model.conds = None

    @serialized_inference
    async def create_voice_prompt(
        self,
        audio_path: str,
        reference_text: str,
        use_cache: bool = True,
    ) -> tuple[dict, bool]:
        """
        Create voice prompt from reference audio.

        Keep path/text compatibility; a hit means validated encoded tensors.
        Bypass prompts defer encoding until generation and never read/write cache.
        """
        voice_prompt = {
            "ref_audio": str(audio_path),
            "ref_text": reference_text,
            "_turbo_use_cache": use_cache,
        }
        if not use_cache:
            return voice_prompt, False
        await self.load_model()

        def _prepare_sync():
            self._capture_default()
            _, hit = self._encoded_conditioning(str(audio_path), reference_text, True)
            return voice_prompt, hit

        return await asyncio.to_thread(_prepare_sync)

    async def combine_voice_prompts(
        self,
        audio_paths: list[str],
        reference_texts: list[str],
    ) -> tuple[np.ndarray, str]:
        return await _combine_voice_prompts(audio_paths, reference_texts)

    @serialized_inference
    async def generate(
        self,
        text: str,
        voice_prompt: dict,
        language: str = "en",
        seed: Optional[int] = None,
        instruct: Optional[str] = None,
    ) -> tuple[np.ndarray, int]:
        """
        Generate audio using Chatterbox Turbo TTS.

        Supports paralinguistic tags in text: [laugh], [cough], [chuckle], etc.

        Args:
            text: Text to synthesize (may include paralinguistic tags)
            voice_prompt: Dict with ref_audio path
            language: Ignored (Turbo is English-only)
            seed: Random seed for reproducibility
            instruct: Unused (protocol compatibility)

        Returns:
            Tuple of (audio_array, sample_rate)
        """
        await self.load_model()

        ref_audio = voice_prompt.get("ref_audio")

        def _generate_sync():
            import torch

            self._capture_default()
            if seed is not None:
                manual_seed(seed, self._device)

            logger.info("[Chatterbox Turbo] Generating (English)")

            try:
                if ref_audio:
                    payload, _ = self._encoded_conditioning(
                        str(ref_audio),
                        voice_prompt.get("ref_text", ""),
                        voice_prompt.get("_turbo_use_cache", True),
                    )
                else:
                    payload = self._default_conditioning
                if payload is None:
                    raise RuntimeError("Chatterbox Turbo has no default voice conditioning")
                with torch.inference_mode():
                    self.model.conds = _restore_conditionals(payload, self._device)
                    wav = self.model.generate(
                        text,
                        audio_prompt_path=None,
                        temperature=0.8,
                        top_k=1000,
                        top_p=0.95,
                        repetition_penalty=1.2,
                    )

                    # Convert tensor -> numpy
                    if isinstance(wav, torch.Tensor):
                        audio = wav.squeeze().cpu().numpy().astype(np.float32)
                    else:
                        audio = np.asarray(wav, dtype=np.float32)
            finally:
                # No request speaker, including a failed encoder/inference, can
                # remain installed for the next profile or built-in voice.
                self.model.conds = None

            sample_rate = getattr(self.model, "sr", None) or getattr(self.model, "sample_rate", 24000)

            return audio, sample_rate

        return await asyncio.to_thread(_generate_sync)
