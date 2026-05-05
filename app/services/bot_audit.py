"""Append-only audit trail for LINE / Slack bot interactions.

Every reply that touches user data should call `record(...)`. Storage:
  bot_audit_logs/{auto_id}
    provider:        "line" | "slack"
    sourceType:      "user" | "group" | "room" | "channel" | "im" | "mpim"
    teamId:          str | None     (slack only)
    sourceUserId:    str            (lineUserId or slackUserId)
    accountId:       str | None     (DeepNote account, if linked)
    deepnoteUid:     str | None
    command:         str            ("credit" | "latest" | "todos" | "decisions"
                                     | "assets" | "pdf" | "docx" | "pptx"
                                     | "help" | "unknown" | "unsupported")
    outcome:         "ok" | "blocked_unsupported_source" | "unlinked"
                     | "lookup_failed" | "config_missing"
    at:              datetime (server time)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.firebase import db

logger = logging.getLogger("app.services.bot_audit")

COLLECTION = "bot_audit_logs"


def record(
    *,
    provider: str,
    source_type: str,
    source_user_id: str,
    command: str,
    outcome: str,
    team_id: Optional[str] = None,
    account_id: Optional[str] = None,
    deepnote_uid: Optional[str] = None,
) -> None:
    """Best-effort: never raise from an audit write."""
    try:
        db.collection(COLLECTION).add({
            "provider": provider,
            "sourceType": source_type,
            "sourceUserId": source_user_id,
            "teamId": team_id,
            "accountId": account_id,
            "deepnoteUid": deepnote_uid,
            "command": command,
            "outcome": outcome,
            "at": datetime.now(timezone.utc),
        })
    except Exception as e:
        # Audit write failures must not break user-facing replies.
        logger.warning("[bot_audit] write failed: %s", e)
