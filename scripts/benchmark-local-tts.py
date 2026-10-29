#!/usr/bin/env python3
"""Benchmark existing Qwen TTS adapters using cached weights and Scratch only.

Importing is inert. main() is a standalone entry point: it installs a permanent
network audit guard before importing adapters. See the developer benchmark guide.
"""

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import wave


SCRATCH = Path("/Volumes/Scratch")
REPO = Path(__file__).resolve().parents[1]
LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it")
PRESETS = ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee")
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    engine: str
    backend: str
    model: str
    language: str
    text: str
    cache_root: Path
    output_root: Path
    reference: Path | None = None
    reference_text: str | None = None
    preset_voice: str | None = None
    design_prompt: str | None = None
    instruct: str | None = None
    seed: int = 0
    warm_repeats: int = 3
    max_audio_seconds: float = 30.0


def validate(settings):
    if settings.engine not in ("qwen", "qwen_custom_voice", "qwen_voice_design"):
        raise ValueError("unsupported engine; only audited Qwen adapters are admitted")
    if settings.backend not in ("mlx", "pytorch"):
        raise ValueError("backend must be explicit: mlx or pytorch")
    if settings.model not in ("1.7B", "0.6B") or (
        settings.engine == "qwen_voice_design" and settings.model != "1.7B"
    ):
        raise ValueError("unsupported model size for this engine")
    if settings.language not in LANGUAGES:
        raise ValueError("unsupported language; automatic language fallback is disabled")
    if not settings.text.strip() or len(settings.text) > 2000:
        raise ValueError("text must contain 1..2000 characters")
    if not 0 <= settings.warm_repeats <= 20 or not 0 <= settings.seed < 2**32:
        raise ValueError("warm repeats must be 0..20; seed must be 0..2**32-1")
    if not math.isfinite(settings.max_audio_seconds) or not 0 < settings.max_audio_seconds <= 120:
        raise ValueError("max audio seconds must be finite and in (0, 120]")
    for value in (settings.instruct, settings.reference_text, settings.design_prompt):
        if value is not None and (not value.strip() or len(value) > 2000):
            raise ValueError("instructions, design and reference text must contain 1..2000 characters")
    if settings.engine == "qwen":
        if settings.reference is None or not settings.reference_text:
            raise ValueError("Base requires explicit --reference and --reference-text")
        if settings.instruct or settings.preset_voice or settings.design_prompt:
            raise ValueError("Base does not support instruct, presets or voice design")
    elif settings.reference is not None or settings.reference_text:
        raise ValueError("reference audio is only supported by Base")
    elif settings.engine == "qwen_custom_voice":
        if settings.preset_voice not in PRESETS or settings.design_prompt:
            raise ValueError("CustomVoice requires a known explicit --preset-voice")
    elif not settings.design_prompt or settings.preset_voice:
        raise ValueError("VoiceDesign requires --design-prompt and no preset")
    if not settings.cache_root.is_absolute() or not settings.cache_root.is_dir():
        raise ValueError("cache root must be an existing absolute Hugging Face hub directory")


class Output:
    """Fresh directory and exclusive, directory-relative artifact writes."""

    def __init__(self, path):
        if not SCRATCH.is_mount():
            raise ValueError("/Volumes/Scratch is not mounted")
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("output root must be an absolute path under /Volumes/Scratch")
        try:
            parts = path.relative_to(SCRATCH).parts
        except ValueError as exc:
            raise ValueError("output root must be under /Volumes/Scratch") from exc
        if not parts:
            raise ValueError("output root must be a fresh subdirectory of Scratch")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open(SCRATCH, flags)
        try:
            for index, part in enumerate(parts):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    if index == len(parts) - 1:
                        raise ValueError("output root already exists; choose a fresh run path")
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
            self.fd, self.path = current, path
        except BaseException:
            os.close(current)
            raise

    def open(self, name):
        if Path(name).name != name:
            raise ValueError("artifact name must be a single filename")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        return os.fdopen(fd, "wb")

    def close(self):
        os.close(self.fd)


def read_json(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"oversized configuration: {path.name}")
    def reject_nonfinite(value):
        raise ValueError(f"nonfinite JSON configuration value: {value}")

    return json.loads(path.read_text(), parse_constant=reject_nonfinite)


