#!/usr/bin/env python3
"""Qualify the Chatterbox Turbo conditioning cache against the real backend and
model on an explicit device, offline, in a fresh process.

Each check drives ChatterboxTurboTTSBackend through its public async methods.
Spies only observe (call counts, tensor devices, storage pointers); inference is
never replaced. results.json holds one entry per check plus device identity,
stage telemetry and the saved WAVs. Exit status is nonzero if any check fails.
"""

import argparse
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import threading
import time
import traceback
from typing import Literal
import weakref


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.portable_output import Output  # noqa: E402
from scripts import device_telemetry as telemetry  # noqa: E402

_spec = importlib.util.spec_from_file_location("benchmark_local_tts", Path(__file__).with_name("benchmark-local-tts.py"))
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)


@dataclass(frozen=True)
class Settings:
    device: str
    cache_root: Path
    output_root: Path
    reference_a: Path
    reference_a_text: str
    reference_b: Path
    reference_b_text: str
    text: str = "The quick brown fox jumps over the lazy dog near the quiet river."
    seed: int = 42
    max_audio_seconds: float = 30.0


def benchmark_settings(settings):
    return bench.Settings("chatterbox_turbo", "pytorch", settings.device, "default", "en", settings.text,
                          settings.cache_root, settings.output_root, settings.reference_a,
                          settings.reference_a_text, seed=settings.seed, max_audio_seconds=settings.max_audio_seconds)


def validate(settings):
    bench.validate(benchmark_settings(settings))
    if settings.reference_b.resolve() == settings.reference_a.resolve():
        raise ValueError("references A and B must be distinct files")
    if not settings.reference_b_text.strip():
        raise ValueError("reference B requires a transcript")
    for path in (settings.reference_a, settings.reference_b):
        bench.reference_info(path)


@dataclass
class CheckResult:
    name: str
    kind: Literal["gating", "recorded"]
    passed: bool
    wall_s: float
    evidence: dict
    error: str | None = None


class Spy:
    """Observe a bound method: count calls, run observers, always call through.

    The owner is held weakly, so an installed spy never keeps a model alive
    past unload_model(); a retained model would hide a VRAM leak."""

    def __init__(self, owner, attribute, before=None):
        self.attribute, self.before = attribute, before
        original = getattr(owner, attribute)
        self.function = getattr(original, "__func__", original)
        self.owner = weakref.ref(owner) if hasattr(original, "__func__") else None
        self.calls = 0
        self.observations = []
        self.started = threading.Event()
        self.finished = threading.Event()
        self.started_at = self.finished_at = None
        setattr(owner, attribute, self)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.started_at = time.perf_counter()
        self.started.set()
        if self.before is not None:
            self.observations.append(self.before())
        try:
            if self.owner is None:
                return self.function(*args, **kwargs)
            owner = self.owner()
            if owner is None:
                raise RuntimeError("spied model was released")
            return self.function(owner, *args, **kwargs)
        finally:
            self.finished_at = time.perf_counter()
            self.finished.set()


def tensor_facts(value):
    """{field: (device, data_ptr)} for every tensor in a conditionals payload or object."""
    import torch
    facts = {}

    def visit(prefix, item):
        if isinstance(item, torch.Tensor):
            facts[prefix] = (str(item.device), item.data_ptr())
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(f"{prefix}.{key}", child)
        elif hasattr(item, "__dict__"):
            for key, child in vars(item).items():
                visit(f"{prefix}.{key}", child)

    visit("", value)
    return {key.lstrip("."): fact for key, fact in facts.items()}


def clone_tensors(payload):
    import torch
    return {key: value.detach().clone() for key, value in
            ((k, v) for section in ("t3", "gen") for k, v in payload[section].items()) if isinstance(value, torch.Tensor)}


def compare_audio(left, right):
    import numpy as np
    same_length = left.shape == right.shape
    if same_length:
        difference = np.abs(left.astype(np.float64) - right.astype(np.float64))
        max_abs = float(difference.max())
        return {"same_length": True, "bit_exact": bool(max_abs == 0.0), "max_abs_difference": max_abs,
                "mean_abs_difference": float(difference.mean())}
    return {"same_length": False, "bit_exact": False, "lengths": [int(left.size), int(right.size)]}


