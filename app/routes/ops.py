"""
Operations endpoints for deployment safety and monitoring.

Provides:
- Presence/heartbeat tracking for active users
- Active summary for safe deployment checks
- Drain mode management
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from google.cloud import firestore

from app.firebase import db
from app.dependencies import get_current_user, CurrentUser
from app.admin_auth import get_current_admin_user as require_admin
from app.services.app_config import (
    is_drain_mode,
    set_drain_mode,
    get_app_config,
    AppConfigResponse,
)

logger = logging.getLogger("app.ops")
router = APIRouter()


# ============================================================================
# Models
# ============================================================================

class HeartbeatRequest(BaseModel):
    """Request model for presence heartbeat.

    [HOTFIX 2026-05-05] `deviceId` made optional. The current ClassnoteX
    iOS build POSTs heartbeats without this field, which previously
    returned 422 and made the iOS bootstrap loop ("同期中 / キャッシュで
    起動しました" stuck on splash). When omitted, the server falls back
    to a stable per-account "ios" identifier for presence tracking.
    """
    deviceId: Optional[str] = Field(None, description="Unique device identifier")
    sessionId: Optional[str] = Field(None, description="Current session ID if any")
    states: List[str] = Field(
        default_factory=list,
        description="Active states: cloud_stt_active, summarization_active, upload_active, etc."
    )
    appVersion: Optional[str] = Field(None, description="App version")
    rev: Optional[str] = Field(None, description="Backend revision: stable or canary")
    platform: Optional[str] = Field(None, description="Client platform (ios/desktop/web)")

    class Config:
        extra = "allow"  # tolerate forward-compat fields from iOS


class HeartbeatResponse(BaseModel):
    """Response model for heartbeat"""
    ok: bool = True
    drainMode: bool = False
    expiresAt: datetime


class ActiveSummary(BaseModel):
    """Summary of active users/operations for deployment safety"""
    activeTotal: int = Field(0, description="Total number of active presences")
    byState: Dict[str, int] = Field(default_factory=dict, description="Count by activity state")
    activeStreams: int = Field(0, description="Active STT WebSocket streams")
    runningJobs: int = Field(0, description="Running async jobs")
    queuedJobs: int = Field(0, description="Queued async jobs")
    drainMode: bool = Field(False, description="Whether drain mode is active")
    safeToPromote: bool = Field(False, description="Whether it's safe to promote canary")
    updatedAt: datetime


class DrainModeRequest(BaseModel):
    """Request to enable/disable drain mode"""
    enabled: bool
    message: Optional[str] = None


# ============================================================================
# Presence Constants
# ============================================================================

PRESENCE_TTL_SECONDS = 90  # Heartbeat must be sent at least every 90 seconds
PRESENCE_COLLECTION = "presence"


# ============================================================================
# Presence Endpoints
# ============================================================================

@router.post("/presence/heartbeat", response_model=HeartbeatResponse)
async def presence_heartbeat(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Send a heartbeat to indicate active presence.

    iOS should call this:
    - When starting a cloud operation (STT, summary, upload)
    - Every 60 seconds while the operation is ongoing
    - When the operation completes (with empty states to clear)

    The presence automatically expires after 90 seconds if no heartbeat is received.

    [HOTFIX 2026-05-08 V-040] Body parsing is fail-soft. The previous
    handler used ``req: HeartbeatRequest`` as a typed dependency, which
    let FastAPI/Pydantic raise 422 when iOS sent a body that could not be
    coerced (empty body, non-JSON, ``states`` not a list of str, …).
    The 422 in turn caused the iOS bootstrap loop to be stuck on
    "同期中 / キャッシュで起動しました" and to fail folder fetch / YouTube
    import / record-to-folder save.

    We now read the body manually and tolerate any malformed payload —
    presence tracking is best-effort, never load-bearing for data
    integrity, so returning 200 with a logged warning is strictly safer
    than 422 retry storms. The original Pydantic ``HeartbeatRequest`` is
    still applied opportunistically when the body parses, so usable
    fields (deviceId / states / appVersion / rev / platform) still flow
    through to Firestore.
    """
    # ---- fail-soft body parse (V-040) ----
    raw: Any = {}
    try:
        if request.headers.get("content-length", "0") not in ("", "0"):
            raw = await request.json()
    except Exception as e:
        logger.warning("[Presence] heartbeat body not JSON, treating as empty: %s", e)
        raw = {}
    if not isinstance(raw, dict):
        logger.warning("[Presence] heartbeat body was %s, not dict, treating as empty",
                       type(raw).__name__)
        raw = {}
    try:
        req = HeartbeatRequest(**raw)
    except Exception as e:
        logger.warning("[Presence] heartbeat Pydantic validation failed, falling back: %s", e)
        # Best-effort coercion of the raw fields one by one. Never raise.
        req = HeartbeatRequest()
        for k in ("deviceId", "sessionId", "appVersion", "rev", "platform"):
            v = raw.get(k)
            if isinstance(v, str):
                setattr(req, k, v)
        v_states = raw.get("states")
        if isinstance(v_states, list):
            req.states = [s for s in v_states if isinstance(s, str)]

    account_id = current_user.account_id or current_user.uid
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=PRESENCE_TTL_SECONDS)

    # [HOTFIX 2026-05-05] Tolerate missing deviceId from older iOS builds.
    # Use a stable per-account fallback so multiple devices on the same
    # account share a row instead of producing 422.
    effective_device_id = (req.deviceId or "").strip() or f"ios-{current_user.uid[:12]}"

    # Build presence document
    presence_data = {
        "accountId": account_id,
        "userId": current_user.uid,
        "deviceId": effective_device_id,
        "sessionId": req.sessionId,
        "states": req.states,
        "appVersion": req.appVersion,
        "rev": req.rev,
        "platform": getattr(req, "platform", None),
        "updatedAt": now,
        "expiresAt": expires_at,
    }

    # Store in presence collection with deviceId as key
    # This allows multiple devices per user. Wrap in try/except so that
    # any Firestore hiccup also returns 200 — heartbeat is non-load-bearing.
    try:
        doc_ref = db.collection(PRESENCE_COLLECTION).document(f"{account_id}_{effective_device_id}")
        doc_ref.set(presence_data)
    except Exception as e:
        logger.warning("[Presence] heartbeat firestore write failed (returning 200 anyway): %s", e)

    logger.debug(f"[Presence] Heartbeat from {account_id}/{effective_device_id}: {req.states}")

    return HeartbeatResponse(
        ok=True,
        drainMode=is_drain_mode(),
        expiresAt=expires_at,
    )


