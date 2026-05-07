"""DeepNote data lookups for the LINE bot Phase 1 reply set.

Phase 1 scope (per release unit):
  - credit balance       : ai_credits.AICreditService.get_credit_report
  - latest session       : sessions where ownerAccountId == X order by createdAt desc
  - recent open todos    : todos where accountId == X and status == "open"
  - decisions            : sessions/{id}/artifacts/summary_v2 -> "decisions"

Out of scope (Phase 1):
  - PDF / DOCX / PPTX export delivery
  - LIFF asset views
  - group / room support
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.firebase import db

logger = logging.getLogger("app.services.line_briefing")


def get_credit_summary(account_id: str) -> Optional[Dict[str, Any]]:
    """Return {plan, remaining, monthlyLimit, unlimited} or None on failure."""
    try:
        from app.services.ai_credits import ai_credits
        report = ai_credits.get_credit_report(account_id)
        return {
            "plan": report.get("plan"),
            "remaining": report.get("remaining"),
            "monthlyLimit": report.get("monthlyLimit"),
            "topupCredits": report.get("topupCredits", 0),
            "used": report.get("used", 0),
            "unlimited": bool(report.get("unlimitedCredits")),
        }
    except Exception as e:
        logger.warning("[line_briefing] credit lookup failed: %s", e)
        return None


def get_latest_session(account_id: str) -> Optional[Dict[str, Any]]:
    """Return {id, title, createdAt, summary} for the most recent session."""
    try:
        query = (
            db.collection("sessions")
            .where("ownerAccountId", "==", account_id)
            .limit(20)
        )
        rows = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            rows.append((snap.id, data))
        if not rows:
            return None
        # Sort in Python (composite index avoidance, matches list_sessions pattern).
        rows.sort(
            key=lambda kv: kv[1].get("createdAt") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        sid, data = rows[0]
        return {
            "id": sid,
            "title": data.get("title") or data.get("name") or "(無題の会議)",
            "createdAt": data.get("createdAt"),
            "summary": (data.get("summary") or data.get("summary_text") or "")[:500],
        }
    except Exception as e:
        logger.warning("[line_briefing] latest session lookup failed: %s", e)
        return None


def get_recent_todos(account_id: str, *, limit: int = 3) -> List[Dict[str, Any]]:
    """Return up to `limit` open todos sorted by dueDate / createdAt."""
    try:
        query = (
            db.collection("todos")
            .where("accountId", "==", account_id)
            .limit(50)
        )
        rows = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            if data.get("status") and data["status"] != "open":
                continue
            rows.append({
                "id": snap.id,
                "title": data.get("title") or "(無題のTODO)",
                "dueDate": data.get("dueDate"),
                "createdAt": data.get("createdAt"),
            })
        # Sort: dueDate ascending (None last), then createdAt descending.
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        rows.sort(key=lambda r: (
            r.get("dueDate") or far_future,
            -(r.get("createdAt").timestamp() if r.get("createdAt") else 0),
        ))
        return rows[:limit]
    except Exception as e:
        logger.warning("[line_briefing] todos lookup failed: %s", e)
        return []


def get_latest_decisions(account_id: str, *, limit: int = 3) -> List[str]:
    """Pull `decisions` array from the latest session's summary_v2 artifact."""
    latest = get_latest_session(account_id)
    if not latest:
        return []
    try:
        snap = (
            db.collection("sessions")
            .document(latest["id"])
            .collection("artifacts")
            .document("summary_v2")
            .get()
        )
        if not snap.exists:
            return []
        data = snap.to_dict() or {}
        decisions = data.get("decisions") or []
        out = []
        for d in decisions[:limit]:
            text = d.get("text") if isinstance(d, dict) else str(d)
            if text:
                out.append(text)
        return out
    except Exception as e:
        logger.warning("[line_briefing] decisions lookup failed: %s", e)
        return []