def admit_cached_model(settings):
    """Resolve refs/main locally; never call a Hub API, even for admission."""
    variant = {"qwen": "Base", "qwen_custom_voice": "CustomVoice",
               "qwen_voice_design": "VoiceDesign"}[settings.engine]
    repo_id = f"Qwen/Qwen3-TTS-12Hz-{settings.model}-{variant}"
    if settings.backend == "mlx":
        repo_id = f"mlx-community/Qwen3-TTS-12Hz-{settings.model}-{variant}-bf16"
    cache = settings.cache_root.resolve()
    repo = cache / ("models--" + repo_id.replace("/", "--"))
    try:
        # Converted MLX weights masquerading as a PyTorch repo are not ready.
        if repo.is_symlink():
            raise ValueError("repository cache aliases are not admitted")
        ref = repo / "refs" / "main"
        if ref.stat().st_size > 128:
            raise ValueError("oversized local main revision")
        revision = ref.read_text().strip()
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
            raise ValueError("invalid local main revision")
        snapshot = repo / "snapshots" / revision
        for incomplete in (repo / "blobs").glob("*.incomplete"):
            if not incomplete.with_suffix("").is_file():
                raise ValueError("incomplete download in repository")

        def required(name):
            candidate = snapshot / name
            if not candidate.resolve().is_relative_to(repo.resolve()):
                raise ValueError("cached file escapes its repository")
            if not candidate.is_file() or candidate.stat().st_size == 0:
                raise ValueError(f"missing/empty cached file: {name}")
            return candidate

        for name in ("config.json", "generation_config.json", "tokenizer_config.json",
                     "vocab.json", "merges.txt", "preprocessor_config.json",
                     "speech_tokenizer/config.json", "speech_tokenizer/preprocessor_config.json"):
            required(name)
        for prefix in ("", "speech_tokenizer/"):
            index_path = snapshot / (prefix + "model.safetensors.index.json")
            if index_path.exists():
                index = read_json(required(prefix + "model.safetensors.index.json"))
                weights = set(index["weight_map"].values())
                if not weights:
                    raise ValueError("empty weight index")
                for name in weights:
                    if Path(name).is_absolute() or ".." in Path(name).parts:
                        raise ValueError("unsafe shard path")
                    required(prefix + name)
            else:
                required(prefix + "model.safetensors")
        config = read_json(required("config.json"))
        expected = {"Base": "base", "CustomVoice": "custom_voice", "VoiceDesign": "voice_design"}
        if config.get("tts_model_type") != expected[variant]:
            raise ValueError("checkpoint voice mode does not match selected engine")
        return {"repo_id": repo_id, "revision": revision, "snapshot": str(snapshot),
                "config_sha256": hashlib.sha256((snapshot / "config.json").read_bytes()).hexdigest(),
                "generation_config": read_json(required("generation_config.json"))}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"model not locally ready: {repo_id}: {exc}") from exc


def reference_info(path):
    if path is None:
        return None
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("reference must be an explicit absolute local WAV of at most 16 MiB")
    with wave.open(str(path), "rb") as source:
        rate, frames = source.getframerate(), source.getnframes()
        if not rate or not frames or frames / rate > 30 or source.getcomptype() != "NONE":
            raise ValueError("reference must be nonempty uncompressed PCM WAV, at most 30 seconds")
        if len(source.readframes(frames)) != frames * source.getnchannels() * source.getsampwidth():
            raise ValueError("reference PCM WAV is truncated")
        duration = frames / rate
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "duration_s": duration, "sample_rate": rate}


def configure_offline(settings, output):
    """Standalone-process guard, applied before model libraries are imported."""
    runtime = output.path / "runtime"
    runtime.mkdir()
    temporary = runtime / "tmp"
    temporary.mkdir()
    os.environ.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
        "HF_HUB_CACHE": str(settings.cache_root.resolve()),
        "VOICEBOX_MODELS_DIR": str(settings.cache_root.resolve()),
        "HF_HOME": str(runtime / "huggingface"), "XDG_CACHE_HOME": str(runtime / "cache"),
        "TORCH_HOME": str(runtime / "torch"), "TMPDIR": str(temporary),
        "PYTHONPYCACHEPREFIX": str(runtime / "bytecode"), "PYTHONDONTWRITEBYTECODE": "1",
        "VOICEBOX_OFFLINE_PATCH": "0",
    })
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(temporary)

    def deny_network(event, args):
        if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            raise OSError("benchmark forbids network lookup")
        if event in ("socket.connect", "socket.sendto") and args[0].family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("benchmark forbids network connections")

    sys.addaudithook(deny_network)


