"""DeepNote data lookups for Slack bot Phase 1 — thin wrapper around line_briefing.

Both Slack and LINE Phase 1 expose the same set of replies, so we route
through the existing helpers to avoid duplicate Firestore queries.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services import line_briefing


def get_credit_summary(account_id: str) -> Optional[Dict[str, Any]]:
    return line_briefing.get_credit_summary(account_id)


def get_latest_session(account_id: str) -> Optional[Dict[str, Any]]:
    return line_briefing.get_latest_session(account_id)


def get_recent_todos(account_id: str, *, limit: int = 3) -> List[Dict[str, Any]]:
    return line_briefing.get_recent_todos(account_id, limit=limit)


def get_latest_decisions(account_id: str, *, limit: int = 3) -> List[str]:
    return line_briefing.get_latest_decisions(account_id, limit=limit)
