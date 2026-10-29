"""
Unit tests for the ``force_offline_if_cached`` helper.

Verifies that the helper mutates the cached module constants in
``huggingface_hub.constants`` and ``transformers.utils.hub`` — not just
``os.environ`` — and that concurrent users are refcount-coordinated so
one thread's exit can't strip another thread's offline protection.

NOTE: These tests mutate process-global state in ``huggingface_hub.constants``
and ``transformers.utils.hub``. They are not safe under cross-process
parallelism (e.g. ``pytest-xdist`` with ``--dist=loadfile``/``loadscope``);
run this file serially.
"""

import multiprocessing
import os
import threading
import time

import pytest

from backend.utils.hf_offline_patch import force_offline_if_cached


def _nest_opposite_modes_in_subprocess(queue, ready):
    """Module-level so it's picklable for multiprocessing's spawn start method.

    Runs in a fresh child process rather than a thread of the test process,
    so a real deadlock here (a regression of the guard this test exists for)
    can't leave the shared _offline_cv state corrupted for every other test
    in this run: the child either exits cleanly (guard raised) or gets
    terminated by the parent after the timeout, and either way the test
    process's own state was never touched.
    """
    ready.set()
    try:
        with force_offline_if_cached(False, "outer-uncached"), force_offline_if_cached(True, "inner-cached"):
            pass
    except Exception as exc:
        queue.put(("exc", type(exc).__name__, str(exc)))
    else:
        queue.put(("ok", None, None))


def _hf_const():
    import huggingface_hub.constants as hf_const

    return hf_const


def _tf_hub():
    import transformers.utils.hub as tf_hub

    return tf_hub


def test_mutates_cached_huggingface_hub_constant():
    original = _hf_const().HF_HUB_OFFLINE
    with force_offline_if_cached(True, "t"):
        assert _hf_const().HF_HUB_OFFLINE is True
    assert original == _hf_const().HF_HUB_OFFLINE


def test_mutates_cached_transformers_constant():
    original = _tf_hub()._is_offline_mode
    with force_offline_if_cached(True, "t"):
        assert _tf_hub()._is_offline_mode is True
    assert original == _tf_hub()._is_offline_mode


def test_sets_env_variable():
    original = os.environ.get("HF_HUB_OFFLINE")
    with force_offline_if_cached(True, "t"):
        assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert original == os.environ.get("HF_HUB_OFFLINE")


def test_noop_when_not_cached():
    before = _hf_const().HF_HUB_OFFLINE
    with force_offline_if_cached(False, "t"):
        assert before == _hf_const().HF_HUB_OFFLINE


def test_nested_contexts_respect_refcount():
    original = _hf_const().HF_HUB_OFFLINE
    with force_offline_if_cached(True, "outer"):
        assert _hf_const().HF_HUB_OFFLINE is True
        with force_offline_if_cached(True, "inner"):
            assert _hf_const().HF_HUB_OFFLINE is True
        # inner exit must not restore while outer is still active
        assert _hf_const().HF_HUB_OFFLINE is True
    assert original == _hf_const().HF_HUB_OFFLINE


def test_concurrent_threads_share_offline_window():
    """A slow thread must keep seeing offline mode even if a peer exits first."""
    original = _hf_const().HF_HUB_OFFLINE
    observations: list[bool] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)
    fast_exited = threading.Event()

    def slow():
        try:
            with force_offline_if_cached(True, "slow"):
                barrier.wait(timeout=5)
                assert fast_exited.wait(timeout=5), "fast thread did not exit"
                observations.append(_hf_const().HF_HUB_OFFLINE)
        except Exception as exc:
            errors.append(exc)

    def fast():
        try:
            with force_offline_if_cached(True, "fast"):
                barrier.wait(timeout=5)
        except Exception as exc:
            errors.append(exc)
        finally:
            fast_exited.set()

    t_slow = threading.Thread(target=slow)
    t_fast = threading.Thread(target=fast)
    t_slow.start()
    t_fast.start()
    t_slow.join(timeout=5)
    t_fast.join(timeout=5)

    assert not t_slow.is_alive(), "slow thread did not finish"
    assert not t_fast.is_alive(), "fast thread did not finish"
    assert not errors, errors
    assert observations == [True], "slow thread lost offline protection"
    assert original == _hf_const().HF_HUB_OFFLINE


