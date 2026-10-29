"""Optional cleanup must preserve disabled content and reject clear meaning loss."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services import refinement


@pytest.mark.asyncio
async def test_all_disabled_skips_model_and_preserves_raw_repetition(monkeypatch):
    def unexpected():
        raise AssertionError("disabled cleanup loaded an LLM")

    monkeypatch.setattr(refinement.llm_service, "get_llm_model", unexpected)
    text = "Ada 2048 " + "repeat " * 8
    result, identity = await refinement.refine_transcript(text, refinement.RefinementFlags(False, False, False))
    assert result == text
    assert identity is None


@pytest.mark.asyncio
async def test_clean_punctuated_transcript_needs_no_model(monkeypatch):
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: pytest.fail("unneeded cleanup"))
    text = "Ada paid 2048 dollars."
    assert await refinement.refine_transcript(text, refinement.RefinementFlags()) == (text, None)


@pytest.mark.asyncio
async def test_changed_names_numbers_or_dropped_meaning_preserve_original(monkeypatch):
    backend = SimpleNamespace(generate=AsyncMock(return_value="Eva paid 48 dollars."))
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: backend)
    monkeypatch.setattr(refinement.llm_service, "resolve_model_size", lambda *args: "fixture")
    text = "um Ada paid 2048 dollars for the medical equipment"
    assert await refinement.refine_transcript(text, refinement.RefinementFlags()) == (text, None)


@pytest.mark.asyncio
async def test_requested_technical_conversion_and_explicit_correction_remain_allowed(monkeypatch):
    backend = SimpleNamespace(generate=AsyncMock(side_effect=["Edit index.tsx.", "The meeting is at 6."]))
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: backend)
    monkeypatch.setattr(refinement.llm_service, "resolve_model_size", lambda *args: "fixture")
    assert await refinement.refine_transcript("edit index dot tsx", refinement.RefinementFlags()) == (
        "Edit index.tsx.",
        "fixture",
    )
    assert await refinement.refine_transcript("the meeting is at 7 no wait at 6", refinement.RefinementFlags()) == (
        "The meeting is at 6.",
        "fixture",
    )


@pytest.mark.asyncio
async def test_disabled_smart_cleanup_does_not_strip_repeated_artifacts(monkeypatch):
    text = "repeat " * 8
    generate = AsyncMock(return_value=text)
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(refinement.llm_service, "resolve_model_size", lambda *args: "fixture")
    assert await refinement.refine_transcript(text, refinement.RefinementFlags(False, False, True)) == (text, None)
    generate.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_cannot_silently_drop_a_negation(monkeypatch):
    generate = AsyncMock(return_value="Ada approved the invoice for the medical equipment.")
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(refinement.llm_service, "resolve_model_size", lambda *args: "fixture")
    original = "um Ada has not approved the invoice for the medical equipment"
    assert await refinement.refine_transcript(original, refinement.RefinementFlags()) == (original, None)


@pytest.mark.asyncio
async def test_technical_only_run_does_not_instruct_disabled_filler_cleanup(monkeypatch):
    generate = AsyncMock(return_value="edit index.tsx")
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(refinement.llm_service, "resolve_model_size", lambda *args: "fixture")
    assert await refinement.refine_transcript("edit index dot tsx", refinement.RefinementFlags(False, False, True)) == (
        "edit index.tsx",
        "fixture",
    )
    assert "Delete disfluencies" not in generate.call_args.kwargs["system"]
    assert "Remove disfluencies" not in generate.call_args.kwargs["system"]
    assert generate.call_args.kwargs["examples"] == []


@pytest.mark.asyncio
async def test_empty_model_output_and_empty_artifact_cleanup_preserve_original(monkeypatch):
    generate = AsyncMock(return_value=" ")
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(refinement.llm_service, "resolve_model_size", lambda *args: "fixture")
    assert await refinement.refine_transcript("um Hello Ada", refinement.RefinementFlags()) == ("um Hello Ada", None)
    generate.reset_mock()
    repeated = "repeat " * 8
    assert await refinement.refine_transcript(repeated, refinement.RefinementFlags()) == (repeated, None)
    generate.assert_not_called()


@pytest.mark.asyncio
async def test_capitalization_still_runs_when_smart_cleanup_was_requested(monkeypatch):
    generate = AsyncMock(return_value="Hello Ada.")
    monkeypatch.setattr(refinement.llm_service, "get_llm_model", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(refinement.llm_service, "resolve_model_size", lambda *args: "fixture")
    assert await refinement.refine_transcript("hello Ada.", refinement.RefinementFlags()) == ("Hello Ada.", "fixture")
