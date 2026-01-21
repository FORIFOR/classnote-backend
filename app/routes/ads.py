import logging
import uuid
import random
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Header
from google.cloud import firestore

from app.ad_models import (
    SponsoredAd, AdCreative, PlacementResponse, AdEventIn,
    AdAssets, AdAsset, AdAction, RenderHints, Theme, Policy
)
from app.dependencies import get_current_user_optional, User
from app.firebase import db

router = APIRouter()
logger = logging.getLogger("app.routes.ads")

# --- Mock Data for Demo (v2) ---
DEMO_RICH_AD = SponsoredAd(
    id="ad_demo_v2_001",
    placementId="plc_demo_v2_01",
    sponsorName="Classnote Enterprise",
    headline="全社会議の議事録を自動化",
    body="セキュリティと管理機能を強化。チーム全体の生産性を向上させましょう。",
    format="rich_v2",
    
    # Legacy Fallback
    ctaText="詳細はこちら",
    clickUrl="https://classnote.app/enterprise",
    creative=AdCreative(
        logoUrl="https://placehold.co/128x128/2563EB/ffffff?text=Ent",
        heroUrl="https://placehold.co/800x600/1e293b/ffffff?text=Enterprise+Ready"
    ),
    minViewSec=10,
    maxViewSec=30,
    
    # v2 Fields
    assets=AdAssets(
        logo=AdAsset(type="image", url="https://placehold.co/128x128/2563EB/ffffff?text=Ent"),
        hero=AdAsset(type="image", url="https://placehold.co/800x600/0f172a/ffffff?text=Enterprise+Logic", blurHash="L02?IV~q..."),
        # Optional: Add video if needed
        # video=AdAsset(type="video", url="https://example.com/demo.mp4", posterUrl="...", muted=True, loop=True)
    ),
    actions=[
        AdAction(id="primary", style="primary", text="資料請求", url="https://classnote.app/contact", openMode="in_app"),
        AdAction(id="secondary", style="secondary", text="機能一覧", url="https://classnote.app/features", openMode="safari")
    ],
    policy=Policy(
        minViewSec=10,
        maxViewSec=30,
        skippableAfterSec=10,
        autodismissAtSec=30
    ),
    renderHints=RenderHints(
        layout="hero_blur_card",
        showSponsorBadge=True,
        showCountdown=True,
        ctaPlacement="card_bottom"
    ),
    theme=Theme(
        accentHex="#3b82f6",
        surfaceStyle="ultraThin",
        cornerRadius=24
    )
)

@router.get("/ads/placement", response_model=PlacementResponse)
async def get_placement(
    slot: str, 
    session_id: str, 
    job_id: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Get an ad placement for the given slot (e.g., 'summary_generating').
    """
    # 1. Plan Check: If Premium, don't show ads
    if current_user and current_user.plan != "free":
        return PlacementResponse(ad=None)
    
    # 2. Slot Validation
    if slot not in ["summary_generating", "quiz_generating", "app_open"]:
        return PlacementResponse(ad=None)

    # [ADS DISABLED] Per user request (2026-01-19), ads are temporarily disabled.
    # Return empty response to hide ads on client.
    return PlacementResponse(ad=None)

    # --- DISABLED LOGIC BELOW ---
    # 3. Ad Selection (Mock Logic: Return Rich v2 Ad)
    # Simulate partial fill rate (90%)
    # if random.random() < 0.1:
    #     return PlacementResponse(ad=None)
        
    # ad = DEMO_RICH_AD.model_copy(deep=True)
    
    # # Generate unique placement instance ID for tracking
    # placement_id = f"plc_{uuid.uuid4().hex[:12]}"
    # ad.placementId = placement_id
    
    # return PlacementResponse(ad=ad)


@router.post("/ads/events")
async def post_ad_event(
    evt: AdEventIn,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Track ad events (impression, click, dismiss).
    """
    if evt.event not in {"impression", "click", "dismiss", "heartbeat"}:
        raise HTTPException(status_code=400, detail="Invalid event type")

    # Log structure for BigQuery / Firestore export
    log_payload = evt.model_dump()
    log_payload["uid"] = current_user.uid if current_user else "anonymous"
    log_payload["server_ts"] = firestore.SERVER_TIMESTAMP
    
    # Persist to Firestore (ad_events collection)
    try:
        db.collection("ad_events").add(log_payload)
        logger.info(f"[AdEvent] {evt.event} ad={evt.ad_id} plc={evt.placement_id}")
    except Exception as e:
        logger.error(f"Failed to log ad event: {e}")
    
    return {"ok": True}
