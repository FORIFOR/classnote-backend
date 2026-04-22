"""Admin-only cost / margin observability endpoints.

Backs the `💰 Costs` tab of tools/monitoring_dashboard and any future
React admin panel. All routes require the Firebase admin custom claim
(via `app.admin_auth.get_current_admin_user` — the same guard used for
/dashboard/*).

Endpoint map:

  GET /admin/costs/overview       KPI cards (revenue / cost / GP / avg)
  GET /admin/costs/timeseries     Daily cost/revenue/GP line chart
  GET /admin/costs/top-users      Top-N users by estimated cost
  GET /admin/costs/top-sessions   Top-N sessions by estimated cost
  GET /admin/costs/features       Per-feature totals (summary/quiz/chat/...)

Design:
  - Reads from `/usage_events` (per-request) — same collection populated
    by app/services/usage_metering.record_usage_event.
  - Supports date-range filter by `dateKey` (index-friendly prefix filter).
  - Returns ISO-formatted dates and USD values. Client decides formatting.
  - Gracefully handles missing data (empty responses, no 500s).
  - No computed pagination; limit via ?limit= parameter.

Phase 5 will additionally load /billing_reconciliation/{yyyy_mm} to apply
correctionFactor to the returned numbers — the response shape already
carries `reconciled: false/true` so clients don't need shape changes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from google.cloud import firestore

from app.admin_auth import get_current_admin_user
from app.firebase import db
from app.services.cost_pricing import get_usd_jpy_rate


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/costs", tags=["admin-costs"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_date_range(from_date: str, to_date: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "BAD_DATE_RANGE",
                    "message": "from_date / to_date must be YYYY-MM-DD",
                }
            },
        )
    if end < start:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_DATE_RANGE", "message": "to_date must be >= from_date"}},
        )
    return start, end


def _iter_events(from_date: str, to_date: str, limit: int = 50_000):
    """Stream usage_events within [from_date, to_date] inclusive.

    Uses `dateKey` prefix filter (indexed) for efficient range scans.
    `limit` is a safety cap — dashboards typically query ≤ month ranges.
    """
    q = (
        db.collection("usage_events")
        .where("dateKey", ">=", from_date)
        .where("dateKey", "<=", to_date)
        .order_by("dateKey")
        .limit(limit)
    )
    for doc in q.stream():
        yield doc.to_dict() or {}


def _usd_to_jpy(usd: float, rate: Optional[float] = None) -> float:
    return usd * get_usd_jpy_rate(rate)


def _safe_num(d: Dict[str, Any], key: str, default: float = 0.0) -> float:
    v = d.get(key)
    if isinstance(v, (int, float)):
        return float(v)
    return default


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/overview")
async def overview(
    from_date: str = Query(..., description="YYYY-MM-DD"),
    to_date: str = Query(..., description="YYYY-MM-DD"),
    _admin=Depends(get_current_admin_user),
):
    """KPI: revenue / cost / gross profit / avg cost per session."""
    _validate_date_range(from_date, to_date)

    totals = {
        "vertex": 0.0, "firestore": 0.0, "cloud_run": 0.0, "storage": 0.0, "stt": 0.0,
        "total": 0.0,
    }
    tokens = {"input": 0, "output": 0}
    session_ids: set[str] = set()
    user_ids: set[str] = set()
    rec_seconds = 0

    for ev in _iter_events(from_date, to_date):
        cb = ev.get("costBreakdown") or {}
        totals["vertex"]    += _safe_num(cb, "vertexUsd")
        totals["firestore"] += _safe_num(cb, "firestoreUsd")
        totals["cloud_run"] += _safe_num(cb, "cloudRunUsd")
        totals["storage"]   += _safe_num(cb, "storageUsd")
        totals["stt"]       += _safe_num(cb, "sttUsd")
        totals["total"]     += _safe_num(ev, "estimatedCostUsd")

        billable = ev.get("billable") or {}
        tokens["input"]  += int(_safe_num(billable, "inputTokens"))
        tokens["output"] += int(_safe_num(billable, "outputTokens"))

        if ev.get("sessionId"):
            session_ids.add(ev["sessionId"])
        if ev.get("userId"):
            user_ids.add(ev["userId"])
        rec_seconds += int(_safe_num(billable, "sttMinutes") * 60)

    # Revenue — Phase 1 leaves this 0 until billing ingest is wired. A
    # dashboard operator can still observe cost/user trends without it.
    revenue_jpy = 0.0

    cost_usd = totals["total"]
    cost_jpy = _usd_to_jpy(cost_usd)
    gp_jpy = revenue_jpy - cost_jpy
    gm_pct = (gp_jpy / revenue_jpy * 100.0) if revenue_jpy > 0 else 0.0

    session_count = len(session_ids)
    avg_cost_per_session = (cost_usd / session_count) if session_count else 0.0

    return {
        "range": {"fromDate": from_date, "toDate": to_date},
        "revenueJpy": round(revenue_jpy, 2),
        "estimatedCostUsd": round(cost_usd, 6),
        "estimatedCostJpy": round(cost_jpy, 2),
        "grossProfitJpy": round(gp_jpy, 2),
        "grossMarginPct": round(gm_pct, 2),
        "costBreakdown": {
            "vertexUsd":    round(totals["vertex"], 6),
            "firestoreUsd": round(totals["firestore"], 6),
            "cloudRunUsd":  round(totals["cloud_run"], 6),
            "storageUsd":   round(totals["storage"], 6),
            "sttUsd":       round(totals["stt"], 6),
        },
        "tokens": tokens,
        "usage": {
            "activeUsers": len(user_ids),
            "sessionCount": session_count,
            "recordingSeconds": rec_seconds,
            "avgCostUsdPerSession": round(avg_cost_per_session, 6),
        },
        "reconciled": False,   # Phase 5 will flip to true on month-closed ranges
    }


# ---------------------------------------------------------------------------
# Timeseries
# ---------------------------------------------------------------------------


@router.get("/timeseries")
async def timeseries(
    from_date: str = Query(...),
    to_date: str = Query(...),
    group_by: str = Query("day", regex="^day$"),
    _admin=Depends(get_current_admin_user),
):
    """Per-day cost / revenue (revenue=0 in Phase 1)."""
    _validate_date_range(from_date, to_date)

    per_day: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"costUsd": 0.0, "vertexUsd": 0.0, "firestoreUsd": 0.0,
                 "cloudRunUsd": 0.0, "storageUsd": 0.0, "sttUsd": 0.0}
    )
    for ev in _iter_events(from_date, to_date):
        key = ev.get("dateKey") or (ev.get("createdAt") or "")[:10]
        if not key:
            continue
        bucket = per_day[key]
        bucket["costUsd"] += _safe_num(ev, "estimatedCostUsd")
        cb = ev.get("costBreakdown") or {}
        bucket["vertexUsd"]    += _safe_num(cb, "vertexUsd")
        bucket["firestoreUsd"] += _safe_num(cb, "firestoreUsd")
        bucket["cloudRunUsd"]  += _safe_num(cb, "cloudRunUsd")
        bucket["storageUsd"]   += _safe_num(cb, "storageUsd")
        bucket["sttUsd"]       += _safe_num(cb, "sttUsd")

    items = [
        {
            "date": date,
            "costUsd":      round(values["costUsd"], 6),
            "vertexUsd":    round(values["vertexUsd"], 6),
            "firestoreUsd": round(values["firestoreUsd"], 6),
            "cloudRunUsd":  round(values["cloudRunUsd"], 6),
            "storageUsd":   round(values["storageUsd"], 6),
            "sttUsd":       round(values["sttUsd"], 6),
            "revenueJpy":   0.0,
            "grossProfitJpy": round(-_usd_to_jpy(values["costUsd"]), 2),
        }
        for date, values in sorted(per_day.items())
    ]
    return {"items": items, "groupBy": group_by}


# ---------------------------------------------------------------------------
# Top users
# ---------------------------------------------------------------------------


@router.get("/top-users")
async def top_users(
    from_date: str = Query(...),
    to_date: str = Query(...),
    limit: int = Query(20, ge=1, le=200),
    _admin=Depends(get_current_admin_user),
):
    _validate_date_range(from_date, to_date)

    per_user: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "userId": None, "accountId": None,
            "costUsd": 0.0, "sessionIds": set(),
            "eventCount": 0, "inputTokens": 0, "outputTokens": 0,
            "features": defaultdict(float),
        }
    )
    for ev in _iter_events(from_date, to_date):
        uid = ev.get("userId")
        if not uid:
            continue
        row = per_user[uid]
        row["userId"] = uid
        row["accountId"] = row["accountId"] or ev.get("accountId")
        row["costUsd"] += _safe_num(ev, "estimatedCostUsd")
        row["eventCount"] += 1
        billable = ev.get("billable") or {}
        row["inputTokens"]  += int(_safe_num(billable, "inputTokens"))
        row["outputTokens"] += int(_safe_num(billable, "outputTokens"))
        if ev.get("sessionId"):
            row["sessionIds"].add(ev["sessionId"])
        if ev.get("feature"):
            row["features"][ev["feature"]] += _safe_num(ev, "estimatedCostUsd")

    ordered = sorted(per_user.values(), key=lambda r: r["costUsd"], reverse=True)[:limit]

    return {
        "items": [
            {
                "userId": r["userId"],
                "accountId": r["accountId"],
                "costUsd":   round(r["costUsd"], 6),
                "costJpy":   round(_usd_to_jpy(r["costUsd"]), 2),
                "sessionCount": len(r["sessionIds"]),
                "eventCount":   r["eventCount"],
                "inputTokens":  r["inputTokens"],
                "outputTokens": r["outputTokens"],
                "topFeature": max(r["features"].items(), key=lambda x: x[1], default=(None, 0))[0],
            }
            for r in ordered
        ],
    }


# ---------------------------------------------------------------------------
# Top sessions
# ---------------------------------------------------------------------------


@router.get("/top-sessions")
async def top_sessions(
    from_date: str = Query(...),
    to_date: str = Query(...),
    limit: int = Query(20, ge=1, le=200),
    _admin=Depends(get_current_admin_user),
):
    _validate_date_range(from_date, to_date)

    per_session: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"sessionId": None, "ownerUid": None, "accountId": None,
                 "costUsd": 0.0, "eventCount": 0,
                 "inputTokens": 0, "outputTokens": 0,
                 "features": defaultdict(float)}
    )
    for ev in _iter_events(from_date, to_date):
        sid = ev.get("sessionId")
        if not sid:
            continue
        row = per_session[sid]
        row["sessionId"] = sid
        row["ownerUid"] = row["ownerUid"] or ev.get("userId")
        row["accountId"] = row["accountId"] or ev.get("accountId")
        row["costUsd"] += _safe_num(ev, "estimatedCostUsd")
        row["eventCount"] += 1
        billable = ev.get("billable") or {}
        row["inputTokens"]  += int(_safe_num(billable, "inputTokens"))
        row["outputTokens"] += int(_safe_num(billable, "outputTokens"))
        if ev.get("feature"):
            row["features"][ev["feature"]] += _safe_num(ev, "estimatedCostUsd")

    ordered = sorted(per_session.values(), key=lambda r: r["costUsd"], reverse=True)[:limit]

    return {
        "items": [
            {
                "sessionId":   r["sessionId"],
                "ownerUid":    r["ownerUid"],
                "accountId":   r["accountId"],
                "costUsd":     round(r["costUsd"], 6),
                "costJpy":     round(_usd_to_jpy(r["costUsd"]), 2),
                "eventCount":  r["eventCount"],
                "inputTokens": r["inputTokens"],
                "outputTokens":r["outputTokens"],
                "topFeature":  max(r["features"].items(), key=lambda x: x[1], default=(None, 0))[0],
            }
            for r in ordered
        ],
    }


# ---------------------------------------------------------------------------
# Per-feature
# ---------------------------------------------------------------------------


@router.get("/features")
async def features(
    from_date: str = Query(...),
    to_date: str = Query(...),
    _admin=Depends(get_current_admin_user),
):
    _validate_date_range(from_date, to_date)

    per_feature: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"feature": None, "costUsd": 0.0, "callCount": 0,
                 "inputTokens": 0, "outputTokens": 0,
                 "sessionIds": set(), "userIds": set(),
                 "sumDurationMs": 0, "durationCount": 0}
    )
    for ev in _iter_events(from_date, to_date):
        feat = ev.get("feature") or "unknown"
        row = per_feature[feat]
        row["feature"] = feat
        row["costUsd"] += _safe_num(ev, "estimatedCostUsd")
        row["callCount"] += 1
        billable = ev.get("billable") or {}
        row["inputTokens"]  += int(_safe_num(billable, "inputTokens"))
        row["outputTokens"] += int(_safe_num(billable, "outputTokens"))
        if ev.get("sessionId"):
            row["sessionIds"].add(ev["sessionId"])
        if ev.get("userId"):
            row["userIds"].add(ev["userId"])
        dur = ev.get("durationMs")
        if isinstance(dur, (int, float)):
            row["sumDurationMs"] += int(dur)
            row["durationCount"] += 1

    items: List[Dict[str, Any]] = []
    for feat, row in per_feature.items():
        call_count = row["callCount"] or 1
        avg_in = row["inputTokens"] / call_count
        avg_out = row["outputTokens"] / call_count
        avg_cost = row["costUsd"] / call_count
        avg_dur = (row["sumDurationMs"] / row["durationCount"]) if row["durationCount"] else 0
        items.append({
            "feature":         feat,
            "costUsd":         round(row["costUsd"], 6),
            "costJpy":         round(_usd_to_jpy(row["costUsd"]), 2),
            "callCount":       row["callCount"],
            "sessionCount":    len(row["sessionIds"]),
            "userCount":       len(row["userIds"]),
            "inputTokens":     row["inputTokens"],
            "outputTokens":    row["outputTokens"],
            "avgInputTokens":  round(avg_in, 1),
            "avgOutputTokens": round(avg_out, 1),
            "avgCostUsd":      round(avg_cost, 8),
            "avgDurationMs":   round(avg_dur, 1),
        })
    items.sort(key=lambda r: r["costUsd"], reverse=True)
    return {"items": items}
