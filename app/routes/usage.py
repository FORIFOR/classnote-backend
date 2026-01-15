"""
Usage API Routes - View usage statistics
"""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException

from app.dependencies import get_current_user, get_current_user_optional
from app.usage_models import UsageSummaryResponse
from app.services.usage import usage_logger
from app.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter

router = APIRouter(prefix="/usage", tags=["Usage"])


@router.post("/admin/backfill")
async def backfill_usage(
    secret: Optional[str] = Query(None),
    admin_user: Optional[object] = Depends(get_current_user_optional)
):
    """
    Admin-only: Backfill usage data from sessions collection.
    Scans all sessions and populates user_daily_usage.
    """
    # Allow bypass with secret key (for curl usage without token)
    if secret != "classnote-admin-secret-123":
        # If secret doesn't match, ensure user is authenticated
        if not admin_user:
             raise HTTPException(status_code=401, detail="Unauthorized")
    
    from collections import defaultdict
    from datetime import datetime
    from app.firebase import db
    from google.cloud import firestore
    
    # Use streaming to avoid memory issues with many docs
    sessions = db.collection("sessions").stream()
    
    # user_id -> date -> {stats}
    usage_map = defaultdict(lambda: defaultdict(lambda: {
        "session_count": 0,
        "total_recording_sec": 0.0,
        "summary_invocations": 0,
        "summary_success": 0,
        "quiz_invocations": 0,
        "quiz_success": 0
    }))
    
    count = 0
    for doc in sessions:
        count += 1
        data = doc.to_dict()
        user_id = data.get("ownerUid") or data.get("userId")
        if not user_id: continue
        
        # Determine date
        created_at = data.get("createdAt")
        date_str = datetime.now().date().isoformat()
        if hasattr(created_at, "date"):
             date_str = created_at.date().isoformat()
        elif isinstance(created_at, str) and len(created_at) >= 10:
             date_str = created_at[:10]
        elif isinstance(created_at, datetime):
             date_str = created_at.date().isoformat()

        stats = usage_map[user_id][date_str]
        
        # Session & Recording
        stats["session_count"] += 1
        # Calculate duration with smart fallback
        duration = float(data.get("durationSec") or 0.0)
        
        if duration == 0.0:
            # Fallback 1: Segments (last segment end time)
            segments = data.get("segments") or data.get("diarizedSegments") or []
            if segments and isinstance(segments, list) and len(segments) > 0:
                last_seg = segments[-1]
                if isinstance(last_seg, dict):
                    duration = float(last_seg.get("endSec", 0.0))
            
            # Fallback 2: endedAt - startedAt
            if duration == 0.0:
                s_at = data.get("startedAt")
                e_at = data.get("endedAt")
                if s_at and e_at:
                    if isinstance(s_at, datetime) and isinstance(e_at, datetime):
                         delta = (e_at - s_at).total_seconds()
                         if delta > 0 and delta < 86400: # Sanity check < 24h
                             duration = delta
        
        stats["total_recording_sec"] += duration
        
        # Mode aggregation
        mode = data.get("mode")
        if mode and duration > 0:
            stats["usage_by_mode"] = stats.get("usage_by_mode", {})
            stats["usage_by_mode"][mode] = stats["usage_by_mode"].get(mode, 0) + duration
            
        # Tag aggregation (autoTags preferred, fallback to tags)
        tags = data.get("autoTags") or data.get("tags") or []
        if tags and duration > 0:
            stats["usage_by_tag"] = stats.get("usage_by_tag", {})
            for t in tags:
                if isinstance(t, str):
                    clean_t = t.strip().replace(".", "_")
                    stats["usage_by_tag"][clean_t] = stats["usage_by_tag"].get(clean_t, 0) + duration

        
        # Summary
        if data.get("summaryStatus") == "completed":
            stats["summary_invocations"] += 1
            stats["summary_success"] += 1
        elif data.get("summaryStatus") == "failed":
            stats["summary_invocations"] += 1
            
        # Quiz
        if data.get("quizStatus") == "completed":
             stats["quiz_invocations"] += 1
             stats["quiz_success"] += 1

    # Batch write
    batch = db.batch()
    batch_count = 0
    
    for user_id, dates in usage_map.items():
        for date_str, stats in dates.items():
            doc_id = f"{user_id}_{date_str}"
            ref = db.collection("user_daily_usage").document(doc_id)
            
            update_data = {
                "user_id": user_id,
                "date": date_str,
                "session_count": stats["session_count"],
                "total_recording_sec": stats["total_recording_sec"],
                "summary_invocations": stats["summary_invocations"],
                "summary_success": stats["summary_success"],
                "quiz_invocations": stats["quiz_invocations"],
                "quiz_success": stats["quiz_success"],
            }
            
            # Add nested fields if they exist
            if "usage_by_mode" in stats:
                for m, sec in stats["usage_by_mode"].items():
                    update_data[f"usage_by_mode.{m}"] = sec
            
            if "usage_by_tag" in stats:
                for t, sec in stats["usage_by_tag"].items():
                    update_data[f"usage_by_tag.{t}"] = sec
            
            batch.set(ref, update_data, merge=True)
            batch_count += 1
            
            if batch_count >= 400:
                batch.commit()
                batch = db.batch()
                batch_count = 0
                
    if batch_count > 0:
        batch.commit()
    
    # Calculate global stats OR user specific stats for response
    # If invoked by a user (admin_user), return THEIR stats to fix iOS display
    # Otherwise return global stats
    
    response_stats = {
        "total_sessions": 0,
        "total_recording_sec": 0.0
    }
    
    target_uid = None
    if admin_user:
        if hasattr(admin_user, "uid"):
            target_uid = admin_user.uid
        elif isinstance(admin_user, dict):
            target_uid = admin_user.get("uid")
            
    if target_uid and target_uid in usage_map:
        # User specific stats
        for date_str, stats in usage_map[target_uid].items():
            response_stats["total_sessions"] += stats["session_count"]
            response_stats["total_recording_sec"] += stats["total_recording_sec"]
    else:
        # Global stats (fallback)
        for uid, dates in usage_map.items():
            for date_str, stats in dates.items():
                response_stats["total_sessions"] += stats["session_count"]
                response_stats["total_recording_sec"] += stats["total_recording_sec"]
                
    response_stats["total_users"] = len(usage_map)

    return {
        "processed_sessions": count,
        "stats": response_stats
    }