@router.delete("/presence/heartbeat")
async def clear_presence(
    deviceId: Optional[str] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Clear presence when app goes to background or operation completes.

    [HOTFIX 2026-05-08 V-040 part 2] ``deviceId`` made Optional. The
    POST handler hotfix landed earlier in this file but the DELETE
    handler still required ``deviceId`` as a query string, so iOS
    builds that omit it produced HTTP 422 — keeping the iOS sync loop
    stuck on the same path the POST handler was just freed from.
    Falls back to the same per-account device id used by the POST
    handler so the delete targets the same Firestore document.
    """
    account_id = current_user.account_id or current_user.uid
    effective_device_id = (deviceId or "").strip() or f"ios-{current_user.uid[:12]}"
    try:
        doc_ref = db.collection(PRESENCE_COLLECTION).document(f"{account_id}_{effective_device_id}")
        doc_ref.delete()
    except Exception as e:
        # Best-effort: never 5xx out of clear_presence. iOS retries on failure
        # and the worst case is a stale document expiring via the TTL.
        logger.warning("[Presence] clear_presence firestore delete failed: %s", e)
    return {"ok": True}

    logger.debug(f"[Presence] Cleared presence for {account_id}/{deviceId}")

    return {"ok": True}


# ============================================================================
# Active Summary Endpoint (Admin/Ops)
# ============================================================================

@router.get("/ops/active_summary", response_model=ActiveSummary)
async def get_active_summary(
    _admin: dict = Depends(require_admin),
):
    """
    Get summary of active operations for deployment safety.

    Used by promote script to check if it's safe to promote canary to stable.

    Safe to promote when:
    - activeTotal == 0 (no active presences)
    - runningJobs == 0 (no jobs in progress)
    - activeStreams == 0 (no active STT connections)
    """
    now = datetime.now(timezone.utc)

    # Count active presences (not expired)
    active_presences = []
    by_state: Dict[str, int] = {}

    try:
        presence_docs = db.collection(PRESENCE_COLLECTION).where(
            "expiresAt", ">", now
        ).stream()

        for doc in presence_docs:
            data = doc.to_dict()
            active_presences.append(data)

            # Count by state
            for state in data.get("states", []):
                by_state[state] = by_state.get(state, 0) + 1
    except Exception as e:
        logger.error(f"[Ops] Error fetching presences: {e}")

    # Count active STT streams
    active_streams = 0
    try:
        stream_docs = db.collection("active_streams").stream()
        for doc in stream_docs:
            data = doc.to_dict()
            # Check if not stale (updated within last 5 minutes)
            updated_at = data.get("updatedAt")
            if updated_at:
                if hasattr(updated_at, 'timestamp'):
                    if (now.timestamp() - updated_at.timestamp()) < 300:
                        active_streams += 1
    except Exception as e:
        logger.error(f"[Ops] Error fetching active_streams: {e}")

    # Count running/queued jobs
    running_jobs = 0
    queued_jobs = 0
    try:
        # Check root jobs collection
        jobs_docs = db.collection("jobs").where("status", "==", "running").stream()
        for doc in jobs_docs:
            data = doc.to_dict()
            # Verify not stale (updated within last 10 minutes)
            updated_at = data.get("updatedAt")
            if updated_at:
                if hasattr(updated_at, 'timestamp'):
                    if (now.timestamp() - updated_at.timestamp()) < 600:
                        running_jobs += 1

        queued_docs = db.collection("jobs").where("status", "==", "queued").stream()
        for doc in queued_docs:
            queued_jobs += 1
    except Exception as e:
        logger.error(f"[Ops] Error fetching jobs: {e}")

    # Determine if safe to promote
    drain_mode = is_drain_mode()
    safe_to_promote = (
        drain_mode and  # Must be in drain mode first
        len(active_presences) == 0 and
        active_streams == 0 and
        running_jobs == 0
        # Note: queued_jobs can exist, they'll be processed by the new revision
    )

    return ActiveSummary(
        activeTotal=len(active_presences),
        byState=by_state,
        activeStreams=active_streams,
        runningJobs=running_jobs,
        queuedJobs=queued_jobs,
        drainMode=drain_mode,
        safeToPromote=safe_to_promote,
        updatedAt=now,
    )


# ============================================================================
# Drain Mode Management (Admin)
# ============================================================================

@router.post("/ops/drain", response_model=AppConfigResponse)
async def set_drain(
    req: DrainModeRequest,
    _admin: dict = Depends(require_admin),
):
    """
    Enable or disable drain mode.

    When enabled:
    - New cloud operations are blocked (return 503)
    - Existing operations can complete
    - Read-only operations are allowed

    Use this before promoting canary to stable.
    """
    result = set_drain_mode(req.enabled, req.message)
    logger.info(f"[Ops] Drain mode set to {req.enabled}")
    return result


@router.get("/ops/drain", response_model=Dict[str, Any])
async def get_drain_status(
    _admin: dict = Depends(require_admin),
):
    """
    Get current drain mode status.
    """
    config = get_app_config()
    return {
        "drainMode": is_drain_mode(),
        "maintenance": config.maintenance.model_dump(),
    }