def create_adapter(settings, admitted, output):
    # No server/services, global backend singleton, model discovery or ASR.
    sys.path.insert(0, str(REPO))
    from backend import config
    config.set_data_dir(output.path / "runtime" / "data")
    from backend.utils import hf_offline_patch
    # mlx_backend explicitly calls this too; never modify shared caches.
    hf_offline_patch.ensure_original_qwen_config_cached = lambda: None
    modules = {
        ("qwen", "mlx"): ("mlx_backend", "MLXTTSBackend"),
        ("qwen", "pytorch"): ("pytorch_backend", "PyTorchTTSBackend"),
        ("qwen_custom_voice", "mlx"): ("qwen_mlx_styles_backend", "QwenMLXCustomVoiceBackend"),
        ("qwen_voice_design", "mlx"): ("qwen_mlx_styles_backend", "QwenMLXVoiceDesignBackend"),
        ("qwen_custom_voice", "pytorch"): ("qwen_custom_voice_backend", "QwenCustomVoiceBackend"),
        ("qwen_voice_design", "pytorch"): ("qwen_voice_design_backend", "QwenVoiceDesignBackend"),
    }
    module, name = modules[(settings.engine, settings.backend)]
    imported = importlib.import_module("backend.backends." + module)
    adapter = getattr(imported, name)(settings.model)
    adapter._benchmark_source_sha256 = hashlib.sha256(Path(imported.__file__).read_bytes()).hexdigest()
    # Bind precisely the admitted snapshot; refs/main cannot switch this run.
    adapter._get_model_path = lambda size: admitted["snapshot"]
    adapter._is_model_cached = lambda size=None: True
    return adapter


def provenance():
    def git(*args):
        return subprocess.check_output(["git", "--no-optional-locks", "-C", str(REPO), *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=5).strip()
    try:
        commit, dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    versions = {}
    for package in ("numpy", "torch", "mlx", "mlx-audio", "qwen-tts", "transformers", "huggingface-hub"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {"git_commit": commit, "git_dirty": dirty, "python": sys.version,
            "platform": platform.platform(), "machine": platform.machine(), "versions": versions,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)

    def percentile(q):
        index = (len(ordered) - 1) * q
        low = math.floor(index)
        return ordered[low] + (ordered[math.ceil(index)] - ordered[low]) * (index - low)

    return {"count": len(values), "p50": percentile(.5), "p95": percentile(.95)}


def audio_metrics(audio, sample_rate, elapsed, limit):
    import numpy as np
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, np.integer)) or not 0 < sample_rate <= 192000:
        raise ValueError("invalid sample rate; expected integer in 1..192000")
    samples = np.asarray(audio)
    if samples.ndim != 1 or not samples.size or samples.dtype.kind not in "fi":
        raise ValueError("expected nonempty mono real audio array")
    duration = samples.size / sample_rate
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("invalid monotonic elapsed time")
    if duration > limit or not np.isfinite(samples).all():
        raise ValueError("audio exceeds duration limit or contains nonfinite samples")
    return samples, {"sample_rate": int(sample_rate), "samples": int(samples.size),
                     "duration_s": duration, "generation_s": elapsed,
                     "wall_audio_rtf": elapsed / duration,
                     "first_model_audio_s": None,
                     "first_model_audio_unavailable": "adapter returns a complete array; no streaming boundary",
                     "clipped_samples_pcm16": int(np.count_nonzero(np.abs(samples) > 1))}


def save_wav(output, name, samples, sample_rate):
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with output.open(name) as handle, wave.open(handle, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm)


