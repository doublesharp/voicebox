#!/usr/bin/env python3
"""Opt-in analysis of existing WAVs. Never synthesizes, transcribes or downloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.utils.audio_quality import (  # noqa: E402
    QUALITY_CORPUS,
    analyze_audio_file,
    transcript_metrics,
)

def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def analyze_manifest(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Manifest must be an object with version: 1")
    entries = manifest.get("cases")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError("Manifest must contain 1 to 100 cases")
    corpus = {case["id"]: case for case in QUALITY_CORPUS}
    results = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Case {index} must be an object")
        for field in ("case_id", "audio_path", "engine", "model", "seed", "config"):
            if field not in entry:
                raise ValueError(f"Case {index} is missing provenance field '{field}'")
        if not all(
            isinstance(entry[field], str) and entry[field]
            for field in ("case_id", "audio_path", "engine", "model")
        ):
            raise ValueError(
                f"Case {index} requires nonempty case_id, audio_path, engine and model"
            )
        if not isinstance(entry["config"], dict) or (
            entry["seed"] is not None and type(entry["seed"]) is not int
        ):
            raise ValueError(
                f"Case {index} requires a config object and an integer or null seed"
            )
        case = corpus.get(entry["case_id"], {})
        text = case.get("text", entry.get("text"))
        if not isinstance(text, str) or not text:
            raise ValueError(f"Case {index} needs text or a known corpus case_id")
        audio_path = (manifest_path.parent / entry["audio_path"]).resolve()
        started = time.perf_counter()
        metrics = analyze_audio_file(audio_path)
        analysis_seconds = time.perf_counter() - started
        generation_seconds = entry.get("generation_seconds")
        if generation_seconds is not None and (
            type(generation_seconds) not in (int, float)
            or not math.isfinite(generation_seconds)
            or generation_seconds < 0
        ):
            raise ValueError(
                f"Case {index} generation_seconds must be finite and nonnegative"
            )
        supplied_transcript = entry.get("asr_transcript")
        reference = entry.get("transcript_reference", text)
        if not isinstance(reference, str):
            raise ValueError(f"Case {index} transcript_reference must be text")
        transcript = None
        if "asr_transcript" in entry:
            if not isinstance(supplied_transcript, str):
                raise ValueError(
                    f"Case {index} asr_transcript must be text, including an empty transcript"
                )
            transcript = transcript_metrics(
                reference,
                supplied_transcript,
                unit=entry.get("transcript_unit", case.get("transcript_unit", "word")),
            )
        results.append(
            {
                "sample": f"sample-{index:03d}",
                "case_id": entry["case_id"],
                "text": text,
                "language": entry.get("language", case.get("language")),
                "audio_path": str(audio_path),
                "audio_sha256": file_hash(audio_path),
                "engine": entry["engine"],
                "model": entry["model"],
                "seed": entry["seed"],
                "config": entry["config"],
                "generation_seconds": generation_seconds,
                "generation_timing_source": "manifest"
                if generation_seconds is not None
                else "not_measured",
                "real_time_factor": generation_seconds / metrics["duration_seconds"]
                if generation_seconds is not None and metrics["duration_seconds"]
                else None,
                "analysis_seconds": analysis_seconds,
                "waveform": metrics,
                "transcript_reference": reference,
                "supplied_asr_transcript": supplied_transcript,
                "transcript": transcript,
            }
        )
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "manifest_sha256": file_hash(manifest_path),
        "proof": {
            "synthesis_performed": False,
            "asr_performed": False,
            "speaker_similarity_measured": False,
            "listening_review_performed": False,
            "model_downloads_performed": False,
        },
        "limits": [
            "Energy silence is not speech recognition; full-scale samples are clipping candidates.",
            "Transcript scores use supplied text only and do not measure pronunciation or prosody.",
            "Generation timings and engine/configuration provenance are supplied by the manifest.",
        ],
        "cases": results,
    }


def write_report(report: dict, output_dir: Path, reports_root: Path) -> None:
    reports_root = reports_root.resolve()
    output_dir = output_dir.resolve()
    if (
        not output_dir.is_relative_to(reports_root)
        or output_dir == reports_root
    ):
        raise ValueError(
            f"Choose an explicit task report directory below {reports_root}"
        )
    targets = [output_dir / "report.json", output_dir / "listening-worksheet.md"]
    if any(target.exists() for target in targets):
        raise ValueError("Report files already exist; choose a new output directory")
    # Serialize before creating output so invalid supplied metadata cannot
    # leave a half-written JSON report (NaN and Infinity are not valid JSON).
    serialized = (
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    lines = [
        "# Listening worksheet",
        "",
        "No listening scores have been assigned. Compare anonymous sample labels before checking engine metadata.",
        "Use 1-5 ratings for intelligibility, naturalness and voice similarity; record omissions, repeats and clicks.",
        "",
        "| Sample | Reference text | Intelligibility | Naturalness | Voice similarity | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in report["cases"]:
        text = case["text"].replace("\n", " ").replace("|", "\\|").replace("<", "&lt;")
        lines.append(f"| [{case['sample']}](<{case['audio_path']}>) | {text} | | | | |")
    output_dir.mkdir(parents=True, exist_ok=True)
    with targets[0].open("x", encoding="utf-8") as target:
        target.write(serialized)
    with targets[1].open("x", encoding="utf-8") as target:
        target.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-corpus",
        action="store_true",
        help="Print the maintained text corpus; write no files",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Version 1 JSON manifest pointing to existing WAV files",
    )
    parser.add_argument(
        "--reports-root", type=Path, help="Disposable report root outside the source checkout"
    )
    parser.add_argument(
        "--output-dir", type=Path, help="New task directory below --reports-root"
    )
    args = parser.parse_args(argv)
    if args.list_corpus:
        print(json.dumps(QUALITY_CORPUS, ensure_ascii=False, indent=2))
        return 0
    if args.manifest is None or args.output_dir is None or args.reports_root is None:
        parser.error(
            "analysis is opt-in: provide --manifest, --reports-root and --output-dir, or --list-corpus"
        )
    # Defense in depth for environments that import additional libraries:
    # this command never calls an engine, provider, server, ASR or model loader.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        report = analyze_manifest(args.manifest.resolve())
        write_report(report, args.output_dir, args.reports_root)
    except (ValueError, OSError, RuntimeError) as error:
        parser.error(str(error))
    print(f"Analyzed {len(report['cases'])} WAVs. Reports: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
