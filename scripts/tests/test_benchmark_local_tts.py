"""Deterministic benchmark behavior; all disposable files belong on Scratch."""

import os
import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "benchmark-local-tts.py"
SPEC = importlib.util.spec_from_file_location("benchmark_local_tts", SCRIPT)
bench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


@pytest.fixture
def settings(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    return bench.Settings("qwen_custom_voice", "pytorch", "1.7B", "en", "Test speech.",
                          cache, tmp_path / "output", preset_voice="Ryan", warm_repeats=2)


@pytest.fixture
def cached(settings):
    repo = settings.cache_root / "models--Qwen--Qwen3-TTS-12Hz-1.7B-CustomVoice"
    snapshot = repo / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("a" * 40)
    for name in ("config.json", "generation_config.json", "tokenizer_config.json",
                 "vocab.json", "merges.txt", "preprocessor_config.json", "model.safetensors",
                 "speech_tokenizer/config.json", "speech_tokenizer/preprocessor_config.json",
                 "speech_tokenizer/model.safetensors"):
        path = snapshot / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"tts_model_type": "custom_voice"}' if name == "config.json" else "{}")
    return repo, snapshot


class FakeAdapter:
    def __init__(self, fail=None, outputs=None):
        self.fail, self.outputs = fail, outputs
        self.loads, self.unloads, self.calls, self.references = [], 0, [], []

    def is_loaded(self):
        return self.fail == "already_loaded"

    async def load_model(self, model):
        self.loads.append(model)
        if self.fail == "load":
            raise RuntimeError("load failed")

    async def create_voice_prompt(self, path, text, use_cache):
        self.references.append((path, text, use_cache))
        if self.fail == "reference":
            raise RuntimeError("reference failed")
        return {"ref_audio": path, "ref_text": text}, False

    async def generate(self, text, prompt, **kwargs):
        self.calls.append((text, prompt, kwargs))
        if self.fail == "generate" or (self.fail == "second" and len(self.calls) == 2):
            raise RuntimeError("generation failed")
        if self.fail == "interrupt":
            raise KeyboardInterrupt()
        return self.outputs if self.outputs is not None else (np.array([0., .25, -.25, 1.5]), 4)

    def unload_model(self):
        self.unloads += 1
        if self.fail == "unload":
            raise RuntimeError("unload failed")


def run_fake(settings, adapter, **kwargs):
    output = bench.Output(settings.output_root)
    ticks = iter(range(0, 100 * 10**9, 10**9))
    try:
        result = asyncio.run(bench.benchmark(
            settings, output, factory=lambda *args: adapter,
            admit=lambda _: {"snapshot": "fake-cached-snapshot"}, clock=lambda: next(ticks),
            get_provenance=lambda: {"fixture": True}, **kwargs,
        ))
        saved = json.loads((settings.output_root / "results.json").read_text())
        assert saved["status"] == result["status"]
        return result
    finally:
        output.close()


def test_cli_refuses_missing_cache_before_loading_or_writing_audio(tmp_path):
    cache = tmp_path / "empty-cache"
    cache.mkdir()
    output = tmp_path / "run"
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--engine", "qwen_custom_voice",
         "--backend", "pytorch", "--model", "1.7B", "--language", "en",
         "--preset-voice", "Ryan", "--text", "A short offline test.",
         "--cache-root", str(cache), "--output-root", str(output)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not locally ready" in result.stderr
    assert not list(output.rglob("*.wav"))
    report = json.loads((output / "results.json").read_text())
    assert report["status"] == "failed" and report["error"]["stage"] == "admission"


