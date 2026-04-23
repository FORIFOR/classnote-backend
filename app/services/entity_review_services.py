"""
entity_review_services.py — PR2 pure services for entity review.

Collapses the spec's 5 service modules (extractor / scoring / patch /
term_memory / meeting_hint) into one file to mirror the project's existing
"one-file service" style. Each function is pure (no Firestore I/O) — the
store layer (`entity_review_store.py`) handles persistence.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("app.entity_review")


# ===========================================================================
# Extractor
# ===========================================================================

# Latin/technical tokens like "Gemini", "Flash-Lite", "OAuth2", "API_KEY"
_TECH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{1,40}")
# Runs of Katakana (likely transliterated names/products)
_KATAKANA_RE = re.compile(r"[ァ-ヶー]{2,}")
# Obvious filler tokens we never want surfaced as candidates
_STOP_SURFACES = {
    "API", "API_KEY", "OK", "NO", "YES", "TODO", "LLM", "AI",
    "PM", "CTO", "CEO",
}


def extract_candidates(text: str) -> List[str]:
    """Extract raw surface candidates from transcript text.

    Returns a de-duplicated list; order follows first appearance so tests
    are deterministic.
    """
    if not text:
        return []
    seen: List[str] = []
    seen_set: set = set()
    for m in _TECH_TOKEN_RE.finditer(text):
        surface = m.group(0)
        if surface in _STOP_SURFACES:
            continue
        if surface not in seen_set:
            seen.append(surface)
            seen_set.add(surface)
    for m in _KATAKANA_RE.finditer(text):
        surface = m.group(0)
        if surface not in seen_set:
            seen.append(surface)
            seen_set.add(surface)
    return seen


# ===========================================================================
# Scoring
# ===========================================================================

_FUZZY_MATCH_THRESHOLD = 72      # 0..100 rapidfuzz WRatio
_FUZZY_SCORE_NORMALIZE = 100.0
_SUSPICION_MIN = 0.65


def _try_rapidfuzz():
    """Lazy rapidfuzz import so unit tests can stub it."""
    try:
        from rapidfuzz import fuzz, process  # type: ignore
        return fuzz, process
    except ImportError:
        logger.warning("[entity_review] rapidfuzz not installed; scoring disabled")
        return None, None


def suggest_matches(
    surface: str,
    known_terms: Sequence[str],
    *,
    limit: int = 5,
    threshold: int = _FUZZY_MATCH_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Return [{'value', 'score 0..1'}] of best fuzzy matches above threshold.

    Empty list when rapidfuzz is absent or no match clears the threshold.
    """
    if not surface or not known_terms:
        return []
    fuzz, process = _try_rapidfuzz()
    if process is None or fuzz is None:
        return []
    matches = process.extract(
        surface,
        list(known_terms),
        scorer=fuzz.WRatio,
        limit=limit,
    )
    out: List[Dict[str, Any]] = []
    for value, score, _idx in matches:
        if score >= threshold:
            out.append({
                "value": value,
                "score": round(score / _FUZZY_SCORE_NORMALIZE, 4),
            })
    return out


def suspicion_score(
    *,
    low_confidence: float = 0.0,
    oov: float = 0.0,
    fuzzy: float = 0.0,
    variant_conflict: float = 0.0,
    context_anomaly: float = 0.0,
) -> float:
    """Weighted suspicion 0..1 (spec weights, kept stable for tests)."""
    score = (
        0.35 * low_confidence
        + 0.20 * oov
        + 0.20 * fuzzy
        + 0.15 * variant_conflict
        + 0.10 * context_anomaly
    )
    return round(min(max(score, 0.0), 1.0), 4)


