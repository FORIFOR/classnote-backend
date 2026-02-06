"""
App Configuration API Routes

Provides:
- GET /app-config - Public endpoint for iOS app to fetch config
- POST /admin/app-config - Admin endpoint to update config
- POST /admin/maintenance - Quick maintenance mode toggle
- POST /admin/feature-flags/{feature} - Quick feature flag toggle
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Header

from app.admin_auth import get_current_admin_user
from app.services.app_config import (
    get_app_config,
    update_app_config,
    set_maintenance_mode,
    set_feature_flag,
    AppConfigResponse,
    AppConfigUpdate,
    MaintenanceInfo,
    FeatureFlags,
)

router = APIRouter()
logger = logging.getLogger("app.routes.app_config")


# ============================================================================
# Public Endpoint (for iOS app)
# ============================================================================

@router.get("/app-config", response_model=AppConfigResponse)
async def get_config(
    platform: Optional[str] = Query(None, description="Client platform (ios, android, web)"),
    version: Optional[str] = Query(None, description="Client app version"),
    build: Optional[str] = Query(None, description="Client build number"),
    lang: Optional[str] = Query("ja", description="Preferred language"),
):
    """
    Get current app configuration.

    This endpoint is called by the iOS app on:
    - App launch (during splash)
    - Foreground resume
    - Periodically (every 30-120 seconds)

    Response includes:
    - maintenance: Maintenance mode status and message
    - featureFlags: Per-feature kill switches
    - minAppVersion: Force update if client version is lower
    - announcements: In-app announcements

    Cache-Control header is set to allow short caching (30s) for performance,
    but still refresh frequently enough for quick emergency updates.
    """
    config = get_app_config()

    # Log for monitoring (useful for seeing version distribution)
    if platform or version:
        logger.info(f"[/app-config] platform={platform} version={version} build={build}")

    # Return with cache headers
    from fastapi.responses import JSONResponse
    response = JSONResponse(
        content=config.model_dump(mode="json"),
        headers={
            "Cache-Control": "public, max-age=30",  # 30 second cache
            "X-Generated-At": config.generatedAt.isoformat(),
        }
    )
    return response


# ============================================================================
# Admin Endpoints
# ============================================================================

@router.post("/admin/app-config", response_model=AppConfigResponse, include_in_schema=False)
async def admin_update_config(
    update: AppConfigUpdate,
    _admin=Depends(get_current_admin_user),
):
    """
    Update app configuration (admin only).

    Supports partial updates - only provided fields are changed.
    """
    logger.info(f"[Admin] Updating app config: {update.model_dump(exclude_none=True)}")
    return update_app_config(update)


@router.post("/admin/maintenance", response_model=AppConfigResponse, include_in_schema=False)
async def admin_toggle_maintenance(
    enabled: bool,
    title: Optional[str] = None,
    message: Optional[str] = None,
    eta: Optional[datetime] = None,
    allow_limited_mode: bool = True,
    _admin=Depends(get_current_admin_user),
):
    """
    Quick toggle for maintenance mode (admin only).

    Examples:
    - Enable hard maintenance: enabled=true, allow_limited_mode=false
    - Enable soft maintenance: enabled=true, allow_limited_mode=true
    - Disable maintenance: enabled=false
    """
    logger.info(f"[Admin] Setting maintenance mode: enabled={enabled}, limited={allow_limited_mode}")
    return set_maintenance_mode(
        enabled=enabled,
        title=title,
        message=message,
        eta=eta,
        allow_limited_mode=allow_limited_mode,
    )


@router.post("/admin/feature-flags/{feature}", response_model=AppConfigResponse, include_in_schema=False)
async def admin_toggle_feature(
    feature: str,
    enabled: bool,
    _admin=Depends(get_current_admin_user),
):
    """
    Toggle a single feature flag (admin only).

    Valid features:
    - recording
    - cloudSync
    - cloudStt
    - summarization
    - quiz
    - payment
    - export
    - youtubeImport
    - share
    - reactions
    - search
    """
    valid_features = [
        "recording", "cloudSync", "cloudStt", "summarization", "quiz",
        "payment", "export", "youtubeImport", "share", "reactions", "search"
    ]

    if feature not in valid_features:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid feature: {feature}. Valid features: {valid_features}"
        )

    logger.info(f"[Admin] Setting feature flag: {feature}={enabled}")
    return set_feature_flag(feature, enabled)


@router.get("/admin/app-config", response_model=AppConfigResponse, include_in_schema=False)
async def admin_get_config(_admin=Depends(get_current_admin_user)):
    """
    Get current app configuration (admin only, bypasses cache).
    """
    return get_app_config()


# ============================================================================
# Announcement Management
# ============================================================================

from pydantic import BaseModel, Field
from typing import List
import uuid


class AnnouncementCreate(BaseModel):
    """Request model for creating an announcement."""
    type: str = Field("info", description="Type: info, warning")
    title: str = Field(..., description="Title (Japanese)")
    title_en: Optional[str] = Field(None, description="Title (English)")
    message: str = Field(..., description="Message (Japanese)")
    message_en: Optional[str] = Field(None, description="Message (English)")
    dismissible: bool = Field(True, description="Can user dismiss this?")
    priority: int = Field(1, description="Higher = shown first")


class AnnouncementResponse(BaseModel):
    """Response model for an announcement."""
    id: str
    type: str
    title: str
    title_en: Optional[str]
    message: str
    message_en: Optional[str]
    dismissible: bool
    priority: int
    createdAt: str


@router.get("/admin/announcements", include_in_schema=False)
async def list_announcements(_admin=Depends(get_current_admin_user)):
    """
    List all current announcements.
    """
    config = get_app_config()
    return {"announcements": config.announcements}


@router.post("/admin/announcements", include_in_schema=False)
async def create_announcement(
    req: AnnouncementCreate,
    _admin=Depends(get_current_admin_user),
):
    """
    Create a new announcement.

    Type must be 'info' or 'warning' (iOS app doesn't support 'maintenance').

    Example:
        POST /admin/announcements
        {
            "type": "warning",
            "title": "メンテナンスのお知らせ",
            "title_en": "Maintenance Notice",
            "message": "本日深夜にメンテナンスを実施します",
            "message_en": "Maintenance scheduled tonight",
            "dismissible": true,
            "priority": 10
        }
    """
    from app.firebase import db

    # Validate type (iOS only supports info/warning)
    if req.type not in ["info", "warning"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type: {req.type}. Must be 'info' or 'warning'"
        )

    now = datetime.now(timezone.utc)
    announcement = {
        "id": f"ann-{uuid.uuid4().hex[:8]}",
        "type": req.type,
        "title": req.title,
        "title_en": req.title_en,
        "message": req.message,
        "message_en": req.message_en,
        "dismissible": req.dismissible,
        "priority": req.priority,
        "createdAt": now.isoformat(),
    }

    # Get current announcements
    config_ref = db.collection("config").document("app_config")
    doc = config_ref.get()
    current_data = doc.to_dict() if doc.exists else {}
    announcements = current_data.get("announcements", [])

    # Add new announcement at top (sorted by priority later by client)
    announcements.insert(0, announcement)

    # Update
    config_ref.set({
        "announcements": announcements,
        "updatedAt": now,
    }, merge=True)

    logger.info(f"[Admin] Created announcement: {announcement['id']} - {req.title}")

    return {"ok": True, "announcement": announcement}


@router.delete("/admin/announcements/{announcement_id}", include_in_schema=False)
async def delete_announcement(
    announcement_id: str,
    _admin=Depends(get_current_admin_user),
):
    """
    Delete an announcement by ID.
    """
    from app.firebase import db

    config_ref = db.collection("config").document("app_config")
    doc = config_ref.get()
    current_data = doc.to_dict() if doc.exists else {}
    announcements = current_data.get("announcements", [])

    # Find and remove
    original_count = len(announcements)
    announcements = [a for a in announcements if a.get("id") != announcement_id]

    if len(announcements) == original_count:
        raise HTTPException(status_code=404, detail="Announcement not found")

    # Update
    config_ref.set({
        "announcements": announcements,
        "updatedAt": datetime.now(timezone.utc),
    }, merge=True)

    logger.info(f"[Admin] Deleted announcement: {announcement_id}")

    return {"ok": True, "deleted": announcement_id}


@router.delete("/admin/announcements", include_in_schema=False)
async def clear_all_announcements(_admin=Depends(get_current_admin_user)):
    """
    Delete ALL announcements.
    """
    from app.firebase import db

    config_ref = db.collection("config").document("app_config")
    config_ref.set({
        "announcements": [],
        "updatedAt": datetime.now(timezone.utc),
    }, merge=True)

    logger.info("[Admin] Cleared all announcements")

    return {"ok": True, "message": "All announcements cleared"}
