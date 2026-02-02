"""
Async Jobs API Routes

Provides:
- GET /jobs/{jobId} - Get job status (lightweight, for polling)
- POST /sessions/{sessionId}/artifacts/{type}:generate - Queue async generation

Design principles:
- Jobs are idempotent: same request returns same jobId
- Status endpoint is lightweight (no LLM results)
- 202 Accepted for all generate requests (even if job exists)
- No "error" in user-facing responses (use errorReason for debugging)
"""

import logging
import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from google.cloud import firestore

from app.firebase import db
from app.dependencies import get_current_user, CurrentUser
from app.util_models import (
    AsyncJobType,
    AsyncJobStatus,
    SummaryStage,
    QuizStage,
    GenerateRequest,
    GenerateResponse,
    JobStatusResponse,
    PartialSummary,
)
from app.task_queue import enqueue_summarize_task, enqueue_quiz_task

logger = logging.getLogger("app.jobs")

router = APIRouter()

# =============================================================================
# Constants
# =============================================================================

LEASE_DURATION_SECONDS = 300  # 5 minutes lease for running jobs
JOB_EXPIRY_DAYS = 7  # Keep job records for 7 days


# =============================================================================
# Helpers
# =============================================================================

def _jobs_collection():
    return db.collection("jobs")


def _job_keys_collection():
    """Collection for idempotency key -> jobId mapping."""
    return db.collection("job_keys")


def _compute_idempotency_key(
    session_id: str,
    job_type: str,
    account_id: str,
    params: Optional[dict] = None
) -> str:
    """
    Compute idempotency key for a job.

    Key components:
    - sessionId
    - job type (summary, quiz, etc.)
    - accountId (prevent cross-account collisions)
    - params hash (promptVersion, mode, etc.)
    """
    params_str = ""
    if params:
        # Sort keys for consistent hashing
        sorted_params = sorted(params.items())
        params_str = str(sorted_params)

    key_source = f"{session_id}:{job_type}:{account_id}:{params_str}"
    return hashlib.sha256(key_source.encode()).hexdigest()[:32]


def _get_or_create_job(
    idempotency_key: str,
    session_id: str,
    job_type: AsyncJobType,
    user_id: str,
    account_id: str,
    request_params: Optional[dict] = None,
) -> tuple[str, dict, bool]:
    """
    Get existing job or create new one (transactional).

    Returns: (jobId, job_data, is_new)
    """
    key_ref = _job_keys_collection().document(idempotency_key)

    @firestore.transactional
    def txn_get_or_create(transaction):
        key_doc = key_ref.get(transaction=transaction)
        now = datetime.now(timezone.utc)

        if key_doc.exists:
            # Existing job found
            existing_job_id = key_doc.to_dict().get("jobId")
            if existing_job_id:
                job_doc = _jobs_collection().document(existing_job_id).get(transaction=transaction)
                if job_doc.exists:
                    job_data = job_doc.to_dict()
                    # Check if job is still valid (not expired/deleted)
                    status = job_data.get("status")
                    if status in [AsyncJobStatus.QUEUED.value, AsyncJobStatus.RUNNING.value,
                                  AsyncJobStatus.SUCCEEDED.value]:
                        return existing_job_id, job_data, False
                    # Job failed - allow re-creation
                    if status == AsyncJobStatus.FAILED.value:
                        # Check if enough time has passed for retry
                        failed_at = job_data.get("completedAt")
                        if failed_at:
                            if hasattr(failed_at, 'replace') and failed_at.tzinfo is None:
                                failed_at = failed_at.replace(tzinfo=timezone.utc)
                            # Allow retry after 30 seconds
                            if now - failed_at < timedelta(seconds=30):
                                return existing_job_id, job_data, False

        # Create new job
        new_job_id = str(uuid.uuid4())
        job_data = {
            "id": new_job_id,
            "type": job_type.value,
            "sessionId": session_id,
            "userId": user_id,
            "accountId": account_id,
            "status": AsyncJobStatus.QUEUED.value,
            "stage": "queued",  # Stage for progress UI
            "idempotencyKey": idempotency_key,
            "createdAt": now,
            "updatedAt": now,
            "startedAt": None,
            "completedAt": None,
            "leaseUntil": None,
            "errorReason": None,
            "resultRef": None,
            "resultUrl": None,
            "progress": None,
            "partial": None,  # Partial results during generation
            "retryCount": 0,
            "request": request_params or {},
        }

        # Write job document
        transaction.set(_jobs_collection().document(new_job_id), job_data)

        # Write key -> jobId mapping
        transaction.set(key_ref, {
            "jobId": new_job_id,
            "createdAt": now,
            "sessionId": session_id,
            "type": job_type.value,
        })

        return new_job_id, job_data, True

    transaction = db.transaction()
    return txn_get_or_create(transaction)


