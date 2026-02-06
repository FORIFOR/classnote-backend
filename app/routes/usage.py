"""
Usage API Routes - View usage statistics
"""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException

import os
from app.dependencies import get_current_user, get_admin_user, get_admin_user_optional
from app.usage_models import UsageSummaryResponse
from app.services.usage import usage_logger
from app.services.cost_guard import cost_guard
from app.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter

router = APIRouter(prefix="/usage", tags=["Usage"])


@router.post("/admin/backfill")
async def backfill_usage(
    secret: Optional[str] = Query(None),
    admin_user: Optional[object] = Depends(get_admin_user_optional)
):
    """
    Admin-only: Backfill usage data from sessions collection.
    Scans all sessions and populates user_daily_usage.
    """
    # Allow bypass with secret key (for curl usage without token)
    # [SECURITY] No default secret - must be set via environment variable
    admin_secret = os.environ.get("USAGE_BACKFILL_SECRET")
    if not admin_secret or secret != admin_secret:
        # If secret doesn't match, require admin auth
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

    # [FIX] Get accountId for unified quota lookup (BEFORE getting summary)
    user_doc = db.collection("users").document(user_id).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    account_id = user_data.get("accountId")

    # [DEBUG] Print for debugging
    print(f"[/usage/me/summary] user_id={user_id}, account_id={account_id}")

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

    print(f"[/usage/me/summary] usage_logger returned: cloud_sec={summary.get('total_recording_cloud_sec')}, summary={summary.get('summary_invocations')}, quiz={summary.get('quiz_invocations')}")

    # [UNIFY] Align with CostGuard - use accountId if available
    if account_id:
        report = await cost_guard.get_usage_report(account_id, mode="account")
    else:
        report = await cost_guard.get_usage_report(user_id, mode="user")

    print(f"[/usage/me/summary] cost_guard returned: usedSeconds={report.get('usedSeconds')}, summaryGenerated={report.get('summaryGenerated')}, quizGenerated={report.get('quizGenerated')}")

    # [FIX] CostGuard (monthly_usage) を課金・制限の信頼できるソースとして使用
    # max() ではなく CostGuard の値を優先（課金データが正確）
    summary["summary_invocations"] = max(summary.get("summary_invocations", 0), report.get("summaryGenerated", 0))
    summary["quiz_invocations"] = max(summary.get("quiz_invocations", 0), report.get("quizGenerated", 0))
    # Cloud録音時間は CostGuard を信頼できるソースとして使用
    summary["total_recording_cloud_sec"] = report.get("usedSeconds", 0.0)
    summary["total_recording_sec"] = summary.get("total_recording_ondevice_sec", 0.0) + summary["total_recording_cloud_sec"]

    print(f"[/usage/me/summary] FINAL: cloud_sec={summary.get('total_recording_cloud_sec')}, total_sec={summary.get('total_recording_sec')}, summary_inv={summary.get('summary_invocations')}, quiz_inv={summary.get('quiz_invocations')}")

    return UsageSummaryResponse(**summary)


@router.get("/admin/users/{user_id}/summary", response_model=UsageSummaryResponse)
async def get_user_usage_summary_admin(
    user_id: str,
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    admin_user: object = Depends(get_admin_user)
):
    """
    Admin endpoint to view any user's usage.
    """
    if not to_date:
        to_date = date.today().isoformat()
    if not from_date:
        from_date = (date.today() - timedelta(days=30)).isoformat()

    summary = await usage_logger.get_user_usage_summary(
        user_id=user_id,
        from_date=from_date,
        to_date=to_date
    )

    # [FIX] Get accountId for unified quota lookup
    user_doc = db.collection("users").document(user_id).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    account_id = user_data.get("accountId")

    # Use accountId if available
    if account_id:
        report = await cost_guard.get_usage_report(account_id, mode="account")
    else:
        report = await cost_guard.get_usage_report(user_id, mode="user")

    # [FIX] CostGuard を信頼できるソースとして使用
    summary["summary_invocations"] = max(summary.get("summary_invocations", 0), report.get("summaryGenerated", 0))
    summary["quiz_invocations"] = max(summary.get("quiz_invocations", 0), report.get("quizGenerated", 0))
    summary["total_recording_cloud_sec"] = report.get("usedSeconds", 0.0)
    summary["total_recording_sec"] = summary.get("total_recording_ondevice_sec", 0.0) + summary["total_recording_cloud_sec"]

    return UsageSummaryResponse(**summary)

