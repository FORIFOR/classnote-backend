"""Phase 3: scheduled morning digests for LINE / Slack linked users.

Design:
  Cloud Scheduler (or any cron) calls
      POST /internal/tasks/run_morning_digests
  with a shared bearer (DIGEST_INTERNAL_TOKEN) at e.g. 08:00 JST daily.

  This module enumerates every linked user across both providers, builds a
  brief digest from line_briefing helpers, and posts via line_messaging /
  slack_client. No client-side cron logic needed.

  Phase 3 scope (stays small):
    - 1:1 only (already enforced — every linked user is 1:1 in Phase 1)
    - Skip users whose preference says digest=false (Phase 7 will surface
      a UI toggle; for now everyone receives unless `digestDisabled` flag
      is set on the link doc)
    - Best effort: a single user failing must not block the rest
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.firebase import db
from app.services import line_briefing, line_messaging
from app.services.integrations import slack_client

logger = logging.getLogger("app.services.digest_scheduler")

LINE_USER_LINKS_COLLECTION = "line_user_links"
SLACK_USER_LINKS_COLLECTION = "slack_user_links"


def _build_digest_text(account_id: str) -> str:
    """Compose a short morning digest for one user."""
    parts: List[str] = []
    parts.append("おはようございます。DeepNote の朝のダイジェストです。")

    credit = line_briefing.get_credit_summary(account_id)
    if credit:
        if credit.get("unlimited"):
            parts.append("クレジット: 無制限プラン")
        else:
            parts.append(
                f"クレジット: {credit.get('remaining', '-')} / {credit.get('monthlyLimit', '-')}"
            )

    latest = line_briefing.get_latest_session(account_id)
    if latest:
        title = latest.get("title") or "(無題)"
        parts.append(f"最新の会議: {title}")

    todos = line_briefing.get_recent_todos(account_id, limit=3)
    if todos:
        lines = ["未完了TODO (上位3件):"]
        for t in todos:
            due = t.get("dueDate")
            due_str = ""
            if isinstance(due, str):
                due_str = f"（期限: {due}）"
            elif due is not None:
                try:
                    due_str = f"（期限: {due.strftime('%Y-%m-%d')}）"
                except Exception:
                    pass
            lines.append(f"・{t.get('title') or '(無題)'}{due_str}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _line_links() -> List[Dict[str, Any]]:
    out = []
    for snap in db.collection(LINE_USER_LINKS_COLLECTION).stream():
        data = snap.to_dict() or {}
        if data.get("digestDisabled"):
            continue
        if not data.get("accountId") or not snap.id:
            continue
        out.append({"lineUserId": snap.id, "accountId": data["accountId"]})
    return out


def _slack_links() -> List[Dict[str, Any]]:
    out = []
    for snap in db.collection(SLACK_USER_LINKS_COLLECTION).stream():
        data = snap.to_dict() or {}
        if data.get("digestDisabled"):
            continue
        if not data.get("accountId") or not data.get("teamId") or not data.get("slackUserId"):
            continue
        out.append({
            "teamId": data["teamId"],
            "slackUserId": data["slackUserId"],
            "accountId": data["accountId"],
        })
    return out


def run_morning_digests() -> Dict[str, int]:
    """Push the digest to every linked user. Returns counts by provider."""
    sent_line = 0
    sent_slack = 0
    failed = 0

    for link in _line_links():
        try:
            text = _build_digest_text(link["accountId"])
            line_messaging.push(link["lineUserId"], [line_messaging.text_message(text)])
            sent_line += 1
        except Exception as e:
            failed += 1
            logger.warning("[digest.line] push failed for line=%s: %s", link.get("lineUserId"), e)

    for link in _slack_links():
        try:
            text = _build_digest_text(link["accountId"])
            # Slack DM channel id is not in the link doc; we open via chat.postMessage
            # using the user id as channel which works for bots that have im:write.
            slack_client.post_message(
                team_id=link["teamId"],
                channel=link["slackUserId"],  # Slack accepts user ID as DM channel
                text=text,
            )
            sent_slack += 1
        except Exception as e:
            failed += 1
            logger.warning("[digest.slack] post failed for team=%s user=%s: %s",
                           link.get("teamId"), link.get("slackUserId"), e)

    logger.info("[digest] sent line=%d slack=%d failed=%d", sent_line, sent_slack, failed)
    return {"line": sent_line, "slack": sent_slack, "failed": failed}