@router.get("/me/summary", response_model=UsageSummaryResponse, response_model_by_alias=False)
async def get_my_usage_summary(
    from_date: Optional[str] = Query(
        None, 
        description="Start date (yyyy-MM-dd). Defaults to 30 days ago."
    ),
    to_date: Optional[str] = Query(
        None,
        description="End date (yyyy-MM-dd). Defaults to today."
    ),
    user: object = Depends(get_current_user)
):
    """
    Get the current user's usage summary for a date range.
    
    Useful for:
    - Showing usage dashboard in the app
    - Checking quota usage
    - Billing purposes
    """
    
    if hasattr(user, "uid"):
         user_id = user.uid
    else:
         user_id = user.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    # Default date range: last 30 days
    if not to_date:
        to_date = date.today().isoformat()
    if not from_date:
        from_date = (date.today() - timedelta(days=30)).isoformat()
    
    summary = await usage_logger.get_user_usage_summary(
        user_id=user_id,
        from_date=from_date,
        to_date=to_date
    )
    
    return UsageSummaryResponse(**summary)


@router.get("/admin/users/{user_id}/summary", response_model=UsageSummaryResponse)
async def get_user_usage_summary_admin(
    user_id: str,
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    admin_user: object = Depends(get_current_user)
):
    """
    Admin endpoint to view any user's usage.
    
    TODO: Add proper admin role check
    """
    # TODO: Verify admin_user has admin role
    # For now, just allow any authenticated user (should be restricted in production)
    
    if not to_date:
        to_date = date.today().isoformat()
    if not from_date:
        from_date = (date.today() - timedelta(days=30)).isoformat()
    
    summary = await usage_logger.get_user_usage_summary(
        user_id=user_id,
        from_date=from_date,
        to_date=to_date
    )
    
    return UsageSummaryResponse(**summary)
    return UsageSummaryResponse(**summary)

@router.get("/analytics/me/timeline")
async def get_usage_timeline(
    range: str = "30d",
    current_user: object = Depends(get_current_user)
):
    """
    時系列データの取得（グラフ用）。
    """
    uid = current_user.uid
    end_date = date.today()
    days = 30
    if range == "7d": days = 7
    elif range == "90d": days = 90
    
    start_date = end_date - timedelta(days=days)
    
    docs = db.collection("user_daily_usage") \
            .where(filter=FieldFilter("user_id", "==", uid)) \
            .where(filter=FieldFilter("date", ">=", start_date.isoformat())) \
            .where(filter=FieldFilter("date", "<=", end_date.isoformat())) \
            .order_by("date") \
            .stream()
            
    timeline = []
    for doc in docs:
        d = doc.to_dict()
        timeline.append({
            "date": d.get("date"),
            "recordingSec": d.get("total_recording_sec", 0),
            "sessionCount": d.get("session_count", 0)
        })
        
    return {"timeline": timeline}