def _update_job_status(
    job_id: str,
    status: AsyncJobStatus,
    error_reason: Optional[str] = None,
    result_url: Optional[str] = None,
    progress: Optional[float] = None,
):
    """Update job status (used by workers)."""
    now = datetime.now(timezone.utc)
    update_data = {
        "status": status.value,
        "updatedAt": now,
    }

    if status == AsyncJobStatus.RUNNING:
        update_data["startedAt"] = now
        update_data["leaseUntil"] = now + timedelta(seconds=LEASE_DURATION_SECONDS)

    if status in [AsyncJobStatus.SUCCEEDED, AsyncJobStatus.FAILED]:
        update_data["completedAt"] = now
        update_data["leaseUntil"] = None

    if error_reason is not None:
        update_data["errorReason"] = error_reason

    if result_url is not None:
        update_data["resultUrl"] = result_url

    if progress is not None:
        update_data["progress"] = progress

    _jobs_collection().document(job_id).update(update_data)


# =============================================================================
# GET /jobs/{jobId}
# =============================================================================

@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Get job status.

    Lightweight endpoint for polling - does not include LLM results.
    Only the job owner (same accountId) can view job status.
    """
    job_doc = _jobs_collection().document(job_id).get()

    if not job_doc.exists:
        raise HTTPException(status_code=404, detail="Job not found")

    job_data = job_doc.to_dict()

    # Authorization: check accountId matches
    if job_data.get("accountId") != current_user.account_id:
        raise HTTPException(status_code=404, detail="Job not found")

    # Build partial results if available
    partial_data = job_data.get("partial")
    partial = None
    if partial_data:
        partial = PartialSummary(
            tldr=partial_data.get("tldr"),
            overview=partial_data.get("overview"),
            keyPoints=partial_data.get("keyPoints"),
        )

    return JobStatusResponse(
        jobId=job_data["id"],
        type=AsyncJobType(job_data["type"]),
        sessionId=job_data["sessionId"],
        status=AsyncJobStatus(job_data["status"]),
        stage=job_data.get("stage"),
        createdAt=job_data["createdAt"],
        updatedAt=job_data["updatedAt"],
        completedAt=job_data.get("completedAt"),
        resultUrl=job_data.get("resultUrl"),
        errorReason=job_data.get("errorReason"),
        progress=job_data.get("progress"),
        partial=partial,
    )


# =============================================================================
# POST /sessions/{sessionId}/artifacts/summary:generate
# =============================================================================

@router.post(
    "/sessions/{session_id}/artifacts/summary:generate",
    response_model=GenerateResponse,
    status_code=202,
)
async def generate_summary(
    session_id: str,
    body: GenerateRequest = GenerateRequest(),
    background_tasks: BackgroundTasks = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Queue summary generation (async).

    Returns 202 Accepted with jobId for status tracking.
    Idempotent: same request returns same jobId.
    """
    # Verify session exists and user has access
    session_doc = db.collection("sessions").document(session_id).get()
    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")

    session_data = session_doc.to_dict()
    owner_account = session_data.get("ownerAccountId")
    owner_uid = session_data.get("ownerUserId") or session_data.get("ownerUid")

    # Check ownership
    if owner_account != current_user.account_id and owner_uid != current_user.uid:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Compute idempotency key
    params = {
        "promptVersion": body.promptVersion,
        "mode": body.mode,
        "language": body.language,
    }
    idempotency_key = _compute_idempotency_key(
        session_id,
        AsyncJobType.SUMMARY.value,
        current_user.account_id,
        params if not body.force else None  # force=True creates new job
    )

    # Get or create job
    job_id, job_data, is_new = _get_or_create_job(
        idempotency_key=idempotency_key,
        session_id=session_id,
        job_type=AsyncJobType.SUMMARY,
        user_id=current_user.uid,
        account_id=current_user.account_id,
        request_params=params,
    )

    # If new job, enqueue to Cloud Tasks
    if is_new:
        try:
            enqueue_summarize_task(
                session_id=session_id,
                job_id=job_id,
                user_id=current_user.uid,
                background_tasks=background_tasks,
            )
            logger.info(f"[generate_summary] Queued job {job_id} for session {session_id}")
        except Exception as e:
            logger.error(f"[generate_summary] Failed to enqueue job {job_id}: {e}")
            # Mark job as failed
            _update_job_status(job_id, AsyncJobStatus.FAILED, error_reason=str(e))
            raise HTTPException(status_code=500, detail="Failed to queue job")
    else:
        logger.info(f"[generate_summary] Reusing existing job {job_id} for session {session_id}")

    status_url = f"/jobs/{job_id}"

    return GenerateResponse(
        jobId=job_id,
        status=AsyncJobStatus(job_data["status"]),
        statusUrl=status_url,
        estimatedSeconds=30 if is_new else None,
        existingResult=job_data["status"] == AsyncJobStatus.SUCCEEDED.value,
    )