class Session:
    """Mutable state the ordered checks share: adapter, spies, audio and references."""

    def __init__(self, settings, output, meter, factory, admitted, verify_residency):
        self.settings, self.output, self.meter = settings, output, meter
        self.factory, self.admitted, self.verify_residency = factory, admitted, verify_residency
        self.device = bench.torch_device(benchmark_settings(settings))
        self.adapter = None
        self.prepare = self.generate_spy = None
        self.audio = []
        self.samples = {}
        self.wavs = []
        self.stage_counter = 0
        work = output.path / "runtime" / "refs"
        work.mkdir(parents=True)
        self.work_a, self.work_b, self.work_c = work / "a.wav", work / "b.wav", work / "c.wav"
        shutil.copyfile(settings.reference_a, self.work_a)
        shutil.copyfile(settings.reference_b, self.work_b)
        shutil.copyfile(settings.reference_a, self.work_c)
        self.bytes_a, self.bytes_b = settings.reference_a.read_bytes(), settings.reference_b.read_bytes()

    def stage(self, name):
        self.stage_counter += 1
        return self.meter.stage(f"{self.stage_counter:02d}_{name}")

    async def load(self):
        with self.stage("load"):
            await self.adapter.load_model()
        residency = self.verify_residency(bench.turbo_inference_roots(self.adapter), self.device)
        loaded = bench.turbo_loaded_revision(self.adapter, self.admitted)
        self.install_spies()
        return {"residency": residency, "loaded_revision": loaded}

    def install_spies(self):
        model = self.adapter.model
        weak_model = weakref.ref(model)
        self.prepare = Spy(model, "prepare_conditionals")
        self.generate_spy = Spy(model, "generate", before=lambda: tensor_facts(weak_model().conds))

    async def prepare_prompt(self, path, text, *, use_cache=True, label="prepare"):
        with self.stage(label):
            prompt, hit = await self.adapter.create_voice_prompt(str(path), text, use_cache=use_cache)
        return prompt, hit

    async def generate(self, label, prompt, *, seed=None):
        with self.stage(f"generate_{label}"):
            audio, rate = await self.adapter.generate(self.settings.text, prompt, seed=seed)
        wall = self.meter.stages[f"{self.stage_counter:02d}_generate_{label}"].wall_s
        samples, metrics = bench.audio_metrics(audio, rate, wall, self.settings.max_audio_seconds)
        name = f"{label}.wav"
        bench.save_wav(self.output, name, samples, rate)
        self.wavs.append(name)
        self.audio.append({"label": label, **metrics})
        self.samples[label] = samples
        return samples

    def stage_wall(self, suffix):
        return next(m.wall_s for name, m in self.meter.stages.items() if name.endswith(suffix))

    def cache_key(self, path, text):
        return self.adapter._conditioning_key(str(path), text)

    def cached_payload(self, path, text):
        from backend.utils import cache
        return cache._memory_cache[self.cache_key(path, text)]

    def cache_dir(self):
        from backend.utils import cache
        return cache._get_cache_dir()


