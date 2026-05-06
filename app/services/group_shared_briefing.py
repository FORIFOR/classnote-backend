"""Phase 7 — group / channel "shared data only" mode.

Decision matrix for non-DM contexts (LINE group, Slack channel):

  Speaker is linked AND speaker has linked-account session that is
  marked sharedToWorkspaceTeams contains the workspace key
       → return that session's briefing (speaker's own data, but only
         for sessions they explicitly opted to share)
  Otherwise
       → return a "未対応" / "共有データなし" notice. NEVER return
         private data.

Workspace key:
  - Slack: "slack:{teamId}"
  - LINE :  "line:{groupId}"  (groups; rooms also keyed similarly)

Session opt-in field on sessions/{id}:
  sharedToWorkspaceTeams: ["slack:T123", "line:G456", ...]

Phase 7 minimum surface:
  - get_latest_shared_session(account_id, workspace_key) -> dict | None
  - get_recent_shared_decisions(account_id, workspace_key, limit) -> List[str]

We deliberately do NOT expose credit / TODO in groups even when shared,
because those are inherently per-account and would leak personal usage.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.firebase import db

logger = logging.getLogger("app.services.group_shared_briefing")


def _shared_sessions_for(account_id: str, workspace_key: str) -> List[Dict[str, Any]]:
    """Return up to ~20 most recent sessions of this account that are
    explicitly shared into the given workspace."""
    try:
        rows = []
        snaps = (
            db.collection("sessions")
            .where("ownerAccountId", "==", account_id)
            .where("sharedToWorkspaceTeams", "array_contains", workspace_key)
            .limit(20)
            .stream()
        )
        for snap in snaps:
            data = snap.to_dict() or {}
            rows.append((snap.id, data))
        rows.sort(
            key=lambda kv: kv[1].get("createdAt") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return [{"id": sid, **data} for sid, data in rows]
    except Exception as e:
        logger.warning("[group_shared] sessions lookup failed: %s", e)
        return []


def get_latest_shared_session(account_id: str, workspace_key: str) -> Optional[Dict[str, Any]]:
    sessions = _shared_sessions_for(account_id, workspace_key)
    if not sessions:
        return None
    s = sessions[0]
    return {
        "id": s["id"],
        "title": s.get("title") or s.get("name") or "(無題の会議)",
        "createdAt": s.get("createdAt"),
        "summary": (s.get("summary") or s.get("summary_text") or "")[:500],
    }


def get_latest_any_session(account_id: str) -> Optional[Dict[str, Any]]:
    """Latest session of this account regardless of share status. Used to
    surface "you have a new meeting; here's how to share it" hints in
    the group bot when ``sharedToWorkspaceTeams`` matches nothing.

    Privacy: callers MUST only expose the *title* and createdAt — NOT
    the summary/transcript — so this hint never leaks meeting content
    into a workspace where it wasn't explicitly shared.
    """
    if not account_id:
        return None
    try:
        snaps = (
            db.collection("sessions")
            .where("ownerAccountId", "==", account_id)
            .limit(20)
            .stream()
        )
        rows = []
        for snap in snaps:
            data = snap.to_dict() or {}
            rows.append((snap.id, data))
        if not rows:
            return None
        rows.sort(
            key=lambda kv: kv[1].get("createdAt") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        sid, d = rows[0]
        return {
            "id": sid,
            "title": d.get("title") or "(無題)",
            "createdAt": d.get("createdAt"),
        }
    except Exception as e:
        logger.warning("[group_shared] latest_any lookup failed: %s", e)
        return None


def get_recent_shared_decisions(account_id: str, workspace_key: str, *, limit: int = 3) -> List[str]:
    latest = get_latest_shared_session(account_id, workspace_key)
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
        out = []
        for d in (data.get("decisions") or [])[:limit]:
            text = d.get("text") if isinstance(d, dict) else str(d)
            if text:
                out.append(text)
        return out
    except Exception as e:
        logger.warning("[group_shared] decisions lookup failed: %s", e)
        return []
