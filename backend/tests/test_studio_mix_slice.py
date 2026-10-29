import numpy as np
import pytest
import soundfile as sf


def test_processed_and_dry_render_are_distinct_and_stems_preserve_levels(tmp_path):
    from backend.studio.rendering import render_project

    source = tmp_path / "source.wav"
    sf.write(source, np.full(32, 0.4), 48000, subtype="FLOAT")
    clip = dict(
        id="a",
        asset_id="a",
        track_id="a",
        start_sample=4,
        trim_start_sample=0,
        trim_end_sample=0,
        gain=1,
        fade_in_samples=0,
        fade_out_samples=0,
    )
    doc = dict(
        sample_rate=48000,
        channels=2,
        tracks=[dict(id="a", gain=1, effect_gain_db=-6), dict(id="b", gain=0.5, effect_gain_db=-6)],
        clips=[clip, dict(clip, id="b", track_id="b")],
        mix=dict(normalize=False, master_gain=0.5, limiter=False),
    )

    def render(kind, processing):
        return render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out", kind=kind, processing=processing)

    dry = render("mix", "dry")
    processed = render("mix", "processed")
    stems = render("stems", "processed")
    dry_pcm, _ = sf.read(dry["files"][0]["path"])
    wet_pcm, _ = sf.read(processed["files"][0]["path"])
    first, _ = sf.read(stems["files"][0]["path"])
    second, _ = sf.read(stems["files"][1]["path"])
    np.testing.assert_allclose(dry_pcm[4:], 0.6, atol=2e-6)
    np.testing.assert_allclose(wet_pcm[4:], 0.3 * 10 ** (-6 / 20), atol=2e-6)
    np.testing.assert_allclose(first[4:], 2 * second[4:], atol=2e-6)
    assert len(first) == len(second) == len(dry_pcm)


def test_mp3_preflight_checks_encoder_not_just_executable(monkeypatch):
    from backend.studio import rendering

    monkeypatch.setattr(rendering.shutil, "which", lambda _: "/fake/ffmpeg")

    def fail(*args, **kwargs):
        raise ValueError("Unknown encoder libmp3lame")

    monkeypatch.setattr(rendering, "_process", fail)
    with pytest.raises(ValueError, match="encoder"):
        rendering.preflight_mp3()


def test_mix_controls_persist_and_reject_invalid_values(tmp_path):
    from backend.studio.storage import StudioStore

    store = StudioStore(tmp_path)
    project = store.create_project("Mix")
    project = store.command(
        project["id"], "mix", project["revision"], "update_mix", {"master_gain": 0.5, "limiter": True}
    )
    track = project["document"]["tracks"][0]
    project = store.command(
        project["id"], "track", project["revision"], "update_track", {"track_id": track["id"], "effect_gain_db": -6}
    )
    assert store.get_project(project["id"])["document"]["mix"]["master_gain"] == 0.5
    assert store.get_project(project["id"])["document"]["tracks"][0]["effect_gain_db"] == -6
    with pytest.raises(ValueError, match="Invalid mix"):
        store.command(project["id"], "bad", project["revision"], "update_mix", {"master_gain": float("nan")})


def test_delay_effect_keeps_state_across_blocks_and_tail(tmp_path):
    from backend.studio.rendering import BLOCK_FRAMES, render_project

    samples = np.zeros(BLOCK_FRAMES + 10, dtype=np.float32)
    samples[BLOCK_FRAMES - 100] = 0.5
    source = tmp_path / "impulse.wav"
    sf.write(source, samples, 48000, subtype="FLOAT")
    track = dict(
        id="a",
        gain=1,
        effects_chain=[dict(type="delay", enabled=True, params=dict(delay_seconds=0.01, feedback=0, mix=1))],
    )
    clip = dict(
        id="c",
        asset_id="a",
        track_id="a",
        start_sample=0,
        trim_start_sample=0,
        trim_end_sample=0,
        gain=1,
        fade_in_samples=0,
        fade_out_samples=0,
    )
    doc = dict(sample_rate=48000, channels=2, tracks=[track], clips=[clip], mix=dict(normalize=False))
    out = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out", kind="stems", processing="processed")
    pcm, _ = sf.read(out["files"][0]["path"], always_2d=True)
    assert len(pcm) > len(samples) + 380
    assert np.max(np.abs(pcm[BLOCK_FRAMES + 360 : BLOCK_FRAMES + 400])) > 0.1


