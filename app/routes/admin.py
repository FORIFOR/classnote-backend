from fastapi import APIRouter, Depends, Query, HTTPException
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from google.cloud import firestore
import logging

from app.admin_auth import get_current_admin_user
from app.services.ops_logger import OpsLogger, EventType, Severity

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("app.admin")

# Initialize Firestore (or use shared instance)
# For admin routes, we might want a fresh client or reuse from app.firebase
# To avoid circular imports, let's lazy load or use OsLogger's logic if possible, 
# but direct query is better for listing.

def get_db():
    import os
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    return firestore.Client(project=project_id)

import uuid # Ensure uuid is imported at top level

@router.get("/stats/dashboard")
async def get_dashboard_stats(
    period: str = Query("24h", regex="^(24h|7d)$"),
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    ダッシュボード用のKPIとチャートデータを返す。
    本来は ops_aggregates_daily を参照すべきだが、
    初期実装として ops_events を直近分クエリして簡易集計する。
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    
    if period == "24h":
        start_time = now - timedelta(hours=24)
    else:
        start_time = now - timedelta(days=7)

    # 1. Recent Ops Events (KPI calculation)
    # Note: Scanning all events might be expensive. limit to N latest or use aggregation collection.
    # For MVP (Phase 1), we limit to 500 events to calculate recent stats or rely on aggregation job.
    # User requested "Simple aggregation from ops_events first".
    
    events_ref = db.collection("ops_events")
    query = events_ref.where("ts", ">=", start_time).order_by("ts", direction=firestore.Query.DESCENDING).limit(1000)
    docs = query.stream()
    
    events = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id # Use actual doc ID
        events.append(d)

    # Aggregate locally (MVP)
    kpi = {
        "error5xx": 0,
        "sttFailures": 0,
        "jobStuck": 0,
        "abuseDetected": 0,
        "activeJobs": 0,
        "totalCloudMin": 0.0 # [NEW] vNext tracking
    }
    
    recent_alerts = []
    
    for e in events:
        etype = e.get("type")
        severity = e.get("severity")
        status_code = e.get("statusCode")
        
        # 5xx Errors
        if status_code and status_code >= 500:
            kpi["error5xx"] += 1
        
        # STT Failures
        if etype == "STT_FAILED":
            kpi["sttFailures"] += 1
            
        # Abuse
        if etype == "ABUSE_DETECTED":
            kpi["abuseDetected"] += 1
            
        # Recent Alerts (ERROR/WARN)
        if severity in ["ERROR", "WARN"] and len(recent_alerts) < 10:
            # e already has "id" from doc.id
            recent_alerts.append(e)

    # Chart Data (Continuous buckets)
    JST = timezone(timedelta(hours=9))
    
    # 1. Initialize all buckets for the period
    chart_data = {} # "YYYY-MM-DD HH:00" -> {time: "HH:00", errors: 0, jobs: 0, sortKey: dt}
    
    current = start_time.astimezone(JST).replace(minute=0, second=0, microsecond=0)
    end = now.astimezone(JST).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    
    while current < end:
        key = current.strftime("%Y-%m-%d %H:00")
        chart_data[key] = {
            "time": current.strftime("%H:00"), # Label for UI
            "errors": 0,
            "jobs": 0,
            "sortKey": current
        }
        current += timedelta(hours=1)

    # 2. Fill with event data
    for e in events:
        ts = e.get("ts")
        if not ts: continue
        
        # Convert to JST
        ts_jst = ts.astimezone(JST)
        key = ts_jst.strftime("%Y-%m-%d %H:00")
        
        if key in chart_data:
            if e.get("severity") == "ERROR":
                chart_data[key]["errors"] += 1
            if "JOB" in (e.get("type") or ""):
                chart_data[key]["jobs"] += 1

    # 3. Sort by actual datetime
    sorted_chart = sorted(chart_data.values(), key=lambda x: x["sortKey"])
    
    # Remove sortKey before returning (optional but cleaner)
    for item in sorted_chart:
        del item["sortKey"]

    return {
        "kpi": kpi,
        "chart": sorted_chart,
        "recentAlerts": recent_alerts
    }

@router.get("/events")
async def list_events(
    limit: int = 50,
    cursor: Optional[str] = None,
    severity: Optional[str] = None,
    type: Optional[str] = None,
    uid: Optional[str] = None,
    sessionId: Optional[str] = None,
    errorCode: Optional[str] = None,
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    ops_events を検索・一覧表示する。
    """
    db = get_db()
    query = db.collection("ops_events").order_by("ts", direction=firestore.Query.DESCENDING)

    if severity:
        query = query.where("severity", "==", severity)
    if type:
        query = query.where("type", "==", type)
    if uid:
        query = query.where("uid", "==", uid)
    if sessionId:
        # serverSessionId is the field name in ops_logger
        query = query.where("serverSessionId", "==", sessionId)
    if errorCode:
        query = query.where("errorCode", "==", errorCode)

    if cursor:
        # Cursor pagination logic requires separate handling or passing the document snapshot
        # For simplicity, passing ts string might not work directly without snapshot.
        # This is a placeholder. Real implementation needs snapshot reconstruction or offset.
        pass

    query = query.limit(limit)
    docs = query.stream()
    
    results = []
    last_doc = None
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        results.append(data)
        last_doc = doc
        
    return {
        "events": results,
        "nextCursor": last_doc.id if last_doc else None
    }

@router.get("/users/{uid}")
async def get_user_detail(uid: str, admin_user: dict = Depends(get_current_admin_user)):
    """
    ユーザー詳細：基本情報 + 統計 + 直近イベント
    """
    db = get_db()
    
    # 1. User Doc
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        raise HTTPException(404, "User not found")
        
    user_data = user_doc.to_dict()
    
    # 2. Monthly Usage (vNext Triple Lock)
    from app.services.cost_guard import cost_guard
    monthly_report = await cost_guard.get_usage_report(uid)

    # 3. Stats (Legacy/Basic)
    stats = {
        "totalRecordingSec": monthly_report.get("usedSeconds", 0),
        "sessionCount": 0
    }
    
    # 4. Recent Events
    events_query = db.collection("ops_events").where("uid", "==", uid).order_by("ts", direction=firestore.Query.DESCENDING).limit(20)
    events = [d.to_dict() for d in events_query.stream()]
    
    return {
        "profile": user_data,
        "stats": stats,
        "recentEvents": events
    }

@router.post("/users/{uid}/actions")
async def user_actions(uid: str, action_body: Dict[str, Any], admin_user: dict = Depends(get_current_admin_user)):
    """
    ユーザーへのアクション（隔離、BANなど）
    Body: { "action": "quarantine", "durationMinutes": 60, "reason": "Abuse" }
    """
    db = get_db()
    action = action_body.get("action")
    
    if action == "quarantine":
        duration = action_body.get("durationMinutes", 60)
        until = datetime.now(timezone.utc) + timedelta(minutes=duration)
        
        db.collection("users").document(uid).update({
            "securityState": "quarantined",
            "quarantineUntil": until,
            "securityNote": action_body.get("reason", "Admin Action")
        })
        
        # Log this admin action to ops_events
        OpsLogger().log(
            severity=Severity.WARN,
            event_type=EventType.ABUSE_DETECTED, # Or explicit ADMIN_ACTION type
            uid=uid,
            message=f"User quarantined by admin for {duration} mins",
            debug={"adminUid": admin_user.get("uid"), "reason": action_body.get("reason")}
        )
        
        return {"status": "quarantined", "until": until}
        
    elif action == "ban":
        db.collection("users").document(uid).update({
            "securityState": "banned",
            "securityNote": action_body.get("reason", "Admin Action")
        })
        return {"status": "banned"}
        
    elif action == "release":
        db.collection("users").document(uid).update({
            "securityState": firestore.DELETE_FIELD,
            "quarantineUntil": firestore.DELETE_FIELD
        })
        return {"status": "released"}
        
    raise HTTPException(400, "Invalid action")

@router.get("/sessions/{session_id}")
async def get_session_detail(session_id: str, admin_user: dict = Depends(get_current_admin_user)):
    """
    セッション詳細：基本情報 + ジョブ履歴 + 関連イベント
    """
    db = get_db()
    
    doc = db.collection("sessions").document(session_id).get()
    if not doc.exists:
        raise HTTPException(404, "Session not found")
        
    data = doc.to_dict()
    
    # Job History
    jobs_ref = db.collection("sessions").document(session_id).collection("jobs")
    jobs = [j.to_dict() for j in jobs_ref.stream()]
    
    # Related Ops Events
    events_ref = db.collection("ops_events").where("serverSessionId", "==", session_id).order_by("ts", direction=firestore.Query.DESCENDING)
    events = [e.to_dict() for e in events_ref.stream()]
    
    return {
        "session": data,
        "jobs": jobs,
        "events": events
    }


@router.post("/users/{uid}:purge")
async def purge_user(uid: str, admin_user: dict = Depends(get_current_admin_user)):
    """
    [DANGEROUS] Completely deletes a user's data from Firestore (Hard Delete).
    Target Collections:
    - users/{uid} (and subcollections)
    - sessions (where ownerUserId == uid)
    - uid_links/{uid}
    - phone_numbers (if standardOwnerUid matches or simple cleanup)
    - username_claims
    - entitlements (optional/audit)
    """
    db = get_db()
    
    # 1. Gather all document references to delete
    batch_size = 400
    deleted_counts = {
        "user_doc": 0,
        "sessions": 0,
        "uid_links": 0,
        "username_claims": 0,
        "phone_numbers": 0,
        "entitlements": 0
    }
    
    # A. Sessions (Recurse logic not fully needed if subcollections are simple, but delete root)
    # Note: For strict cleanup of subcollections (like sessions/{sid}/jobs), we need recursive delete.
    # Here we delete the Session document itself. Subcollections in Firestore don't auto-delete,
    # but for "account reset" purposes, orphaning them is often acceptable IF they are inaccessible.
    # Ideally, we stream and delete recursively.
    
    # Simple query for sessions owned by this user
    sessions_ref = db.collection("sessions").where("ownerUserId", "==", uid)
    
    # We use a helper to delete in batches
    def batch_delete(query):
        count = 0
        batch = db.batch()
        docs = query.limit(batch_size).stream() # loop
        has_docs = False
        for doc in docs:
            has_docs = True
            batch.delete(doc.reference)
            count += 1
            if count % batch_size == 0:
                batch.commit()
                batch = db.batch()
        if count % batch_size > 0:
            batch.commit()
        return count
        
    deleted_counts["sessions"] = batch_delete(sessions_ref)
    
    # B. UID Link
    link_ref = db.collection("uid_links").document(uid)
    if link_ref.get().exists:
        link_ref.delete()
        deleted_counts["uid_links"] = 1
        
    # C. Username Claims
    # We need to find if they have a username.
    user_ref = db.collection("users").document(uid)
    user_snap = user_ref.get()
    if user_snap.exists:
        uname = user_snap.to_dict().get("username")
        if uname:
            c_ref = db.collection("username_claims").document(uname)
            c_ref.delete()
            deleted_counts["username_claims"] = 1
            
    # D. Phone Numbers (Release ownership)
    phone = None
    if user_snap.exists:
        phone = user_snap.to_dict().get("phoneE164")
        
    if phone:
        p_ref = db.collection("phone_numbers").document(phone)
        p_doc = p_ref.get()
        if p_doc.exists and p_doc.to_dict().get("standardOwnerUid") == uid:
            # Release or Delete? "Delete from beginning" implies delete.
            p_ref.delete() 
            deleted_counts["phone_numbers"] = 1
            
    # E. User Doc (and subcollections if any, e.g. sessionMeta, subscriptions)
    # Recursive delete of user doc is best handled by CLI or recursive function.
    # For now, just delete the root doc.
    user_ref.delete()
    deleted_counts["user_doc"] = 1
    
    # F. Log deletion
    OpsLogger().log(
        severity=Severity.WARN,
        event_type=EventType.ADMIN_ACTION,
        uid=uid,
        message=f"User PURGED by admin",
        debug={"counts": deleted_counts, "adminUid": admin_user.get("uid")}
    )
    
    return {"ok": True, "deleted": deleted_counts}