"""Reference segment suggestions preserve sources and avoid clipped/quiet spans."""

import numpy as np

from backend.services.sample_quality import analyze_reference


def test_ranked_reference_intervals_prefer_clean_signal_and_preserve_source():
    rate = 8000
    tone = 0.15 * np.sin(2 * np.pi * 220 * np.arange(rate * 8) / rate)
    audio = np.concatenate((np.zeros(rate), np.ones(rate), tone, np.zeros(rate)))
    before = audio.copy()
    report = analyze_reference(audio, rate)
    segments = report["suggested_segments"]
    assert segments
    assert segments[0]["start_seconds"] >= 2
    assert segments[0]["end_seconds"] <= 10.2
    assert segments[0]["clipped_fraction"] == 0
    assert segments[0]["duration_seconds"] <= 15
    np.testing.assert_array_equal(audio, before)
    assert len(report["decoded_audio_sha256"]) == 64


def test_silent_recording_has_no_suggested_speaker_segment():
    report = analyze_reference(np.zeros(8000 * 4), 8000)
    assert report["suggested_segments"] == []