def build_candidates(
    *,
    text: str,
    known_terms: Sequence[str],
    max_candidates: int = 10,
    suspicion_min: float = _SUSPICION_MIN,
) -> List[Dict[str, Any]]:
    """Top-level extractor + scorer pipeline.

    Returns a list of candidate dicts in the shape the store layer expects.
    Pure function; no Firestore I/O.
    """
    surfaces = extract_candidates(text)
    out: List[Dict[str, Any]] = []
    for surface in surfaces:
        suggestions = suggest_matches(surface, known_terms)
        # Only bubble up surfaces that look like they might be misspellings:
        # either a fuzzy hit exists, or the surface is pure katakana (harder
        # to score fuzzy) and long enough to care about.
        is_katakana_only = bool(_KATAKANA_RE.fullmatch(surface))
        if not suggestions and not is_katakana_only:
            continue
        fuzzy_hint = max((s["score"] for s in suggestions), default=0.0)
        score = suspicion_score(
            low_confidence=0.7,
            oov=0.6 if not suggestions else 0.4,
            fuzzy=fuzzy_hint,
            variant_conflict=0.4,
            context_anomaly=0.3,
        )
        if score < suspicion_min:
            continue
        out.append({
            "surface": surface,
            "normalized": surface.lower(),
            "entityType": "unknown",
            "suspicionScore": score,
            "reasons": ["near_known_term"] if suggestions else ["out_of_vocabulary"],
            "occurrenceCount": text.count(surface),
            "occurrences": [],
            "suggestions": suggestions,
        })
    out.sort(key=lambda c: c["suspicionScore"], reverse=True)
    return out[:max_candidates]


# ===========================================================================
# Patching
# ===========================================================================

def apply_replace_all(text: str, find: str, replace: str) -> Tuple[str, int]:
    """Naive global replace. Returns (new_text, occurrences_replaced)."""
    if not find or find == replace:
        return text, 0
    count = text.count(find)
    if count == 0:
        return text, 0
    return text.replace(find, replace), count


