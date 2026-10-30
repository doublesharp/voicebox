"""Turbo qualification runner logic and failure paths with a fake model (no weights)."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import wave

import numpy as np
import pytest

torch = pytest.importorskip("torch")
from chatterbox.models.t3.modules.cond_enc import T3Cond  # noqa: E402
from chatterbox.tts_turbo import Conditionals  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "qualify-turbo-conditioning.py"
SPEC = importlib.util.spec_from_file_location("qualify_turbo_conditioning", SCRIPT)
qualify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qualify
SPEC.loader.exec_module(qualify)

from backend.backends import chatterbox_turbo_backend as turbo  # noqa: E402
from backend.utils import cache  # noqa: E402

REVISION = "c" * 40


def make_reference(path, fill, frames=800, rate=800):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(bytes([fill, 0]) * frames)
    return path


def speaker_of(path):
    return int.from_bytes(hashlib.sha256(Path(path).read_bytes()).digest()[:2], "big") / 65535


def conditioning(speaker):
    return Conditionals(
        T3Cond(speaker_emb=torch.full((1, 256), float(speaker)),
               cond_prompt_speech_tokens=torch.ones((1, 3), dtype=torch.long),
               emotion_adv=torch.zeros((1, 1, 1))),
        dict(prompt_token=torch.ones((1, 3), dtype=torch.long), prompt_token_len=torch.tensor([3]),
             prompt_feat=torch.ones((1, 6, 80)), prompt_feat_len=None, embedding=torch.full((1, 192), float(speaker))),
    )


class FakeModel:
    """Fake ChatterboxTurboTTS: conditioning derived from reference bytes, no weights."""

    sr = 24000

    def __init__(self, *, builtin=True, nonfinite=False):
        self.conds = conditioning(-1.0) if builtin else None
        self.t3 = self.s3gen = self.ve = torch.nn.Linear(1, 1)
        self.nonfinite = nonfinite

    def prepare_conditionals(self, path, exaggeration=0.5, norm_loudness=True):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        self.conds = conditioning(speaker_of(path))

    def generate(self, text, audio_prompt_path=None, **kwargs):
        time.sleep(0.02)
        speaker = self.conds.t3.speaker_emb[0, 0].item()
        self.conds.t3.speaker_emb.fill_(999)  # upstream mutates request conditioning in place
        noise = torch.rand(12000) * 1e-3  # consumes the seeded RNG like sampling does
        value = float("nan") if self.nonfinite else speaker * 0.5
        return (torch.full((1, 12000), value) + noise).unsqueeze(0)


def fake_factory(model_kwargs=None):
    def factory(settings, admitted, output):
        from backend import config
        config.set_data_dir(output.path / "runtime" / "data")
        adapter = turbo.ChatterboxTurboTTSBackend()

        def load_sync():
            adapter._device = "cpu"
            adapter.model = FakeModel(**(model_kwargs or {}))
            adapter._conditioning_model_id = f"{turbo.CHATTERBOX_TURBO_HF_REPO}@{REVISION}"

        adapter._load_model_sync = load_sync
        return adapter
    return factory


@pytest.fixture(autouse=True)
def clean_memory_cache():
    cache.clear_voice_prompt_memory_cache()
    yield
    cache.clear_voice_prompt_memory_cache()


@pytest.fixture
def settings(tmp_path):
    cache_root = tmp_path / "hub"
    cache_root.mkdir()
    return qualify.Settings("cpu", cache_root, tmp_path / "run",
                            make_reference(tmp_path / "a.wav", 10), "Reference A.",
                            make_reference(tmp_path / "b.wav", 200), "Reference B.", seed=7)


def run(settings, factory, **kwargs):
    output = qualify.Output(settings.output_root)
    admitted = {"repo_id": turbo.CHATTERBOX_TURBO_HF_REPO, "revision": REVISION, "snapshot": "fake"}
    try:
        report = asyncio.run(qualify.qualify(
            settings, output, factory=factory, admit=lambda _: admitted, identify=lambda _: {"fixture": True},
            get_provenance=lambda: {"fixture": True},
            **{"verify_residency": lambda roots, device: {"roots": sorted(roots), "device": device}, **kwargs}))
    finally:
        output.close()
    saved = json.loads((settings.output_root / "results.json").read_text())
    assert saved["status"] == report["status"] and len(saved["checks"]) == len(report["checks"])
    return report


def test_every_check_passes_against_the_fake_backend(settings):
    report = run(settings, fake_factory())
    failures = {c["name"]: c["error"] for c in report["checks"] if not c["passed"]}
    assert failures == {}
    assert report["status"] == "ok" and report["summary"] == {"passed": len(qualify.CHECKS), "failed": 0, "expected": len(qualify.CHECKS)}
    assert [c["name"] for c in report["checks"]] == [c.__name__.removeprefix("check_") for c in qualify.CHECKS]
    assert report["load"] == {"residency": {"roots": ["s3gen", "t3", "ve"], "device": "cpu"},
                              "loaded_revision": f"{turbo.CHATTERBOX_TURBO_HF_REPO}@{REVISION}"}
    by_name = {c["name"]: c["evidence"] for c in report["checks"]}
    assert by_name["memory_hit_skips_prepare"]["prepare_calls"] == 1
    assert by_name["disk_hit_skips_prepare"]["hit"] is True
    assert by_name["cached_payload_cpu_owned"]["memory_devices"] == ["cpu"]
    assert by_name["device_restoration_uses_fresh_storage"]["shared_storage"] == []
    assert by_name["bypass_never_touches_cache"]["prepare_calls_per_generate"] == 1
    assert by_name["unload_releases_vram_and_reload_hits_disk"]["disk_hit_after_reload"] is True
    assert by_name["cancellation_then_admission"]["cancelled"] is True
    assert by_name["fixed_seed_cache_vs_bypass_parity"]["same_length"] is True
    assert by_name["same_mode_repeat_reproducibility"]["cached"]["bit_exact"] is True
    assert by_name["all_audio_finite_mono"]["count"] == len(report["wavs"])
    for name in report["wavs"]:
        with wave.open(str(settings.output_root / name), "rb") as audio:
            assert audio.getnchannels() == 1 and audio.getnframes() == 12000
    assert set(report["stages"]) >= {"01_adapter_init", "02_load"} and report["stages"]["02_load"]["rss_bytes"] > 0
    # Authorized references are copied, never mutated.
    assert settings.reference_a.read_bytes() == make_reference(settings.reference_a.with_name("a2.wav"), 10).read_bytes()


def test_broken_cache_fails_hit_checks_but_keeps_running(settings, monkeypatch):
    monkeypatch.setattr(cache, "get_cached_voice_prompt", lambda key: None)
    report = run(settings, fake_factory())
    assert report["status"] == "failed"
    results = {c["name"]: c for c in report["checks"]}
    assert results["memory_hit_skips_prepare"]["passed"] is False
    assert "memory hit: hit=False" in results["memory_hit_skips_prepare"]["error"]
    assert results["all_audio_finite_mono"]["passed"] is True
    assert report["summary"]["expected"] == len(qualify.CHECKS) and report["summary"]["failed"] >= 1


def test_nonfinite_audio_fails_the_first_generating_check(settings):
    report = run(settings, fake_factory({"nonfinite": True}))
    first = report["checks"][0]
    assert first["name"] == "memory_hit_skips_prepare" and first["passed"] is False
    assert "nonfinite" in first["error"]
    assert not list(settings.output_root.glob("*.wav"))


def test_missing_builtin_voice_fails_builtin_check_only(settings):
    report = run(settings, fake_factory({"builtin": False}))
    results = {c["name"]: c["passed"] for c in report["checks"]}
    assert results["builtin_voice_generates"] is False
    assert results["memory_hit_skips_prepare"] is True and results["bypass_never_touches_cache"] is True


def test_revision_mismatch_stops_before_checks(settings):
    factory = fake_factory()

    def wrong_revision(*args):
        adapter = factory(*args)
        original = adapter._load_model_sync

        def load_sync():
            original()
            adapter._conditioning_model_id = f"{turbo.CHATTERBOX_TURBO_HF_REPO}@{'d' * 40}"

        adapter._load_model_sync = load_sync
        return adapter

    report = run(settings, wrong_revision)
    assert report["status"] == "failed" and report["checks"] == []
    assert "expected" in report["error"]["message"] and not report["wavs"]


def test_residency_mismatch_stops_before_checks(settings):
    def mismatch(roots, device):
        raise qualify.telemetry.ResidencyError("fake: tensors on ['cpu'], expected cuda:0")

    report = run(settings, fake_factory(), verify_residency=mismatch)
    assert report["status"] == "failed" and report["error"]["type"] == "ResidencyError" and report["checks"] == []


def test_spy_observes_and_calls_through():
    calls = []
    owner = SimpleNamespace(method=lambda value: calls.append(value) or value * 2)
    spy = qualify.Spy(owner, "method", before=lambda: "observed")
    assert owner.method(21) == 42 and spy.calls == 1 and spy.observations == ["observed"]
    assert calls == [21] and spy.started.is_set() and spy.finished.is_set()


def test_compare_audio_reports_exactness_or_difference():
    left = np.array([0.0, 0.5, -0.5])
    assert qualify.compare_audio(left, left.copy())["bit_exact"] is True
    different = qualify.compare_audio(left, np.array([0.0, 0.25, -0.5]))
    assert different["bit_exact"] is False and different["max_abs_difference"] == 0.25
    assert qualify.compare_audio(left, np.zeros(2)) == {"same_length": False, "bit_exact": False, "lengths": [3, 2]}


@pytest.mark.parametrize("change", [
    {"device": "auto"}, {"device": "mlx"}, {"reference_b_text": " "}, {"text": ""},
])
def test_invalid_settings_are_refused(settings, change):
    from dataclasses import replace
    with pytest.raises(ValueError):
        qualify.validate(replace(settings, **change))


def test_identical_references_are_refused(settings):
    from dataclasses import replace
    with pytest.raises(ValueError, match="distinct"):
        qualify.validate(replace(settings, reference_b=settings.reference_a))


def test_main_reports_failed_checks_with_nonzero_exit(settings, monkeypatch):
    import functools
    monkeypatch.setattr(qualify.bench, "configure_offline", lambda *args: None)
    admitted = {"repo_id": turbo.CHATTERBOX_TURBO_HF_REPO, "revision": REVISION, "snapshot": "fake"}
    monkeypatch.setattr(qualify, "qualify", functools.partial(
        qualify.qualify, factory=fake_factory(), admit=lambda _: admitted, identify=lambda _: {"fixture": True},
        verify_residency=lambda roots, device: {"fixture": True}, get_provenance=lambda: {"fixture": True}))
    argv = ["--device", "cpu", "--cache-root", str(settings.cache_root), "--output-root", str(settings.output_root),
            "--reference-a", str(settings.reference_a), "--reference-a-text", "A.",
            "--reference-b", str(settings.reference_b), "--reference-b-text", "B."]
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(cache, "get_cached_voice_prompt", lambda key: None)
        assert qualify.main(argv) == 2
    report = json.loads((settings.output_root / "results.json").read_text())
    assert report["status"] == "failed" and report["summary"]["failed"] >= 1
    cache.clear_voice_prompt_memory_cache()
    assert qualify.main(argv[:5] + [str(settings.output_root.with_name("run-ok"))] + argv[6:]) == 0


def test_cli_refuses_missing_cache_before_importing_models(tmp_path, settings):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--device", "cpu", "--cache-root", str(settings.cache_root),
         "--output-root", str(tmp_path / "cli-run"), "--reference-a", str(settings.reference_a),
         "--reference-a-text", "A.", "--reference-b", str(settings.reference_b), "--reference-b-text", "B."],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not locally ready" in result.stderr
    report = json.loads((tmp_path / "cli-run" / "results.json").read_text())
    assert report["status"] == "failed" and report["checks"] == []


def test_spy_does_not_keep_its_owner_alive():
    import gc
    import weakref

    class Owner:
        def method(self, value):
            return value + 1

    owner = Owner()
    spy = qualify.Spy(owner, "method")
    assert owner.method(1) == 2 and spy.calls == 1
    alive = weakref.ref(owner)
    del owner
    gc.collect()
    assert alive() is None, "spy retained the model it observes"
