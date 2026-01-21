from enum import Enum
from typing import Optional, List, Dict, Any, Union
from datetime import datetime
from pydantic import BaseModel

class AdStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"

class AdType(str, Enum):
    BRANDED_LOADING = "branded_loading"

# --- Legacy Support ---
class AdCreative(BaseModel):
    logoUrl: str
    heroUrl: Optional[str] = None
    backgroundHex: Optional[str] = None

# --- Rich Ads v2 Models ---

class AdAsset(BaseModel):
    type: str  # "image" | "video"
    url: str
    posterUrl: Optional[str] = None
    muted: Optional[bool] = None
    loop: Optional[bool] = None
    blurHash: Optional[str] = None

class AdAssets(BaseModel):
    logo: Optional[AdAsset] = None
    hero: Optional[AdAsset] = None
    video: Optional[AdAsset] = None

class AdAction(BaseModel):
    id: str
    style: str  # "primary" | "secondary"
    text: str
    url: str
    openMode: str  # "safari" | "in_app" | "deeplink"
    fallbackUrl: Optional[str] = None

class RenderHints(BaseModel):
    layout: str  # "hero_blur_card" | "hero_full_bleed" | "minimal_card"
    showSponsorBadge: bool = True
    showCountdown: bool = True
    ctaPlacement: str = "card_bottom"
    videoPlacement: str = "inline_in_card"

class Theme(BaseModel):
    accentHex: Optional[str] = None
    surfaceStyle: Optional[str] = "ultraThin"
    cornerRadius: Optional[int] = 22

class Policy(BaseModel):
    minViewSec: int = 10
    maxViewSec: int = 30
    skippableAfterSec: int = 10
    autodismissAtSec: int = 30

class SponsoredAd(BaseModel):
    id: str
    placementId: str
    sponsorName: str
    headline: str
    body: Optional[str] = None
    format: Optional[str] = None  # null (legacy) or "rich_v2"
    
    # Legacy Fields (for backward compatibility)
    ctaText: Optional[str] = None
    clickUrl: Optional[str] = None
    creative: Optional[AdCreative] = None
    minViewSec: Optional[int] = None
    maxViewSec: Optional[int] = None
    
    # v2 Fields
    assets: Optional[AdAssets] = None
    actions: Optional[List[AdAction]] = None
    renderHints: Optional[RenderHints] = None
    theme: Optional[Theme] = None
    policy: Optional[Policy] = None
    
    priority: int = 100

class PlacementResponse(BaseModel):
    ad: Optional[SponsoredAd] = None

class AdEventIn(BaseModel):
    event: str  # impression, click, dismiss, heartbeat
    placement_id: str
    ad_id: str
    session_id: str
    job_id: Optional[str] = None
    ts_ms: int
    meta: Dict[str, Any] = {}