def test_cold_warm_metrics_prompt_and_real_wav(settings):
    adapter = FakeAdapter()
    result = run_fake(settings, adapter)
    assert result["status"] == "ok"
    assert adapter.loads == ["1.7B"] and adapter.unloads == 1
    assert len(adapter.calls) == 3
    assert adapter.calls[0] == ("Test speech.", {"voice_type": "preset", "preset_engine": "qwen_custom_voice",
                                               "preset_voice_id": "Ryan"},
                                {"language": "en", "seed": 0, "instruct": None})
    assert result["stages_s"] == {"adapter_init": 1, "load": 1, "reference_preparation": 1}
    assert result["cold_load_reference_generation_rtf"] == 3
    assert result["warm"]["wall_audio_rtf"] == {"count": 2, "p50": 1, "p95": 1}
    assert [run["kind"] for run in result["runs"]] == ["cold", "warm", "warm"]
    for run in result["runs"]:
        assert run["duration_s"] == 1 and run["generation_s"] == 1 and run["wall_audio_rtf"] == 1
        assert run["first_model_audio_s"] is None
        assert run["clipped_samples_pcm16"] == 1
        with wave.open(str(settings.output_root / run["wav"]), "rb") as audio:
            assert (audio.getnframes(), audio.getframerate(), audio.getsampwidth()) == (4, 4, 2)
            assert np.frombuffer(audio.readframes(4), dtype="<i2").tolist() == [0, 8191, -8191, 32767]


def test_distribution_uses_linear_interpolation_and_empty_is_unavailable():
    assert bench.distribution([]) is None
    assert bench.distribution([9]) == {"count": 1, "p50": 9, "p95": 9}
    assert bench.distribution([9, 1, 3, 7])["p50"] == 5
    assert bench.distribution([9, 1, 3, 7])["p95"] == pytest.approx(8.7)


def test_no_warm_repeats_is_not_a_fake_distribution(settings):
    result = run_fake(replace(settings, warm_repeats=0), FakeAdapter())
    assert result["warm"]["generation_s"] is None
    assert len(result["runs"]) == 1


def make_reference(path, frames=8, rate=8):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * frames)
    return path


def test_reference_prompt_is_prepared_once_without_shared_cache(settings, tmp_path):
    ref = make_reference(tmp_path / "ref.wav")
    settings = replace(settings, engine="qwen", preset_voice=None, reference=ref, reference_text="Reference speech.")
    adapter = FakeAdapter()
    result = run_fake(settings, adapter)
    assert adapter.references == [(str(ref), "Reference speech.", False)]
    assert result["reference"]["duration_s"] == 1
    assert len(result["reference"]["sha256"]) == 64


def test_voice_design_and_instruction_are_explicit(settings):
    settings = replace(settings, engine="qwen_voice_design", preset_voice=None,
                       design_prompt="A clear voice.", instruct="Speak slowly.")
    bench.validate(settings)
    adapter = FakeAdapter()
    result = run_fake(settings, adapter)
    assert result["status"] == "ok"
    assert adapter.calls[0][1] == {"voice_type": "designed", "design_prompt": "A clear voice."}
    assert adapter.calls[0][2]["instruct"] == "Speak slowly."


@pytest.mark.parametrize("failure,stage", [("load", "load"), ("generate", "generation_0"),
                                          ("second", "generation_1"), ("already_loaded", "adapter_init"),
                                          ("interrupt", "generation_0")])
def test_failures_keep_partial_results_and_always_unload(settings, failure, stage):
    adapter = FakeAdapter(fail=failure)
    result = run_fake(settings, adapter)
    assert result["status"] == "failed" and result["error"]["stage"] == stage
    assert adapter.unloads == 1
    assert result["warm"] is None
    assert len(result["runs"]) == (1 if failure == "second" else 0)


def test_reference_failure_unloads(settings, tmp_path):
    settings = replace(settings, reference=make_reference(tmp_path / "ref.wav"), reference_text="Ref")
    adapter = FakeAdapter(fail="reference")
    result = run_fake(settings, adapter)
    assert result["status"] == "failed" and result["error"]["stage"] == "reference_preparation"
    assert adapter.unloads == 1


def test_unload_failure_cannot_be_success(settings):
    result = run_fake(settings, FakeAdapter(fail="unload"))
    assert result["status"] == "failed" and result["cleanup_error"]["message"] == "unload failed"


@pytest.mark.parametrize("audio,rate", [([], 24000), ([float("nan")], 24000), ([float("inf")], 24000),
                                       ([[0., 1.]], 24000), ([1+2j], 24000), ([0.], 0), ([0.], -1),
                                       ([0.], float("nan")), ([0.], 2.5), ([0.], True), ([0.], 192001)])