def test_processed_effect_stems_keep_relative_level_and_zero_alignment(tmp_path):
    from backend.studio.rendering import render_project

    source = tmp_path / "tone.wav"
    sf.write(source, np.full(1000, 0.2), 48000, subtype="FLOAT")
    effect = [dict(type="gain", enabled=True, params={"gain_db": -6})]
    tracks = [dict(id="a", gain=1, effects_chain=effect), dict(id="b", gain=0.25, effects_chain=effect)]
    clip = dict(
        id="c",
        asset_id="a",
        track_id="a",
        start_sample=200,
        trim_start_sample=0,
        trim_end_sample=0,
        gain=1,
        fade_in_samples=0,
        fade_out_samples=0,
    )
    doc = dict(
        sample_rate=48000,
        channels=2,
        tracks=tracks,
        clips=[clip, dict(clip, id="d", track_id="b")],
        mix=dict(normalize=True, master_gain=3, limiter=True, max_true_peak_dbtp=-6),
    )
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out", kind="stems", processing="processed")
    a, _ = sf.read(result["files"][0]["path"])
    b, _ = sf.read(result["files"][1]["path"])
    assert len(a) == len(b) == 1200
    np.testing.assert_allclose(a[:200], 0, atol=2e-6)
    np.testing.assert_allclose(a[200:], 0.2 * 10 ** (-6 / 20), atol=2e-6)
    np.testing.assert_allclose(b[200:], a[200:] / 4, atol=2e-6)


def test_limiter_respects_target_without_normalization(tmp_path):
    import shutil

    from backend.studio.rendering import render_project

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "hot.wav"
    sf.write(source, np.full(48000, 0.8), 48000, subtype="FLOAT")
    clip = dict(
        id="c",
        asset_id="a",
        track_id="a",
        start_sample=0,
        trim_start_sample=0,
        trim_end_sample=0,
        gain=2,
        fade_in_samples=0,
        fade_out_samples=0,
    )
    doc = dict(
        sample_rate=48000,
        channels=2,
        tracks=[dict(id="a", gain=1)],
        clips=[clip],
        mix=dict(normalize=False, limiter=True, max_true_peak_dbtp=-6),
    )
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")
    pcm, rate = sf.read(result["files"][0]["path"])
    assert rate == 48000
    assert len(pcm) == 48000
    assert np.max(np.abs(pcm)) <= 0.51


@pytest.mark.parametrize(
    "chain",
    [
        [dict(type="gain", params=dict(gain_db=float("nan")))],
        [dict(type="gain", enabled=1, params={})],
        [dict(type="missing", params={})],
        [dict(type="gain", params={"gain_db": 1})] * 17,
    ],
)
def test_effect_validation_applies_to_saved_document(tmp_path, chain):
    from backend.studio.storage import StudioStore

    store = StudioStore(tmp_path)
    project = store.create_project("Invalid")
    with pytest.raises(ValueError, match="(?i)(effect|chain)"):
        store.command(
            project["id"],
            "bad",
            project["revision"],
            "update_track",
            {"track_id": project["document"]["tracks"][0]["id"], "effects_chain": chain},
        )
    assert store.get_project(project["id"])["revision"] == 0


@pytest.mark.asyncio
async def test_delivery_records_phases_recipe_and_replay(tmp_path):
    from backend.studio.delivery import StudioDelivery
    from backend.studio.jobs import StudioJobs
    from backend.studio.storage import Conflict, StudioStore
    from backend.tests.test_studio_review_regressions import fixture_capabilities

    store = StudioStore(tmp_path)
    project = store.create_project("Mix", script="One")
    pid = project["document"]["passages"][0]["id"]
    queue = []

    async def synth(request):
        return np.full(1000, 0.1, dtype=np.float32), 48000

    jobs = StudioJobs(
        store, synth=synth, capability_resolver=fixture_capabilities, enqueue=lambda ident, coro: queue.append(coro)
    )
    jobs.submit(project["id"], "take", [pid], 1, True)
    await queue.pop()
    project = store.get_project(project["id"])
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": [pid]})
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Ready")
    revision = project["revisions"][0]["id"]
    pending = []
    delivery = StudioDelivery(store, schedule=lambda coro: pending.append(coro))
    job = delivery.submit(project["id"], "render", revision, "mix", "wav", processing="dry")
    assert delivery.submit(project["id"], "render", revision, "mix", "wav", processing="dry") == job
    with pytest.raises(Conflict):
        delivery.submit(project["id"], "render", revision, "mix", "wav", processing="processed")
    await pending.pop()
    result = jobs.get(job["id"])
    assert result["status"] == "completed"
    phases = [p["phase"] for p in result["attempts"][0]["phases"]]
    assert all(p in phases for p in ["mixing", "encoding", "metering", "publishing", "completed"])
    item = result["result"]["deliverables"][0]
    path, metadata = delivery.path(item["id"])
    import hashlib

    assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert metadata["recipe"]["processing"] == "dry"
    assert sf.info(path).frames == 1000


@pytest.mark.asyncio
async def test_stems_reject_mixed_frozen_and_unknown_voices(tmp_path):
    from backend.studio.delivery import StudioDelivery
    from backend.tests.test_studio_review_regressions import generate, setup

    store, project = setup(tmp_path, "One\n\nTwo")
    ids = [p["id"] for p in project["document"]["passages"]]
    for i, pid in enumerate(ids):
        project = store.command(
            project["id"],
            f"voice{i}",
            project["revision"],
            "assign_voice",
            {"passage_ids": [pid], "profile_id": f"voice{i}"},
        )
    project = await generate(store, project, ids)
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": ids})
    clips = project["document"]["clips"]
    project = store.command(
        project["id"],
        "move",
        project["revision"],
        "update_clip",
        {"clip_id": clips[1]["id"], "track_id": clips[0]["track_id"]},
    )
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Mixed")
    delivery = StudioDelivery(store, schedule=lambda coro: coro.close())
    with pytest.raises(ValueError, match="multiple frozen voice"):
        delivery.submit(project["id"], "stems", project["revisions"][0]["id"], "stems", "wav")