async def benchmark(settings, output, *, factory=create_adapter, admit=admit_cached_model,
                    clock=time.perf_counter_ns, get_provenance=provenance):
    """Injectable test boundary. Caller owns offline setup and validation."""
    report = {"schema_version": 1, "status": "failed", "config": asdict(settings),
              "provenance": get_provenance(), "stages_s": {}, "runs": [], "warm": None,
              "timing_scope": "adapter calls; WAV IO/validation excluded; first load in fresh process, not cold disk cache"}
    adapter = None
    stage = "admission"
    try:
        report["model"] = admit(settings)
        report["reference"] = reference_info(settings.reference)
        stage = "adapter_init"
        start = clock()
        adapter = factory(settings, report["model"], output)
        report["stages_s"][stage] = (clock() - start) / 1e9
        report["adapter"] = {"class": type(adapter).__name__, "device": str(getattr(adapter, "device", settings.backend)),
                             "source_sha256": getattr(adapter, "_benchmark_source_sha256", None)}
        if adapter.is_loaded():
            raise ValueError("cold load requires a fresh unloaded adapter")
        stage = "load"
        start = clock()
        await adapter.load_model(settings.model)
        report["stages_s"][stage] = (clock() - start) / 1e9
        stage = "reference_preparation"
        start = clock()
        if settings.reference is not None:
            prompt, _ = await adapter.create_voice_prompt(str(settings.reference), settings.reference_text, use_cache=False)
        elif settings.preset_voice:
            prompt = {"voice_type": "preset", "preset_engine": settings.engine,
                      "preset_voice_id": settings.preset_voice}
        else:
            prompt = {"voice_type": "designed", "design_prompt": settings.design_prompt}
        report["stages_s"][stage] = (clock() - start) / 1e9
        artifact_bytes = 0
        for index in range(settings.warm_repeats + 1):
            stage = f"generation_{index}"
            start = clock()
            audio, rate = await adapter.generate(settings.text, prompt, language=settings.language,
                                                  seed=settings.seed, instruct=settings.instruct)
            elapsed = (clock() - start) / 1e9
            samples, metrics = audio_metrics(audio, rate, elapsed, settings.max_audio_seconds)
            artifact_bytes += samples.size * 2 + 44
            if artifact_bytes > MAX_ARTIFACT_BYTES:
                raise ValueError("WAV artifacts exceed 128 MiB run budget")
            name = f"{'cold' if index == 0 else 'warm'}-{index:02d}.wav"
            save_wav(output, name, samples, rate)
            report["runs"].append({"kind": "cold" if index == 0 else "warm", "index": index,
                                   "wav": name, **metrics})
        cold = report["runs"][0]
        report["cold_load_reference_generation_rtf"] = (
            report["stages_s"]["load"] + report["stages_s"]["reference_preparation"] + cold["generation_s"]
        ) / cold["duration_s"]
        warm = report["runs"][1:]
        report["warm"] = {"generation_s": distribution([r["generation_s"] for r in warm]),
                          "wall_audio_rtf": distribution([r["wall_audio_rtf"] for r in warm]),
                          "percentile_method": "linear interpolation at (n-1)*q"}
        report["status"] = "ok"
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = {"stage": stage, "type": type(exc).__name__, "message": str(exc)[:2000]}
    finally:
        if adapter is not None:
            try:
                adapter.unload_model()
            except Exception as exc:
                report["status"] = "failed"
                report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        with output.open("results.json") as handle:
            handle.write(json.dumps(report, default=str, indent=2, allow_nan=False).encode() + b"\n")
    return report


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("engine", "backend", "model", "language", "text", "cache-root", "output-root"):
        parser.add_argument("--" + name, required=True, type=Path if name.endswith("root") else str)
    parser.add_argument("--reference", type=Path)
    for name in ("reference-text", "preset-voice", "design-prompt", "instruct"):
        parser.add_argument("--" + name)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--warm-repeats", type=int, default=3)
    parser.add_argument("--max-audio-seconds", type=float, default=30)
    return Settings(**vars(parser.parse_args(argv)))


def main(argv=None):
    output = None
    try:
        settings = parse_args(argv)
        validate(settings)
        output = Output(settings.output_root)
        configure_offline(settings, output)
        report = asyncio.run(benchmark(settings, output))
        print(str(output.path / "results.json"))
        if report["status"] != "ok":
            print(json.dumps(report.get("error") or report["cleanup_error"]), file=sys.stderr)
            return 2
        return 0
    except (OSError, ValueError) as exc:
        print(f"benchmark refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
