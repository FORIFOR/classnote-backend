"""
Public (no-auth) read-only endpoints for the admin dashboard.
Mirrors a subset of /admin/* endpoints without authentication.
"""
import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from fastapi import APIRouter, Query
from google.cloud import firestore

from app.services.ops_logger import OpsLogger, EventType, Severity

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger("app.dashboard")


def get_db():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    return firestore.Client(project=project_id)


@router.get("/stats")
async def dashboard_stats(period: str = Query("24h", regex="^(24h|7d)$")):
    """KPI + chart data (same as /admin/stats/dashboard)."""
    db = get_db()
    now = datetime.now(timezone.utc)
    start_time = now - (timedelta(hours=24) if period == "24h" else timedelta(days=7))

    events_ref = db.collection("ops_events")
    query = events_ref.where("ts", ">=", start_time).order_by("ts", direction=firestore.Query.DESCENDING).limit(1000)
    events = [doc.to_dict() | {"id": doc.id} for doc in query.stream()]

    kpi = {"error5xx": 0, "sttFailures": 0, "jobStuck": 0, "abuseDetected": 0, "activeJobs": 0, "totalCloudMin": 0.0}
    recent_alerts = []

    for e in events:
        etype = e.get("type")
        status_code = e.get("statusCode")
        if status_code and status_code >= 500:
            kpi["error5xx"] += 1
        if etype == "STT_FAILED":
            kpi["sttFailures"] += 1
        if etype == "ABUSE_DETECTED":
            kpi["abuseDetected"] += 1
        if e.get("severity") in ["ERROR", "WARN"] and len(recent_alerts) < 10:
            recent_alerts.append(e)

    JST = timezone(timedelta(hours=9))
    chart_data = {}
    current = start_time.astimezone(JST).replace(minute=0, second=0, microsecond=0)
    end = now.astimezone(JST).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while current < end:
        key = current.strftime("%Y-%m-%d %H:00")
        chart_data[key] = {"time": current.strftime("%H:00"), "errors": 0, "jobs": 0, "sortKey": current}
        current += timedelta(hours=1)

    for e in events:
        ts = e.get("ts")
        if not ts:
            continue
        ts_jst = ts.astimezone(JST)
        key = ts_jst.strftime("%Y-%m-%d %H:00")
        if key in chart_data:
            if e.get("severity") == "ERROR":
                chart_data[key]["errors"] += 1
            if "JOB" in (e.get("type") or ""):
                chart_data[key]["jobs"] += 1

    sorted_chart = sorted(chart_data.values(), key=lambda x: x["sortKey"])
    for item in sorted_chart:
        del item["sortKey"]

    return {"kpi": kpi, "chart": sorted_chart, "recentAlerts": recent_alerts}


@router.get("/events")
async def dashboard_events(
    limit: int = 50,
    cursor: Optional[str] = None,
    severity: Optional[str] = None,
    type: Optional[str] = None,
    uid: Optional[str] = None,
    sessionId: Optional[str] = None,
):
    """Events list with filters and pagination."""
    db = get_db()
    query = db.collection("ops_events").order_by("ts", direction=firestore.Query.DESCENDING)

    if severity:
        query = query.where("severity", "==", severity)
    if type:
        query = query.where("type", "==", type)
    if uid:
        query = query.where("uid", "==", uid)
    if sessionId:
        query = query.where("serverSessionId", "==", sessionId)

    query = query.limit(limit)
    docs = list(query.stream())

    results = []
    last_doc = None
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        results.append(data)
        last_doc = doc

    return {"events": results, "nextCursor": last_doc.id if last_doc else None}


@router.get("/daily-sessions")
async def dashboard_daily_sessions(days: int = Query(14, ge=1, le=90)):
    """Daily session stats."""
    db = get_db()
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    JST = timezone(timedelta(hours=9))

    sessions = list(
        db.collection("sessions")
        .where("createdAt", ">=", start)
        .order_by("createdAt")
        .stream()
    )

    daily = defaultdict(lambda: {
        "date": "", "sessions": 0, "uniqueUsers": 0,
        "cloud": 0, "device": 0, "withTranscript": 0,
        "withSummary": 0, "totalMinutes": 0.0, "_users": set(),
    })

    for s in sessions:
        d = s.to_dict()
        created = d.get("createdAt")
        if not created:
            continue
        day_key = created.astimezone(JST).strftime("%Y-%m-%d")
        day_label = created.astimezone(JST).strftime("%m/%d (%a)")
        mode = d.get("transcriptionMode", "")
        uid = d.get("ownerUid", "")
        dur = d.get("durationSec") or 0

        bucket = daily[day_key]
        bucket["date"] = day_label
        bucket["sessions"] += 1
        bucket["_users"].add(uid)
        bucket["totalMinutes"] += dur / 60
        if "cloud" in mode:
            bucket["cloud"] += 1
        else:
            bucket["device"] += 1
        if len(d.get("transcriptText", "") or "") > 0:
            bucket["withTranscript"] += 1
        if d.get("summaryMarkdown"):
            bucket["withSummary"] += 1

    result = []
    for key in sorted(daily.keys()):
        v = daily[key]
        v["uniqueUsers"] = len(v.pop("_users"))
        v["totalMinutes"] = round(v["totalMinutes"], 1)
        result.append(v)

    return {"days": result, "totalSessions": len(sessions), "period": f"{days}d"}