def provenance():
    return {**bench.provenance(), "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "benchmark_module_sha256": hashlib.sha256(Path(bench.__file__).read_bytes()).hexdigest()}


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


async def check_memory_hit_skips_prepare(s):
    prompt, hit = await s.prepare_prompt(s.work_a, s.settings.reference_a_text, label="prepare_a_miss")
    expect(hit is False and s.prepare.calls == 1, f"first preparation: hit={hit}, prepare calls={s.prepare.calls}")
    _, hit_again = await s.prepare_prompt(s.work_a, s.settings.reference_a_text, label="prepare_a_memory_hit")
    expect(hit_again is True and s.prepare.calls == 1, f"memory hit: hit={hit_again}, prepare calls={s.prepare.calls}")
    s.prompt_a = prompt
    await s.generate("a_memory_hit", prompt, seed=s.settings.seed)
    expect(s.prepare.calls == 1, f"generation re-encoded: prepare calls={s.prepare.calls}")
    return {"first_hit": hit, "second_hit": hit_again, "prepare_calls": s.prepare.calls,
            "miss_s": s.stage_wall("prepare_a_miss"), "memory_hit_s": s.stage_wall("prepare_a_memory_hit")}


async def check_disk_hit_skips_prepare(s):
    from backend.utils import cache
    entry = s.cache_dir() / f"{s.cache_key(s.work_a, s.settings.reference_a_text)}.prompt"
    expect(entry.is_file(), f"no disk entry at {entry}")
    cache.clear_voice_prompt_memory_cache()
    before = s.prepare.calls
    _, hit = await s.prepare_prompt(s.work_a, s.settings.reference_a_text, label="prepare_a_disk_hit")
    expect(hit is True and s.prepare.calls == before, f"disk hit: hit={hit}, prepare calls {before}->{s.prepare.calls}")
    return {"disk_entry": str(entry), "disk_entry_bytes": entry.stat().st_size, "hit": hit,
            "disk_hit_s": s.stage_wall("prepare_a_disk_hit")}


async def check_cached_payload_cpu_owned(s):
    import torch
    payload = s.cached_payload(s.work_a, s.settings.reference_a_text)
    facts = tensor_facts({"t3": payload["t3"], "gen": payload["gen"]})
    devices = sorted({device for device, _ in facts.values()})
    expect(devices == ["cpu"], f"cached payload tensors on {devices}")
    entry = s.cache_dir() / f"{payload['key']}.prompt"
    reloaded = torch.load(entry, weights_only=True)
    disk_devices = sorted({device for device, _ in tensor_facts({"t3": reloaded["t3"], "gen": reloaded["gen"]}).values()})
    expect(disk_devices == ["cpu"], f"disk payload tensors on {disk_devices}")
    return {"memory_devices": devices, "disk_devices": disk_devices, "tensor_count": len(facts)}


def storage_isolation_carrier(device):
    """Which assertion proves restored tensors do not alias the CPU cache: on
    CPU only distinct data_ptr values can, while on another device the device
    check already does and pointer collisions across address spaces mean nothing."""
    return "data_ptr" if telemetry.normalize_device(device) == "cpu" else "device"


async def check_device_restoration_uses_fresh_storage(s):
    import torch
    payload = s.cached_payload(s.work_a, s.settings.reference_a_text)
    before = clone_tensors(payload)
    cached_pointers = {pointer for _, pointer in tensor_facts({"t3": payload["t3"], "gen": payload["gen"]}).values()}
    await s.generate("a_restoration", s.prompt_a, seed=s.settings.seed)
    restored = s.generate_spy.observations[-1]
    devices = sorted({device for device, _ in restored.values()})
    expect(devices == [telemetry.normalize_device(s.device)], f"restored conditioning on {devices}, expected {s.device}")
    shared = [name for name, (_, pointer) in restored.items() if pointer in cached_pointers]
    expect(not shared, f"restored tensors share storage with the cache: {shared}")
    after = clone_tensors(payload)
    changed = [name for name in before if not torch.equal(before[name], after[name])]
    expect(not changed, f"cached tensors changed during generation: {changed}")
    expect(s.adapter.model.conds is None, "request conditioning left installed after generation")
    return {"restored_devices": devices, "restored_tensors": len(restored), "shared_storage": shared,
            "isolation_carried_by": storage_isolation_carrier(s.device),
            "cached_tensors_unchanged": True, "conds_cleared": True}


async def check_miss_on_reference_content_change_same_path(s):
    # Path c gets its own transcript so only the audio bytes change between the two preparations.
    text_c = s.settings.reference_a_text + " (path c)"
    before = s.prepare.calls
    _, first = await s.prepare_prompt(s.work_c, text_c, label="prepare_c_as_a")
    expect(first is False and s.prepare.calls == before + 1, f"first preparation of c: hit={first}")
    s.work_c.write_bytes(s.bytes_b)
    _, second = await s.prepare_prompt(s.work_c, text_c, label="prepare_c_as_b")
    expect(second is False and s.prepare.calls == before + 2,
           f"content change at same path: hit={second}, prepare calls {before}->{s.prepare.calls}")
    _, third = await s.prepare_prompt(s.work_c, text_c, label="prepare_c_as_b_again")
    expect(third is True and s.prepare.calls == before + 2, f"unchanged content should hit: hit={third}")
    return {"first_hit": first, "changed_bytes_hit": second, "unchanged_hit": third, "prepare_calls": s.prepare.calls}


async def check_miss_on_transcript_change(s):
    before = s.prepare.calls
    _, hit = await s.prepare_prompt(s.work_a, s.settings.reference_a_text + " Altered transcript.", label="prepare_a_other_text")
    expect(hit is False and s.prepare.calls == before + 1, f"transcript change: hit={hit}")
    return {"hit": hit, "prepare_calls": s.prepare.calls}


async def check_miss_on_model_identity_change(s):
    identity = s.adapter._conditioning_model_id
    before = s.prepare.calls
    s.adapter._conditioning_model_id = identity + "#other-revision"
    try:
        _, hit = await s.prepare_prompt(s.work_a, s.settings.reference_a_text, label="prepare_a_other_model")
    finally:
        s.adapter._conditioning_model_id = identity
    expect(hit is False and s.prepare.calls == before + 1, f"model identity change: hit={hit}")
    _, restored_hit = await s.prepare_prompt(s.work_a, s.settings.reference_a_text, label="prepare_a_identity_restored")
    expect(restored_hit is True and s.prepare.calls == before + 1, f"original entry lost: hit={restored_hit}")
    return {"other_identity_hit": hit, "restored_identity_hit": restored_hit, "model_identity": identity}


async def check_builtin_voice_generates(s):
    before = s.prepare.calls
    audio = await s.generate("builtin", {}, seed=s.settings.seed)
    expect(s.prepare.calls == before, "built-in voice ran the encoder")
    expect(s.adapter.model.conds is None, "built-in conditioning left installed")
    comparison = compare_audio(audio, s.samples["a_memory_hit"])
    expect(not comparison["bit_exact"], "built-in voice reproduced reference A's audio")
    return {"samples": int(audio.size), "differs_from_reference_a": comparison}


async def check_missing_reference_raises(s):
    missing = s.work_a.with_name("missing.wav")
    generated_before = s.generate_spy.calls
    try:
        with s.stage("generate_missing_reference"):
            await s.adapter.generate(s.settings.text, {"ref_audio": str(missing), "ref_text": "Nobody."}, seed=s.settings.seed)
    except Exception as exc:  # the contract is "raises", whatever the encoder's type
        raised = type(exc).__name__
    else:
        raise AssertionError("missing reference generated audio")
    expect(s.generate_spy.calls == generated_before, "missing reference still ran inference")
    expect(s.adapter.model.conds is None, "failed request left conditioning installed")
    await s.generate("builtin_after_failure", {}, seed=s.settings.seed)
    return {"raised": raised, "inference_calls_unchanged": True, "builtin_after_failure": True}


async def check_distinct_references_distinct_conditioning(s):
    import torch
    before = s.prepare.calls
    prompt_b, hit = await s.prepare_prompt(s.work_b, s.settings.reference_b_text, label="prepare_b_miss")
    expect(hit is False and s.prepare.calls == before + 1, f"reference B: hit={hit}")
    s.prompt_b = prompt_b
    payload_a = s.cached_payload(s.work_a, s.settings.reference_a_text)
    payload_b = s.cached_payload(s.work_b, s.settings.reference_b_text)
    expect(payload_a["key"] != payload_b["key"], "A and B share a cache key")
    speaker_a, speaker_b = payload_a["t3"]["speaker_emb"].float(), payload_b["t3"]["speaker_emb"].float()
    cosine = float(torch.nn.functional.cosine_similarity(speaker_a, speaker_b).item())
    expect(not torch.allclose(speaker_a, speaker_b), "A and B produced the same speaker embedding")
    audio_b = await s.generate("b", prompt_b, seed=s.settings.seed)
    audio_a = await s.generate("a_vs_b", s.prompt_a, seed=s.settings.seed)
    comparison = compare_audio(audio_a, audio_b)
    expect(not comparison["bit_exact"], "A and B generated identical audio")
    return {"speaker_embedding_cosine": cosine, "keys_distinct": True, "audio": comparison}


async def check_bypass_never_touches_cache(s):
    from backend.utils import cache
    keys_before = sorted(cache._memory_cache)
    disk_before = {p.name: p.read_bytes() for p in s.cache_dir().glob("*.prompt")}
    before = s.prepare.calls
    prompt, hit = await s.prepare_prompt(s.work_a, s.settings.reference_a_text, use_cache=False, label="prepare_a_bypass")
    expect(hit is False and s.prepare.calls == before, f"bypass preparation encoded or hit: hit={hit}")
    s.prompt_bypass = prompt
    await s.generate("bypass_1", prompt, seed=s.settings.seed)
    await s.generate("bypass_2", prompt, seed=s.settings.seed)
    expect(s.prepare.calls == before + 2, f"bypass must encode per call: prepare calls {before}->{s.prepare.calls}")
    expect(sorted(cache._memory_cache) == keys_before, "bypass changed memory cache keys")
    disk_after = {p.name: p.read_bytes() for p in s.cache_dir().glob("*.prompt")}
    expect(disk_after == disk_before, "bypass changed disk cache entries")
    return {"prepare_calls_per_generate": 1, "memory_keys": len(keys_before), "disk_entries": len(disk_before)}


async def check_reference_replacement_at_same_path(s):
    import torch
    before = s.prepare.calls
    key_a = s.cache_key(s.work_a, s.settings.reference_a_text)
    s.work_a.write_bytes(s.bytes_b)
    try:
        await s.generate("a_replaced_by_b", s.prompt_a, seed=s.settings.seed)
        expect(s.prepare.calls == before + 1, f"replaced reference was not re-encoded: {before}->{s.prepare.calls}")
        key_now = s.cache_key(s.work_a, s.settings.reference_a_text)
        speaker_b = s.cached_payload(s.work_b, s.settings.reference_b_text)["t3"]["speaker_emb"].float()
        speaker_now = s.cached_payload(s.work_a, s.settings.reference_a_text)["t3"]["speaker_emb"].float()
        expect(torch.allclose(speaker_now, speaker_b), "replaced reference did not condition on B's content")
    finally:
        s.work_a.write_bytes(s.bytes_a)
    return {"re_encoded": True, "key_changed": key_now != key_a, "speaker_matches_b": True}


async def check_repeated_requests_reuse_conditioning(s):
    before = s.prepare.calls
    for index in range(3):
        await s.generate(f"a_repeat_{index}", s.prompt_a, seed=s.settings.seed + index)
    expect(s.prepare.calls == before, f"repeated requests re-encoded: {before}->{s.prepare.calls}")
    return {"generations": 3, "prepare_calls_added": s.prepare.calls - before}


async def check_unload_releases_vram_and_reload_hits_disk(s):
    """Problems are collected, not raised, until the model is loaded again so
    later checks still run against a loaded, spied model."""
    from backend.utils import cache
    problems = []
    old_prepare = s.prepare.calls
    allocated_before = s.meter.cuda_allocated_bytes()
    with s.stage("unload"):
        s.adapter.unload_model()
    if s.adapter.is_loaded():
        problems.append("adapter still loaded")
    allocated_after = s.meter.cuda_allocated_bytes()
    if allocated_before is not None and not allocated_after < allocated_before * 0.05:
        problems.append(f"VRAM not released: {allocated_before} -> {allocated_after}")
    cache.clear_voice_prompt_memory_cache()
    loaded = await s.load()
    _, hit = await s.prepare_prompt(s.work_a, s.settings.reference_a_text, label="prepare_a_after_reload")
    if not (hit is True and s.prepare.calls == 0):
        problems.append(f"disk hit after reload: hit={hit}, prepare calls={s.prepare.calls}")
    await s.generate("a_after_reload", s.prompt_a, seed=s.settings.seed)
    if s.prepare.calls != 0:
        problems.append("generation after reload re-encoded")
    expect(not problems, "; ".join(problems))
    return {"cuda_allocated_before_unload": allocated_before, "cuda_allocated_after_unload": allocated_after,
            "prepare_calls_before_unload": old_prepare, "disk_hit_after_reload": hit, **loaded}


async def check_cancellation_then_admission(s):
    spy = s.generate_spy
    spy.started.clear()
    spy.finished.clear()
    calls_before = spy.calls
    running = asyncio.create_task(s.adapter.generate(s.settings.text, s.prompt_a, seed=s.settings.seed))
    expect(await asyncio.to_thread(spy.started.wait, 120), "in-flight generation never started")
    # The in-flight request has finished its own encoding once model.generate
    # starts, so from here _encoded_conditioning is the queued request's first
    # backend work; observe whether the in-flight worker had finished by then.
    queued_work = Spy(s.adapter, "_encoded_conditioning", before=spy.finished.is_set)
    try:
        running.cancel()
        queued = asyncio.create_task(s.adapter.create_voice_prompt(str(s.work_b), s.settings.reference_b_text))
        cancelled = False
        try:
            await running
        except asyncio.CancelledError:
            cancelled = True
        expect(cancelled, "cancelled generation did not raise CancelledError")
        expect(spy.finished.is_set(), "physical worker was not drained before the caller returned")
        _, hit = await queued
    finally:
        del s.adapter._encoded_conditioning
    expect(queued_work.calls == 1, f"queued request entered the encoder path {queued_work.calls} times")
    drained_first = queued_work.observations[0]
    drain_to_queued_start_s = queued_work.started_at - spy.finished_at
    expect(drained_first, "queued request started backend work before the in-flight worker drained")
    expect(hit is True and spy.calls == calls_before + 1, f"queued request: hit={hit}, inference calls={spy.calls}")
    expect(s.adapter.model.conds is None, "cancelled request left conditioning installed")
    await s.generate("after_cancellation", s.prompt_b, seed=s.settings.seed)
    return {"cancelled": cancelled, "worker_drained": True, "queued_started_after_drain": drained_first,
            "drain_to_queued_start_s": drain_to_queued_start_s, "queued_hit": hit, "generation_after_cancellation": True}


async def check_fixed_seed_cache_vs_bypass_parity(s):
    cached = await s.generate("parity_cached", s.prompt_a, seed=s.settings.seed)
    bypass = await s.generate("parity_bypass", s.prompt_bypass, seed=s.settings.seed)
    comparison = compare_audio(cached, bypass)
    cause = None
    if not comparison["bit_exact"]:
        cause = ("the bypass path runs the reference encoder after manual_seed, so any RNG or nondeterministic "
                 "CUDA kernel use inside encoding or restoration shifts T3 sampling (temperature 0.8, top-k/top-p); "
                 "lengths differ when sampled tokens diverge" if not comparison["same_length"] else
                 "same token count, so the difference is numeric (nondeterministic CUDA kernels or reduction order)")
    return {"device": s.device, "seed": s.settings.seed, **comparison, "likely_cause_if_not_exact": cause}


async def check_same_mode_repeat_reproducibility(s):
    cached_1 = await s.generate("repeat_cached_1", s.prompt_a, seed=s.settings.seed)
    cached_2 = await s.generate("repeat_cached_2", s.prompt_a, seed=s.settings.seed)
    bypass_1 = await s.generate("repeat_bypass_1", s.prompt_bypass, seed=s.settings.seed)
    bypass_2 = await s.generate("repeat_bypass_2", s.prompt_bypass, seed=s.settings.seed)
    return {"device": s.device, "seed": s.settings.seed, "cached": compare_audio(cached_1, cached_2),
            "bypass": compare_audio(bypass_1, bypass_2)}


async def check_all_audio_finite_mono(s):
    expect(s.audio, "no audio generated")
    durations = [entry["duration_s"] for entry in s.audio]
    short = [entry["label"] for entry in s.audio if entry["duration_s"] < 0.25]
    expect(not short, f"implausibly short audio: {short}")
    rates = sorted({entry["sample_rate"] for entry in s.audio})
    expect(len(rates) == 1, f"inconsistent sample rates: {rates}")
    return {"count": len(s.audio), "sample_rate": rates[0], "min_duration_s": min(durations),
            "max_duration_s": max(durations), "clipped_samples_total": sum(e["clipped_samples_pcm16"] for e in s.audio)}


CHECKS = [
    check_memory_hit_skips_prepare,
    check_disk_hit_skips_prepare,
    check_cached_payload_cpu_owned,
    check_device_restoration_uses_fresh_storage,
    check_miss_on_reference_content_change_same_path,
    check_miss_on_transcript_change,
    check_miss_on_model_identity_change,
    check_builtin_voice_generates,
    check_missing_reference_raises,
    check_distinct_references_distinct_conditioning,
    check_bypass_never_touches_cache,
    check_reference_replacement_at_same_path,
    check_repeated_requests_reuse_conditioning,
    check_unload_releases_vram_and_reload_hits_disk,
    check_cancellation_then_admission,
    check_fixed_seed_cache_vs_bypass_parity,
    check_same_mode_repeat_reproducibility,
    check_all_audio_finite_mono,
]
# Bit-exactness across cache modes and repeats is recorded, never assumed:
# these checks contribute evidence, not exit status.
RECORDED_CHECKS = frozenset({check_fixed_seed_cache_vs_bypass_parity, check_same_mode_repeat_reproducibility})


def check_kind(check):
    return "recorded" if check in RECORDED_CHECKS else "gating"


async def qualify(settings, output, *, factory=bench.create_adapter, admit=bench.admit_cached_turbo,
                  identify=bench.device_identity, meter_factory=telemetry.StageMeter.for_device,
                  verify_residency=telemetry.check_residency, get_provenance=provenance, checks=CHECKS):
    """Injectable test boundary. Caller owns offline setup and validation."""
    report = {"schema_version": 1, "status": "failed", "config": asdict(settings), "provenance": get_provenance(),
              "checks": [], "stages": {}, "audio": [], "wavs": []}
    session = None
    try:
        report["model"] = admitted = admit(benchmark_settings(settings))
        report["references"] = {"a": bench.reference_info(settings.reference_a), "b": bench.reference_info(settings.reference_b)}
        report["device"] = identify(benchmark_settings(settings))
        meter = meter_factory(bench.torch_device(benchmark_settings(settings)))
        session = Session(settings, output, meter, factory, admitted, verify_residency)
        with session.stage("adapter_init"):
            session.adapter = factory(benchmark_settings(settings), admitted, output)
        if session.adapter.is_loaded():
            raise ValueError("qualification requires a fresh unloaded adapter")
        report["load"] = await session.load()
        for check in checks:
            name, kind = check.__name__.removeprefix("check_"), check_kind(check)
            start = time.perf_counter()
            try:
                evidence = await check(session)
                result = CheckResult(name, kind, True, time.perf_counter() - start, evidence)
            except (Exception, asyncio.CancelledError) as exc:
                result = CheckResult(name, kind, False, time.perf_counter() - start, {},
                                     f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")
            report["checks"].append(asdict(result))
        report["status"] = "ok" if all(c["passed"] for c in report["checks"] if c["kind"] == "gating") else "failed"
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
    finally:
        if session is not None:
            report["audio"], report["wavs"] = session.audio, session.wavs
            report["stages"] = {name: asdict(m) for name, m in session.meter.stages.items()}
            if session.adapter is not None:
                try:
                    session.adapter.unload_model()
                except Exception as exc:
                    report["status"] = "failed"
                    report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        gating = [c for c in report["checks"] if c["kind"] == "gating"]
        report["summary"] = {"passed": sum(c["passed"] for c in gating), "failed": sum(not c["passed"] for c in gating),
                             "expected": sum(check_kind(c) == "gating" for c in checks),
                             "recorded": {c["name"]: c["passed"] for c in report["checks"] if c["kind"] == "recorded"}}
        with output.open("results.json") as handle:
            handle.write(json.dumps(report, default=str, indent=2, allow_nan=False).encode() + b"\n")
    return report


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("device", "cache-root", "output-root", "reference-a", "reference-a-text", "reference-b", "reference-b-text"):
        parser.add_argument("--" + name, required=True, type=Path if name.endswith(("root", "-a", "-b")) else str)
    parser.add_argument("--text", default=Settings.text)
    parser.add_argument("--seed", type=int, default=Settings.seed)
    parser.add_argument("--max-audio-seconds", type=float, default=Settings.max_audio_seconds)
    return Settings(**vars(parser.parse_args(argv)))


def main(argv=None):
    output = None
    try:
        settings = parse_args(argv)
        validate(settings)
        output = Output(settings.output_root)
        bench.configure_offline(benchmark_settings(settings), output)
        report = asyncio.run(qualify(settings, output))
        print(str(output.path / "results.json"))
        for check in report["checks"]:
            verdict = "RECORDED" if check["kind"] == "recorded" else "PASS" if check["passed"] else "FAIL"
            print(f"{verdict} {check['name']}", file=sys.stderr)
        if report["status"] != "ok":
            print(json.dumps(report.get("error") or report.get("cleanup_error") or report["summary"]), file=sys.stderr)
            return 2
        return 0
    except (OSError, ValueError) as exc:
        print(f"qualification refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
