"""Deterministic waveform regressions; these do not qualify model quality."""

import numpy as np
import pytest

from backend.utils.audio import normalize_audio
from backend.utils.audio_processing import process_generated_audio
from backend.utils.effects import apply_effects


def test_normalization_preserves_peak_ratios_in_sparse_speech():
    audio = np.zeros(24000, dtype=np.float32)
    audio[:3] = [1.0, 0.5, -0.25]
    original = audio.copy()

    normalized = normalize_audio(audio)

    np.testing.assert_allclose(normalized[:3], [0.85, 0.425, -0.2125], atol=1e-6)
    np.testing.assert_array_equal(audio, original)
    assert normalized.dtype == np.float32


def test_normalization_still_reaches_rms_target_when_headroom_allows():
    audio = (0.2 * np.sin(np.arange(24000) * 2 * np.pi * 440 / 24000)).astype(np.float32)
    normalized = normalize_audio(audio)
    assert np.sqrt(np.mean(normalized**2)) == pytest.approx(0.1, rel=1e-5)


@pytest.mark.parametrize("audio", [np.zeros(0, dtype=np.float32), np.zeros(240, dtype=np.float32)])
def test_normalization_handles_empty_and_silent_audio(audio):
    normalized = normalize_audio(audio)
    np.testing.assert_array_equal(normalized, audio)


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_normalization_rejects_non_finite_samples(value):
    with pytest.raises(ValueError, match="finite"):
        normalize_audio(np.array([0.1, value], dtype=np.float32))


def test_delay_preserves_echo_after_source_ends():
    sample_rate = 24000
    source = np.zeros(2400, dtype=np.float32)
    source[-1] = 0.4
    effects = [{"type": "delay", "params": {"delay_seconds": 0.2, "feedback": 0.0, "mix": 1.0}}]

    processed = apply_effects(source, sample_rate, effects)

    assert len(processed) > len(source) + 0.19 * sample_rate
    assert np.max(np.abs(processed[len(source) :])) > 0.1
    assert len(processed) <= len(source) + 10 * sample_rate


def test_reverb_preserves_decay_after_source_ends():
    source = np.zeros(2400, dtype=np.float32)
    source[-1] = 0.4
    processed = apply_effects(source, 24000, [{"type": "reverb"}])
    assert len(processed) > len(source)
    assert np.max(np.abs(processed[len(source) :])) > 1e-5


def test_gain_does_not_add_a_silent_effect_tail():
    source = np.full(2400, 0.2, dtype=np.float32)
    processed = apply_effects(source, 24000, [{"type": "gain", "params": {"gain_db": -6}}])
    assert len(processed) == len(source)
    np.testing.assert_allclose(processed, source * 10 ** (-6 / 20), rtol=1e-5)


@pytest.mark.parametrize("normalize", [False, True])
def test_postprocessing_guards_hot_effects_without_touching_clean_version(normalize):
    source = np.zeros(2400, dtype=np.float32)
    source[:3] = [0.2, 0.1, -0.05]
    effects = [{"type": "gain", "params": {"gain_db": 40}}]

    clean, processed = process_generated_audio(source, 24000, normalize=normalize, effects_chain=effects)

    assert np.max(np.abs(processed)) <= 0.99901
    np.testing.assert_allclose(processed[:3], [0.999, 0.4995, -0.24975], atol=1e-6)
    expected_clean = normalize_audio(source) if normalize else source
    np.testing.assert_allclose(clean, expected_clean)
    np.testing.assert_allclose(source[:3], [0.2, 0.1, -0.05])
    assert not np.shares_memory(clean, processed)


def test_disabled_normalization_does_not_boost_quiet_audio():
    source = np.full(2400, 0.001, dtype=np.float32)
    clean, processed = process_generated_audio(source, 24000, normalize=False)
    np.testing.assert_array_equal(clean, source)
    np.testing.assert_array_equal(processed, source)


def test_disabled_normalization_still_protects_pcm_encoding():
    source = np.array([2.0, 1.0, -0.5], dtype=np.float32)
    clean, processed = process_generated_audio(source, 24000, normalize=False)
    np.testing.assert_allclose(clean, [0.999, 0.4995, -0.24975], atol=1e-6)
    np.testing.assert_array_equal(clean, processed)


def test_effect_tail_render_has_a_fixed_budget_at_high_feedback():
    source = np.zeros(2400, dtype=np.float32)
    source[-1] = 0.2
    effects = [{"type": "delay", "params": {"delay_seconds": 2.0, "feedback": 0.95, "mix": 1.0}}]
    processed = apply_effects(source, 24000, effects)
    assert len(processed) <= len(source) + 10 * 24000
    assert np.isfinite(processed).all()


def test_atomic_audio_save_does_not_publish_a_partial_prepared_reference(tmp_path, monkeypatch):
    from pathlib import Path
    from backend.utils import audio as audio_utils

    final = tmp_path / "combined-reference.wav"
    final.write_bytes(b"existing-complete-reference")

    def fail_write(path, *args, **kwargs):
        Path(path).write_bytes(b"partial")
        raise OSError("simulated full disk")

    monkeypatch.setattr(audio_utils.sf, "write", fail_write)
    with pytest.raises(OSError, match="simulated full disk"):
        audio_utils.save_audio(np.zeros(240), str(final), 24000)
    assert final.read_bytes() == b"existing-complete-reference"
    assert not Path(f"{final}.tmp").exists()
