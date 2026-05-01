"""
Orb Theme API — server-driven orb appearance.

Firestore structure:
- config/orbTheme         → global theme config (mode, defaultThemeId)
- orb_themes/{themeId}    → theme definitions (colors, assets, targeting)
- orb_overrides/{uid}     → per-user theme override

GET /orb-theme  → resolves the active theme for the current user
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.firebase import db
from app.dependencies import get_current_user, get_current_user_optional, CurrentUser

logger = logging.getLogger("app.routes.orb_theme")
router = APIRouter()

# ---------------------------------------------------------------------------
# In-memory cache for global theme (config + campaign themes)
# Per-user overrides are NOT cached (low volume, user-specific)
# ---------------------------------------------------------------------------
_global_theme_cache: Optional[Dict[str, Any]] = None
_global_theme_cache_ts: float = 0.0
_GLOBAL_THEME_CACHE_TTL = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------

class OrbCoreConfig(BaseModel):
    """Center disc configuration."""
    opacity: float = 0.85                          # core image draw opacity
    gradientStart: Optional[str] = None            # hex e.g. "#FAD9E1"
    gradientEnd: Optional[str] = None              # hex e.g. "#F6C1CF"
    fallbackAssetName: Optional[str] = None        # bundled asset name (e.g. "core_spring")


class OrbRingConfig(BaseModel):
    """Ring configuration."""
    colors: List[str] = []             # hex array for angular gradient
    glowColor: Optional[str] = None    # hex for glow field


class OrbParticleConfig(BaseModel):
    """Particle configuration."""
    color: Optional[str] = None        # hex color
    minCount: int = 4
    maxCount: int = 16


class OrbThemeResponse(BaseModel):
    """Resolved orb theme returned to client."""
    themeId: str
    label: Optional[str] = None
    core: OrbCoreConfig = OrbCoreConfig()
    ring: OrbRingConfig = OrbRingConfig()
    particle: OrbParticleConfig = OrbParticleConfig()
    texture: Optional[str] = None      # "sakura" | "ocean" | "maple" | "snow" | null
    expiresAt: Optional[str] = None    # ISO 8601 — client should re-fetch after this


# ---------------------------------------------------------------------------
# Default seasonal themes (fallback when Firestore has no data)
# ---------------------------------------------------------------------------

_DEFAULT_THEME: Dict[str, Any] = {
    "themeId": "default_monochrome",
    "label": "モノクロ",
    "core": {},
    "ring": {"colors": ["#FFFFFF"], "glowColor": "#67758C"},
    "particle": {},
}


def _current_season() -> str:
    """Return current season based on UTC month."""
    month = datetime.now(timezone.utc).month
    if month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    elif month in (9, 10, 11):
        return "autumn"
    else:
        return "winter"


# ---------------------------------------------------------------------------
# Theme Resolution
# ---------------------------------------------------------------------------

def _resolve_global_theme() -> dict:
    """Resolve the global theme (steps 2-5) with in-memory caching.

    Cached for 10 minutes to minimize Firestore reads.
    """
    global _global_theme_cache, _global_theme_cache_ts

    now_ts = time.monotonic()
    if _global_theme_cache is not None and (now_ts - _global_theme_cache_ts) < _GLOBAL_THEME_CACHE_TTL:
        return _global_theme_cache

    theme = _resolve_global_theme_uncached()
    _global_theme_cache = theme
    _global_theme_cache_ts = now_ts
    return theme


def _resolve_global_theme_uncached() -> dict:
    """Fetch global theme from Firestore (steps 2-5)."""
    now = datetime.now(timezone.utc)
    season = _current_season()

    # 2-4. Global config
    try:
        config_ref = db.collection("config").document("orbTheme")
        config_snap = config_ref.get()
        config = config_snap.to_dict() if config_snap.exists else {}
    except Exception as e:
        logger.warning(f"[OrbTheme] Failed to load config/orbTheme: {e}")
        config = {}

    mode = config.get("mode", "auto")  # "force" | "auto"
    logger.info(f"[OrbTheme] Config: mode={mode} themeId={config.get('themeId')} defaultThemeId={config.get('defaultThemeId')} season={season}")

    # 2. Forced theme
    if mode == "force":
        forced_id = config.get("themeId")
        if forced_id:
            theme = _load_theme(forced_id)
            if theme:
                logger.info(f"[OrbTheme] Using forced theme: {forced_id}")
                return theme

    # 3. Active campaign themes (by date + targeting)
    try:
        themes_ref = db.collection("orb_themes")\
            .where("enabled", "==", True)\
            .order_by("priority", direction="DESCENDING")\
            .limit(10)
        campaign_count = 0
        for doc in themes_ref.stream():
            campaign_count += 1
            td = doc.to_dict() or {}
            # Check date range
            valid_from = td.get("validFrom")
            valid_to = td.get("validTo")
            if valid_from and hasattr(valid_from, "timestamp") and valid_from.timestamp() > now.timestamp():
                logger.debug(f"[OrbTheme] Skipping {doc.id}: not yet valid")
                continue
            if valid_to and hasattr(valid_to, "timestamp") and valid_to.timestamp() < now.timestamp():
                logger.debug(f"[OrbTheme] Skipping {doc.id}: expired")
                continue
            # Check targeting (simple season match for now)
            targeting = td.get("targeting") or {}
            seasons = targeting.get("seasons")
            if seasons and season not in seasons:
                logger.debug(f"[OrbTheme] Skipping {doc.id}: season {season} not in {seasons}")
                continue
            logger.info(f"[OrbTheme] Using campaign theme: {doc.id} (season={season})")
            return _theme_from_doc(doc.id, td)
        logger.info(f"[OrbTheme] No matching campaign theme (scanned {campaign_count})")
    except Exception as e:
        logger.warning(f"[OrbTheme] Failed to query campaign themes: {e}")

    # 4. Default theme (from config)
    auto_theme_id = config.get("defaultThemeId")
    if auto_theme_id:
        theme = _load_theme(auto_theme_id)
        if theme:
            logger.info(f"[OrbTheme] Using default theme: {auto_theme_id}")
            return theme
        else:
            logger.warning(f"[OrbTheme] Failed to load default theme: {auto_theme_id}")

    # 5. Season map fallback
    season_map = config.get("seasonMap") or {}
    season_theme_id = season_map.get(season)
    if season_theme_id:
        theme = _load_theme(season_theme_id)
        if theme:
            logger.info(f"[OrbTheme] Using season map theme: {season_theme_id}")
            return theme

    # 6. Hardcoded default — monochrome
    logger.info("[OrbTheme] Using hardcoded default: default_monochrome")
    return _DEFAULT_THEME


def _resolve_theme(uid: Optional[str]) -> dict:
    """Resolve the active orb theme for a user.

    Priority:
    1. User override (orb_overrides/{uid})
    2. Forced theme (config/orbTheme.mode == "force")
    3. Targeted campaign themes (orb_themes with valid dates + targeting)
    4. Season-based auto theme (config/orbTheme.mode == "auto" or default)
    5. Hardcoded seasonal defaults
    """
    # 1. User override
    if uid:
        try:
            override_ref = db.collection("orb_overrides").document(uid)
            override_snap = override_ref.get()
            if override_snap.exists:
                od = override_snap.to_dict() or {}
                theme_id = od.get("themeId")
                if theme_id:
                    theme = _load_theme(theme_id)
                    if theme:
                        return theme
        except Exception as e:
            logger.warning(f"[OrbTheme] Failed to load user override for {uid}: {e}")

    # 2-5. Global theme (cached)
    return _resolve_global_theme()


def _load_theme(theme_id: str) -> Optional[dict]:
    """Load a single theme document from Firestore."""
    try:
        ref = db.collection("orb_themes").document(theme_id)
        snap = ref.get()
        if snap.exists:
            return _theme_from_doc(snap.id, snap.to_dict() or {})
    except Exception as e:
        logger.warning(f"[OrbTheme] Failed to load theme {theme_id}: {e}")
    return None


def _theme_from_doc(doc_id: str, data: dict) -> dict:
    """Convert a Firestore theme document to API response dict."""
    ui = data.get("ui") or data
    core = ui.get("core") or {}
    ring = ui.get("ring") or {}
    particle = ui.get("particle") or {}

    result = {
        "themeId": doc_id,
        "label": data.get("label"),
        "core": {
            "opacity": core.get("opacity", 0.85),
            "gradientStart": core.get("gradientStart"),
            "gradientEnd": core.get("gradientEnd"),
            "fallbackAssetName": core.get("fallbackAssetName"),
        },
        "ring": {
            "colors": ring.get("colors", []),
            "glowColor": ring.get("glowColor"),
        },
        "particle": {
            "color": particle.get("color"),
            "minCount": particle.get("minCount", 4),
            "maxCount": particle.get("maxCount", 16),
        },
        "texture": ui.get("texture"),
    }

    # Add expiry from validTo if present
    valid_to = data.get("validTo")
    if valid_to and hasattr(valid_to, "isoformat"):
        result["expiresAt"] = valid_to.isoformat()

    return result


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

_ORB_THEME_SAFE_DEFAULT: dict = {
    "themeId": "default",
    "variant": None,
    "colors": None,
    "source": "fallback",
    "version": 1,
}


async def get_orb_theme(
    uid: Optional[str] = None,
    plan: Optional[str] = None,
    current_user: Optional[CurrentUser] = Depends(get_current_user_optional),
):
    """Resolve orb theme. Safe-default on failure.

    Public endpoint: returns seasonal default for anonymous users. With auth,
    checks per-user overrides and campaign targeting.
    """
    try:
        effective_uid = uid or (current_user.uid if current_user else None)
        theme = _resolve_theme(effective_uid)
        logger.info(f"[OrbTheme] Resolved theme={theme.get('themeId')} for uid={effective_uid or 'anon'}")
        from fastapi.responses import JSONResponse
        return JSONResponse(
            content=OrbThemeResponse(**theme).model_dump(mode="json"),
            headers={"Cache-Control": "public, max-age=300"},
        )
    except Exception as exc:
        logger.warning(
            "orb_theme.fallback_to_default",
            extra={"props": {"error": str(exc), "type": type(exc).__name__}},
        )
        from fastapi.responses import JSONResponse
        return JSONResponse(
            content=_ORB_THEME_SAFE_DEFAULT,
            headers={"Cache-Control": "public, max-age=60"},
        )


@router.get("/orb-theme", deprecated=True, include_in_schema=False)
async def legacy_orb_theme_alias(
    request: Request,
    current_user: Optional[CurrentUser] = Depends(get_current_user_optional),
):
    """Deprecated alias of /system/orb-theme."""
    try:
        from app.services.deprecation import log_deprecated_path
        log_deprecated_path(request, replacement="/system/orb-theme")
    except ImportError:
        # deprecation module not present in this build; alias still works
        pass
    return await get_orb_theme(current_user=current_user)