# =============================================================================
# POST /sessions/{sessionId}/artifacts/quiz:generate
# =============================================================================

@router.post(
    "/sessions/{session_id}/artifacts/quiz:generate",
    response_model=GenerateResponse,
    status_code=202,
)
async def generate_quiz(
    session_id: str,
    body: GenerateRequest = GenerateRequest(),
    background_tasks: BackgroundTasks = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Queue quiz generation (async).

    Returns 202 Accepted with jobId for status tracking.
    Idempotent: same request returns same jobId.
    """
    # Verify session exists and user has access
    session_doc = db.collection("sessions").document(session_id).get()
    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")

    session_data = session_doc.to_dict()
    owner_account = session_data.get("ownerAccountId")
    owner_uid = session_data.get("ownerUserId") or session_data.get("ownerUid")

    # Check ownership
    if owner_account != current_user.account_id and owner_uid != current_user.uid:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Compute idempotency key
    params = {
        "promptVersion": body.promptVersion,
        "mode": body.mode,
        "language": body.language,
    }
    idempotency_key = _compute_idempotency_key(
        session_id,
        AsyncJobType.QUIZ.value,
        current_user.account_id,
        params if not body.force else None
    )

    # Get or create job
    job_id, job_data, is_new = _get_or_create_job(
        idempotency_key=idempotency_key,
        session_id=session_id,
        job_type=AsyncJobType.QUIZ,
        user_id=current_user.uid,
        account_id=current_user.account_id,
        request_params=params,
    )

    # If new job, enqueue to Cloud Tasks
    if is_new:
        try:
            enqueue_quiz_task(
                session_id=session_id,
                job_id=job_id,
                user_id=current_user.uid,
                background_tasks=background_tasks,
            )
            logger.info(f"[generate_quiz] Queued job {job_id} for session {session_id}")
        except Exception as e:
            logger.error(f"[generate_quiz] Failed to enqueue job {job_id}: {e}")
            _update_job_status(job_id, AsyncJobStatus.FAILED, error_reason=str(e))
            raise HTTPException(status_code=500, detail="Failed to queue job")
    else:
        logger.info(f"[generate_quiz] Reusing existing job {job_id} for session {session_id}")

    status_url = f"/jobs/{job_id}"

    return GenerateResponse(
        jobId=job_id,
        status=AsyncJobStatus(job_data["status"]),
        statusUrl=status_url,
        estimatedSeconds=45 if is_new else None,
        existingResult=job_data["status"] == AsyncJobStatus.SUCCEEDED.value,
    )


# =============================================================================
# Worker helpers (exported for task_queue.py)
# =============================================================================

def start_job(job_id: str) -> Optional[dict]:
    """
    Mark job as running and acquire lease.

    Returns job data if successfully acquired, None if job is already
    completed or being processed by another worker.
    """
    job_ref = _jobs_collection().document(job_id)

    @firestore.transactional
    def txn_start(transaction):
        job_doc = job_ref.get(transaction=transaction)
        if not job_doc.exists:
            return None

        job_data = job_doc.to_dict()
        status = job_data.get("status")
        now = datetime.now(timezone.utc)

        # Already completed
        if status in [AsyncJobStatus.SUCCEEDED.value, AsyncJobStatus.FAILED.value]:
            return None

        # Check lease
        if status == AsyncJobStatus.RUNNING.value:
            lease_until = job_data.get("leaseUntil")
            if lease_until:
                if hasattr(lease_until, 'replace') and lease_until.tzinfo is None:
                    lease_until = lease_until.replace(tzinfo=timezone.utc)
                if lease_until > now:
                    # Another worker has the lease
                    return None

        # Acquire lease
        update_data = {
            "status": AsyncJobStatus.RUNNING.value,
            "startedAt": now,
            "updatedAt": now,
            "leaseUntil": now + timedelta(seconds=LEASE_DURATION_SECONDS),
            "retryCount": (job_data.get("retryCount") or 0) + 1,
        }
        transaction.update(job_ref, update_data)

        return {**job_data, **update_data}

    transaction = db.transaction()
    return txn_start(transaction)


def complete_job(job_id: str, result_url: str):
    """Mark job as succeeded with result URL."""
    _update_job_status(job_id, AsyncJobStatus.SUCCEEDED, result_url=result_url)
    logger.info(f"[Job] {job_id} completed successfully")


def fail_job(job_id: str, error_reason: str, is_permanent: bool = False):
    """
    Mark job as failed.

    Args:
        job_id: Job ID
        error_reason: Error description (for debugging, not user-facing)
        is_permanent: If True, job won't be retried
    """
    _update_job_status(job_id, AsyncJobStatus.FAILED, error_reason=error_reason)
    logger.warning(f"[Job] {job_id} failed: {error_reason} (permanent={is_permanent})")


def update_job_progress(job_id: str, progress: float):
    """Update job progress (0.0 - 1.0)."""
    _update_job_status(job_id, AsyncJobStatus.RUNNING, progress=progress)


def update_job_stage(
    job_id: str,
    stage: str,
    progress: Optional[float] = None,
    partial: Optional[dict] = None,
):
    """
    Update job stage and optionally partial results.

    Args:
        job_id: Job ID
        stage: Current stage (e.g., "generating_tldr", "generating_overview")
        progress: Progress value 0.0-1.0
        partial: Partial results dict (tldr, overview, keyPoints)
    """
    now = datetime.now(timezone.utc)
    update_data = {
        "stage": stage,
        "updatedAt": now,
    }

    if progress is not None:
        update_data["progress"] = progress

    if partial is not None:
        update_data["partial"] = partial

    try:
        _jobs_collection().document(job_id).update(update_data)
        logger.debug(f"[Job] {job_id} stage={stage} progress={progress}")
    except Exception as e:
        logger.warning(f"[Job] Failed to update stage for {job_id}: {e}")
