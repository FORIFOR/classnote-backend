"""DeepNote Smart Share — safe-by-default summary notifications.

This module replaces the earlier "auto-share-to-group" prototype
(``bot_auto_share``) which posted full summaries into a Slack channel /
LINE group automatically. Pushing un-reviewed AI output that may contain
PII, mis-recognised names, or confidential decisions into a public space
is unsafe by default, so we walked it back.

Design (4-level model):

  Lv0  no automation                              (default)
  Lv1  notify summary ready (DM only)             ← this module
  Lv2  send full digest to user's own DM only     ← this module
  Lv3  team share with confirmation card          (next phase)
  Lv4  fully automatic team posting               (NOT implemented)

Storage on the bot link doc::

    users/{lineLinks|slackLinks}/{linkUserId}.notifyOnSummaryReady: bool
    users/{lineLinks|slackLinks}/{linkUserId}.dmDigestOnSummary:    bool

Both are user-controlled per provider via DM commands. Both target the
linked user's own DM — never a group / channel — so we never leak
content into a workspace where the user did not press a confirm button.
The legacy ``autoShareToWorkspaces`` field is intentionally ignored
here; existing entries can be migrated by a one-shot script.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from app.firebase import db

logger = logging.getLogger("app.services.bot_smart_share")

LINE_LINKS = "lineLinks"
SLACK_LINKS = "slackLinks"


def _coll(provider: str) -> Optional[str]:
    return {"line": LINE_LINKS, "slack": SLACK_LINKS}.get(provider)


def set_notify(provider: str, source_user_id: str, enabled: bool) -> bool:
    """Toggle Lv1 (summary-ready notification to this user's DM).
    Returns True if the value actually changed.
    """
    coll = _coll(provider)
    if not coll or not source_user_id:
        return False
    ref = db.collection(coll).document(source_user_id)
    snap = ref.get()
    if not snap.exists:
        return False
    cur = bool((snap.to_dict() or {}).get("notifyOnSummaryReady"))
    if cur == enabled:
        return False
    ref.update({"notifyOnSummaryReady": enabled})
    return True


def set_dm_digest(provider: str, source_user_id: str, enabled: bool) -> bool:
    """Toggle Lv2 (full summary + TODOs sent to this user's DM)."""
    coll = _coll(provider)
    if not coll or not source_user_id:
        return False
    ref = db.collection(coll).document(source_user_id)
    snap = ref.get()
    if not snap.exists:
        return False
    cur = bool((snap.to_dict() or {}).get("dmDigestOnSummary"))
    if cur == enabled:
        return False
    ref.update({"dmDigestOnSummary": enabled})
    return True


def get_settings(provider: str, source_user_id: str) -> Dict[str, bool]:
    coll = _coll(provider)
    if not coll or not source_user_id:
        return {"notifyOnSummaryReady": False, "dmDigestOnSummary": False}
    snap = db.collection(coll).document(source_user_id).get()
    if not snap.exists:
        return {"notifyOnSummaryReady": False, "dmDigestOnSummary": False}
    d = snap.to_dict() or {}
    return {
        "notifyOnSummaryReady": bool(d.get("notifyOnSummaryReady")),
        "dmDigestOnSummary": bool(d.get("dmDigestOnSummary")),
    }


def _links_for_account(account_id: str) -> List[Tuple[str, str, Dict]]:
    """Return [(provider, source_user_id, link_doc), ...] for every bot
    link tied to this account that has notify/digest enabled."""
    out: List[Tuple[str, str, Dict]] = []
    if not account_id:
        return out
    for provider, coll in (("line", LINE_LINKS), ("slack", SLACK_LINKS)):
        try:
            q = db.collection(coll).where("accountId", "==", account_id).limit(50)
            for snap in q.stream():
                d = snap.to_dict() or {}
                if d.get("notifyOnSummaryReady") or d.get("dmDigestOnSummary"):
                    out.append((provider, snap.id, d))
        except Exception as e:
            logger.warning("[smart_share.links_for_account] %s lookup failed: %s", coll, e)
    return out


def _format_short(title: str, mode: str) -> str:
    return f"📝 「{title}」の要約が完了しました" + (f" (モード: {mode})" if mode else "")


def _format_digest(session_data: Dict, summary_text: str, todos: List[str]) -> str:
    title = session_data.get("title") or "(無題)"
    lines = [f"📝 {title} の要約が完了しました。", ""]
    if summary_text:
        snippet = summary_text.strip().splitlines()[0][:240]
        lines.append(snippet)
    if todos:
        lines.append("")
        lines.append("▼ TODO (最大3件)")
        for t in todos[:3]:
            lines.append(f"・{t}")
    lines.append("")
    lines.append("チームへの共有は DeepNote アプリから明示的に行ってください。")
    return "\n".join(lines)


def notify_after_summary(session_id: str, owner_account_id: str, session_data: Optional[Dict] = None) -> int:
    """Best-effort DM notification fan-out. Sends ONLY to linked users'
    private chats — never to a group / channel. Returns the number of
    notifications successfully dispatched.

    Privacy contract:
      - Lv1: short "summary ready" notice with a link
      - Lv2: includes 1-line summary + up to 3 TODOs (still DM-only)
      - Group / channel auto-posting is intentionally unsupported here
        because a confirmation step (Lv3) must always precede team share
    """
    links = _links_for_account(owner_account_id)
    if not links:
        return 0

    if session_data is None:
        try:
            snap = db.collection("sessions").document(session_id).get()
            session_data = snap.to_dict() or {} if snap.exists else {}
        except Exception:
            session_data = {}
    if not session_data:
        return 0

    title = session_data.get("title") or "(無題)"
    mode = session_data.get("mode") or ""

    summary_text = ""
    todos: List[str] = []
    try:
        derived = (
            db.collection("sessions").document(session_id)
            .collection("derived").document("summary").get()
        )
        if derived.exists:
            d = derived.to_dict() or {}
            res = d.get("result") or {}
            summary_text = (res.get("topicSummary") or res.get("markdown") or "")[:600]
    except Exception:
        pass
    try:
        todo_q = (
            db.collection("accounts").document(owner_account_id)
            .collection("todos")
            .where("sessionId", "==", session_id).limit(5)
        )
        for t in todo_q.stream():
            td = t.to_dict() or {}
            txt = td.get("title") or td.get("text")
            if txt:
                todos.append(txt[:80])
    except Exception:
        pass

    sent = 0
    for provider, source_user_id, link in links:
        try:
            wants_digest = bool(link.get("dmDigestOnSummary"))
            text = _format_digest(session_data, summary_text, todos) if wants_digest else _format_short(title, mode)
            if provider == "line":
                from app.services import line_messaging
                if line_messaging.is_configured():
                    line_messaging.push(source_user_id, [line_messaging.text_message(text)])
                    sent += 1
            elif provider == "slack":
                from app.services.integrations import slack_client
                team_id = link.get("teamId") or link.get("workspaceId") or ""
                if not team_id:
                    continue
                slack_client.post_message(team_id=team_id, channel=source_user_id, text=text)
                sent += 1
        except Exception as e:
            logger.warning("[smart_share.notify] %s/%s failed: %s", provider, source_user_id, e)
    if sent:
        logger.info("[smart_share.notify] session=%s account=%s sent=%d", session_id, owner_account_id, sent)
    return sent
