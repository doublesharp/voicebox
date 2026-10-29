"""Performance boundaries: event-loop responsiveness, bounded copying and cache work."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import huggingface_hub
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from huggingface_hub import constants as hf_constants

from backend import backends
from backend.routes import models as model_routes
from backend.studio import waveforms
from backend.studio.storage import StudioStore
from backend.utils import chunked_tts


@pytest.mark.asyncio
async def test_model_status_scan_runs_off_loop_and_shares_disk_inventory(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    scan_threads = []
    loaded = False
    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path))
    config = SimpleNamespace(
        model_name="perf", display_name="Perf", hf_repo_id="perf/model", aux_hf_repo_ids=[], model_size="default"
    )
    monkeypatch.setattr(backends, "get_all_model_configs", lambda: [config])
    monkeypatch.setattr(backends, "check_model_loaded", lambda _: loaded)
    monkeypatch.setattr(model_routes, "get_task_manager", lambda: SimpleNamespace(get_pending_downloads=list))

    def scan(cache_dir=None):
        assert cache_dir == str(tmp_path)
        scan_threads.append(threading.get_ident())
        return SimpleNamespace(repos=[])

    monkeypatch.setattr(huggingface_hub, "scan_cache_dir", scan)
    first, second = await asyncio.gather(model_routes.get_model_status(), model_routes.get_model_status())
    assert first.models[0].loaded is False
    assert second.models[0].loaded is False
    loaded = True
    third = await model_routes.get_model_status()
    assert third.models[0].loaded is True
    assert scan_threads
    assert loop_thread not in scan_threads, "disk inventory blocked the API event loop"
    assert len(scan_threads) == 1, "concurrent and warm reads should share one disk scan"


def test_chunk_assembly_does_not_recopy_accumulated_audio(monkeypatch):
    chunks = [np.full(10000, index / 20, dtype=np.float32) for index in range(20)]
    concatenate = np.concatenate
    written = 0

    def counted(arrays, *args, **kwargs):
        nonlocal written
        result = concatenate(arrays, *args, **kwargs)
        written += result.nbytes
        return result

    monkeypatch.setattr(np, "concatenate", counted)
    result = chunked_tts.concatenate_audio_chunks(chunks, 1000, 5)
    assert result.size == 200000 - 19 * 5
    assert result[100] == 0
    assert result[-100] == np.float32(0.95)
    assert written <= result.nbytes, "assembly recopied the growing recording"


@pytest.mark.parametrize("lengths", [[], [0], [0, 4, 0, 2], [10, 2, 1, 20], [2, 2, 2]])
@pytest.mark.parametrize("fade", [0, 1, 5, 30])
def test_preallocated_crossfades_preserve_samples_and_inputs(lengths, fade):
    rng = np.random.default_rng(42)
    chunks = [rng.random(length, dtype=np.float32) for length in lengths]
    saved = [chunk.copy() for chunk in chunks]
    expected = chunks[0].copy() if chunks else np.empty(0, dtype=np.float32)
    for chunk in chunks[1:]:
        overlap = min(fade, len(expected), len(chunk))
        if overlap:
            expected[-overlap:] = expected[-overlap:] * np.linspace(1, 0, overlap, dtype=np.float32) + chunk[
                :overlap
            ] * np.linspace(0, 1, overlap, dtype=np.float32)
        expected = np.concatenate([expected, chunk[overlap:]])
    np.testing.assert_array_equal(chunked_tts.concatenate_audio_chunks(chunks, 1000, fade), expected)
    for chunk, original in zip(chunks, saved, strict=True):
        np.testing.assert_array_equal(chunk, original)


@pytest.mark.asyncio
async def test_inventory_invalidation_expiry_and_waiter_cancellation(monkeypatch):
    from backend.utils import model_inventory as module

    inventory = module.ModelInventory(ttl=10)
    started, release = threading.Event(), threading.Event()
    count = 0
    clock = 100
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock))

    def scan():
        nonlocal count
        count += 1
        started.set()
        assert release.wait(3)
        return count

    first = asyncio.create_task(inventory.get("cache", scan))
    await asyncio.to_thread(started.wait, 1)
    second = asyncio.create_task(inventory.get("cache", scan))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == 1
    assert await inventory.get("cache", scan) == 1
    inventory.invalidate()
    assert await inventory.get("cache", scan) == 2
    clock += 11
    assert await inventory.get("cache", scan) == 3


@pytest.mark.asyncio
async def test_inventory_mutation_during_scan_discards_old_snapshot():
    from backend.utils.model_inventory import ModelInventory

    inventory = ModelInventory()
    started, release = threading.Event(), threading.Event()
    disk_value = "old"
    calls = 0

    def scan():
        nonlocal calls
        calls += 1
        snapshot = disk_value
        started.set()
        assert release.wait(3)
        return snapshot

    pending = asyncio.create_task(inventory.get("same-cache", scan))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        disk_value = "new"
        inventory.invalidate()
    finally:
        release.set()
    assert await pending == "new"
    assert await inventory.get("same-cache", scan) == "new"
    assert calls == 2


def test_waveform_cache_hit_is_not_blocked_by_another_source(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Performance")
    paths = []
    for index in range(2):
        path = tmp_path / f"source-{index}.wav"
        sf.write(path, np.full(64, index / 2, dtype=np.float32), 8000, subtype="FLOAT")
        paths.append(store.add_asset(project["id"], path))
    app = FastAPI()
    app.include_router(waveforms.make_waveform_router(store))
    started, release = threading.Event(), threading.Event()
    original = waveforms._envelope

    def slow_envelope(path, bins):
        started.set()
        assert release.wait(3)
        return original(path, bins)

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        warm = f"/assets/{paths[0]['id']}/waveform?bins=32"
        cold = f"/assets/{paths[1]['id']}/waveform?bins=32"
        expected = client.get(warm).json()
        monkeypatch.setattr(waveforms, "_envelope", slow_envelope)
        slow = pool.submit(client.get, cold)
        try:
            assert started.wait(1)
            hit = pool.submit(client.get, warm)
            assert hit.result(timeout=0.5).json() == expected
        finally:
            release.set()
        assert slow.result(timeout=3).status_code == 200


def test_waveform_same_source_is_deduplicated(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Shared")
    path = tmp_path / "source.wav"
    sf.write(path, np.ones(64, dtype=np.float32), 8000, subtype="FLOAT")
    asset = store.add_asset(project["id"], path)
    app = FastAPI()
    app.include_router(waveforms.make_waveform_router(store))
    started, release = threading.Event(), threading.Event()
    original = waveforms._envelope
    calls = []

    def envelope(path, bins):
        calls.append(path)
        started.set()
        assert release.wait(3)
        return original(path, bins)

    monkeypatch.setattr(waveforms, "_envelope", envelope)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        url = f"/assets/{asset['id']}/waveform?bins=32"
        first = pool.submit(client.get, url)
        try:
            assert started.wait(1)
            second = pool.submit(client.get, url)
        finally:
            release.set()
        assert first.result(timeout=3).json() == second.result(timeout=3).json()
        assert len(calls) == 1
    assert not waveforms._source_locks


def test_waveform_cold_sources_have_bounded_workers(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Bounded")
    assets = []
    for index in range(3):
        path = tmp_path / f"source-{index}.wav"
        sf.write(path, np.full(64, index / 3, dtype=np.float32), 8000, subtype="FLOAT")
        assets.append(store.add_asset(project["id"], path))
    route = waveforms.make_waveform_router(store).routes[0].endpoint
    two_working, all_acquired, release = threading.Event(), threading.Event(), threading.Event()
    guard = threading.Lock()
    original_envelope, original_lock = waveforms._envelope, waveforms._source_lock
    acquired = active = peak = calls = 0

    @contextmanager
    def acquired_lock(key):
        nonlocal acquired
        with original_lock(key):
            with guard:
                acquired += 1
                if acquired == 3:
                    all_acquired.set()
            yield

    def envelope(path, bins):
        nonlocal active, peak, calls
        with guard:
            calls += 1
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_working.set()
        try:
            assert release.wait(3)
            return original_envelope(path, bins)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(waveforms, "_source_lock", acquired_lock)
    monkeypatch.setattr(waveforms, "_envelope", envelope)
    with ThreadPoolExecutor(max_workers=3) as pool:
        requests = [pool.submit(route, asset["id"], 32) for asset in assets]
        try:
            assert all_acquired.wait(1)
            assert two_working.wait(1)
            assert calls == 2
        finally:
            release.set()
        for request in requests:
            assert request.result(timeout=3)["frames"] == 64
    assert peak == 2
    assert calls == 3
    assert not waveforms._source_locks
