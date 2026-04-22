"""
firestore_summary_v2.py — Summary v2 Firestore read/write surface (PR1).

All Summary v2 Firestore access funnels through this module. No other file
should touch `sessions/{sid}/derived/summary_v2` or write the session-doc
summaryV2* mirror fields directly. That keeps the dual write (derived doc +
session mirror) consistent and the call sites grep-able.

Storage shape (spec §4.2):

  sessions/{session_id}/derived/summary_v2
    status: "pending" | "running" | "succeeded" | "failed"
    idempotencyKey: str
    jobId: str | None
    startedAt: datetime | None
    updatedAt: datetime
    errorReason: str | None
    result: dict           # SummaryV2.model_dump() when succeeded
    modelInfo:
      provider / model / promptVersion / tokensIn / tokensOut / latencyMs
    meta:
      transcriptVersion / transcriptHash / mode

Session doc mirror (spec §4.4):
    summaryV2Status, summaryV2UpdatedAt, summaryV2Quality,
    title / titleSource / titleUpdatedAt (only when not user-edited).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.firebase import db

logger = logging.getLogger("app.firestore_summary_v2")


_DERIVED_COLLECTION = "derived"
_DERIVED_DOC = "summary_v2"


# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------

def _session_ref(session_id: str):
    return db.collection("sessions").document(session_id)


def _derived_ref(session_id: str):
    return _session_ref(session_id).collection(_DERIVED_COLLECTION).document(_DERIVED_DOC)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_summary_v2_doc(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the derived/summary_v2 doc dict, or None if absent."""
    snap = _derived_ref(session_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


# ---------------------------------------------------------------------------
# Write: status transitions
# ---------------------------------------------------------------------------

def write_summary_v2_running(
    session_id: str,
    *,
    idempotency_key: str,
    job_id: Optional[str],
    prompt_version: str,
    transcript_hash: str,
    mode: str,
) -> None:
    """Mark the derived doc as running at the start of a worker run."""
    now = datetime.now(timezone.utc)
    _derived_ref(session_id).set({
        "status": "running",
        "idempotencyKey": idempotency_key,
        "jobId": job_id,
        "startedAt": now,
        "updatedAt": now,
        "errorReason": None,
        "modelInfo": {
            "promptVersion": prompt_version,
        },
        "meta": {
            "transcriptHash": transcript_hash,
            "mode": mode,
        },
    }, merge=True)

    # Session mirror (spec §4.4)
    _session_ref(session_id).set({
        "summaryV2Status": "running",
        "summaryV2UpdatedAt": now,
    }, merge=True)


def write_summary_v2_success(
    session_id: str,
    *,
    summary_result: Dict[str, Any],
    model_info: Dict[str, Any],
    meta: Dict[str, Any],
) -> None:
    """Persist a successful summary v2 run.

    summary_result is the SummaryV2.model_dump() output.
    model_info should include at least {provider, model, promptVersion,
    tokensIn, tokensOut, latencyMs}. meta should include at least
    {transcriptVersion, transcriptHash, mode}.
    """
    now = datetime.now(timezone.utc)
    _derived_ref(session_id).set({
        "status": "succeeded",
        "updatedAt": now,
        "errorReason": None,
        "result": summary_result,
        "modelInfo": model_info,
        "meta": meta,
    }, merge=True)


def write_summary_v2_failed(
    session_id: str,
    *,
    error_reason: str,
    job_id: Optional[str] = None,
    model_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist a failed summary v2 run."""
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "status": "failed",
        "updatedAt": now,
        "errorReason": error_reason,
    }
    if job_id is not None:
        payload["jobId"] = job_id
    if model_info is not None:
        payload["modelInfo"] = model_info
    _derived_ref(session_id).set(payload, merge=True)

    _session_ref(session_id).set({
        "summaryV2Status": "failed",
        "summaryV2UpdatedAt": now,
    }, merge=True)


# ---------------------------------------------------------------------------
# Session mirror sync (spec §4.4, §4.5)
# ---------------------------------------------------------------------------

def _quality_bucket(quality: Optional[Dict[str, Any]]) -> str:
    """Collapse SummaryV2Quality into a 3-bucket label for the session mirror.

    v0.1 heuristic:
        high:   avgConfidence >= 0.7 AND unsupportedCount == 0
        low:    avgConfidence < 0.4 OR more than half items unsupported
        mid:    otherwise
    """
    if not quality:
        return "unknown"
    try:
        avg = float(quality.get("avgConfidence") or 0.0)
        full = int(quality.get("fullCount") or 0)
        partial = int(quality.get("partialCount") or 0)
        unsup = int(quality.get("unsupportedCount") or 0)
    except (TypeError, ValueError):
        return "unknown"
    total = full + partial + unsup
    if total == 0:
        return "unknown"
    if avg >= 0.7 and unsup == 0:
        return "high"
    if avg < 0.4 or unsup * 2 > total:
        return "low"
    return "mid"


def sync_session_summary_v2_fields(
    session_id: str,
    *,
    summary_result: Dict[str, Any],
) -> None:
    """Mirror SummaryV2 success state onto the session doc.

    - summaryV2Status = "completed" (we keep the classic "completed" label
      on the session mirror for iOS compat; the canonical status on the
      derived doc remains "succeeded")
    - summaryV2UpdatedAt = now
    - summaryV2Quality = high | mid | low | unknown bucket
    - title / titleSource / titleUpdatedAt when suggestedTitle present AND
      session.titleEditedByUser != true (spec §4.5)
    """
    now = datetime.now(timezone.utc)
    quality_bucket = _quality_bucket(summary_result.get("quality"))
    payload: Dict[str, Any] = {
        "summaryV2Status": "completed",
        "summaryV2UpdatedAt": now,
        "summaryV2Quality": quality_bucket,
    }

    suggested = (summary_result.get("suggestedTitle") or "").strip()
    if suggested:
        session_snap = _session_ref(session_id).get()
        session_data = session_snap.to_dict() if session_snap.exists else {}
        title_edited_by_user = bool((session_data or {}).get("titleEditedByUser", False))
        if not title_edited_by_user:
            payload["title"] = suggested
            payload["titleSource"] = "summary_v2"
            payload["titleUpdatedAt"] = now
        else:
            logger.info(
                "[summary_v2] title update suppressed (titleEditedByUser=true) "
                "session=%s suggested=%r",
                session_id, suggested[:40],
            )

    _session_ref(session_id).set(payload, merge=True)


# ---------------------------------------------------------------------------
# Response mapper (spec §12)
# ---------------------------------------------------------------------------

def to_summary_v2_response(doc: Optional[Dict[str, Any]]):
    """Map a derived doc into a SummaryV2Response-compatible dict.

    Contract (spec §5.1):
      - no doc          -> pending / summary=None
      - status=pending  -> pending
      - status=running  -> running
      - status=succeeded -> ready (with parsed SummaryV2)
      - status=failed   -> failed / errorReason
      - parse error     -> failed / errorReason='parse_error'

    Returns a plain dict so callers can wrap in the Pydantic response or
    return as-is; doing the Pydantic construction inside a route handler
    keeps this module free of FastAPI imports.
    """
    from app.util_models import SummaryV2  # local to avoid circular at import

    if doc is None:
        return {
            "status": "pending",
            "summary": None,
            "jobId": None,
            "updatedAt": None,
            "errorReason": None,
        }

    status_raw = (doc.get("status") or "pending").lower()
    job_id = doc.get("jobId")
    updated_at = doc.get("updatedAt")
    error_reason = doc.get("errorReason")

    if status_raw == "succeeded":
        try:
            summary = SummaryV2(**(doc.get("result") or {}))
            return {
                "status": "ready",
                "summary": summary,
                "jobId": job_id,
                "updatedAt": updated_at,
                "errorReason": None,
            }
        except Exception as exc:
            logger.warning(
                "[summary_v2] failed to parse stored result: %s", exc,
            )
            return {
                "status": "failed",
                "summary": None,
                "jobId": job_id,
                "updatedAt": updated_at,
                "errorReason": "parse_error",
            }

    # passthrough for pending / running / failed
    if status_raw not in {"pending", "running", "failed"}:
        status_raw = "pending"

    return {
        "status": status_raw,
        "summary": None,
        "jobId": job_id,
        "updatedAt": updated_at,
        "errorReason": error_reason if status_raw == "failed" else None,
    }
