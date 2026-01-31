"""
Feature Gate Dependencies

Provides FastAPI dependencies for checking feature flags before allowing API access.

Usage in routes:

    from app.feature_gate import require_feature, FeatureName

    @router.post("/sessions/{session_id}/summarize")
    async def summarize(
        session_id: str,
        _gate=Depends(require_feature(FeatureName.SUMMARIZATION)),
    ):
        # This endpoint is blocked if summarization feature is disabled
        ...

Or for simpler cases:

    from app.feature_gate import gate_summarization

    @router.post("/sessions/{session_id}/summarize")
    async def summarize(
        session_id: str,
        _gate=Depends(gate_summarization),
    ):
        ...
"""

import logging
from enum import Enum
from typing import Callable

from fastapi import HTTPException, Depends

from app.services.app_config import (
    is_feature_enabled,
    get_maintenance_error_response,
    get_app_config,
)

logger = logging.getLogger("app.feature_gate")


class FeatureName(str, Enum):
    """Feature names that can be gated"""
    RECORDING = "recording"
    CLOUD_SYNC = "cloudSync"
    CLOUD_STT = "cloudStt"
    SUMMARIZATION = "summarization"
    QUIZ = "quiz"
    PAYMENT = "payment"
    EXPORT = "export"
    YOUTUBE_IMPORT = "youtubeImport"
    SHARE = "share"
    REACTIONS = "reactions"
    SEARCH = "search"


def require_feature(feature: FeatureName) -> Callable:
    """
    Factory function that creates a dependency for checking a specific feature.

    Usage:
        @router.post("/endpoint")
        async def endpoint(_gate=Depends(require_feature(FeatureName.SUMMARIZATION))):
            ...
    """
    async def check_feature():
        if not is_feature_enabled(feature.value):
            error_response = get_maintenance_error_response(feature.value)
            logger.warning(f"[FeatureGate] Blocked request: feature={feature.value}")
            raise HTTPException(
                status_code=503,
                detail=error_response,
            )
        return True

    return check_feature


def require_not_maintenance() -> Callable:
    """
    Dependency that blocks requests during hard maintenance mode.

    Usage:
        @router.post("/critical-endpoint")
        async def endpoint(_gate=Depends(require_not_maintenance())):
            ...
    """
    async def check_maintenance():
        config = get_app_config()
        if config.maintenance.enabled and not config.maintenance.allowLimitedMode:
            error_response = get_maintenance_error_response()
            logger.warning("[FeatureGate] Blocked request: hard maintenance mode")
            raise HTTPException(
                status_code=503,
                detail=error_response,
            )
        return True

    return check_maintenance


# ============================================================================
# Pre-built Dependencies (Convenience)
# ============================================================================

async def gate_recording():
    """Dependency: blocks if recording feature is disabled"""
    if not is_feature_enabled("recording"):
        raise HTTPException(503, detail=get_maintenance_error_response("recording"))
    return True


async def gate_cloud_sync():
    """Dependency: blocks if cloudSync feature is disabled"""
    if not is_feature_enabled("cloudSync"):
        raise HTTPException(503, detail=get_maintenance_error_response("cloudSync"))
    return True


async def gate_cloud_stt():
    """Dependency: blocks if cloudStt feature is disabled"""
    if not is_feature_enabled("cloudStt"):
        raise HTTPException(503, detail=get_maintenance_error_response("cloudStt"))
    return True


async def gate_summarization():
    """Dependency: blocks if summarization feature is disabled"""
    if not is_feature_enabled("summarization"):
        raise HTTPException(503, detail=get_maintenance_error_response("summarization"))
    return True


async def gate_quiz():
    """Dependency: blocks if quiz feature is disabled"""
    if not is_feature_enabled("quiz"):
        raise HTTPException(503, detail=get_maintenance_error_response("quiz"))
    return True


async def gate_payment():
    """Dependency: blocks if payment feature is disabled"""
    if not is_feature_enabled("payment"):
        raise HTTPException(503, detail=get_maintenance_error_response("payment"))
    return True


async def gate_export():
    """Dependency: blocks if export feature is disabled"""
    if not is_feature_enabled("export"):
        raise HTTPException(503, detail=get_maintenance_error_response("export"))
    return True


async def gate_youtube_import():
    """Dependency: blocks if youtubeImport feature is disabled"""
    if not is_feature_enabled("youtubeImport"):
        raise HTTPException(503, detail=get_maintenance_error_response("youtubeImport"))
    return True


async def gate_share():
    """Dependency: blocks if share feature is disabled"""
    if not is_feature_enabled("share"):
        raise HTTPException(503, detail=get_maintenance_error_response("share"))
    return True


async def gate_reactions():
    """Dependency: blocks if reactions feature is disabled"""
    if not is_feature_enabled("reactions"):
        raise HTTPException(503, detail=get_maintenance_error_response("reactions"))
    return True


async def gate_search():
    """Dependency: blocks if search feature is disabled"""
    if not is_feature_enabled("search"):
        raise HTTPException(503, detail=get_maintenance_error_response("search"))
    return True


async def gate_hard_maintenance():
    """Dependency: blocks during hard maintenance (all features off)"""
    config = get_app_config()
    if config.maintenance.enabled and not config.maintenance.allowLimitedMode:
        raise HTTPException(503, detail=get_maintenance_error_response())
    return True