def test_bad_model_audio_is_failed_and_not_written(settings, audio, rate):
    adapter = FakeAdapter(outputs=(audio, rate))
    result = run_fake(settings, adapter)
    assert result["status"] == "failed" and result["error"]["stage"] == "generation_0"
    assert not list(settings.output_root.glob("*.wav")) and adapter.unloads == 1


def test_duration_and_total_artifact_budget(settings, monkeypatch):
    result = run_fake(replace(settings, max_audio_seconds=.5), FakeAdapter())
    assert result["status"] == "failed" and "duration limit" in result["error"]["message"]
    monkeypatch.setattr(bench, "MAX_ARTIFACT_BYTES", 1)
    result = run_fake(replace(settings, output_root=settings.output_root.parent / "budget"), FakeAdapter())
    assert result["status"] == "failed" and "budget" in result["error"]["message"]


@pytest.mark.parametrize("change", [
    {"engine": "remote"}, {"backend": "auto"}, {"backend": "mlx"}, {"model": "2B"},
    {"engine": "qwen_voice_design", "model": "0.6B"}, {"language": "auto"},
    {"text": " "}, {"text": "a" * 2001}, {"warm_repeats": -1}, {"warm_repeats": 21},
    {"seed": -1}, {"seed": 2**32}, {"max_audio_seconds": float("nan")}, {"max_audio_seconds": 121},
    {"max_audio_seconds": 0}, {"instruct": " "}, {"preset_voice": "unknown"}, {"preset_voice": None},
    {"reference_text": "Unexpected reference"}, {"design_prompt": "Unexpected design"},
    {"engine": "qwen"}, {"engine": "qwen_voice_design"}, {"cache_root": Path("relative")},
])
def test_invalid_configuration_is_rejected(settings, change):
    with pytest.raises(ValueError):
        bench.validate(replace(settings, **change))


def test_base_rejects_silently_ignored_instruction(settings, tmp_path):
    settings = replace(settings, engine="qwen", reference=make_reference(tmp_path / "ref.wav"),
                       reference_text="Ref", preset_voice=None, instruct="Ignored style")
    with pytest.raises(ValueError, match="does not support"):
        bench.validate(settings)
    bench.validate(replace(settings, instruct=None))
    bench.validate(replace(settings, backend="mlx", instruct=None))


def test_readiness_is_local_and_checks_selected_revision(settings, cached, monkeypatch):
    repo, snapshot = cached
    # No network libraries are imported for admission.
    before = set(sys.modules)
    result = bench.admit_cached_model(settings)
    assert result["revision"] == "a" * 40 and result["snapshot"] == str(snapshot)
    assert not any(name.startswith("huggingface_hub") for name in set(sys.modules) - before)
    (snapshot / "model.safetensors").unlink()
    stale = repo / "snapshots" / ("b" * 40)
    stale.mkdir()
    (stale / "model.safetensors").write_text("stale")
    with pytest.raises(ValueError, match="not locally ready"):
        bench.admit_cached_model(settings)


@pytest.mark.parametrize("corruption", ["revision", "oversized_revision", "incomplete", "missing_codec", "shard", "escape", "mode", "json", "alias"])
def test_cache_admission_refuses_partial_or_unsafe_snapshots(settings, cached, corruption, tmp_path):
    repo, snapshot = cached
    if corruption == "revision":
        (repo / "refs" / "main").write_text("../../elsewhere")
    elif corruption == "oversized_revision":
        (repo / "refs" / "main").write_text("a" * 129)
    elif corruption == "incomplete":
        (repo / "blobs").mkdir()
        (repo / "blobs" / "abc.incomplete").write_text("partial")
    elif corruption == "missing_codec":
        (snapshot / "speech_tokenizer" / "model.safetensors").unlink()
    elif corruption in ("shard", "escape"):
        name = "missing.safetensors" if corruption == "shard" else "../../escape.safetensors"
        (snapshot / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"weight": name}}))
    elif corruption == "mode":
        (snapshot / "config.json").write_text('{"tts_model_type": "base"}')
    elif corruption == "json":
        (snapshot / "config.json").write_text("broken json")
    elif corruption == "alias":
        renamed = repo.with_name("foreign-repo")
        repo.rename(renamed)
        repo.symlink_to(renamed, target_is_directory=True)
    with pytest.raises(ValueError, match="not locally ready"):
        bench.admit_cached_model(settings)


