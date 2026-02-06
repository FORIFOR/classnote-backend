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

from fastapi import APIRouter, Depends, HTTPException
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
    """Request model for presence heartbeat"""
    deviceId: str = Field(..., description="Unique device identifier")
    sessionId: Optional[str] = Field(None, description="Current session ID if any")
    states: List[str] = Field(
        default_factory=list,
        description="Active states: cloud_stt_active, summarization_active, upload_active, etc."
    )
    appVersion: Optional[str] = Field(None, description="App version")
    rev: Optional[str] = Field(None, description="Backend revision: stable or canary")


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
    req: HeartbeatRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Send a heartbeat to indicate active presence.

    iOS should call this:
    - When starting a cloud operation (STT, summary, upload)
    - Every 60 seconds while the operation is ongoing
    - When the operation completes (with empty states to clear)

    The presence automatically expires after 90 seconds if no heartbeat is received.
    """
    account_id = current_user.account_id or current_user.uid
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=PRESENCE_TTL_SECONDS)

    # Build presence document
    presence_data = {
        "accountId": account_id,
        "userId": current_user.uid,
        "deviceId": req.deviceId,
        "sessionId": req.sessionId,
        "states": req.states,
        "appVersion": req.appVersion,
        "rev": req.rev,
        "updatedAt": now,
        "expiresAt": expires_at,
    }

    # Store in presence collection with deviceId as key
    # This allows multiple devices per user
    doc_ref = db.collection(PRESENCE_COLLECTION).document(f"{account_id}_{req.deviceId}")
    doc_ref.set(presence_data)

    logger.debug(f"[Presence] Heartbeat from {account_id}/{req.deviceId}: {req.states}")

    return HeartbeatResponse(
        ok=True,
        drainMode=is_drain_mode(),
        expiresAt=expires_at,
    )


@router.delete("/presence/heartbeat")
async def clear_presence(
    deviceId: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Clear presence when app goes to background or operation completes.
    """
    account_id = current_user.account_id or current_user.uid
    doc_ref = db.collection(PRESENCE_COLLECTION).document(f"{account_id}_{deviceId}")
    doc_ref.delete()

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
