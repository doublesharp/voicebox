"""The refinement prompt must pin output language to the transcript's language.

Regression tests for #603: the refinement system prompt is written in English
with English-only few-shot examples, and without an explicit language
constraint small refinement models silently translate non-English transcripts
into English. These tests pin the prompt-level guardrails (no model needed).
"""

from backend.services.refinement import RefinementFlags, build_refinement_prompt


def test_known_language_pins_output_language():
    prompt = build_refinement_prompt(RefinementFlags(), language="zh")
    assert "Chinese" in prompt
    assert "MUST be written in Chinese" in prompt
    assert "Never translate the transcript" in prompt


def test_unknown_language_falls_back_to_same_language_rule():
    prompt = build_refinement_prompt(RefinementFlags())
    assert "same language as the transcript" in prompt
    assert "Never translate the transcript" in prompt


def test_language_rule_coexists_with_flag_sections():
    prompt = build_refinement_prompt(
        RefinementFlags(smart_cleanup=True, self_correction=True, preserve_technical=True),
        language="ko",
    )
    assert "Korean" in prompt
    # The behaviour sections are still present alongside the language rule.
    assert "disfluencies" in prompt.lower()
    assert "correction cue" in prompt.lower()
    assert "dot" in prompt  # preserve_technical symbol mapping survives


def test_newer_language_has_readable_name():
    prompt = build_refinement_prompt(RefinementFlags(), language="sv")
    assert "MUST be written in Swedish" in prompt


def test_language_code_is_case_insensitive():
    prompt = build_refinement_prompt(RefinementFlags(), language="ZH")
    assert "MUST be written in Chinese" in prompt


def test_unmapped_language_uses_transcript_instead_of_raw_code():
    prompt = build_refinement_prompt(RefinementFlags(), language="unsupported-code")
    assert "same language as the transcript" in prompt
    assert "unsupported-code" not in prompt


async def test_capture_language_reaches_llm_and_refined_text_is_persisted(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from backend.database import Base, Capture
    from backend.services import captures, refinement

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    generate = AsyncMock(return_value="我们周四开会。")
    monkeypatch.setattr(
        refinement.llm_service, "get_llm_model", lambda: SimpleNamespace(model_size="0.6B", generate=generate)
    )
    try:
        with Session(engine) as db:
            db.add(Capture(id="zh-capture", audio_path="unused.wav", language="zh", transcript_raw="我们周四开会。"))
            db.commit()
            response = await captures.refine_capture("zh-capture", RefinementFlags(), "1.7B", db)
            assert response.transcript_refined == "我们周四开会。"
            assert db.get(Capture, "zh-capture").llm_model == "1.7B"
            assert "MUST be written in Chinese" in generate.call_args.kwargs["system"]
            assert generate.call_args.kwargs["prompt"] == "我们周四开会。"
    finally:
        engine.dispose()