def test_shards_and_hf_blob_symlinks_are_supported(settings, cached):
    repo, snapshot = cached
    blobs = repo / "blobs"
    blobs.mkdir()
    blob = blobs / "complete"
    blob.write_bytes(b"pretend weights")
    (blobs / "complete.incomplete").write_text("stale incomplete")
    (snapshot / "shard.safetensors").symlink_to(blob)
    (snapshot / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"weight": "shard.safetensors"}}))
    assert bench.admit_cached_model(settings)["revision"] == "a" * 40


def test_cache_file_symlink_escape_is_refused(settings, cached, tmp_path):
    _, snapshot = cached
    outside = tmp_path / "outside"
    outside.write_text("weights")
    (snapshot / "model.safetensors").unlink()
    (snapshot / "model.safetensors").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        bench.admit_cached_model(settings)


def test_outputs_require_scratch_fresh_directory_and_no_symlinks(tmp_path):
    with pytest.raises(ValueError, match="under /Volumes/Scratch"):
        bench.Output(Path("/Users/justin/unsafe-benchmark"))
    with pytest.raises(ValueError, match="absolute"):
        bench.Output(Path("relative"))
    with pytest.raises(ValueError, match="fresh subdirectory"):
        bench.Output(bench.SCRATCH)
    with pytest.raises(ValueError, match="absolute"):
        bench.Output(tmp_path / ".." / "unsafe")
    output = bench.Output(tmp_path / "safe")
    try:
        with output.open("owned.json") as handle:
            handle.write(b"first")
        with pytest.raises(FileExistsError):
            output.open("owned.json")
        with pytest.raises(ValueError, match="single filename"):
            output.open("../escape")
    finally:
        output.close()
    with pytest.raises(ValueError, match="already exists"):
        bench.Output(tmp_path / "safe")
    (tmp_path / "link").symlink_to(tmp_path / "safe", target_is_directory=True)
    with pytest.raises(OSError):
        bench.Output(tmp_path / "link" / "escape")
    assert not (tmp_path / "safe" / "escape").exists()


def test_missing_scratch_mount_is_a_hard_refusal(settings, monkeypatch):
    monkeypatch.setattr(Path, "is_mount", lambda _: False)
    with pytest.raises(ValueError, match="not mounted"):
        bench.Output(settings.output_root)


def test_reference_bounds_and_empty_audio(settings, tmp_path):
    with pytest.raises(ValueError, match="absolute local WAV"):
        bench.reference_info(Path("relative.wav"))
    with pytest.raises(ValueError, match="nonempty"):
        bench.reference_info(make_reference(tmp_path / "empty.wav", frames=0))
    with pytest.raises(ValueError, match="30 seconds"):
        bench.reference_info(make_reference(tmp_path / "long.wav", frames=31, rate=1))


def test_read_json_size_is_bounded(tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="oversized"):
        bench.read_json(oversized)


@pytest.mark.parametrize("engine,backend,module,class_name", [
    ("qwen", "mlx", "mlx_backend", "MLXTTSBackend"),
    ("qwen", "pytorch", "pytorch_backend", "PyTorchTTSBackend"),
    ("qwen_custom_voice", "pytorch", "qwen_custom_voice_backend", "QwenCustomVoiceBackend"),
    ("qwen_voice_design", "pytorch", "qwen_voice_design_backend", "QwenVoiceDesignBackend"),
])
def test_factory_uses_only_selected_adapter_and_pins_local_snapshot(settings, monkeypatch, engine, backend, module, class_name):
    config_dirs, requested = [], []
    patch = SimpleNamespace(ensure_original_qwen_config_cached=lambda: pytest.fail("shared cache mutation"))
    monkeypatch.setitem(sys.modules, "backend", SimpleNamespace(config=SimpleNamespace(set_data_dir=config_dirs.append)))
    monkeypatch.setitem(sys.modules, "backend.utils", SimpleNamespace(hf_offline_patch=patch))

    class Constructed:
        def __init__(self, size):
            self.size = size

    def import_module(name):
        requested.append(name)
        return SimpleNamespace(**{class_name: Constructed}, __file__=str(SCRIPT))

    monkeypatch.setattr(bench.importlib, "import_module", import_module)
    adapter = bench.create_adapter(replace(settings, engine=engine, backend=backend),
                                   {"snapshot": "/explicit/cached/snapshot"}, SimpleNamespace(path=settings.output_root))
    assert adapter.size == "1.7B" and requested == ["backend.backends." + module]
    assert adapter._get_model_path("1.7B") == "/explicit/cached/snapshot"
    assert adapter._is_model_cached() is True
    assert len(adapter._benchmark_source_sha256) == 64
    assert config_dirs == [settings.output_root / "runtime" / "data"]
    patch.ensure_original_qwen_config_cached()  # now a no-op


