"""Host/device identity, CUDA admission, stage telemetry and tensor residency.

Shared by the local benchmark and the Turbo qualification runner. Every number
comes from a tool (nvidia-smi, torch, psutil); nothing is inferred from a
device string. CUDA is synchronized only at stage boundaries.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import platform
import subprocess
import time


class DeviceRefused(ValueError):
    """The selected device does not satisfy the NVIDIA CUDA requirement."""


class ResidencyError(ValueError):
    """Loaded tensors do not live on the selected device."""


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    total_vram_mib: int
    compute_capability: str
    driver_version: str


@dataclass(frozen=True)
class DeviceIdentity:
    os: str
    machine: str
    torch_version: str
    torch_cuda: str | None
    torch_hip: str | None
    cuda_available: bool
    cuda_device_count: int
    gpus: tuple[Gpu, ...]
    nvidia_smi_error: str | None
    bf16_supported: bool | None
    selected_device: str
    torch_device_name: str | None
    compute_capability: str | None


@dataclass(frozen=True)
class StageMeasurement:
    name: str
    wall_s: float
    rss_bytes: int | None
    peak_rss_bytes: int | None
    cuda_allocated_start_bytes: int | None
    cuda_allocated_bytes: int | None
    cuda_allocated_peak_bytes: int | None
    cuda_reserved_bytes: int | None
    cuda_reserved_peak_bytes: int | None


def as_record(value):
    return asdict(value)


SMI_QUERY = ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader,nounits"]


def run_nvidia_smi():
    """Return nvidia-smi's CSV text, or None when the tool is absent or fails."""
    try:
        return subprocess.check_output(SMI_QUERY, text=True, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def parse_nvidia_smi(text):
    gpus = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 5:
            raise ValueError(f"unexpected nvidia-smi row: {line!r}")
        index, name, total, driver, capability = fields
        gpus.append(Gpu(int(index), name, int(total), capability, driver))
    return tuple(gpus)


def collect_identity(selected_device, *, torch_module=None, smi=run_nvidia_smi):
    """Build the frozen identity record from nvidia-smi and torch. Importing
    torch here is deliberate: callers finish their offline setup first."""
    if torch_module is None:
        import torch as torch_module
    smi_output = smi()
    gpus, smi_error = (), "nvidia-smi unavailable or failed"
    if smi_output is not None:
        gpus, smi_error = parse_nvidia_smi(smi_output), None
    cuda = torch_module.cuda
    available = bool(cuda.is_available())
    name = capability = bf16 = None
    if available and selected_device.startswith("cuda"):
        index = int(selected_device.partition(":")[2] or 0)
        name = cuda.get_device_name(index)
        capability = "%d.%d" % tuple(cuda.get_device_capability(index))
        bf16 = bool(cuda.is_bf16_supported())
    return DeviceIdentity(
        os=platform.platform(), machine=platform.machine(),
        torch_version=torch_module.__version__,
        torch_cuda=torch_module.version.cuda, torch_hip=getattr(torch_module.version, "hip", None),
        cuda_available=available, cuda_device_count=cuda.device_count() if available else 0,
        gpus=gpus, nvidia_smi_error=smi_error, bf16_supported=bf16,
        selected_device=selected_device, torch_device_name=name, compute_capability=capability,
    )


def require_cuda(identity):
    """NVIDIA only: torch's cuda frontend also describes ROCm/HIP builds."""
    if identity.torch_hip:
        raise DeviceRefused(f"ROCm/HIP torch build ({identity.torch_hip}) is not NVIDIA CUDA")
    if not identity.torch_cuda:
        raise DeviceRefused("torch is not a CUDA build (torch.version.cuda is unset)")
    if not identity.cuda_available:
        raise DeviceRefused("torch reports CUDA not available")
    if not identity.gpus:
        raise DeviceRefused(f"NVIDIA GPU not confirmed: {identity.nvidia_smi_error}")


def _rusage_peak_rss_bytes():
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if platform.system() == "Darwin" else peak * 1024


def process_memory(process):
    """(rss, peak rss) in bytes; peak is the Windows peak working set or ru_maxrss.
    Without psutil both are None rather than guessed."""
    if process is None:
        return None, None
    info = process.memory_info()
    peak = getattr(info, "peak_wset", None)
    if peak is None:
        peak = _rusage_peak_rss_bytes()
    return info.rss, peak


class StageMeter:
    """Per-stage wall time plus CUDA and process memory, synchronized at the
    stage's boundaries only."""

    def __init__(self, device, *, cuda, process, clock=time.perf_counter_ns):
        self.device, self.cuda, self.process, self.clock = device, cuda, process, clock
        self.stages = {}

    @classmethod
    def for_device(cls, device, clock=time.perf_counter_ns):
        try:
            import psutil
            process = psutil.Process()
        except ImportError:
            process = None
        cuda = None
        if device.startswith("cuda"):
            import torch
            cuda = torch.cuda
        return cls(device, cuda=cuda, process=process, clock=clock)

    def cuda_allocated_bytes(self):
        return None if self.cuda is None else self.cuda.memory_allocated(self.device)

    @contextmanager
    def stage(self, name):
        if name in self.stages:
            raise ValueError(f"stage already measured: {name}")
        start_allocated = None
        if self.cuda is not None:
            self.cuda.synchronize(self.device)
            self.cuda.reset_peak_memory_stats(self.device)
            start_allocated = self.cuda.memory_allocated(self.device)
        start = self.clock()
        try:
            yield
        finally:
            if self.cuda is not None:
                self.cuda.synchronize(self.device)
            wall_s = (self.clock() - start) / 1e9
            rss, peak_rss = process_memory(self.process)
            measurement = StageMeasurement(name, wall_s, rss, peak_rss, start_allocated, None, None, None, None)
            if self.cuda is not None:
                measurement = replace(
                    measurement,
                    cuda_allocated_bytes=self.cuda.memory_allocated(self.device),
                    cuda_allocated_peak_bytes=self.cuda.max_memory_allocated(self.device),
                    cuda_reserved_bytes=self.cuda.memory_reserved(self.device),
                    cuda_reserved_peak_bytes=self.cuda.max_memory_reserved(self.device),
                )
            self.stages[name] = measurement


def tensor_devices(root, *, max_depth=4):
    """Devices of every parameter, buffer and tensor reachable from root."""
    import torch
    devices, seen, count = set(), set(), 0

    def visit(value, depth):
        nonlocal count
        if id(value) in seen or depth > max_depth:
            return
        seen.add(id(value))
        if isinstance(value, torch.Tensor):
            devices.add(str(value.device))
            count += 1
        elif isinstance(value, torch.nn.Module):
            for tensor in list(value.parameters()) + list(value.buffers()):
                devices.add(str(tensor.device))
                count += 1
        elif isinstance(value, dict):
            for item in value.values():
                visit(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
        elif hasattr(value, "__dict__"):
            for item in vars(value).values():
                visit(item, depth + 1)

    visit(root, 0)
    return devices, count


def normalize_device(device):
    return "cuda:0" if device == "cuda" else device


def check_residency(roots, expected):
    """Fail unless every tensor under every root lives on the expected device."""
    expected = normalize_device(expected)
    evidence = {}
    for name, root in roots.items():
        devices, count = tensor_devices(root)
        evidence[name] = {"devices": sorted(devices), "tensors": count}
        if not count:
            raise ResidencyError(f"{name}: no tensors found to verify residency")
        if devices != {expected}:
            raise ResidencyError(f"{name}: tensors on {sorted(devices)}, expected {expected}")
    return evidence
