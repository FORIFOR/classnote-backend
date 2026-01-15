"""
Admin API Routes - Dashboard management endpoints
"""
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.dependencies import get_current_user, User
from app.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter
import logging

router = APIRouter(prefix="/admin", tags=["Admin"])
logger = logging.getLogger("app.admin")

# ============================================================
# Models
# ============================================================

class PricingConfig(BaseModel):
    llm_input_per_1k_tokens_usd: float = 0.0015
    llm_output_per_1k_tokens_usd: float = 0.002
    storage_gb_month_usd: float = 0.02
    egress_gb_usd: float = 0.12
    speech_per_min_usd: float = 0.024
    cloudrun_shared_monthly_usd: float = 50.0
    firestore_shared_monthly_usd: float = 20.0

class OverviewResponse(BaseModel):
    total_users: int
    dau: int
    wau: int
    mau: int
    new_users_today: int
    new_users_7d: int
    total_sessions_today: int
    total_sessions_7d: int
    total_recording_sec_today: float
    total_recording_sec_7d: float
    estimated_cost_today_usd: float
    estimated_cost_7d_usd: float
    jobs_failed_24h: int

class UserSummary(BaseModel):
    uid: str
    displayName: Optional[str] = None
    email: Optional[str] = None
    plan: str = "free"
    providers: List[str] = []
    createdAt: Optional[datetime] = None
    lastSeenAt: Optional[datetime] = None
    session_count_30d: int = 0
    total_recording_min_30d: float = 0.0
    total_recording_min_lifetime: float = 0.0
    estimated_cost_30d_usd: float = 0.0
    active_badge: str = "inactive"  # "7d", "30d", "inactive"

class UserDetailResponse(UserSummary):
    session_count_lifetime: int = 0
    llm_input_tokens_30d: int = 0
    llm_output_tokens_30d: int = 0
    audio_bytes_30d: int = 0

class RankingItem(BaseModel):
    uid: str
    displayName: Optional[str] = None
    value: float
    unit: str

class AdminSessionListItem(BaseModel):
    id: str
    userId: str
    title: Optional[str] = None
    createdAt: Optional[datetime] = None
    durationSec: float = 0.0
    status: str = "created"  # recording, processing, completed, failed
    mode: str = "lecture"
    audioStatus: Optional[str] = None
    summaryStatus: Optional[str] = None
    sizeBytes: int = 0

# ============================================================
# Helper Functions
# ============================================================

def _get_pricing_config() -> PricingConfig:
    """Fetch pricing config from Firestore or return defaults."""
    try:
        doc = db.collection("pricing_config").document("current").get()
        if doc.exists:
            return PricingConfig(**doc.to_dict())
    except Exception as e:
        logger.warning(f"Failed to fetch pricing config: {e}")
    return PricingConfig()

def _calculate_estimated_cost(recording_sec: float, pricing: PricingConfig) -> float:
    """Calculate estimated cost based on recording duration."""
    recording_min = recording_sec / 60
    return round(recording_min * pricing.speech_per_min_usd, 4)

def _get_active_badge(last_seen: Optional[datetime]) -> str:
    """Determine activity badge based on last seen timestamp."""
    if not last_seen:
        return "inactive"
    now = datetime.now(timezone.utc)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    delta = now - last_seen
    if delta < timedelta(days=7):
        return "7d"
    elif delta < timedelta(days=30):
        return "30d"
    return "inactive"

# ============================================================
# Endpoints
# ============================================================