def apply_decisions_to_text(
    *,
    text: str,
    decisions: List[Dict[str, Any]],
    candidate_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Apply a list of ApplyDecision dicts to `text`.

    Returns (new_text, patches) where each patch dict has
    {candidateId, surface, replacement, occurrences, action}.
    Only replace_all is honored in PR2 (replace_once / keep / ignore → noop).
    """
    out_text = text
    patches: List[Dict[str, Any]] = []
    for d in decisions:
        action = d.get("action")
        cand = candidate_by_id.get(d.get("candidateId", ""))
        if not cand:
            continue
        if action != "replace_all":
            # PR2 v0.1: non-replace_all actions short-circuit.
            continue
        replacement = (d.get("replacement") or "").strip()
        if not replacement:
            continue
        surface = cand.get("surface", "")
        out_text, n = apply_replace_all(out_text, surface, replacement)
        if n == 0:
            continue
        patches.append({
            "candidateId": cand.get("candidateId"),
            "surface": surface,
            "replacement": replacement,
            "occurrences": n,
            "action": "replace_all",
        })
    return out_text, patches


# ===========================================================================
# Term-memory learning
# ===========================================================================

def decisions_to_term_upserts(
    decisions: List[Dict[str, Any]],
    candidate_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert apply decisions into term-upsert payloads.

    Only decisions with action=replace_all AND learnTerm=True (default) AND a
    non-empty replacement are learned. Caller passes each payload to
    `entity_review_store.upsert_term(user_id=...)`.
    """
    out: List[Dict[str, Any]] = []
    for d in decisions:
        if d.get("action") != "replace_all":
            continue
        if d.get("learnTerm") is False:
            continue
        replacement = (d.get("replacement") or "").strip()
        if not replacement:
            continue
        cand = candidate_by_id.get(d.get("candidateId", ""))
        if not cand:
            continue
        surface = (cand.get("surface") or "").strip()
        if not surface or surface == replacement:
            continue
        out.append({
            "canonical": replacement,
            "alias": surface,
            "entity_type": cand.get("entityType", "unknown"),
        })
    return out


# ===========================================================================
# Term hints (next-session glossary)
# ===========================================================================

def build_term_hints(
    user_terms: List[Dict[str, Any]],
    *,
    limit: int = 40,
) -> Dict[str, Any]:
    """Convert custom_terms list → term_hints payload shape."""
    terms = []
    for t in user_terms[:limit]:
        terms.append({
            "canonical": t.get("canonical", ""),
            "aliases": list(t.get("aliases") or []),
            "entityType": t.get("entityType", "unknown"),
            "priority": round(float(t.get("weight", 0.8)), 4),
        })
    return {"version": 1, "terms": terms}


# ===========================================================================
# Regeneration dispatch (reuses existing enqueue_* functions from task_queue)
# ===========================================================================

def enqueue_regeneration(
    session_id: str,
    *,
    regenerate_summary: bool,
    regenerate_summary_v2: bool,
    regenerate_todos: bool,
    regenerate_highlights: bool,
    regenerate_quiz: bool,
    user_id: Optional[str] = None,
) -> Dict[str, bool]:
    """Kick existing queues after the canonical transcript is patched.

    Returns a dict with which queues were actually enqueued, mainly for
    tests / log assertions.
    """
    enqueued: Dict[str, bool] = {}

    # Lazy imports: task_queue touches google-cloud-tasks at import time in prod.
    if regenerate_summary:
        try:
            from app.task_queue import enqueue_summarize_task
            enqueue_summarize_task(session_id, user_id=user_id, idempotency_key=f"canon:{session_id}:summary")
            enqueued["summary"] = True
        except Exception as exc:
            logger.warning("[entity_review] summary re-enqueue failed: %s", exc)
            enqueued["summary"] = False

    if regenerate_summary_v2:
        try:
            from app.task_queue import enqueue_summary_v2_task
            enqueue_summary_v2_task(
                session_id, user_id=user_id,
                idempotency_key=f"canon:{session_id}:summary_v2",
            )
            enqueued["summary_v2"] = True
        except Exception as exc:
            logger.warning("[entity_review] summary_v2 re-enqueue failed: %s", exc)
            enqueued["summary_v2"] = False

    if regenerate_highlights:
        try:
            from app.task_queue import enqueue_generate_highlights_task
            enqueue_generate_highlights_task(session_id, user_id=user_id)
            enqueued["highlights"] = True
        except Exception as exc:
            logger.warning("[entity_review] highlights re-enqueue failed: %s", exc)
            enqueued["highlights"] = False

    if regenerate_todos:
        try:
            # todo extractor expects strings for summary/account; fetch from
            # Firestore lazily to avoid requiring caller to pass everything.
            from app.services.entity_review_store import (
                get_canonical_transcript,
            )
            from app.firebase import db as _db
            from app.task_queue import enqueue_todo_extraction_task
            sess_snap = _db.collection("sessions").document(session_id).get()
            sess_data = sess_snap.to_dict() if sess_snap.exists else {}
            canonical = get_canonical_transcript(session_id) or {}
            enqueue_todo_extraction_task(
                session_id=session_id,
                account_id=(sess_data or {}).get("ownerAccountId") or "",
                source_key=f"canon:{session_id}",
                summary_text=(sess_data or {}).get("summaryMarkdown") or "",
                transcript_text=canonical.get("text") or (sess_data or {}).get("transcriptText") or "",
                mode=(sess_data or {}).get("mode") or "lecture",
                user_id=user_id,
            )
            enqueued["todos"] = True
        except Exception as exc:
            logger.warning("[entity_review] todos re-enqueue failed: %s", exc)
            enqueued["todos"] = False

    if regenerate_quiz:
        try:
            from app.task_queue import enqueue_quiz_task
            enqueue_quiz_task(session_id, user_id=user_id)
            enqueued["quiz"] = True
        except Exception as exc:
            logger.warning("[entity_review] quiz re-enqueue failed: %s", exc)
            enqueued["quiz"] = False

    return enqueued