def test_provenance_records_versions_commit_and_source_hash():
    result = bench.provenance()
    assert len(result["git_commit"]) == 40 and isinstance(result["git_dirty"], bool)
    assert result["versions"]["numpy"] and len(result["runner_sha256"]) == 64
    assert result["recorded_at_utc"].endswith("+00:00")


def test_provenance_preserves_unknowns_instead_of_inventing_versions(monkeypatch):
    def missing(*args, **kwargs):
        raise bench.importlib.metadata.PackageNotFoundError()

    def no_git(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(bench.importlib.metadata, "version", missing)
    monkeypatch.setattr(bench.subprocess, "check_output", no_git)
    result = bench.provenance()
    assert result["git_commit"] is None and result["git_dirty"] is None
    assert all(value is None for value in result["versions"].values())


def test_cli_help_and_unsafe_output_refusal(tmp_path):
    help_result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=10)
    assert help_result.returncode == 0 and "--warm-repeats" in help_result.stdout
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--engine", "qwen_custom_voice",
                             "--backend", "pytorch", "--model", "1.7B", "--language", "en",
                             "--preset-voice", "Ryan", "--text", "Offline test.",
                             "--cache-root", str(tmp_path), "--output-root", "/Users/justin/unsafe-benchmark"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "under /Volumes/Scratch" in result.stderr


def test_main_entry_point_failure_is_nonzero_and_closes_output(settings, monkeypatch):
    # Keep the irreversible network audit hook out of the pytest process.
    monkeypatch.setattr(bench, "configure_offline", lambda *args: None)
    code = bench.main(["--engine", settings.engine, "--backend", settings.backend,
                       "--model", settings.model, "--language", settings.language,
                       "--text", settings.text, "--preset-voice", "Ryan",
                       "--cache-root", str(settings.cache_root), "--output-root", str(settings.output_root)])
    assert code == 2
    assert json.loads((settings.output_root / "results.json").read_text())["status"] == "failed"
    assert bench.main(["--engine", "invalid", "--backend", "pytorch", "--model", "1.7B",
                       "--language", "en", "--text", "Test", "--cache-root", str(settings.cache_root),
                       "--output-root", str(settings.output_root)]) == 2


def test_offline_guard_blocks_connections_and_dns_in_subprocess(settings):
    code = '''
import importlib.util, os, socket, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("benchmark_local_tts", sys.argv[1])
b = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = b
spec.loader.exec_module(b)
settings = b.Settings("qwen_custom_voice", "pytorch", "1.7B", "en", "Test", Path(sys.argv[2]), Path(sys.argv[3]), preset_voice="Ryan")
output = b.Output(settings.output_root)
b.configure_offline(settings, output)
assert os.environ["HF_HUB_OFFLINE"] == os.environ["TRANSFORMERS_OFFLINE"] == "1"
assert os.environ["HF_HUB_CACHE"] == str(settings.cache_root)
assert Path(tempfile.gettempdir()).is_relative_to(output.path)
assert not any(name.startswith("backend") for name in sys.modules)
for callback in (lambda: socket.getaddrinfo("example.invalid", 443), lambda: socket.gethostbyname("example.invalid"), lambda: socket.gethostbyaddr("192.0.2.1")):
    try:
        callback()
    except OSError as error:
        assert "forbids" in str(error)
    else:
        raise AssertionError("DNS lookup admitted")
with socket.socket() as stream:
    try:
        stream.connect(("192.0.2.1", 443))
    except OSError as error:
        assert "forbids" in str(error)
    else:
        raise AssertionError("connection admitted")
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
    try:
        datagram.sendto(b"test", ("192.0.2.1", 443))
    except OSError as error:
        assert "forbids" in str(error)
    else:
        raise AssertionError("datagram admitted")
# The asyncio self-pipe uses local socket pairs and must remain usable.
left, right = socket.socketpair()
left.close()
right.close()
output.close()
print("offline guard passed")
'''
    result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPT), str(settings.cache_root),
                             str(settings.output_root)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "offline guard passed"