@router.get("/overview", response_model=OverviewResponse)
async def get_admin_overview(
    current_user: User = Depends(get_current_user)
):
    """
    Get KPI overview for admin dashboard.
    TODO: Add proper admin role check.
    """
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    
    pricing = _get_pricing_config()
    
    # Fetch users
    users_docs = list(db.collection("users").stream())
    total_users = len(users_docs)
    
    # Calculate DAU/WAU/MAU and new users
    dau = 0
    wau = 0
    mau = 0
    new_users_today = 0
    new_users_7d = 0
    
    for doc in users_docs:
        data = doc.to_dict()
        last_seen = data.get("lastSeenAt")
        created_at = data.get("createdAt")
        
        if last_seen:
            if hasattr(last_seen, 'replace'):
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                if last_seen >= today:
                    dau += 1
                if last_seen >= week_ago:
                    wau += 1
                if last_seen >= month_ago:
                    mau += 1
        
        if created_at:
            if hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= today:
                    new_users_today += 1
                if created_at >= week_ago:
                    new_users_7d += 1
    
    # Fetch sessions (limit for performance)
    sessions_query = db.collection("sessions").order_by("createdAt", direction="DESCENDING").limit(1000)
    sessions_docs = list(sessions_query.stream())
    
    total_sessions_today = 0
    total_sessions_7d = 0
    total_recording_sec_today = 0.0
    total_recording_sec_7d = 0.0
    jobs_failed_24h = 0
    
    for doc in sessions_docs:
        data = doc.to_dict()
        created_at = data.get("createdAt")
        duration = data.get("durationSec") or 0
        status = data.get("status", "")
        
        if created_at:
            if hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= today:
                    total_sessions_today += 1
                    total_recording_sec_today += duration
                if created_at >= week_ago:
                    total_sessions_7d += 1
                    total_recording_sec_7d += duration
                if created_at >= (now - timedelta(hours=24)) and status == "failed":
                    jobs_failed_24h += 1
    
    return OverviewResponse(
        total_users=total_users,
        dau=dau,
        wau=wau,
        mau=mau,
        new_users_today=new_users_today,
        new_users_7d=new_users_7d,
        total_sessions_today=total_sessions_today,
        total_sessions_7d=total_sessions_7d,
        total_recording_sec_today=total_recording_sec_today,
        total_recording_sec_7d=total_recording_sec_7d,
        estimated_cost_today_usd=_calculate_estimated_cost(total_recording_sec_today, pricing),
        estimated_cost_7d_usd=_calculate_estimated_cost(total_recording_sec_7d, pricing),
        jobs_failed_24h=jobs_failed_24h
    )


@router.get("/usage/cloud-stt")
async def get_cloud_stt_usage(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    group_by: Optional[str] = Query(None, description="Group results: 'user' or None"),
    current_user: User = Depends(get_current_user)
):
    """
    Get aggregated usage for Cloud STT (transcriptionMode='cloud_google').
    If group_by="user", returns breakdown by user.
    """
    query = db.collection("sessions")
    if user_id:
        query = query.where("userId", "==", user_id)
    
    # Filter by transcriptionMode
    query = query.where("transcriptionMode", "==", "cloud_google")

    total_sec = 0.0
    user_stats = {}
    
    # Stream all matching documents
    docs = query.stream()
    
    for doc in docs:
        data = doc.to_dict()
        duration = data.get("durationSec", 0)
        uid = data.get("userId") or "unknown"
        
        total_sec += duration
        
        if group_by == "user":
            if uid not in user_stats:
                user_stats[uid] = {"userId": uid, "total_stt_sec": 0.0, "session_count": 0}
            user_stats[uid]["total_stt_sec"] += duration
            user_stats[uid]["session_count"] += 1

    resp = {"total_cloud_stt_sec": total_sec}
    
    if group_by == "user":
        by_user_list = []
        
        # Hydrate with user details (email/name)
        # Only fetch for found users. Limit to reasonable batch size if needed.
        # For admin tool, doing individual gets or small batches is okay for < 1000 users.
        # Ideally, we should fetch users in parallel or use 'in' query if list is small.
        # For simplicity, we just iterate.
        
        unique_uids = list(user_stats.keys())
        # Fetch user info logic...
        # If user count is huge, this is slow. But Cloud STT users are likely fewer.
        
        # Optimization: Fetch all users map if needed, or just fetch as we go.
        # Let's simple-fetch for now.
        
        for uid in unique_uids:
            stat = user_stats[uid]
            
            # Default display
            stat["email"] = None
            stat["displayName"] = None
            
            if uid != "unknown":
                try:
                    user_doc = db.collection("users").document(uid).get()
                    if user_doc.exists:
                        udata = user_doc.to_dict()
                        stat["email"] = udata.get("email")
                        stat["displayName"] = udata.get("displayName")
                except:
                    pass
            
            by_user_list.append(stat)
            
        # Sort by total_stt_sec desc
        by_user_list.sort(key=lambda x: x["total_stt_sec"], reverse=True)
        resp["by_user"] = by_user_list

    return resp


