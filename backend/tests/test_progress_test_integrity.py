"""The progress integration check must fail if its subscriber sees no events."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from backend.tests import test_progress as checks


@pytest.mark.asyncio
async def test_progress_integration_rejects_an_empty_event_stream(monkeypatch):
    class Manager:
        async def subscribe(self, _name):
            if False:
                yield ""

        def update_progress(self, **_kwargs):
            pass

        def mark_complete(self, _name):
            pass

    class Bar:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def update(self, _amount):
            pass

    async def no_delay(_seconds):
        pass

    manager = Manager()
    monkeypatch.setattr(checks, "get_progress_manager", lambda: manager)
    monkeypatch.setattr(checks, "create_hf_progress_callback", lambda *_args: lambda *_values: None)
    monkeypatch.setattr(checks, "HFProgressTracker", lambda _callback: SimpleNamespace(patch_download=nullcontext))
    monkeypatch.setattr(checks, "asyncio", SimpleNamespace(gather=asyncio.gather, sleep=no_delay))
    monkeypatch.setitem(__import__("sys").modules, "tqdm", SimpleNamespace(tqdm=Bar))
    with pytest.raises(AssertionError, match="No progress events"):
        await checks.test_full_integration()
