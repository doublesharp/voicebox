"""Studio and the legacy scheduler must share one physical inference owner."""

import asyncio

import pytest

from backend.services.inference_scheduler import run_serialized_inference
from backend.utils.inference import run_inference


@pytest.mark.asyncio
async def test_studio_and_legacy_scheduler_cannot_overlap():
    entered = asyncio.Event()
    release = asyncio.Event()
    legacy_started = []

    async def studio():
        entered.set()
        await release.wait()

    async def legacy():
        legacy_started.append(True)

    first = asyncio.create_task(run_inference(studio))
    await entered.wait()
    second = asyncio.create_task(run_serialized_inference(legacy))
    try:
        await asyncio.sleep(0.01)
        overlap = bool(legacy_started)
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert not overlap, "Legacy model state overlapped the Studio physical owner"
