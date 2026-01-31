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

from app.admin_auth import require_admin
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
    _admin=Depends(require_admin),
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
    _admin=Depends(require_admin),
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
    _admin=Depends(require_admin),
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
async def admin_get_config(_admin=Depends(require_admin)):
    """
    Get current app configuration (admin only, bypasses cache).
    """
    return get_app_config()
