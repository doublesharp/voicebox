"""Deterministic metrics for the opt-in offline quality tier."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from backend.utils.audio_quality import analyze_audio_file, analyze_waveform, transcript_metrics


def test_known_waveform_reports_peak_rms_clipping_and_silence():
    audio = np.r_[np.zeros(20), np.ones(20), np.zeros(20)].astype(np.float32)
    metrics = analyze_waveform(audio, 1000)
    assert metrics["duration_seconds"] == pytest.approx(0.06)
    assert metrics["channels"] == 1
    assert metrics["peak"] == 1.0
    assert metrics["rms"] == pytest.approx(np.sqrt(1 / 3))
    assert metrics["near_full_scale_samples"] == 20
    assert metrics["silence_fraction"] == pytest.approx(2 / 3)
    assert metrics["leading_silence_seconds"] == pytest.approx(0.02)
    assert metrics["trailing_silence_seconds"] == pytest.approx(0.02)


def test_stereo_silence_detection_does_not_cancel_opposite_channels():
    metrics = analyze_waveform(np.column_stack([np.full(200, 0.1), np.full(200, -0.1)]), 1000)
    assert metrics["channels"] == 2
    assert metrics["silence_fraction"] == 0
    assert metrics["rms"] == pytest.approx(0.1)


def test_invalid_samples_are_reported_without_fabricating_quality_metrics():
    metrics = analyze_waveform(np.array([0.0, np.nan, np.inf]), 1000)
    assert metrics["nonfinite_samples"] == 2
    assert metrics["valid_samples"] is False
    assert metrics["rms"] is None
    assert metrics["silence_fraction"] is None


def test_empty_audio_is_distinguished_from_silence():
    metrics = analyze_waveform(np.zeros(0), 24000)
    assert metrics["duration_seconds"] == 0
    assert metrics["sample_frames"] == 0
    assert metrics["rms"] == 0
    assert metrics["silence_fraction"] is None


def test_transcript_metrics_separate_omissions_insertions_and_repetitions():
    metrics = transcript_metrics("One two three four five", "one one two four five extra")
    assert metrics["deletions"] == 1
    assert metrics["insertions"] == 2
    assert metrics["substitutions"] == 0
    assert metrics["error_rate"] == pytest.approx(0.6)
    assert metrics["extra_repeated_tokens"] == 1


def test_transcript_comparison_can_use_characters_for_cjk():
    metrics = transcript_metrics("你好世界", "你好界", unit="character")
    assert metrics["reference_tokens"] == 4
    assert metrics["deletions"] == 1
    assert metrics["error_rate"] == pytest.approx(0.25)


def test_blockwise_file_metrics_match_waveform_metrics(tmp_path):
    source = np.zeros((24000 * 11 + 180, 2), dtype=np.float32)
    source[2400:4800] = [0.2, -0.1]
    source[-1000:] = [0.1, 0.2]
    path = tmp_path / "long.wav"
    sf.write(path, source, 24000, subtype="FLOAT")
    file_metrics = analyze_audio_file(path)
    array_metrics = analyze_waveform(source, 24000)
    for field in ("sample_frames", "peak", "rms", "silence_fraction", "longest_silence_seconds"):
        assert file_metrics[field] == pytest.approx(array_metrics[field])


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate-audio-quality.py"


def run_cli(*args, **kwargs):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *map(str, args)], text=True, capture_output=True, **kwargs
    )


def test_cli_lists_corpus_without_models_or_output():
    result = run_cli("--list-corpus")
    assert result.returncode == 0, result.stderr
    corpus = json.loads(result.stdout)
    assert {case["language"] for case in corpus} >= {"en", "es", "zh", "ja"}
    assert {case["id"] for case in corpus} >= {"names-en", "numbers-en", "pauses-en", "narration-en"}


def manifest(tmp_path):
    source = np.r_[np.zeros(20), np.ones(20), np.zeros(20)].astype(np.float32)
    sf.write(tmp_path / "existing.wav", source, 1000, subtype="FLOAT")
    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "case_id": "custom",
                        "text": "One two three four five",
                        "audio_path": "existing.wav",
                        "engine": "test-waveform",
                        "model": "synthetic",
                        "seed": 42,
                        "config": {"normalize": False},
                        "generation_seconds": 0.12,
                        "asr_transcript": "one one two four five extra",
                    }
                ],
            }
        )
    )
    return path


@pytest.fixture
def report_output():
    if not Path("/Volumes/Scratch").is_mount():
        pytest.skip("The quality CLI requires the Scratch volume")
    reports = Path("/Volumes/Scratch/voicebox/reports")
    reports.mkdir(parents=True, exist_ok=True)
    # Exercise the real subprocess path policy independently of pytest's
    # configurable temporary fixture directory. This directory is task-owned.
    with tempfile.TemporaryDirectory(prefix="quality-cli-test-", dir=reports) as directory:
        yield Path(directory) / "report"


def test_cli_analyzes_existing_wav_and_keeps_proof_domains_explicit(tmp_path, report_output):
    input_path = manifest(tmp_path)
    output = report_output
    absent_models = tmp_path / "no-models"
    env = {**os.environ, "VOICEBOX_MODELS_DIR": str(absent_models), "HF_HUB_OFFLINE": "1"}
    result = run_cli("--manifest", input_path, "--output-dir", output, env=env)
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "report.json").read_text())
    assert not absent_models.exists()
    assert not any(report["proof"].values())
    case = report["cases"][0]
    assert case["waveform"]["near_full_scale_samples"] == 20
    assert case["transcript"]["deletions"] == 1
    assert case["real_time_factor"] == pytest.approx(2)
    assert case["generation_timing_source"] == "manifest"
    assert case["seed"] == 42
    assert (output / "listening-worksheet.md").exists()
    # Repeated runs cannot overwrite evidence silently.
    retry = run_cli("--manifest", input_path, "--output-dir", output, env=env)
    assert retry.returncode != 0
    assert "already exist" in retry.stderr


def test_cli_refuses_non_report_output_and_requires_explicit_opt_in(tmp_path):
    input_path = manifest(tmp_path)
    forbidden = Path.cwd() / "quality-report-should-not-exist"
    result = run_cli("--manifest", input_path, "--output-dir", forbidden)
    assert result.returncode != 0
    assert "explicit task report directory" in result.stderr
    assert not forbidden.exists()
    assert run_cli().returncode != 0
