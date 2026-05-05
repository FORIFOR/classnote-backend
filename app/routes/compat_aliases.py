"""
compat_aliases.py — iOS / legacy client compatibility shim.

Reasons for each alias:
- iOS APIClient.swift uses hyphenated paths (`summary-v2:generate`,
  `summary-v2:feedback`, `quiz-attempts`) while the canonical handlers in
  this branch use underscored paths. Restore the hyphen variants by
  delegating to the canonical handlers.
- iOS expects `transcript_segments` under `/artifacts/` prefix; the canonical
  handler is registered at `/sessions/{id}/transcript_segments` only.
- iOS expects a `POST /sessions/{id}/playlist:generate` trigger that this
  branch lacks (the canonical generation path is internal). Implemented as
  a thin wrapper around `enqueue_playlist_task`.

These aliases are pure pass-through and do not introduce new business logic.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies import CurrentUser, get_current_user
from app.firebase import db

logger = logging.getLogger("app.routes.compat_aliases")

router = APIRouter(tags=["Compat Aliases"], include_in_schema=False)


# ---------------------------------------------------------------------------
# Summary v2 — hyphen aliases
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/artifacts/summary-v2:generate")
async def alias_summary_v2_generate(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Hyphen alias of POST /sessions/{id}/artifacts/summary_v2:generate (iOS compat)."""
    from app.routes.sessions import generate_summary_v2_endpoint
    from app.util_models import SummaryV2GenerateRequest

    raw = await request.body()
    body_dict = {} if not raw else (await request.json())
    body = SummaryV2GenerateRequest(**body_dict)
    return await generate_summary_v2_endpoint(
        session_id=session_id,
        body=body,
        background_tasks=background_tasks,
        current_user=current_user,
    )


@router.post("/sessions/{session_id}/artifacts/summary-v2:feedback")
async def alias_summary_v2_feedback(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Hyphen alias of POST /sessions/{id}/artifacts/summary_v2:feedback (iOS compat)."""
    from app.routes.sessions import submit_summary_v2_feedback
    from app.util_models import SummaryV2FeedbackRequest

    body_dict = await request.json()
    body = SummaryV2FeedbackRequest(**body_dict)
    return await submit_summary_v2_feedback(
        session_id=session_id,
        body=body,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# Quiz attempts — hyphen alias
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/quiz-attempts")
async def alias_quiz_attempts(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Hyphen alias of POST /sessions/{id}/quiz_attempts (iOS compat)."""
    from app.routes.quiz_analytics import create_quiz_attempt
    from app.util_models import QuizAttemptCreate

    body_dict = await request.json()
    attempt = QuizAttemptCreate(**body_dict)
    return await create_quiz_attempt(
        session_id=session_id,
        attempt=attempt,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# Transcript segments — artifacts/ prefix alias
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/artifacts/transcript_segments")
async def alias_artifacts_transcript_segments(
    session_id: str,
    fromMs: Optional[int] = None,
    toMs: Optional[int] = None,
    limit: int = 100,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Alias for /sessions/{id}/transcript_segments under /artifacts/ prefix (iOS compat)."""
    from app.routes.sessions import get_transcript_segments
    return await get_transcript_segments(
        session_id=session_id,
        fromMs=fromMs,
        toMs=toMs,
        limit=limit,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# Playlist generate — new thin wrapper
# ---------------------------------------------------------------------------

class PlaylistGenerateRequest(BaseModel):
    force: bool = False
    idempotencyKey: Optional[str] = None


class PlaylistGenerateResponse(BaseModel):
    status: str
    jobId: str
    statusUrl: str


@router.post("/sessions/{session_id}/playlist:generate", response_model=PlaylistGenerateResponse)
async def alias_playlist_generate(
    session_id: str,
    body: PlaylistGenerateRequest = PlaylistGenerateRequest(),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Trigger playlist (aiMarkers) generation as an async Cloud Tasks job."""
    from app.task_queue import enqueue_playlist_task

    # Resolve + ownership check
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")
    data = doc.to_dict() or {}
    owner_uid = data.get("ownerUid") or data.get("ownerUserId") or data.get("userId")
    owner_account = data.get("ownerAccountId")
    if owner_uid != current_user.uid and owner_account != current_user.account_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Idempotency: if a playlist generation is already running and force is False, reuse
    derived_ref = doc_ref.collection("derived").document("playlist")
    derived_snap = derived_ref.get()
    if derived_snap.exists and not body.force:
        derived_data = derived_snap.to_dict() or {}
        running_status = derived_data.get("status")
        if running_status in ("running", "queued", "succeeded", "completed"):
            existing_job = derived_data.get("jobId") or ""
            return PlaylistGenerateResponse(
                status=("already_completed" if running_status in ("succeeded", "completed") else "already_running"),
                jobId=existing_job,
                statusUrl=f"/jobs/{existing_job}" if existing_job else "",
            )

    job_id = f"playlist_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    derived_ref.set({
        "status": "queued",
        "jobId": job_id,
        "updatedAt": now,
    }, merge=True)

    try:
        enqueue_playlist_task(session_id, user_id=current_user.uid, job_id=job_id)
        logger.info(f"[playlist:generate] enqueued job {job_id} for session {session_id}")
    except Exception as e:
        logger.error(f"[playlist:generate] enqueue failed: {e}")
        derived_ref.set({"status": "failed", "errorReason": str(e), "updatedAt": now}, merge=True)
        raise HTTPException(status_code=500, detail="Failed to enqueue playlist task")

    return PlaylistGenerateResponse(
        status="queued",
        jobId=job_id,
        statusUrl=f"/jobs/{job_id}",
    )
