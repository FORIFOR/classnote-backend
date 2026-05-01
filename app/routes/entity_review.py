"""
routes/entity_review.py — PR2 Entity Review HTTP surface.

Endpoints:
    POST /v1/sessions/{session_id}/entity-review/run    — trigger extraction
    GET  /v1/sessions/{session_id}/entity-review        — latest review + candidates
    POST /v1/sessions/{session_id}/entity-review/apply  — commit decisions
    POST /v1/sessions/{session_id}/entity-review/skip   — mark as skipped
    GET  /v1/sessions/{session_id}/term-hints           — build next-session hints

All endpoints require an authenticated user (Firebase JWT via CurrentUser)
and enforce ownership of the target session.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import CurrentUser, get_current_user, ensure_is_owner
from app.util_models import (
    ApplyEntityReviewRequest,
    ApplyEntityReviewResponse,
    EntityReviewCandidate,
    EntityReviewResponse,
    EntityReviewSummary,
    SkipEntityReviewRequest,
    TermHint,
    TermHintsResponse,
)

logger = logging.getLogger("app.entity_review_routes")

router = APIRouter(prefix="/v1/sessions", tags=["entity-review"])


def _resolve_and_authorize(session_id: str, user: CurrentUser) -> Dict[str, Any]:
    """Shared session-resolve + owner check used by every handler."""
    from app.routes.sessions import _resolve_session
    _, snap, resolved_id = _resolve_session(session_id, user.uid)
    data = snap.to_dict() or {}
    ensure_is_owner(data, user, resolved_id)
    data["_id"] = resolved_id
    return data


# ---------------------------------------------------------------------------
# POST /entity-review/run
# ---------------------------------------------------------------------------

@router.post("/{session_id}/entity-review/run")
async def run_entity_review(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Build (or rebuild) the entity-review candidate set from canonical text."""
    from app.services import entity_review_services as svc
    from app.services import entity_review_store as store

    data = _resolve_and_authorize(session_id, current_user)
    resolved_id = data["_id"]

    canonical = store.get_canonical_transcript(resolved_id)
    if not canonical:
        raise HTTPException(
            status_code=409,
            detail="canonical_transcript_missing — finalize must run first",
        )

    user_terms = store.list_terms_for_user(current_user.uid)
    known_surfaces: List[str] = []
    for t in user_terms:
        canon = t.get("canonical")
        if canon:
            known_surfaces.append(canon)
        for a in t.get("aliases") or []:
            if a and a not in known_surfaces:
                known_surfaces.append(a)

    candidates = svc.build_candidates(
        text=canonical.get("text") or "",
        known_terms=known_surfaces,
    )

    review = store.create_review(
        resolved_id,
        source_transcript_version=int(canonical.get("version", 1)),
        candidate_count=len(candidates),
        language=canonical.get("language", "ja"),
    )
    if candidates:
        store.save_candidates(resolved_id, review["reviewId"], candidates)
    store.update_entity_review_status(
        resolved_id,
        "pending" if candidates else "none",
        review["reviewId"] if candidates else None,
    )
    return {
        "ok": True,
        "reviewId": review["reviewId"],
        "candidateCount": len(candidates),
    }


# ---------------------------------------------------------------------------
# GET /entity-review
# ---------------------------------------------------------------------------

