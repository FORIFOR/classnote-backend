from fastapi import APIRouter, Depends, Query, HTTPException
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from google.cloud import firestore
from pydantic import BaseModel
import logging

from app.admin_auth import get_current_admin_user
from app.services.ops_logger import OpsLogger, EventType, Severity
from app.services.metrics import MetricsService, MetricName
from app.services.job_manager import job_manager, JobStatus, ErrorCategory, can_retry
from firebase_admin import auth as firebase_auth

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("app.admin")


# --- Account Disable/Enable Models ---

class DisableAccountRequest(BaseModel):
    reason: Optional[str] = None
    scope: str = "all"
    expiresAt: Optional[datetime] = None
    revokeTokens: bool = True
    disableFirebaseAuth: bool = True


class EnableAccountRequest(BaseModel):
    reason: Optional[str] = None

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


# --- Account Disable/Enable Endpoints ---

@router.post("/users/{uid}/disable")
async def disable_user_account(
    uid: str,
    req: DisableAccountRequest,
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    アカウントを停止（凍結/BAN）する。
    - Firestore の status を disabled に設定
    - Firebase Auth のユーザーを無効化（オプション）
    - リフレッシュトークンを無効化（オプション）
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    admin_uid = admin_user.get("uid")

    # 1. Get user's accountId
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()

    if not user_doc.exists:
        raise HTTPException(404, "User not found")

    user_data = user_doc.to_dict()
    account_id = user_data.get("accountId")

    # 2. Update Account status (primary)
    if account_id:
        acc_ref = db.collection("accounts").document(account_id)
        acc_ref.set({
            "status": "disabled",
            "disabledAt": now,
            "disabledReason": req.reason,
            "disabledBy": admin_uid,
            "disabledExpiresAt": req.expiresAt,
            "updatedAt": now,
        }, merge=True)

    # 3. Update User status (backup/legacy)
    user_ref.set({
        "status": "disabled",
        "securityState": "banned",
        "disabledAt": now,
        "disabledReason": req.reason,
        "disabledBy": admin_uid,
        "updatedAt": now,
    }, merge=True)

    # 4. Disable Firebase Auth (prevents new logins)
    if req.disableFirebaseAuth:
        try:
            firebase_auth.update_user(uid, disabled=True)
            logger.info(f"Firebase Auth disabled for uid={uid}")
        except Exception as e:
            logger.error(f"Failed to disable Firebase Auth for uid={uid}: {e}")

    # 5. Revoke refresh tokens (force logout on next token refresh)
    if req.revokeTokens:
        try:
            firebase_auth.revoke_refresh_tokens(uid)
            logger.info(f"Refresh tokens revoked for uid={uid}")
        except Exception as e:
            logger.error(f"Failed to revoke tokens for uid={uid}: {e}")

    # 6. Audit log
    db.collection("admin_audit").add({
        "action": "disable_user",
        "targetUid": uid,
        "targetAccountId": account_id,
        "reason": req.reason,
        "by": admin_uid,
        "at": now,
        "options": {
            "disableFirebaseAuth": req.disableFirebaseAuth,
            "revokeTokens": req.revokeTokens,
            "expiresAt": req.expiresAt.isoformat() if req.expiresAt else None
        }
    })

    # 7. Ops log
    OpsLogger().log(
        severity=Severity.WARN,
        event_type=EventType.ADMIN_ACTION,
        uid=uid,
        message=f"User account DISABLED by admin: {req.reason or 'No reason provided'}",
        debug={"adminUid": admin_uid, "accountId": account_id}
    )

    return {
        "uid": uid,
        "accountId": account_id,
        "status": "disabled",
        "disabledAt": now.isoformat(),
        "reason": req.reason
    }


@router.post("/users/{uid}/enable")
async def enable_user_account(
    uid: str,
    req: EnableAccountRequest,
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    アカウント停止を解除する。
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    admin_uid = admin_user.get("uid")

    # 1. Get user's accountId
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()

    if not user_doc.exists:
        raise HTTPException(404, "User not found")

    user_data = user_doc.to_dict()
    account_id = user_data.get("accountId")

    # 2. Update Account status
    if account_id:
        acc_ref = db.collection("accounts").document(account_id)
        acc_ref.set({
            "status": "active",
            "disabledAt": None,
            "disabledReason": None,
            "disabledBy": None,
            "disabledExpiresAt": None,
            "updatedAt": now,
        }, merge=True)

    # 3. Update User status
    user_ref.set({
        "status": "active",
        "securityState": firestore.DELETE_FIELD,
        "disabledAt": firestore.DELETE_FIELD,
        "disabledReason": firestore.DELETE_FIELD,
        "disabledBy": firestore.DELETE_FIELD,
        "quarantineUntil": firestore.DELETE_FIELD,
        "updatedAt": now,
    }, merge=True)

    # 4. Re-enable Firebase Auth
    try:
        firebase_auth.update_user(uid, disabled=False)
        logger.info(f"Firebase Auth enabled for uid={uid}")
    except Exception as e:
        logger.error(f"Failed to enable Firebase Auth for uid={uid}: {e}")

    # 5. Audit log
    db.collection("admin_audit").add({
        "action": "enable_user",
        "targetUid": uid,
        "targetAccountId": account_id,
        "reason": req.reason,
        "by": admin_uid,
        "at": now,
    })

    # 6. Ops log
    OpsLogger().log(
        severity=Severity.INFO,
        event_type=EventType.ADMIN_ACTION,
        uid=uid,
        message=f"User account ENABLED by admin: {req.reason or 'No reason provided'}",
        debug={"adminUid": admin_uid, "accountId": account_id}
    )

    return {
        "uid": uid,
        "accountId": account_id,
        "status": "active"
    }


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


@router.get("/metrics/summary")
async def get_metrics_summary(
    hours: int = Query(1, ge=1, le=24),
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    Get metrics summary for the last N hours.
    Returns aggregated metrics for monitoring dashboards.
    """
    metrics_service = MetricsService()
    summary = metrics_service.get_metrics_summary(hours=hours)

    return {
        "hours": hours,
        "metrics": summary
    }


@router.get("/metrics/gauges")
async def get_metrics_gauges(
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    Get current gauge metrics (queue depth, active connections, etc.).
    """
    db = get_db()

    # Fetch all gauge metrics
    gauges = {}
    try:
        docs = list(db.collection("metrics_gauges").stream())
        for doc in docs:
            data = doc.to_dict()
            gauges[data.get("metric", doc.id)] = {
                "value": data.get("value"),
                "labels": data.get("labels", {}),
                "updatedAt": data.get("updatedAt")
            }
    except Exception as e:
        logger.error(f"Failed to fetch gauges: {e}")

    return {"gauges": gauges}


# --- Job Management ---

@router.get("/jobs/{session_id}")
async def get_session_jobs(
    session_id: str,
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    Get all jobs for a session with detailed status.
    """
    db = get_db()

    session_doc = db.collection("sessions").document(session_id).get()
    if not session_doc.exists:
        raise HTTPException(404, "Session not found")

    jobs_ref = db.collection("sessions").document(session_id).collection("jobs")
    jobs = []

    for job_doc in jobs_ref.stream():
        job_data = job_doc.to_dict()
        job_data["id"] = job_doc.id
        job_data["sessionId"] = session_id

        # Check if retryable
        error_category = job_data.get("errorCategory")
        retry_count = job_data.get("retryCount", 0)
        if error_category and job_data.get("status") == "failed":
            try:
                cat = ErrorCategory(error_category)
                job_data["canRetry"] = can_retry(cat, retry_count)
            except ValueError:
                job_data["canRetry"] = False
        else:
            job_data["canRetry"] = False

        jobs.append(job_data)

    return {"sessionId": session_id, "jobs": jobs}


@router.post("/jobs/{session_id}/{job_id}/retry")
async def retry_job(
    session_id: str,
    job_id: str,
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    Manually retry a failed job.

    This will:
    1. Check if job exists and is failed
    2. Verify retry is allowed (category + count)
    3. Increment retry count
    4. Re-enqueue the job
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    admin_uid = admin_user.get("uid")

    # 1. Get job
    job = job_manager.get_job(session_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    job_type = job.get("type")
    job_status = job.get("status")
    error_category = job.get("errorCategory")
    retry_count = job.get("retryCount", 0)

    # 2. Check if retry is allowed
    if job_status not in ["failed", "abandoned"]:
        raise HTTPException(400, f"Cannot retry job with status: {job_status}")

    # Admin can force retry even if normally not allowed
    force_retry = True

    if not force_retry:
        if error_category:
            try:
                cat = ErrorCategory(error_category)
                if not can_retry(cat, retry_count):
                    raise HTTPException(
                        400,
                        f"Job cannot be retried: category={error_category}, retryCount={retry_count}"
                    )
            except ValueError:
                pass

    # 3. Get session owner for re-enqueue
    session_doc = db.collection("sessions").document(session_id).get()
    if not session_doc.exists:
        raise HTTPException(404, "Session not found")

    session_data = session_doc.to_dict()
    owner_uid = session_data.get("ownerUid") or session_data.get("userId")

    # 4. Record retry
    new_retry_count = job_manager.record_retry(session_id, job_id)

    # 5. Re-enqueue based on job type
    from app.task_queue import (
        enqueue_summarize_task,
        enqueue_quiz_task,
        enqueue_transcribe_task,
        enqueue_translate_task,
    )

    idempotency_key = f"admin_retry_{job_id}_{new_retry_count}"

    if job_type == "summary" or job_type == "summarize":
        enqueue_summarize_task(session_id, job_id=job_id, user_id=owner_uid, idempotency_key=idempotency_key)
    elif job_type == "quiz":
        enqueue_quiz_task(session_id, job_id=job_id, user_id=owner_uid, idempotency_key=idempotency_key)
    elif job_type == "transcribe":
        enqueue_transcribe_task(session_id, user_id=owner_uid)
    elif job_type == "translate":
        target_lang = job.get("metadata", {}).get("targetLang", "en")
        enqueue_translate_task(session_id, target_lang, user_id=owner_uid)
    else:
        raise HTTPException(400, f"Unknown job type: {job_type}")

    # 6. Audit log
    OpsLogger().log(
        severity=Severity.INFO,
        event_type=EventType.ADMIN_ACTION,
        server_session_id=session_id,
        job_id=job_id,
        message=f"Job manually retried by admin (attempt #{new_retry_count})",
        debug={"adminUid": admin_uid, "jobType": job_type, "previousError": job.get("errorMessage")}
    )

    return {
        "sessionId": session_id,
        "jobId": job_id,
        "type": job_type,
        "retryCount": new_retry_count,
        "status": "queued",
        "retriedBy": admin_uid,
        "retriedAt": now.isoformat()
    }


@router.get("/jobs/failed")
async def list_failed_jobs(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(100, ge=1, le=500),
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    List recently failed jobs across all sessions.
    """
    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Query recent sessions and check their jobs
    # Note: This is not optimal for large scale - would need a dedicated index
    sessions_query = db.collection("sessions")\
        .order_by("updatedAt", direction=firestore.Query.DESCENDING)\
        .limit(500)

    failed_jobs = []

    for session_doc in sessions_query.stream():
        session_id = session_doc.id

        jobs_query = session_doc.reference.collection("jobs")\
            .where("status", "in", ["failed", "abandoned"])\
            .limit(20)

        for job_doc in jobs_query.stream():
            job_data = job_doc.to_dict()
            updated_at = job_data.get("updatedAt") or job_data.get("createdAt")

            # Filter by time
            if updated_at and hasattr(updated_at, "timestamp"):
                if updated_at < cutoff:
                    continue

            job_data["id"] = job_doc.id
            job_data["sessionId"] = session_id

            # Check if retryable
            error_category = job_data.get("errorCategory")
            retry_count = job_data.get("retryCount", 0)
            if error_category:
                try:
                    cat = ErrorCategory(error_category)
                    job_data["canRetry"] = can_retry(cat, retry_count)
                except ValueError:
                    job_data["canRetry"] = False
            else:
                job_data["canRetry"] = False

            failed_jobs.append(job_data)

            if len(failed_jobs) >= limit:
                break

        if len(failed_jobs) >= limit:
            break

    # Sort by updatedAt descending
    failed_jobs.sort(
        key=lambda x: x.get("updatedAt") or x.get("createdAt") or datetime.min,
        reverse=True
    )

    return {
        "hours": hours,
        "count": len(failed_jobs),
        "jobs": failed_jobs[:limit]
    }