@pytest.mark.asyncio
async def test_unknown_voice_takes_are_not_assumed_identical(tmp_path):
    from backend.studio.delivery import StudioDelivery
    from backend.tests.test_studio_review_regressions import generate, setup

    store, project = setup(tmp_path, "One\n\nTwo")
    ids = [p["id"] for p in project["document"]["passages"]]
    project = await generate(store, project, ids)
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": ids})
    clips = project["document"]["clips"]
    project = store.command(
        project["id"],
        "combine-unknown",
        project["revision"],
        "update_clip",
        {"clip_id": clips[1]["id"], "track_id": clips[0]["track_id"]},
    )
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Unknown")
    delivery = StudioDelivery(store, schedule=lambda coro: coro.close())
    with pytest.raises(ValueError, match="multiple frozen voice"):
        delivery.submit(project["id"], "stems", project["revisions"][0]["id"], "stems", "wav")


@pytest.mark.asyncio
async def test_cancel_render_waits_for_worker_and_keeps_existing_output(tmp_path, monkeypatch):
    import asyncio
    import threading

    import backend.studio.rendering as rendering
    from backend.studio.delivery import StudioDelivery
    from backend.studio.jobs import StudioJobs
    from backend.tests.test_studio_review_regressions import generate, setup

    store, project = setup(tmp_path)
    project = await generate(store, project, [project["document"]["passages"][0]["id"]])
    project = store.command(
        project["id"],
        "insert",
        project["revision"],
        "insert_passages",
        {"passage_ids": [project["document"]["passages"][0]["id"]]},
    )
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Ready")
    pending = []
    delivery = StudioDelivery(store, schedule=lambda coro: pending.append(coro))
    old = delivery.directory / "previous"
    old.write_bytes(b"preserved")
    started = threading.Event()

    def running(*args, **kwargs):
        started.set()
        while not kwargs["cancelled"]():
            threading.Event().wait(0.01)
        raise ValueError("Render cancelled")

    monkeypatch.setattr(rendering, "render_project", running)
    job = delivery.submit(project["id"], "render", project["revisions"][0]["id"], "mix", "wav")
    task = asyncio.create_task(pending.pop())
    assert await asyncio.to_thread(started.wait, 5)
    jobs = StudioJobs(store)
    assert jobs.cancel(job["id"])["status"] == "cancelling"
    await asyncio.wait_for(task, 5)
    result = jobs.get(job["id"])
    assert result["status"] == "cancelled"
    assert result["attempts"][0]["phase"] == "cancelled"
    assert old.read_bytes() == b"preserved"
    assert list(delivery.directory.iterdir()) == [old]


@pytest.mark.asyncio
async def test_failed_render_retries_with_new_attempt_and_keeps_old_output(tmp_path, monkeypatch):
    import backend.services.task_queue as task_queue
    import backend.studio.rendering as rendering
    from backend.studio.delivery import StudioDelivery
    from backend.studio.jobs import StudioJobs
    from backend.tests.test_studio_review_regressions import generate, setup

    store, project = setup(tmp_path)
    pid = project["document"]["passages"][0]["id"]
    project = await generate(store, project, [pid])
    project = store.command(project["id"], "insert", project["revision"], "insert_passages", {"passage_ids": [pid]})
    project = store.checkpoint(project["id"], "checkpoint", project["revision"], "Ready")
    scheduled = []
    delivery = StudioDelivery(store, schedule=lambda coro: scheduled.append(coro))
    old = delivery.directory / "previous"
    old.write_bytes(b"previous output")
    job = delivery.submit(project["id"], "render", project["revisions"][0]["id"], "mix", "wav")
    original = rendering.render_project

    def fail(*args, **kwargs):
        raise ValueError("injected render failure")

    monkeypatch.setattr(rendering, "render_project", fail)
    await scheduled.pop()
    jobs = StudioJobs(store)
    assert jobs.get(job["id"])["status"] == "failed"
    assert old.read_bytes() == b"previous output"
    monkeypatch.setattr(rendering, "render_project", original)
    monkeypatch.setattr(task_queue, "create_background_task", lambda coro: scheduled.append(coro))
    jobs.retry(job["id"])
    await scheduled.pop()
    result = jobs.get(job["id"])
    assert result["status"] == "completed"
    assert [a["phase"] for a in result["attempts"]] == ["failed", "completed"]
    assert old.read_bytes() == b"previous output"
    path, _ = delivery.path(result["result"]["deliverables"][0]["id"])
    assert sf.info(path).frames > 0