@router.get("/users", response_model=List[UserSummary])
async def get_admin_users(
    query: Optional[str] = Query(None, description="Search by name, email, or UID"),
    plan: Optional[str] = Query(None, description="Filter by plan"),
    active: Optional[str] = Query(None, description="Filter by activity: 7d, 30d, inactive"),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user)
):
    """
    Get paginated user list with filters.
    """
    now = datetime.now(timezone.utc)
    month_ago = now - timedelta(days=30)
    
    pricing = _get_pricing_config()
    
    # Fetch users
    users_query = db.collection("users").limit(limit * 2)  # Fetch more for filtering
    users_docs = list(users_query.stream())
    
    # Fetch sessions for stats (last 30 days)
    sessions_query = db.collection("sessions").order_by("createdAt", direction="DESCENDING").limit(2000)
    sessions_docs = list(sessions_query.stream())
    
    # Aggregate sessions by user
    user_stats = {}
    for doc in sessions_docs:
        data = doc.to_dict()
        uid = data.get("userId") or data.get("ownerUserId")
        created_at = data.get("createdAt")
        duration = data.get("durationSec") or 0
        
        if not uid:
            continue
        
        if uid not in user_stats:
            user_stats[uid] = {"sessions_30d": 0, "recording_sec_30d": 0.0, "recording_sec_lifetime": 0.0}
        
        user_stats[uid]["recording_sec_lifetime"] += duration
        
        if created_at:
            if hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= month_ago:
                    user_stats[uid]["sessions_30d"] += 1
                    user_stats[uid]["recording_sec_30d"] += duration
    
    results = []
    for doc in users_docs:
        data = doc.to_dict()
        uid = doc.id
        
        # Search filter
        if query:
            q_lower = query.lower()
            name = (data.get("displayName") or "").lower()
            email = (data.get("email") or "").lower()
            if q_lower not in name and q_lower not in email and q_lower not in uid.lower():
                continue
        
        # Plan filter
        user_plan = data.get("plan", "free")
        if plan and user_plan != plan:
            continue
        
        # Get stats
        stats = user_stats.get(uid, {"sessions_30d": 0, "recording_sec_30d": 0.0, "recording_sec_lifetime": 0.0})
        
        last_seen = data.get("lastSeenAt")
        active_badge = _get_active_badge(last_seen)
        
        # Active filter
        if active and active_badge != active:
            continue
        
        recording_min_30d = stats["recording_sec_30d"] / 60
        recording_min_lifetime = stats["recording_sec_lifetime"] / 60
        
        results.append(UserSummary(
            uid=uid,
            displayName=data.get("displayName"),
            email=data.get("email"),
            plan=user_plan,
            providers=data.get("providers", []),
            createdAt=data.get("createdAt"),
            lastSeenAt=last_seen,
            session_count_30d=stats["sessions_30d"],
            total_recording_min_30d=round(recording_min_30d, 1),
            total_recording_min_lifetime=round(recording_min_lifetime, 1),
            estimated_cost_30d_usd=_calculate_estimated_cost(stats["recording_sec_30d"], pricing),
            active_badge=active_badge
        ))
    
    # Sort by lastSeenAt descending
    results.sort(key=lambda x: x.lastSeenAt or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    
    return results[:limit]


@router.get("/users/rankings", response_model=List[RankingItem])
async def get_user_rankings(
    metric: str = Query("recording", description="Metric: recording, cost, sessions"),
    limit: int = Query(10, le=50),
    current_user: User = Depends(get_current_user)
):
    """
    Get top users by specified metric.
    """
    now = datetime.now(timezone.utc)
    month_ago = now - timedelta(days=30)
    pricing = _get_pricing_config()
    
    # Fetch users for display names
    users_docs = list(db.collection("users").limit(2000).stream())
    user_names = {doc.id: doc.to_dict().get("displayName") or doc.to_dict().get("email") or doc.id[:8] for doc in users_docs}
    
    # Fetch sessions
    sessions_query = db.collection("sessions").order_by("createdAt", direction="DESCENDING").limit(2000)
    sessions_docs = list(sessions_query.stream())
    
    # Aggregate
    user_stats = {}
    for doc in sessions_docs:
        data = doc.to_dict()
        uid = data.get("userId") or data.get("ownerUserId")
        created_at = data.get("createdAt")
        duration = data.get("durationSec") or 0
        
        if not uid:
            continue
        
        if uid not in user_stats:
            user_stats[uid] = {"recording_sec": 0.0, "sessions": 0}
        
        if created_at:
            if hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= month_ago:
                    user_stats[uid]["recording_sec"] += duration
                    user_stats[uid]["sessions"] += 1
    
    # Build rankings
    rankings = []
    for uid, stats in user_stats.items():
        if metric == "recording":
            value = stats["recording_sec"] / 60
            unit = "min"
        elif metric == "cost":
            value = _calculate_estimated_cost(stats["recording_sec"], pricing)
            unit = "USD"
        elif metric == "sessions":
            value = stats["sessions"]
            unit = "sessions"
        else:
            continue
        
        rankings.append(RankingItem(
            uid=uid,
            displayName=user_names.get(uid),
            value=round(value, 2),
            unit=unit
        ))
    
    # Sort descending
    rankings.sort(key=lambda x: x.value, reverse=True)
    
    return rankings[:limit]


@router.get("/users/{uid}", response_model=UserDetailResponse)
async def get_user_detail(
    uid: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed user info with usage stats.
    """
    now = datetime.now(timezone.utc)
    month_ago = now - timedelta(days=30)
    pricing = _get_pricing_config()
    
    # Fetch user
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
    
    data = user_doc.to_dict()
    
    # Fetch user's sessions
    sessions_query = db.collection("sessions").where("userId", "==", uid).limit(500)
    sessions_docs = list(sessions_query.stream())
    
    session_count_lifetime = len(sessions_docs)
    session_count_30d = 0
    recording_sec_30d = 0.0
    recording_sec_lifetime = 0.0
    audio_bytes_30d = 0
    
    for doc in sessions_docs:
        sdata = doc.to_dict()
        created_at = sdata.get("createdAt")
        duration = sdata.get("durationSec") or 0
        audio_meta = sdata.get("audioMeta") or {}
        audio_bytes = audio_meta.get("sizeBytes") or sdata.get("audio", {}).get("sizeBytes") or 0
        
        recording_sec_lifetime += duration
        
        if created_at:
            if hasattr(created_at, 'replace'):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= month_ago:
                    session_count_30d += 1
                    recording_sec_30d += duration
                    audio_bytes_30d += audio_bytes
    
    last_seen = data.get("lastSeenAt")
    
    return UserDetailResponse(
        uid=uid,
        displayName=data.get("displayName"),
        email=data.get("email"),
        plan=data.get("plan", "free"),
        providers=data.get("providers", []),
        createdAt=data.get("createdAt"),
        lastSeenAt=last_seen,
        session_count_30d=session_count_30d,
        session_count_lifetime=session_count_lifetime,
        total_recording_min_30d=round(recording_sec_30d / 60, 1),
        total_recording_min_lifetime=round(recording_sec_lifetime / 60, 1),
        estimated_cost_30d_usd=_calculate_estimated_cost(recording_sec_30d, pricing),
        active_badge=_get_active_badge(last_seen),
        llm_input_tokens_30d=0,  # TODO: Aggregate from usage_logs when implemented
        llm_output_tokens_30d=0,
        audio_bytes_30d=audio_bytes_30d
    )


@router.get("/config/pricing", response_model=PricingConfig)
async def get_pricing_config(
    current_user: User = Depends(get_current_user)
):
    """Get current pricing configuration."""
    return _get_pricing_config()


@router.patch("/config/pricing", response_model=PricingConfig)
async def update_pricing_config(
    config: PricingConfig,
    current_user: User = Depends(get_current_user)
):
    """Update pricing configuration."""
    db.collection("pricing_config").document("current").set(config.dict())
    return config


@router.get("/sessions", response_model=List[AdminSessionListItem])
async def get_admin_sessions(
    status: Optional[str] = Query(None, description="Filter by status"),
    mode: Optional[str] = Query(None, description="Filter by mode"),
    userId: Optional[str] = Query(None, description="Filter by userId"),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user)
):
    """
    Get paginated session list with filters.
    """
    query = db.collection("sessions").order_by("createdAt", direction="DESCENDING")
    
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))
    if mode:
        query = query.where(filter=FieldFilter("mode", "==", mode))
    if userId:
        # Note: Inequality filter property and first sort order must be the same field.
        # If we use orderBy createdAt, we can't filter by userId easily without composite index.
        # For admin dashboard, let's prioritize filtering over sorting flexibility for now, 
        # OR just filter in memory if result set is small, but Firestore is large.
        # Actually, for userId filter, we usually want to see THAT user's sessions.
        # So we should swap to userId filtering if provided.
        query = db.collection("sessions").where(filter=FieldFilter("userId", "==", userId)).order_by("createdAt", direction="DESCENDING")

    # Execute query
    docs = list(query.limit(limit).stream())
    
    results = []
    for doc in docs:
        data = doc.to_dict()
        
        # Safe get for size
        audio_meta = data.get("audioMeta") or {}
        size_bytes = audio_meta.get("sizeBytes") or data.get("audio", {}).get("sizeBytes") or 0

        results.append(AdminSessionListItem(
            id=doc.id,
            userId=data.get("userId") or data.get("ownerUserId") or "unknown",
            title=data.get("title"),
            createdAt=data.get("createdAt"),
            durationSec=data.get("durationSec") or 0.0,
            status=data.get("status", "unknown"),
            mode=data.get("mode", "lecture"),
            audioStatus=data.get("audioStatus"),
            summaryStatus=data.get("summaryStatus"),
            sizeBytes=int(size_bytes)
        ))
    
    return results
