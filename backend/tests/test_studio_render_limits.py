import numpy as np
import pytest
import soundfile as sf

from backend.studio.rendering import render_project


def fixture(tmp_path, frames=96000):
    source = tmp_path / "source.wav"
    sf.write(source, np.sin(np.arange(frames) * 0.1) * 0.1, 48000, subtype="FLOAT")
    clip = {
        "id": "clip",
        "track_id": "one",
        "asset_id": "a",
        "start_sample": 0,
        "trim_start_sample": 0,
        "trim_end_sample": 0,
        "gain": 1,
        "fade_in_samples": 0,
        "fade_out_samples": 0,
    }
    doc = {
        "sample_rate": 48000,
        "channels": 1,
        "tracks": [{"id": "one", "gain": 1}],
        "clips": [clip],
        "mix": {"normalize": False},
    }
    return source, doc


def test_split_sources_decode_only_the_ranges_in_use(tmp_path, monkeypatch):
    source, doc = fixture(tmp_path)
    frames = 96000
    original = doc["clips"][0]
    doc["clips"] = [
        dict(
            original,
            id=str(i),
            start_sample=i * 4800,
            trim_start_sample=i * 4800,
            trim_end_sample=frames - (i + 1) * 4800,
        )
        for i in range(20)
    ]
    decoded = []
    read = sf.SoundFile.read

    def counted(self, *args, **kwargs):
        data = read(self, *args, **kwargs)
        decoded.append(len(data))
        return data

    monkeypatch.setattr(sf.SoundFile, "read", counted)
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")
    assert result["files"]
    assert sum(decoded) <= frames * 2, "Split clips repeatedly decoded their entire source"


def test_dry_stems_preserve_relative_gain_when_master_normalization_is_enabled(tmp_path):
    source, doc = fixture(tmp_path)
    doc["tracks"].append({"id": "two", "gain": 0.25})
    doc["clips"].append(dict(doc["clips"][0], id="second", track_id="two"))
    doc["mix"] = {"normalize": True, "target_lufs": -16, "max_true_peak_dbtp": -1}
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out", kind="stems")
    audio = {f["track_id"]: sf.read(f["path"])[0] for f in result["files"]}
    np.testing.assert_allclose(audio["two"], audio["one"] * 0.25, atol=2e-6)


def test_cancel_between_blocks_does_not_publish(tmp_path):
    source, doc = fixture(tmp_path, frames=48000 * 4)
    progress = []
    with pytest.raises(ValueError, match="cancel"):
        render_project(
            doc,
            {"a": {}},
            lambda _: source,
            tmp_path / "out",
            cancelled=lambda: bool(progress),
            progress=lambda done, total: progress.append(done),
        )
    assert not list((tmp_path / "out").iterdir())


def test_block_resampling_matches_full_source_at_every_boundary(tmp_path):
    from scipy.signal import resample_poly

    source, doc = fixture(tmp_path)
    audio = np.sin(np.arange(44100 * 3) * 0.1) * 0.1
    sf.write(source, audio, 44100, subtype="FLOAT")
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")
    actual = sf.read(result["files"][0]["path"])[0]
    stored = sf.read(source)[0]
    expected = resample_poly(stored, 160, 147)[: round(len(stored) * 48000 / 44100)]
    np.testing.assert_allclose(actual, expected, atol=2e-6)


def test_extreme_placement_is_rejected_before_allocation(tmp_path):
    source, doc = fixture(tmp_path)
    doc["clips"][0]["start_sample"] = 48000 * 7200
    with pytest.raises(ValueError, match="limit"):
        render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")


def test_resampling_half_sample_rounding_matches_frontend(tmp_path):
    source, doc = fixture(tmp_path, frames=3)
    sf.write(source, np.full(3, 0.1), 32000, subtype="FLOAT")
    result = render_project(doc, {"a": {}}, lambda _: source, tmp_path / "out")
    assert sf.info(result["files"][0]["path"]).frames == 5