@router.get("/analytics/me/timeline")
async def get_usage_timeline(
    range: str = "30d",
    from_date: Optional[str] = Query(None, description="Start date (yyyy-MM-dd)"),
    to_date: Optional[str] = Query(None, description="End date (yyyy-MM-dd)"),
    current_user: object = Depends(get_current_user)
):
    """
    時系列データの取得（グラフ用）。
    [FIX] クラウド録音時間も含むように修正
    [FIX] from_date/to_date パラメータをサポート
    """
    uid = current_user.uid
    account_id = current_user.account_id

    # [FIX] from_date/to_date が指定されていればそれを使用
    print(f"[/analytics/me/timeline] params: from_date={from_date}, to_date={to_date}, range={range}")

    if from_date and to_date:
        try:
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to_date)
            print(f"[/analytics/me/timeline] Using from_date/to_date: {start_date} to {end_date}")
        except ValueError:
            # Invalid date format, fall back to range
            end_date = date.today()
            days = 30
            if range == "7d": days = 7
            elif range == "90d": days = 90
            start_date = end_date - timedelta(days=days)
            print(f"[/analytics/me/timeline] Date parse error, using range: {start_date} to {end_date}")
    else:
        end_date = date.today()
        days = 30
        if range == "7d": days = 7
        elif range == "90d": days = 90
        start_date = end_date - timedelta(days=days)
        print(f"[/analytics/me/timeline] Using range={range}: {start_date} to {end_date}")

    docs = db.collection("user_daily_usage") \
            .where(filter=FieldFilter("user_id", "==", uid)) \
            .where(filter=FieldFilter("date", ">=", start_date.isoformat())) \
            .where(filter=FieldFilter("date", "<=", end_date.isoformat())) \
            .order_by("date") \
            .stream()

    timeline = []
    total_cloud_sec = 0.0
    total_recording_sec = 0.0
    total_summary = 0
    total_quiz = 0
    total_share = 0
    total_sessions = 0
    for doc in docs:
        d = doc.to_dict()
        cloud_sec = d.get("total_recording_cloud_sec", 0.0)
        recording_sec = d.get("total_recording_sec", 0.0)
        summary_count = d.get("summary_invocations", 0)
        quiz_count = d.get("quiz_invocations", 0)
        share_count = d.get("share_count", 0)
        session_count = d.get("session_count", 0)

        total_cloud_sec += cloud_sec
        total_recording_sec += recording_sec
        total_summary += summary_count
        total_quiz += quiz_count
        total_share += share_count
        total_sessions += session_count

        ondevice_sec = max(0.0, recording_sec - cloud_sec)
        timeline.append({
            "date": d.get("date"),
            # 総録音時間
            "totalRecordingSec": recording_sec,
            "total_recording_sec": recording_sec,
            # クラウド録音時間
            "totalRecordingCloudSec": cloud_sec,
            "total_recording_cloud_sec": cloud_sec,
            # オンデバイス録音時間
            "totalRecordingOnDeviceSec": ondevice_sec,
            "total_recording_ondevice_sec": ondevice_sec,
            # その他
            "sessionCount": session_count,
            "session_count": session_count,
            "summaryCount": summary_count,
            "summary_count": summary_count,
            "quizCount": quiz_count,
            "quiz_count": quiz_count,
            "shareCount": share_count,
            "share_count": share_count,
        })

    # [FIX] CostGuard (monthly_usage) を課金・制限の信頼できるソースとして使用
    # タイムライン（日別データ）はuser_daily_usageから取得（グラフ表示用）
    # 集計値はCostGuardから取得（課金データが正確）
    report = await cost_guard.get_usage_report(account_id, mode="account")
    cost_guard_cloud_sec = report.get("usedSeconds", 0.0)
    cost_guard_summary = report.get("summaryGenerated", 0)
    cost_guard_quiz = report.get("quizGenerated", 0)

    # [FIX] CostGuard を信頼できるソースとして使用（max() ではなく CostGuard の値を優先）
    final_cloud_sec = cost_guard_cloud_sec
    final_summary = max(total_summary, cost_guard_summary)  # 要約/クイズは履歴も有用なのでmax
    final_quiz = max(total_quiz, cost_guard_quiz)

    print(f"[/analytics/me/timeline] RESULT: timeline={len(timeline)} entries, totalRecordingSec={total_recording_sec}, totalCloudRecordingSec={final_cloud_sec}, summaryInvocations={final_summary}, quizInvocations={final_quiz}, shareCount={total_share}")

    return {
        "timelineDaily": timeline,
        # [FIX] 集計データも追加 (複数のフィールド名でiOS互換性を確保)
        # 総録音時間
        "totalRecordingSec": total_recording_sec,
        "total_recording_sec": total_recording_sec,
        "recordingSec": total_recording_sec,
        "recording_sec": total_recording_sec,
        # クラウド録音時間
        "totalCloudRecordingSec": final_cloud_sec,
        "totalRecordingCloudSec": final_cloud_sec,
        "total_recording_cloud_sec": final_cloud_sec,
        # オンデバイス録音時間
        "totalOnDeviceRecordingSec": max(0.0, total_recording_sec - final_cloud_sec),
        "total_recording_ondevice_sec": max(0.0, total_recording_sec - final_cloud_sec),
        # 要約生成回数
        "summaryInvocations": final_summary,
        "summaryCount": final_summary,
        "summary_invocations": final_summary,
        "summary_count": final_summary,
        # クイズ生成回数
        "quizInvocations": final_quiz,
        "quizCount": final_quiz,
        "quiz_invocations": final_quiz,
        "quiz_count": final_quiz,
        # 共有回数
        "shareCount": total_share,
        "share_count": total_share,
        # セッション数
        "sessionCount": total_sessions,
        "session_count": total_sessions
    }
