"""
Unit tests for Summary v2 PR1:
  - SummaryV2 / SummaryV2Item schema validation
  - make_stable_item_id determinism
  - compute_transcript_hash
  - build_summary_v2_prompt shape
  - anchor_resolver v0.1 (resolve_item_anchors_v2 / apply_quality_gate_v2)
  - merge_user_edited_items (hidden/userEdited rules)
  - to_summary_v2_response mapper
  - finalize._compute_default_plan opt-in behavior
  - render_summary_v2_markdown smoke
"""
from __future__ import annotations

import os
import sys
import types

# Stub heavy imports so tests don't require fastapi / vertexai / firestore.
for _m in [
    "google", "google.cloud", "google.cloud.firestore",
    "vertexai", "vertexai.generative_models",
    "app.firebase",
]:
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)
sys.modules["app.firebase"].db = None  # type: ignore[attr-defined]
sys.path.insert(0, ".")

from app.util_models import (  # noqa: E402
    EvidenceRef,
    EvidenceSupport,
    SummaryV2,
    SummaryV2Item,
    SummaryV2ItemStatus,
    SummaryV2ItemType,
    SummaryV2LectureAddendum,
    SummaryV2Quality,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_version_locked_to_2():
    try:
        SummaryV2(version=1)
    except Exception:
        pass
    else:
        raise AssertionError("version=1 should be rejected")


def test_schema_version_locked():
    try:
        SummaryV2(schemaVersion="1.0")
    except Exception:
        pass
    else:
        raise AssertionError("schemaVersion=1.0 should be rejected")


def test_importance_score_range():
    try:
        SummaryV2Item(id="x", type=SummaryV2ItemType.NOTE, text="t", importanceScore=1.5)
    except Exception:
        pass
    else:
        raise AssertionError("importanceScore>1 should be rejected")


def test_confidence_range():
    try:
        SummaryV2Item(id="x", type=SummaryV2ItemType.NOTE, text="t", confidence=-0.1)
    except Exception:
        pass
    else:
        raise AssertionError("confidence<0 should be rejected")


def test_meeting_type_enum_whitelist():
    for v in ["lecture", "meeting", "translate", "interview", "other"]:
        SummaryV2(meetingType=v)  # ok
    try:
        SummaryV2(meetingType="wibble")
    except Exception:
        pass
    else:
        raise AssertionError("unknown meetingType should be rejected")


def test_lecture_addendum_stripped_when_not_lecture():
    s = SummaryV2(meetingType="meeting", lectureAddendum=SummaryV2LectureAddendum(theme="X"))
    assert s.lectureAddendum is None


def test_lecture_addendum_kept_when_lecture():
    s = SummaryV2(meetingType="lecture", lectureAddendum=SummaryV2LectureAddendum(theme="X"))
    assert s.lectureAddendum is not None
    assert s.lectureAddendum.theme == "X"


def test_response_status_literal():
    from app.util_models import SummaryV2Response
    for v in ["pending", "running", "ready", "failed"]:
        SummaryV2Response(status=v)  # ok
    try:
        SummaryV2Response(status="bogus")
    except Exception:
        pass
    else:
        raise AssertionError("status='bogus' should be rejected")


# ---------------------------------------------------------------------------
# summary_v2 helpers (prompt / hash / id / merge / markdown)
# ---------------------------------------------------------------------------

def test_make_stable_item_id_deterministic():
    from app.services.summary_v2 import make_stable_item_id
    a = make_stable_item_id("decision", "チケット発行を先に進める", "chunk_3")
    b = make_stable_item_id("decision", "チケット発行を先に進める", "chunk_3")
    c = make_stable_item_id("decision", "チケット発行を先に進める", "chunk_4")
    assert a == b
    assert a != c


def test_compute_transcript_hash_stable_order():
    from app.services.summary_v2 import compute_transcript_hash
    chunks_a = [
        {"id": "c1", "startMs": 0, "text": "hello"},
        {"id": "c2", "startMs": 1000, "text": "world"},
    ]
    chunks_b = list(reversed(chunks_a))
    assert compute_transcript_hash(chunks_a) == compute_transcript_hash(chunks_b)


def test_compute_transcript_hash_text_fallback():
    from app.services.summary_v2 import compute_transcript_hash
    assert compute_transcript_hash([], fallback_text="abc") == compute_transcript_hash(
        [], fallback_text="abc"
    )


def test_build_summary_v2_prompt_shape():
    from app.services.summary_v2 import SUMMARY_V2_PROMPT_VERSION, build_summary_v2_prompt
    p = build_summary_v2_prompt(
        mode="meeting",
        meeting_purpose="Q2 kickoff",
        participants=["A", "B"],
        language="ja",
        transcript_chunks=[
            {"id": "c1", "speakerId": "spk_1", "text": "hi", "startMs": 0, "endMs": 2000},
            {"segmentId": "c2", "speaker": "spk_2", "text": "ok", "startMs": 2000, "endMs": 4000},
            {"text": "no id"},  # silently dropped
        ],
    )
    assert p["promptVersion"] == SUMMARY_V2_PROMPT_VERSION
    assert p["mode"] == "meeting"
    assert p["meetingPurpose"] == "Q2 kickoff"
    assert p["participants"] == ["A", "B"]
    assert len(p["chunks"]) == 2
    assert {c["id"] for c in p["chunks"]} == {"c1", "c2"}


def test_prompt_normalizes_translation_to_translate():
    from app.services.summary_v2 import build_summary_v2_prompt
    p = build_summary_v2_prompt(
        mode="translation",
        meeting_purpose=None, participants=[], language=None, transcript_chunks=[],
    )
    assert p["mode"] == "translate"


def test_prompt_normalizes_unknown_to_other():
    from app.services.summary_v2 import build_summary_v2_prompt
    p = build_summary_v2_prompt(
        mode="doge", meeting_purpose=None, participants=[], language=None, transcript_chunks=[],
    )
    assert p["mode"] == "other"


def test_merge_user_edited_items_hidden_dropped():
    from app.services.summary_v2 import merge_user_edited_items
    old = [
        SummaryV2Item(id="k1", type=SummaryV2ItemType.NOTE, text="hidden", hidden=True),
    ]
    new = [
        SummaryV2Item(id="k1", type=SummaryV2ItemType.NOTE, text="now visible again"),
        SummaryV2Item(id="k2", type=SummaryV2ItemType.NOTE, text="fresh"),
    ]
    out = merge_user_edited_items(old, new)
    ids = [it.id for it in out]
    assert "k1" not in ids  # hidden item stays dropped
    assert "k2" in ids


def test_merge_user_edited_items_user_edited_wins():
    from app.services.summary_v2 import merge_user_edited_items
    old = [
        SummaryV2Item(id="x1", type=SummaryV2ItemType.NOTE, text="ORIGINAL USER EDIT", userEdited=True),
    ]
    new = [
        SummaryV2Item(id="x1", type=SummaryV2ItemType.NOTE, text="LLM regenerated (should not win)"),
    ]
    out = merge_user_edited_items(old, new)
    assert len(out) == 1
    assert out[0].text == "ORIGINAL USER EDIT"


def test_merge_user_edited_items_retains_dropped_old_edits():
    """An old userEdited item that the new run no longer produces is retained."""
    from app.services.summary_v2 import merge_user_edited_items
    old = [
        SummaryV2Item(id="keep", type=SummaryV2ItemType.NOTE, text="my note", userEdited=True),
    ]
    new: list = []
    out = merge_user_edited_items(old, new)
    assert len(out) == 1
    assert out[0].id == "keep"


def test_render_summary_v2_markdown_smoke():
    from app.services.summary_v2 import render_summary_v2_markdown
    s = SummaryV2(
        meetingType="meeting",
        items=[
            SummaryV2Item(id="a", type=SummaryV2ItemType.DECISION, text="決定A"),
            SummaryV2Item(id="b", type=SummaryV2ItemType.ACTION, text="TODO B"),
        ],
    )
    md = render_summary_v2_markdown(s)
    assert isinstance(md, str)
    assert len(md) > 0


# ---------------------------------------------------------------------------
# anchor_resolver v0.1
# ---------------------------------------------------------------------------

def test_resolve_item_anchors_v2_populates_start_end_text():
    from app.services.anchor_resolver import build_chunks_by_id, resolve_item_anchors_v2
    chunks_by_id = build_chunks_by_id([
        {"id": "c1", "startMs": 0,   "endMs": 1000, "text": "hello"},
        {"id": "c2", "startMs": 1000, "endMs": 2000, "text": "world"},
    ])
    item = SummaryV2Item(
        id="x", type=SummaryV2ItemType.NOTE, text="t",
        evidence=[EvidenceRef(segmentIds=["c1", "c2", "ghost"])],
    )
    out = resolve_item_anchors_v2(item, chunks_by_id)
    assert len(out.evidence) == 1
    ev = out.evidence[0]
    assert ev.segmentIds == ["c1", "c2"]  # ghost pruned
    assert ev.startMs == 0
    assert ev.endMs == 2000
    assert "hello" in ev.text and "world" in ev.text
    assert out.support == EvidenceSupport.PARTIAL
    assert out.anchorMs == 0


def test_resolve_item_anchors_v2_no_valid_drops_evidence_and_sets_none():
    from app.services.anchor_resolver import build_chunks_by_id, resolve_item_anchors_v2
    chunks_by_id = build_chunks_by_id([])
    item = SummaryV2Item(
        id="x", type=SummaryV2ItemType.NOTE, text="t",
        evidence=[EvidenceRef(segmentIds=["ghost"])],
    )
    out = resolve_item_anchors_v2(item, chunks_by_id)
    assert out.evidence == []
    assert out.support == EvidenceSupport.NONE


def test_apply_quality_gate_v2_passes_through_and_aggregates():
    from app.services.anchor_resolver import apply_quality_gate_v2
    items = [
        SummaryV2Item(id="1", type=SummaryV2ItemType.NOTE, text="a", support=EvidenceSupport.PARTIAL, confidence=0.8),
        SummaryV2Item(id="2", type=SummaryV2ItemType.NOTE, text="b", support=EvidenceSupport.NONE, confidence=0.2),
    ]
    kept, quality, filtered = apply_quality_gate_v2(items)
    # v0.1 pass-through
    assert len(kept) == 2
    assert filtered == []
    # aggregation still runs
    assert isinstance(quality, SummaryV2Quality)
    assert quality.partialCount == 1
    assert quality.unsupportedCount == 1
    assert quality.fullCount == 0
    assert quality.filteredCount == 0
    assert abs(quality.avgConfidence - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# to_summary_v2_response mapper
# ---------------------------------------------------------------------------

def test_mapper_none_doc_returns_pending_empty():
    from app.services.firestore_summary_v2 import to_summary_v2_response
    r = to_summary_v2_response(None)
    assert r["status"] == "pending"
    assert r["summary"] is None
    assert r["jobId"] is None
    assert r["errorReason"] is None


def test_mapper_succeeded_returns_ready():
    from app.services.firestore_summary_v2 import to_summary_v2_response
    doc = {
        "status": "succeeded",
        "jobId": "job1",
        "updatedAt": None,
        "result": SummaryV2(meetingType="meeting").model_dump(),
    }
    r = to_summary_v2_response(doc)
    assert r["status"] == "ready"
    assert r["summary"] is not None
    assert r["jobId"] == "job1"
    assert r["errorReason"] is None


def test_mapper_parse_error_flips_to_failed():
    from app.services.firestore_summary_v2 import to_summary_v2_response
    bad = {
        "status": "succeeded",
        "jobId": "j",
        "result": {"version": 999},  # violates locked version
    }
    r = to_summary_v2_response(bad)
    assert r["status"] == "failed"
    assert r["errorReason"] == "parse_error"


def test_mapper_running_passthrough():
    from app.services.firestore_summary_v2 import to_summary_v2_response
    r = to_summary_v2_response({"status": "running", "jobId": "j"})
    assert r["status"] == "running"
    assert r["summary"] is None


def test_mapper_failed_passthrough_with_error_reason():
    from app.services.firestore_summary_v2 import to_summary_v2_response
    r = to_summary_v2_response({"status": "failed", "errorReason": "llm_timeout"})
    assert r["status"] == "failed"
    assert r["errorReason"] == "llm_timeout"


# ---------------------------------------------------------------------------
# finalize opt-in defaults
# ---------------------------------------------------------------------------

def test_default_plan_v1_only_by_default(monkeypatch):
    # v1_only (default): no summary_v2, no summary_quick
    monkeypatch.delenv("ENABLE_SUMMARY_V2_FINALIZE", raising=False)
    monkeypatch.delenv("ENABLE_SUMMARY_QUICK_FINALIZE", raising=False)
    monkeypatch.setenv("SUMMARY_PIPELINE_MODE", "v1_only")
    import importlib
    from app.services import finalize as _fin
    importlib.reload(_fin)
    plan = _fin._compute_default_plan()
    assert plan.get("summary") is True
    assert plan.get("summary_v2") is not True
    assert plan.get("summary_quick") is not True


def test_default_plan_dual_mode(monkeypatch):
    monkeypatch.setenv("SUMMARY_PIPELINE_MODE", "dual")
    monkeypatch.delenv("ENABLE_SUMMARY_QUICK_FINALIZE", raising=False)
    import importlib
    from app.services import finalize as _fin
    importlib.reload(_fin)
    plan = _fin._compute_default_plan()
    assert plan.get("summary") is True
    assert plan.get("summary_v2") is True
    assert plan.get("summary_quick") is not True


def test_default_plan_v2_only(monkeypatch):
    monkeypatch.setenv("SUMMARY_PIPELINE_MODE", "v2_only")
    import importlib
    from app.services import finalize as _fin
    importlib.reload(_fin)
    plan = _fin._compute_default_plan()
    assert plan.get("summary") is not True
    assert plan.get("summary_v2") is True


def test_default_plan_quick_enabled(monkeypatch):
    monkeypatch.setenv("SUMMARY_PIPELINE_MODE", "v1_only")
    monkeypatch.setenv("ENABLE_SUMMARY_QUICK_FINALIZE", "1")
    import importlib
    from app.services import finalize as _fin
    importlib.reload(_fin)
    plan = _fin._compute_default_plan()
    assert plan.get("summary_quick") is True
