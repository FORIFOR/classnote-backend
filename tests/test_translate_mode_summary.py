"""
Tests for translate mode as a first-class summary path (replaces the old
translate→lecture fallback).

Locks:
  - _get_summary_schema("translate") returns the meeting schema shape
    (decisions/todos/openQuestions/etc. are appropriate for bilingual
    sessions).
  - _pick_map_prompt / _pick_reduce_prompt dispatch translate to their
    dedicated prompts, not to meeting/lecture.
  - The translate prompts preserve the ===ORIGINAL===/===TRANSLATION===
    delimiter instructions so the LLM uses both sides.
  - The single-pass summary builder picks _build_translate_summary_prompt
    when mode=="translate".
"""
from __future__ import annotations

from app.services import llm


# ---------------------------------------------------------------------------
# Schema selection
# ---------------------------------------------------------------------------

def test_translate_uses_meeting_schema():
    """translate mode is structurally meeting-like: decisions/todos/
    highlights/participants/openQuestions are the right primitives."""
    schema = llm._get_summary_schema("translate")
    assert schema is llm._SCHEMA_MEETING_SUMMARY, (
        "translate should share the meeting schema (not lecture)"
    )


def test_meeting_schema_unchanged():
    assert llm._get_summary_schema("meeting") is llm._SCHEMA_MEETING_SUMMARY


def test_lecture_schema_unchanged():
    assert llm._get_summary_schema("lecture") is llm._SCHEMA_LECTURE_SUMMARY


def test_unknown_mode_defaults_to_lecture_schema():
    """Legacy safety: unknown modes fall back to lecture, not meeting."""
    assert llm._get_summary_schema("wibble") is llm._SCHEMA_LECTURE_SUMMARY


# ---------------------------------------------------------------------------
# Map / Reduce prompt dispatch
# ---------------------------------------------------------------------------

def test_pick_map_prompt_translate():
    assert llm._pick_map_prompt("translate") is llm._MAP_PROMPT_TRANSLATE


def test_pick_map_prompt_meeting():
    assert llm._pick_map_prompt("meeting") is llm._MAP_PROMPT_MEETING


def test_pick_map_prompt_lecture():
    assert llm._pick_map_prompt("lecture") is llm._MAP_PROMPT_LECTURE


def test_pick_reduce_prompt_translate():
    assert llm._pick_reduce_prompt("translate") is llm._REDUCE_PROMPT_TRANSLATE


def test_pick_reduce_prompt_meeting():
    assert llm._pick_reduce_prompt("meeting") is llm._REDUCE_PROMPT_MEETING


def test_pick_reduce_prompt_lecture():
    assert llm._pick_reduce_prompt("lecture") is llm._REDUCE_PROMPT_LECTURE


# ---------------------------------------------------------------------------
# Translate prompt content contracts
# ---------------------------------------------------------------------------

def test_translate_summary_prompt_mentions_delimiter():
    """The single-pass prompt must tell the LLM about the bilingual
    delimiter format so it uses both sides."""
    p = llm._build_translate_summary_prompt("dummy")
    assert "===ORIGINAL===" in p
    assert "===TRANSLATION===" in p


def test_translate_summary_prompt_requests_japanese_output():
    p = llm._build_translate_summary_prompt("dummy")
    assert "日本語" in p


def test_translate_summary_prompt_has_type_translate():
    p = llm._build_translate_summary_prompt("dummy")
    assert '"type": "translate"' in p


def test_translate_summary_prompt_has_keyterms_section():
    """keyTerms is the translate-specific structural addition — bilingual
    term glossary for review."""
    p = llm._build_translate_summary_prompt("dummy")
    assert "keyTerms" in p


def test_translate_map_prompt_mentions_delimiter():
    assert "===ORIGINAL===" in llm._MAP_PROMPT_TRANSLATE
    assert "===TRANSLATION===" in llm._MAP_PROMPT_TRANSLATE


def test_translate_reduce_prompt_has_type_translate():
    assert '"type": "translate"' in llm._REDUCE_PROMPT_TRANSLATE


def test_translate_quick_summary_prompt_exists():
    """Quick path also needs translate-aware prompt (Japanese output,
    delimiter awareness)."""
    p = llm._build_quick_summary_prompt("dummy", "translate")
    assert "===ORIGINAL===" in p
    assert "日本語" in p


def test_quick_summary_prompts_are_distinct_per_mode():
    tq = llm._build_quick_summary_prompt("x", "translate")
    mq = llm._build_quick_summary_prompt("x", "meeting")
    lq = llm._build_quick_summary_prompt("x", "lecture")
    assert tq != mq
    assert tq != lq


# ---------------------------------------------------------------------------
# Single-pass summary prompt builder (the wrapper _build_summary_tags_prompt)
# ---------------------------------------------------------------------------

def test_single_pass_translate_uses_translate_builder():
    """_build_summary_tags_prompt is the single-pass entry used by
    generate_summary_and_tags(). When mode=translate, it must call the
    translate builder (not meeting/lecture)."""
    prompt = llm._build_summary_tags_prompt("dummy text", "translate", segments=None)
    # The translate prompt is uniquely identified by these phrases
    assert "===ORIGINAL===" in prompt
    assert '"type": "translate"' in prompt


def test_single_pass_meeting_builder_unchanged():
    prompt = llm._build_summary_tags_prompt("dummy", "meeting", segments=None)
    assert '"type": "meeting"' in prompt


def test_single_pass_lecture_builder_unchanged():
    prompt = llm._build_summary_tags_prompt("dummy", "lecture", segments=None)
    assert '"type": "lecture"' in prompt