@router.get("/{session_id}/entity-review", response_model=EntityReviewResponse)
async def get_entity_review(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> EntityReviewResponse:
    from app.services import entity_review_store as store

    data = _resolve_and_authorize(session_id, current_user)
    resolved_id = data["_id"]

    review_raw = store.get_latest_review(resolved_id)
    if not review_raw:
        return EntityReviewResponse(ok=True, review=None, candidates=[])

    candidates_raw = store.list_candidates(resolved_id, review_raw["reviewId"])
    review_summary = EntityReviewSummary(**review_raw)
    candidates = []
    for c in candidates_raw:
        try:
            candidates.append(EntityReviewCandidate(**c))
        except Exception as exc:
            logger.warning("[entity_review] candidate parse failed: %s", exc)
            continue
    return EntityReviewResponse(ok=True, review=review_summary, candidates=candidates)


# ---------------------------------------------------------------------------
# POST /entity-review/apply
# ---------------------------------------------------------------------------

@router.post("/{session_id}/entity-review/apply", response_model=ApplyEntityReviewResponse)
async def apply_entity_review(
    session_id: str,
    body: ApplyEntityReviewRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> ApplyEntityReviewResponse:
    from app.services import entity_review_services as svc
    from app.services import entity_review_store as store

    data = _resolve_and_authorize(session_id, current_user)
    resolved_id = data["_id"]

    canonical = store.get_canonical_transcript(resolved_id)
    if not canonical:
        raise HTTPException(status_code=404, detail="canonical_transcript_missing")

    candidates = store.list_candidates(resolved_id, body.reviewId)
    candidate_by_id = {c.get("candidateId"): c for c in candidates if c.get("candidateId")}

    decisions = [d.model_dump() for d in body.decisions]
    new_text, patches = svc.apply_decisions_to_text(
        text=canonical.get("text") or "",
        decisions=decisions,
        candidate_by_id=candidate_by_id,
    )

    patch_count = sum(p["occurrences"] for p in patches)
    next_version = int(canonical.get("version", 1)) + 1

    if patches:
        store.save_canonical_transcript(
            resolved_id,
            version=next_version,
            text=new_text,
            base_version=int(canonical.get("version", 1)),
            patch_count=patch_count,
            source="entity_review_apply",
            language=canonical.get("language", "ja"),
        )
        store.update_canonical_version(resolved_id, next_version)
        for p in patches:
            store.create_patch(
                resolved_id,
                review_id=body.reviewId,
                candidate_id=p["candidateId"],
                action=p["action"],
                find=p["surface"],
                replace=p["replacement"],
                applied_to_occurrences=p["occurrences"],
            )
    else:
        # No patches applied — keep version as-is but still mark reviewed.
        next_version = int(canonical.get("version", 1))

    # Learn custom terms
    for upsert in svc.decisions_to_term_upserts(decisions, candidate_by_id):
        store.upsert_term(
            current_user.uid,
            canonical=upsert["canonical"],
            alias=upsert["alias"],
            entity_type=upsert["entity_type"],
            created_from_session_id=resolved_id,
        )

    # Enqueue regenerations only when canonical actually changed.
    enqueued: Dict[str, bool] = {}
    if patches:
        enqueued = svc.enqueue_regeneration(
            resolved_id,
            regenerate_summary=body.regenerate.summary,
            regenerate_summary_v2=body.regenerate.summary_v2,
            regenerate_todos=body.regenerate.todos,
            regenerate_highlights=body.regenerate.highlights,
            regenerate_quiz=body.regenerate.quiz,
            user_id=current_user.uid,
        )

    store.mark_review_applied(
        resolved_id, body.reviewId, applied_count=len(patches),
    )
    store.update_entity_review_status(resolved_id, "applied", body.reviewId)

    return ApplyEntityReviewResponse(
        ok=True,
        canonicalTranscriptVersion=next_version,
        patchCount=patch_count,
        regenerationEnqueued=any(enqueued.values()) if enqueued else False,
    )


# ---------------------------------------------------------------------------
# POST /entity-review/skip
# ---------------------------------------------------------------------------

@router.post("/{session_id}/entity-review/skip")
async def skip_entity_review(
    session_id: str,
    body: SkipEntityReviewRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    from app.services import entity_review_store as store

    data = _resolve_and_authorize(session_id, current_user)
    resolved_id = data["_id"]

    store.mark_review_skipped(resolved_id, body.reviewId)
    store.update_entity_review_status(resolved_id, "skipped", body.reviewId)
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /term-hints
# ---------------------------------------------------------------------------

@router.get("/{session_id}/term-hints", response_model=TermHintsResponse)
async def get_term_hints(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> TermHintsResponse:
    from app.services import entity_review_services as svc
    from app.services import entity_review_store as store

    data = _resolve_and_authorize(session_id, current_user)
    resolved_id = data["_id"]

    user_terms = store.list_terms_for_user(current_user.uid)
    payload = svc.build_term_hints(user_terms)
    store.save_term_hints(resolved_id, payload)

    terms = [TermHint(**t) for t in payload["terms"]]
    return TermHintsResponse(ok=True, version=payload["version"], terms=terms)