@pytest.mark.parametrize("baseline", [False, True])
def test_uncached_load_never_observes_offline_flag_from_concurrent_cached_load(monkeypatch, baseline):
    """An uncached (network-needing) load must never inherit the forced
    offline mode of a concurrent, unrelated cached load — even when both
    start at nearly the same time.
    """
    monkeypatch.setattr(_hf_const(), "HF_HUB_OFFLINE", baseline)
    original = _hf_const().HF_HUB_OFFLINE
    observations: list[bool] = []
    errors: list[Exception] = []
    cached_entered = threading.Event()

    def cached_load():
        try:
            with force_offline_if_cached(True, "cached"):
                cached_entered.set()
                time.sleep(0.2)
        except Exception as exc:
            errors.append(exc)

    def uncached_load():
        try:
            assert cached_entered.wait(timeout=5), "cached thread never entered"
            with force_offline_if_cached(False, "uncached"):
                observations.append(_hf_const().HF_HUB_OFFLINE)
        except Exception as exc:
            errors.append(exc)

    t_cached = threading.Thread(target=cached_load)
    t_uncached = threading.Thread(target=uncached_load)
    t_cached.start()
    t_uncached.start()
    t_cached.join(timeout=5)
    t_uncached.join(timeout=5)

    assert not t_cached.is_alive(), "cached thread did not finish"
    assert not t_uncached.is_alive(), "uncached thread did not finish"
    assert not errors, errors
    assert observations == [baseline], "uncached load observed offline mode forced by a concurrent cached load"
    assert original == _hf_const().HF_HUB_OFFLINE


def test_nesting_opposite_mode_on_same_thread_raises_instead_of_deadlocking():
    """Nesting is_cached=True inside is_cached=False (or vice versa) on the
    same thread must raise immediately, not hang: the inner call's wait
    condition can only be cleared by the outer call's own exit, which can
    never run because it's blocked inside the inner call waiting for it.

    Runs in a spawned child process with a bounded join, terminated if it's
    still alive after the timeout, so a regression fails this test instead of
    hanging the suite or leaving _offline_cv's shared state corrupted for
    every other test in this run.
    """
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    ready = ctx.Event()
    proc = ctx.Process(target=_nest_opposite_modes_in_subprocess, args=(queue, ready))
    proc.start()
    # Spawn imports Torch/Transformers before entering the target. Bound startup
    # separately so concurrent build load cannot masquerade as a guard deadlock.
    started = ready.wait(timeout=30)
    if started:
        proc.join(timeout=5)

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)
        if not started:
            pytest.fail("spawned test process did not finish importing within 30 seconds")
        pytest.fail(
            "nesting the opposite mode on the same thread hung instead of raising, "
            "this is the deadlock the per-thread mode-stack guard exists to prevent"
        )

    kind, exc_type, exc_msg = queue.get(timeout=2)
    assert kind == "exc", (kind, exc_type, exc_msg)
    assert exc_type == "RuntimeError", (kind, exc_type, exc_msg)


def test_cached_load_waits_until_active_uncached_load_finishes(monkeypatch):
    """A cached load must not force offline mode while a network load is active."""
    monkeypatch.setattr(_hf_const(), "HF_HUB_OFFLINE", False)
    uncached_entered = threading.Event()
    release_uncached = threading.Event()
    cached_entered = threading.Event()
    cached_attempting = threading.Event()
    errors = []

    def uncached():
        try:
            with force_offline_if_cached(False, "network"):
                uncached_entered.set()
                assert release_uncached.wait(timeout=5)
                assert not _hf_const().HF_HUB_OFFLINE
        except Exception as exc:
            errors.append(exc)

    def cached():
        try:
            assert uncached_entered.wait(timeout=5)
            cached_attempting.set()
            with force_offline_if_cached(True, "cached"):
                assert _hf_const().HF_HUB_OFFLINE
                cached_entered.set()
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=uncached)
    second = threading.Thread(target=cached)
    first.start()
    second.start()
    try:
        assert uncached_entered.wait(timeout=5)
        assert cached_attempting.wait(timeout=5)
        assert not cached_entered.wait(timeout=0.2)
    finally:
        release_uncached.set()
        first.join(timeout=5)
        second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert cached_entered.is_set()
    assert not _hf_const().HF_HUB_OFFLINE


def test_uncached_loads_can_overlap_without_forcing_offline(monkeypatch):
    monkeypatch.setattr(_hf_const(), "HF_HUB_OFFLINE", False)
    barrier = threading.Barrier(2)
    observations = []
    errors = []

    def load():
        try:
            with force_offline_if_cached(False, "network"):
                barrier.wait(timeout=5)
                observations.append(_hf_const().HF_HUB_OFFLINE)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=load) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=6)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert observations == [False, False]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
