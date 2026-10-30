"""Device identity, CUDA refusal, stage telemetry and residency with labelled fakes."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import device_telemetry as telemetry


SMI = "0, NVIDIA GeForce RTX 5070 Laptop GPU, 8151, 610.88, 12.0\n"


def fake_torch(*, cuda="12.8", hip=None, available=True, bf16=True, count=1):
    """Fake torch namespace: only the attributes the helper reads."""
    return SimpleNamespace(
        __version__="2.9.0+cu128",
        version=SimpleNamespace(cuda=cuda, hip=hip),
        cuda=SimpleNamespace(
            is_available=lambda: available,
            device_count=lambda: count,
            get_device_name=lambda index: f"fake gpu {index}",
            get_device_capability=lambda index: (12, 0),
            is_bf16_supported=lambda: bf16,
        ),
    )


def test_identity_is_frozen_and_records_smi_and_torch_facts():
    identity = telemetry.collect_identity("cuda:0", torch_module=fake_torch(), smi=lambda: SMI)
    assert identity.gpus == (telemetry.Gpu(0, "NVIDIA GeForce RTX 5070 Laptop GPU", 8151, "12.0", "610.88"),)
    assert identity.torch_cuda == "12.8" and identity.torch_hip is None and identity.bf16_supported is True
    assert identity.selected_device == "cuda:0" and identity.torch_device_name == "fake gpu 0"
    assert identity.compute_capability == "12.0"
    with pytest.raises(AttributeError):
        identity.selected_device = "cpu"
    record = telemetry.as_record(identity)
    assert record["gpus"][0]["name"].startswith("NVIDIA") and record["os"]


def test_identity_without_nvidia_smi_or_cuda_keeps_unknowns():
    identity = telemetry.collect_identity("cpu", torch_module=fake_torch(available=False, cuda=None), smi=lambda: None)
    assert identity.gpus == () and identity.nvidia_smi_error is not None
    assert identity.bf16_supported is None and identity.torch_device_name is None
    assert identity.compute_capability is None


@pytest.mark.parametrize("change,reason", [
    ({"hip": "6.2"}, "ROCm"),
    ({"cuda": None}, "CUDA build"),
    ({"available": False}, "not available"),
])
def test_require_cuda_refuses_rocm_cpu_builds_and_unavailable_devices(change, reason):
    identity = telemetry.collect_identity("cuda:0", torch_module=fake_torch(**change), smi=lambda: SMI)
    with pytest.raises(telemetry.DeviceRefused, match=reason):
        telemetry.require_cuda(identity)


def test_require_cuda_refuses_without_nvidia_smi_and_accepts_real_nvidia():
    refused = telemetry.collect_identity("cuda:0", torch_module=fake_torch(), smi=lambda: None)
    with pytest.raises(telemetry.DeviceRefused, match="nvidia-smi"):
        telemetry.require_cuda(refused)
    telemetry.require_cuda(telemetry.collect_identity("cuda:0", torch_module=fake_torch(), smi=lambda: SMI))


def test_malformed_smi_rows_are_refused_not_guessed():
    with pytest.raises(ValueError, match="nvidia-smi"):
        telemetry.parse_nvidia_smi("0, broken row\n")


class FakeCuda:
    """Fake torch.cuda memory API recording sync/reset order."""

    def __init__(self):
        self.events = []
        self.allocated, self.peak_allocated, self.reserved, self.peak_reserved = 10, 99, 20, 99

    def synchronize(self, device):
        self.events.append(("sync", device))

    def reset_peak_memory_stats(self, device):
        self.events.append(("reset", device))
        self.peak_allocated, self.peak_reserved = self.allocated, self.reserved

    def memory_allocated(self, device):
        return self.allocated

    def max_memory_allocated(self, device):
        return self.peak_allocated

    def memory_reserved(self, device):
        return self.reserved

    def max_memory_reserved(self, device):
        return self.peak_reserved


class FakeProcess:
    """Fake psutil.Process exposing Windows peak working set."""

    def __init__(self):
        self.rss, self.peak = 1000, 1500

    def memory_info(self):
        return SimpleNamespace(rss=self.rss, peak_wset=self.peak)


def test_stage_meter_syncs_only_at_boundaries_and_resets_peak_at_start():
    cuda, process = FakeCuda(), FakeProcess()
    ticks = iter([0, 2 * 10**9])
    meter = telemetry.StageMeter("cuda:0", cuda=cuda, process=process, clock=lambda: next(ticks))
    with meter.stage("load"):
        assert cuda.events == [("sync", "cuda:0"), ("reset", "cuda:0")]
        cuda.allocated, cuda.peak_allocated, cuda.reserved, cuda.peak_reserved = 30, 50, 40, 60
        process.rss, process.peak = 2000, 2500
    assert cuda.events == [("sync", "cuda:0"), ("reset", "cuda:0"), ("sync", "cuda:0")]
    stage = meter.stages["load"]
    assert stage == telemetry.StageMeasurement(
        name="load", wall_s=2.0, rss_bytes=2000, peak_rss_bytes=2500,
        cuda_allocated_start_bytes=10, cuda_allocated_bytes=30, cuda_allocated_peak_bytes=50,
        cuda_reserved_bytes=40, cuda_reserved_peak_bytes=60,
    )
    assert telemetry.as_record(stage)["wall_s"] == 2.0


def test_stage_meter_records_failed_stage_and_reraises():
    meter = telemetry.StageMeter("cuda:0", cuda=FakeCuda(), process=FakeProcess(), clock=iter([0, 10**9]).__next__)
    with pytest.raises(RuntimeError, match="boom"):
        with meter.stage("generation_0"):
            raise RuntimeError("boom")
    assert meter.stages["generation_0"].wall_s == 1.0


def test_cpu_meter_has_no_cuda_fields_and_never_touches_cuda():
    meter = telemetry.StageMeter("cpu", cuda=None, process=FakeProcess(), clock=iter([0, 1]).__next__)
    with meter.stage("load"):
        pass
    stage = meter.stages["load"]
    assert stage.cuda_allocated_bytes is None and stage.cuda_reserved_peak_bytes is None
    assert stage.rss_bytes == 1000 and stage.peak_rss_bytes == 1500


def test_duplicate_stage_names_are_refused():
    meter = telemetry.StageMeter("cpu", cuda=None, process=FakeProcess(), clock=iter(range(10)).__next__)
    with meter.stage("load"):
        pass
    with pytest.raises(ValueError, match="load"):
        with meter.stage("load"):
            pass


def test_peak_rss_falls_back_to_rusage_without_peak_wset(monkeypatch):
    class PosixProcess:
        def memory_info(self):
            return SimpleNamespace(rss=5)

    monkeypatch.setattr(telemetry, "_rusage_peak_rss_bytes", lambda: 7)
    assert telemetry.process_memory(PosixProcess()) == (5, 7)


def test_meter_without_psutil_records_unknown_process_memory():
    meter = telemetry.StageMeter("cpu", cuda=None, process=None, clock=iter([0, 1]).__next__)
    with meter.stage("load"):
        pass
    assert (meter.stages["load"].rss_bytes, meter.stages["load"].peak_rss_bytes) == (None, None)


def test_real_meter_for_cpu_measures_this_process():
    meter = telemetry.StageMeter.for_device("cpu")
    with meter.stage("noop"):
        pass
    assert meter.stages["noop"].wall_s >= 0 and meter.stages["noop"].rss_bytes > 0


torch = pytest.importorskip("torch")


def test_residency_reports_devices_of_modules_and_tensors_and_refuses_mismatch():
    class Wrapper:
        def __init__(self):
            self.t3 = torch.nn.Linear(2, 2)
            self.extra = {"buffer": torch.zeros(1)}

    roots = {"model": Wrapper()}
    evidence = telemetry.check_residency(roots, "cpu")
    assert evidence == {"model": {"devices": ["cpu"], "tensors": 3}}
    with pytest.raises(telemetry.ResidencyError, match="cuda:0"):
        telemetry.check_residency(roots, "cuda")
    with pytest.raises(telemetry.ResidencyError, match="no tensors"):
        telemetry.check_residency({"empty": object()}, "cpu")