def test_offline_configuration_and_guard_without_process_side_effects(settings, monkeypatch):
    output = bench.Output(settings.output_root)
    hooks = []
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(bench.tempfile, "tempdir", None)
    try:
        bench.configure_offline(settings, output)
        assert os.environ["VOICEBOX_OFFLINE_PATCH"] == "0"
        assert Path(os.environ["TORCH_HOME"]).is_relative_to(settings.output_root)
        assert bench.tempfile.tempdir == str(settings.output_root / "runtime" / "tmp")
        guard = hooks[0]
        with pytest.raises(OSError, match="network lookup"):
            guard("socket.getaddrinfo", ())
        with pytest.raises(OSError, match="network connections"):
            guard("socket.connect", (SimpleNamespace(family=bench.socket.AF_INET), ("192.0.2.1", 443)))
        guard("socket.connect", (SimpleNamespace(family=bench.socket.AF_UNIX), "local"))
        guard("unrelated.event", ())
    finally:
        output.close()


def test_main_success_and_output_close(settings, monkeypatch):
    opened = []
    original_output = bench.Output

    def open_output(path):
        result = original_output(path)
        opened.append(result)
        return result

    async def fake_benchmark(*args):
        return {"status": "ok"}

    monkeypatch.setattr(bench, "Output", open_output)
    monkeypatch.setattr(bench, "configure_offline", lambda *args: None)
    monkeypatch.setattr(bench, "benchmark", fake_benchmark)
    assert bench.main(["--engine", settings.engine, "--backend", settings.backend,
                       "--model", settings.model, "--language", settings.language,
                       "--text", settings.text, "--preset-voice", "Ryan",
                       "--cache-root", str(settings.cache_root), "--output-root", str(settings.output_root)]) == 0
    with pytest.raises(OSError):
        os.fstat(opened[0].fd)


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf")])
def test_invalid_clock_metrics_are_refused(elapsed):
    with pytest.raises(ValueError, match="monotonic elapsed"):
        bench.audio_metrics(np.zeros(4), 4, elapsed, 30)


def test_mlx_cache_mapping_and_empty_index_fail_closed(settings, cached):
    repo, snapshot = cached
    mlx_repo = repo.with_name("models--mlx-community--Qwen3-TTS-12Hz-1.7B-Base-bf16")
    repo.rename(mlx_repo)
    snapshot = mlx_repo / "snapshots" / snapshot.name
    (snapshot / "config.json").write_text('{"tts_model_type": "base"}')
    mlx_settings = replace(settings, backend="mlx", engine="qwen")
    assert bench.admit_cached_model(mlx_settings)["repo_id"].startswith("mlx-community/")
    (snapshot / "model.safetensors.index.json").write_text('{"weight_map": {}}')
    with pytest.raises(ValueError, match="empty weight index"):
        bench.admit_cached_model(mlx_settings)


def test_reference_truncated_payload_is_refused(tmp_path):
    path = make_reference(tmp_path / "truncated.wav")
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError, match="truncated"):
        bench.reference_info(path)


def test_nonfinite_json_is_refused_before_model_loading(settings, cached):
    _, snapshot = cached
    (snapshot / "generation_config.json").write_text('{"temperature": NaN}')
    with pytest.raises(ValueError, match="nonfinite JSON"):
        bench.admit_cached_model(settings)
