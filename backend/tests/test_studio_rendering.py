import numpy as np
import soundfile as sf
import pytest


def test_mix_preserves_placement_trim_overlap_and_gain(tmp_path):
    from backend.studio.rendering import render_project

    source = tmp_path / "source.wav"
    sf.write(source, np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]), 48000, subtype="FLOAT")
    track = {"id": "t", "gain": 0.5, "mute": False, "solo": False}
    clip = {
        "id": "c",
        "asset_id": "a",
        "track_id": "t",
        "start_sample": 2,
        "trim_start_sample": 1,
        "trim_end_sample": 2,
        "gain": 1,
        "fade_in_samples": 0,
        "fade_out_samples": 0,
    }
    doc = {
        "sample_rate": 48000,
        "channels": 2,
        "tracks": [track],
        "clips": [clip, dict(clip, id="d")],
        "mix": {"normalize": False},
    }
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")
    samples, rate = sf.read(result["files"][0]["path"], always_2d=True)
    assert rate == 48000
    np.testing.assert_allclose(samples, [[0, 0], [0, 0], [0.2, 0.2], [0.3, 0.3], [0.4, 0.4]], atol=2e-6)
    assert sf.info(result["files"][0]["path"]).subtype == "PCM_24"


def test_invalid_trim_does_not_publish(tmp_path):
    from backend.studio.rendering import render_project

    source = tmp_path / "source.wav"
    sf.write(source, np.ones(4) * 0.1, 48000)
    doc = {
        "sample_rate": 48000,
        "channels": 1,
        "tracks": [{"id": "t", "gain": 1}],
        "clips": [
            {
                "id": "c",
                "asset_id": "a",
                "track_id": "t",
                "start_sample": 0,
                "trim_start_sample": 4,
                "trim_end_sample": 0,
                "gain": 1,
                "fade_in_samples": 0,
                "fade_out_samples": 0,
            }
        ],
        "mix": {"normalize": False},
    }
    with pytest.raises(ValueError):
        render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")


def test_solo_fades_and_stereo_stems(tmp_path):
    from backend.studio.rendering import render_project

    source = tmp_path / "stereo.wav"
    sf.write(source, np.tile([0.4, 0.2], (4, 1)), 48000, subtype="FLOAT")
    tracks = [{"id": "one", "gain": 1, "solo": True}, {"id": "two", "gain": 1}]
    clip = {
        "id": "c",
        "asset_id": "a",
        "track_id": "one",
        "start_sample": 0,
        "trim_start_sample": 0,
        "trim_end_sample": 0,
        "gain": 1,
        "fade_in_samples": 2,
        "fade_out_samples": 2,
    }
    doc = {
        "sample_rate": 48000,
        "channels": 2,
        "tracks": tracks,
        "clips": [clip, dict(clip, id="d", track_id="two")],
        "mix": {"normalize": False},
    }
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out", kind="stems")
    assert len(result["files"]) == 1 and result["files"][0]["track_id"] == "one"
    samples, _ = sf.read(result["files"][0]["path"], always_2d=True)
    np.testing.assert_allclose(samples, [[0, 0], [0.2, 0.1], [0.4, 0.2], [0.2, 0.1]], atol=2e-6)


def test_resampling_uses_project_sample_rounding(tmp_path):
    from backend.studio.rendering import render_project

    source = tmp_path / "mono.wav"
    sf.write(source, np.ones(5) * 0.1, 32000, subtype="FLOAT")
    clip = {
        "id": "c",
        "asset_id": "a",
        "track_id": "t",
        "start_sample": 3,
        "trim_start_sample": 1,
        "trim_end_sample": 1,
        "gain": 1,
        "fade_in_samples": 0,
        "fade_out_samples": 0,
    }
    doc = {
        "sample_rate": 48000,
        "channels": 1,
        "tracks": [{"id": "t", "gain": 1}],
        "clips": [clip],
        "mix": {"normalize": False},
    }
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")
    assert sf.info(result["files"][0]["path"]).frames == 3 + round(5 * 48000 / 32000) - 2


def test_measurement_and_encoded_mp3(tmp_path):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    from backend.studio.rendering import render_project

    rate = 48000
    source = tmp_path / "tone.wav"
    sf.write(source, 0.1 * np.sin(2 * np.pi * 1000 * np.arange(rate * 2) / rate), rate, subtype="PCM_24")
    clip = {
        "id": "c",
        "asset_id": "a",
        "track_id": "t",
        "start_sample": 0,
        "trim_start_sample": 0,
        "trim_end_sample": 0,
        "gain": 1,
        "fade_in_samples": 0,
        "fade_out_samples": 0,
    }
    doc = {
        "sample_rate": rate,
        "channels": 1,
        "tracks": [{"id": "t", "gain": 1}],
        "clips": [clip],
        "mix": {"normalize": False},
    }
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out", format="mp3")
    measurement = next(iter(result["measurements"].values()))
    assert measurement["status"] == "measured"
    assert -26 < measurement["integrated_lufs"] < -20
    assert -23 < measurement["true_peak_dbtp"] < -18
    assert sf.info(result["files"][0]["path"]).frames > 0


def test_failed_publication_keeps_existing_file(tmp_path, monkeypatch):
    import backend.studio.rendering as rendering

    source = tmp_path / "tone.wav"
    sf.write(source, np.ones(8) * 0.1, 48000)
    out = tmp_path / "out"
    out.mkdir()
    old = out / "old.wav"
    old.write_bytes(b"old")
    doc = {
        "sample_rate": 48000,
        "channels": 1,
        "tracks": [{"id": "t", "gain": 1}],
        "clips": [
            {
                "id": "c",
                "asset_id": "a",
                "track_id": "t",
                "start_sample": 0,
                "trim_start_sample": 0,
                "trim_end_sample": 0,
                "gain": 1,
                "fade_in_samples": 0,
                "fade_out_samples": 0,
            }
        ],
        "mix": {"normalize": False},
    }

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(rendering.os, "replace", fail)
    with pytest.raises(OSError):
        rendering.render_project(doc, {"a": {}}, lambda _: source, out)
    assert old.read_bytes() == b"old" and list(out.iterdir()) == [old]
