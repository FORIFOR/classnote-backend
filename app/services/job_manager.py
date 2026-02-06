"""
job_manager.py - Job Management Service

Handles job lifecycle, error categorization, and retry logic.
"""

import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any
from google.cloud import firestore

from app.services.metrics import track_job_queued, track_job_completed, track_job_failed

logger = logging.getLogger("app.job_manager")


class ErrorCategory(str, Enum):
    """Job error categories for retry decisions."""

    # Transient errors - safe to retry
    TRANSIENT = "transient"  # Network issues, timeouts, temporary service unavailable

    # Permanent errors - do not retry
    PERMANENT = "permanent"  # Invalid input, missing data, logic errors

    # Quota errors - wait before retry
    QUOTA = "quota"  # Rate limits, monthly limits, API quotas

    # User errors - need user action
    USER_ERROR = "user_error"  # Payment required, account disabled, missing permissions

    # Unknown - investigate
    UNKNOWN = "unknown"


class JobStatus(str, Enum):
    """Standard job statuses."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"  # Max retries exceeded


# Error message patterns for categorization
ERROR_PATTERNS = {
    ErrorCategory.TRANSIENT: [
        "timeout",
        "connection",
        "unavailable",
        "503",
        "504",
        "network",
        "temporary",
        "retry",
    ],
    ErrorCategory.QUOTA: [
        "quota",
        "rate limit",
        "too many requests",
        "429",
        "monthly limit",
        "exceeded",
        "resource exhausted",
    ],
    ErrorCategory.USER_ERROR: [
        "payment required",
        "402",
        "account disabled",
        "permission denied",
        "403",
        "unauthorized",
        "401",
    ],
    ErrorCategory.PERMANENT: [
        "invalid",
        "not found",
        "404",
        "missing",
        "bad request",
        "400",
        "schema",
        "parse error",
    ],
}

# Max retries by error category
MAX_RETRIES = {
    ErrorCategory.TRANSIENT: 3,
    ErrorCategory.QUOTA: 1,  # Don't auto-retry quota errors
    ErrorCategory.USER_ERROR: 0,  # Never retry user errors
    ErrorCategory.PERMANENT: 0,  # Never retry permanent errors
    ErrorCategory.UNKNOWN: 1,
}


def categorize_error(error_message: str, error_code: Optional[str] = None) -> ErrorCategory:
    """
    Categorize an error message into an ErrorCategory.

    Args:
        error_message: The error message string
        error_code: Optional error code for more precise categorization

    Returns:
        ErrorCategory enum value
    """
    if not error_message:
        return ErrorCategory.UNKNOWN

    error_lower = error_message.lower()

    # Check error code first (more reliable)
    if error_code:
        code_lower = error_code.lower()
        if "quota" in code_lower or "limit" in code_lower:
            return ErrorCategory.QUOTA
        if "auth" in code_lower or "permission" in code_lower:
            return ErrorCategory.USER_ERROR
        if "timeout" in code_lower or "unavailable" in code_lower:
            return ErrorCategory.TRANSIENT

    # Check error message patterns
    for category, patterns in ERROR_PATTERNS.items():
        for pattern in patterns:
            if pattern in error_lower:
                return category

    return ErrorCategory.UNKNOWN


def can_retry(error_category: ErrorCategory, current_retry_count: int) -> bool:
    """
    Determine if a job can be retried based on error category and retry count.

    Args:
        error_category: The categorized error
        current_retry_count: How many times the job has been retried

    Returns:
        True if retry is allowed, False otherwise
    """
    max_retries = MAX_RETRIES.get(error_category, 0)
    return current_retry_count < max_retries


class JobManager:
    """
    Manages job lifecycle including creation, status updates, and retries.
    """

    _instance: Optional["JobManager"] = None
    _db: Optional[firestore.Client] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_db(self) -> firestore.Client:
        if self._db is None:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
            self._db = firestore.Client(project=project_id)
        return self._db

    def get_job_ref(self, session_id: str, job_id: str) -> firestore.DocumentReference:
        """Get a reference to a job document."""
        db = self._get_db()
        return db.collection("sessions").document(session_id).collection("jobs").document(job_id)

    def create_job(
        self,
        session_id: str,
        job_id: str,
        job_type: str,
        user_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a new job document with initial state.

        Returns the created job data.
        """
        now = datetime.now(timezone.utc)

        job_data = {
            "type": job_type,
            "status": JobStatus.QUEUED.value,
            "createdAt": now,
            "updatedAt": now,
            "userId": user_id,
            "idempotencyKey": idempotency_key,
            "retryCount": 0,
            "lastRetryAt": None,
            "errorCategory": None,
            "errorMessage": None,
            "errorCode": None,
            "completedAt": None,
            "result": None,
        }

        if metadata:
            job_data["metadata"] = metadata

        job_ref = self.get_job_ref(session_id, job_id)
        job_ref.set(job_data)

        # Track metric
        track_job_queued(job_type)

        logger.info(f"[JobManager] Created job {job_id} for session {session_id}, type={job_type}")
        return job_data

    def start_job(self, session_id: str, job_id: str) -> None:
        """Mark a job as running."""
        now = datetime.now(timezone.utc)

        job_ref = self.get_job_ref(session_id, job_id)
        job_ref.update({
            "status": JobStatus.RUNNING.value,
            "startedAt": now,
            "updatedAt": now,
        })

        logger.info(f"[JobManager] Started job {job_id}")

    def complete_job(
        self,
        session_id: str,
        job_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark a job as completed successfully."""
        now = datetime.now(timezone.utc)

        job_ref = self.get_job_ref(session_id, job_id)

        # Get job data for metrics
        job_doc = job_ref.get()
        job_data = job_doc.to_dict() if job_doc.exists else {}
        job_type = job_data.get("type", "unknown")
        started_at = job_data.get("startedAt")

        # Calculate duration
        duration_sec = 0
        if started_at:
            if hasattr(started_at, "timestamp"):
                duration_sec = (now.timestamp() - started_at.timestamp())

        job_ref.update({
            "status": JobStatus.COMPLETED.value,
            "completedAt": now,
            "updatedAt": now,
            "result": result,
            "durationSec": duration_sec,
        })

        # Track metric
        track_job_completed(job_type, duration_sec)

        logger.info(f"[JobManager] Completed job {job_id}, duration={duration_sec:.1f}s")

    def fail_job(
        self,
        session_id: str,
        job_id: str,
        error_message: str,
        error_code: Optional[str] = None,
        should_retry: bool = True,
    ) -> Dict[str, Any]:
        """
        Mark a job as failed with error categorization.

        Returns dict with retry info: {"can_retry": bool, "error_category": str}
        """
        now = datetime.now(timezone.utc)

        job_ref = self.get_job_ref(session_id, job_id)

        # Get current job state
        job_doc = job_ref.get()
        job_data = job_doc.to_dict() if job_doc.exists else {}

        job_type = job_data.get("type", "unknown")
        current_retry_count = job_data.get("retryCount", 0)

        # Categorize error
        error_category = categorize_error(error_message, error_code)

        # Determine if we can retry
        can_retry_job = should_retry and can_retry(error_category, current_retry_count)

        # Determine final status
        if can_retry_job:
            # Job will be retried, increment count but keep as queued/failed
            new_status = JobStatus.FAILED.value
        elif current_retry_count >= MAX_RETRIES.get(error_category, 0):
            # Max retries exceeded
            new_status = JobStatus.ABANDONED.value
        else:
            # Permanent failure
            new_status = JobStatus.FAILED.value

        job_ref.update({
            "status": new_status,
            "updatedAt": now,
            "errorMessage": error_message,
            "errorCode": error_code,
            "errorCategory": error_category.value,
            "lastErrorAt": now,
        })

        # Track metric
        track_job_failed(job_type, error_category.value)

        logger.warning(
            f"[JobManager] Failed job {job_id}: {error_message} "
            f"(category={error_category.value}, retry_count={current_retry_count}, can_retry={can_retry_job})"
        )

        return {
            "can_retry": can_retry_job,
            "error_category": error_category.value,
            "retry_count": current_retry_count,
            "max_retries": MAX_RETRIES.get(error_category, 0),
        }

    def record_retry(self, session_id: str, job_id: str) -> int:
        """
        Record a retry attempt for a job.

        Returns the new retry count.
        """
        now = datetime.now(timezone.utc)

        job_ref = self.get_job_ref(session_id, job_id)

        # Get current retry count
        job_doc = job_ref.get()
        job_data = job_doc.to_dict() if job_doc.exists else {}
        current_count = job_data.get("retryCount", 0)
        new_count = current_count + 1

        job_ref.update({
            "retryCount": new_count,
            "lastRetryAt": now,
            "status": JobStatus.QUEUED.value,
            "updatedAt": now,
        })

        logger.info(f"[JobManager] Recorded retry #{new_count} for job {job_id}")
        return new_count

    def get_job(self, session_id: str, job_id: str) -> Optional[Dict[str, Any]]:
        """Get a job document."""
        job_ref = self.get_job_ref(session_id, job_id)
        job_doc = job_ref.get()

        if not job_doc.exists:
            return None

        data = job_doc.to_dict()
        data["id"] = job_id
        data["sessionId"] = session_id
        return data

    def get_retryable_jobs(self, session_id: str) -> list:
        """Get all jobs that can be retried for a session."""
        db = self._get_db()
        jobs_ref = db.collection("sessions").document(session_id).collection("jobs")

        # Query failed jobs
        failed_jobs = list(jobs_ref.where("status", "==", JobStatus.FAILED.value).stream())

        retryable = []
        for job_doc in failed_jobs:
            job_data = job_doc.to_dict()
            error_category = ErrorCategory(job_data.get("errorCategory", "unknown"))
            retry_count = job_data.get("retryCount", 0)

            if can_retry(error_category, retry_count):
                job_data["id"] = job_doc.id
                retryable.append(job_data)

        return retryable


# Singleton instance
job_manager = JobManager()


# Convenience functions
def create_job(
    session_id: str,
    job_id: str,
    job_type: str,
    user_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new job."""
    return job_manager.create_job(session_id, job_id, job_type, user_id, idempotency_key)


def start_job(session_id: str, job_id: str) -> None:
    """Mark job as running."""
    job_manager.start_job(session_id, job_id)


def complete_job(session_id: str, job_id: str, result: Optional[Dict[str, Any]] = None) -> None:
    """Mark job as completed."""
    job_manager.complete_job(session_id, job_id, result)


def fail_job(
    session_id: str,
    job_id: str,
    error_message: str,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark job as failed with categorization."""
    return job_manager.fail_job(session_id, job_id, error_message, error_code)


def record_retry(session_id: str, job_id: str) -> int:
    """Record a retry attempt."""
    return job_manager.record_retry(session_id, job_id)
