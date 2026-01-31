"""
App Configuration Service
Provides maintenance mode, feature flags, and kill switch functionality.

Configuration is stored in Firestore at:
  - config/app_config (main config document)

The config can be updated via admin API or directly in Firestore console for emergencies.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field

from app.firebase import db

logger = logging.getLogger("app.config")

# ============================================================================
# Models
# ============================================================================

class MaintenanceInfo(BaseModel):
    """Maintenance mode configuration"""
    enabled: bool = False
    title: Optional[str] = None
    message: Optional[str] = None
    message_ja: Optional[str] = None
    message_en: Optional[str] = None
    eta: Optional[datetime] = None  # Estimated time of availability
    allowLimitedMode: bool = True  # If true, some features still work
    contactUrl: Optional[str] = None
    statusPageUrl: Optional[str] = None


class FeatureFlags(BaseModel):
    """Feature-level kill switches"""
    recording: bool = True
    cloudSync: bool = True
    cloudStt: bool = True
    summarization: bool = True
    quiz: bool = True
    payment: bool = True
    export: bool = True
    youtubeImport: bool = True
    share: bool = True
    reactions: bool = True
    search: bool = True


class AppConfigResponse(BaseModel):
    """Response model for GET /app-config"""
    status: str = "ok"  # "ok" | "maintenance" | "degraded"
    generatedAt: datetime
    maintenance: MaintenanceInfo
    minAppVersion: Optional[str] = None  # Force update if app version < this
    recommendedAppVersion: Optional[str] = None
    featureFlags: FeatureFlags
    announcements: List[Dict[str, Any]] = []  # Optional in-app announcements


class AppConfigUpdate(BaseModel):
    """Request model for updating app config"""
    maintenance: Optional[MaintenanceInfo] = None
    minAppVersion: Optional[str] = None
    recommendedAppVersion: Optional[str] = None
    featureFlags: Optional[FeatureFlags] = None
    announcements: Optional[List[Dict[str, Any]]] = None


# ============================================================================
# Config Document Reference
# ============================================================================

CONFIG_DOC_PATH = "config/app_config"


def _config_doc_ref():
    """Get reference to the app config document"""
    return db.collection("config").document("app_config")


# ============================================================================
# Service Functions
# ============================================================================

def get_app_config() -> AppConfigResponse:
    """
    Retrieve current app configuration from Firestore.
    Returns default config if document doesn't exist.
    """
    doc_ref = _config_doc_ref()
    doc = doc_ref.get()

    now = datetime.now(timezone.utc)

    if not doc.exists:
        # Return default configuration
        return AppConfigResponse(
            status="ok",
            generatedAt=now,
            maintenance=MaintenanceInfo(),
            featureFlags=FeatureFlags(),
        )

    data = doc.to_dict()

    # Parse maintenance info
    maint_data = data.get("maintenance", {})
    maintenance = MaintenanceInfo(
        enabled=maint_data.get("enabled", False),
        title=maint_data.get("title"),
        message=maint_data.get("message"),
        message_ja=maint_data.get("message_ja"),
        message_en=maint_data.get("message_en"),
        eta=maint_data.get("eta"),
        allowLimitedMode=maint_data.get("allowLimitedMode", True),
        contactUrl=maint_data.get("contactUrl"),
        statusPageUrl=maint_data.get("statusPageUrl"),
    )

    # Parse feature flags
    flags_data = data.get("featureFlags", {})
    feature_flags = FeatureFlags(
        recording=flags_data.get("recording", True),
        cloudSync=flags_data.get("cloudSync", True),
        cloudStt=flags_data.get("cloudStt", True),
        summarization=flags_data.get("summarization", True),
        quiz=flags_data.get("quiz", True),
        payment=flags_data.get("payment", True),
        export=flags_data.get("export", True),
        youtubeImport=flags_data.get("youtubeImport", True),
        share=flags_data.get("share", True),
        reactions=flags_data.get("reactions", True),
        search=flags_data.get("search", True),
    )

    # Determine overall status
    if maintenance.enabled and not maintenance.allowLimitedMode:
        status = "maintenance"
    elif maintenance.enabled:
        status = "degraded"
    elif not all([
        feature_flags.cloudSync,
        feature_flags.summarization,
        feature_flags.payment,
    ]):
        status = "degraded"
    else:
        status = "ok"

    return AppConfigResponse(
        status=status,
        generatedAt=now,
        maintenance=maintenance,
        minAppVersion=data.get("minAppVersion"),
        recommendedAppVersion=data.get("recommendedAppVersion"),
        featureFlags=feature_flags,
        announcements=data.get("announcements", []),
    )


def update_app_config(update: AppConfigUpdate) -> AppConfigResponse:
    """
    Update app configuration in Firestore.
    Only updates fields that are provided (merge).
    """
    doc_ref = _config_doc_ref()

    update_data = {
        "updatedAt": datetime.now(timezone.utc),
    }

    if update.maintenance is not None:
        update_data["maintenance"] = update.maintenance.model_dump(exclude_none=True)

    if update.minAppVersion is not None:
        update_data["minAppVersion"] = update.minAppVersion

    if update.recommendedAppVersion is not None:
        update_data["recommendedAppVersion"] = update.recommendedAppVersion

    if update.featureFlags is not None:
        update_data["featureFlags"] = update.featureFlags.model_dump()

    if update.announcements is not None:
        update_data["announcements"] = update.announcements

    doc_ref.set(update_data, merge=True)
    logger.info(f"[AppConfig] Updated config: {list(update_data.keys())}")

    return get_app_config()


def set_maintenance_mode(
    enabled: bool,
    title: Optional[str] = None,
    message: Optional[str] = None,
    eta: Optional[datetime] = None,
    allow_limited_mode: bool = True,
) -> AppConfigResponse:
    """
    Quick helper to enable/disable maintenance mode.
    """
    maintenance = MaintenanceInfo(
        enabled=enabled,
        title=title or ("メンテナンス中" if enabled else None),
        message=message,
        eta=eta,
        allowLimitedMode=allow_limited_mode,
    )
    return update_app_config(AppConfigUpdate(maintenance=maintenance))


def set_feature_flag(feature: str, enabled: bool) -> AppConfigResponse:
    """
    Quick helper to toggle a single feature flag.
    """
    doc_ref = _config_doc_ref()
    doc_ref.set({
        f"featureFlags.{feature}": enabled,
        "updatedAt": datetime.now(timezone.utc),
    }, merge=True)
    logger.info(f"[AppConfig] Set feature flag: {feature}={enabled}")
    return get_app_config()


def is_feature_enabled(feature: str) -> bool:
    """
    Check if a specific feature is enabled.
    Used by feature gate middleware/dependencies.

    Returns True by default if config doesn't exist (fail-open for resilience).
    """
    try:
        config = get_app_config()

        # If hard maintenance, all features are disabled
        if config.maintenance.enabled and not config.maintenance.allowLimitedMode:
            return False

        # Check specific feature flag
        flags = config.featureFlags
        return getattr(flags, feature, True)
    except Exception as e:
        logger.error(f"[AppConfig] Error checking feature flag {feature}: {e}")
        # Fail-open: if we can't read config, allow the feature
        return True


def get_maintenance_error_response(feature: str = None) -> dict:
    """
    Get a standardized error response for maintenance/disabled features.
    """
    config = get_app_config()

    if config.maintenance.enabled:
        return {
            "error": "MAINTENANCE",
            "code": "MAINTENANCE_MODE",
            "title": config.maintenance.title,
            "message": config.maintenance.message or "サービスは現在メンテナンス中です",
            "eta": config.maintenance.eta.isoformat() if config.maintenance.eta else None,
            "allowLimitedMode": config.maintenance.allowLimitedMode,
        }

    if feature:
        return {
            "error": "FEATURE_DISABLED",
            "code": f"FEATURE_DISABLED_{feature.upper()}",
            "message": f"この機能は現在利用できません: {feature}",
        }

    return {
        "error": "SERVICE_UNAVAILABLE",
        "code": "SERVICE_UNAVAILABLE",
        "message": "サービスは現在利用できません",
    }
