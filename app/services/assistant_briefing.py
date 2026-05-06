"""DeepNote — pre-meeting briefing & post-meeting follow-up.

Phase D: connect the AI assistant to the rest of a user's workflow so
they don't have to ask. Two pieces:

1. **Pre-meeting briefing**
   On a daily cadence (typically 7:30 JST) we read the user's Google /
   Microsoft Calendar for the day and compose a single DM:
       「今日の会議 3 件 — 関連する過去の議事録 / 未完了 TODO」
   The user can read it in Slack / LINE before the day starts. We
   never auto-post into a public channel; this is strictly DM.

2. **Post-meeting follow-up**
   24 hours after a session is summarised, we re-check its TODOs.
   Anything still incomplete is DM'd to the linked user as a nudge:
       「昨日の会議「X」の未完了 TODO 2件:
        ・見積書の再提出 (期限 5/10)
        ・顧客資料の修正 (期限 未設定)」
   We never escalate — this is informational, opt-in via the existing
   Smart Share notify toggle.

Both fan-outs reuse the existing ``bot_smart_share`` DM machinery so
LINE / Slack target resolution stays in one place.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.firebase import db

logger = logging.getLogger("app.services.assistant_briefing")


# ──────────────────────────────────────────────────────────────────────
# Pre-meeting briefing
# ──────────────────────────────────────────────────────────────────────

def _fetch_today_events(uid: str) -> List[Dict[str, Any]]:
    """Read today's events from whichever calendar provider the user
    has linked. Best-effort — returns empty list if neither is linked.
    """
    out: List[Dict[str, Any]] = []
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    try:
        from app.services.integrations import google_client
        raw = google_client.list_calendar_events(
            uid=uid, calendar_id="primary",
            time_min=today_start.isoformat(), time_max=today_end.isoformat(),
        )
        for ev in (raw.get("items") or [])[:10]:
            out.append({
                "summary": ev.get("summary") or "(無題)",
                "start": (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date"),
                "end": (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date"),
                "attendees": [a.get("email") for a in (ev.get("attendees") or []) if a.get("email")],
                "source": "google",
            })
    except Exception as e:
        logger.debug("[briefing] google calendar read skipped: %s", e)
    if out:
        return out
    try:
        from app.services.integrations import microsoft_client
        raw = microsoft_client.list_calendar_events(
            uid=uid, start_datetime=today_start.isoformat(), end_datetime=today_end.isoformat(),
        )
        for ev in (raw.get("value") or [])[:10]:
            out.append({
                "summary": ev.get("subject") or "(無題)",
                "start": (ev.get("start") or {}).get("dateTime"),
                "end": (ev.get("end") or {}).get("dateTime"),
                "attendees": [a.get("emailAddress", {}).get("address")
                              for a in (ev.get("attendees") or [])
                              if a.get("emailAddress")],
                "source": "microsoft",
            })
    except Exception as e:
        logger.debug("[briefing] microsoft calendar read skipped: %s", e)
    return out


def _related_past_sessions(account_id: str, query_text: str, *, limit: int = 3) -> List[Dict[str, Any]]:
    """Cheap title-keyword match against the account's recent sessions.
    Phase D doesn't run embeddings here; the briefing card is small."""
    if not account_id or not query_text:
        return []
    out: List[Dict[str, Any]] = []
    try:
        q_terms = [t for t in query_text.split() if len(t) >= 2]
        if not q_terms:
            return []
        snaps = (
            db.collection("sessions")
            .where("ownerAccountId", "==", account_id)
            .limit(40).stream()
        )
        rows = []
        for s in snaps:
            d = s.to_dict() or {}
            title = d.get("title") or ""
            score = sum(title.count(t) for t in q_terms)
            if score:
                rows.append((score, s.id, d))
        rows.sort(key=lambda r: -r[0])
        for _, sid, d in rows[:limit]:
            out.append({"id": sid, "title": d.get("title") or "(無題)"})
    except Exception as e:
        logger.warning("[briefing.related] failed: %s", e)
    return out


def _open_todos(account_id: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    out: List[Dict[str, Any]] = []
    try:
        q = (
            db.collection("accounts").document(account_id)
            .collection("todos")
            .where("status", "in", ["open", "pending", None])
            .limit(limit)
        )
        for t in q.stream():
            d = t.to_dict() or {}
            out.append({"id": t.id, "title": d.get("title") or d.get("text") or "(無題)",
                        "due": d.get("dueDate") or d.get("due"),
                        "assignee": d.get("assignee")})
    except Exception:
        # fallback: no status filter (some todo writers don't set it)
        try:
            q = (
                db.collection("accounts").document(account_id)
                .collection("todos").limit(limit)
            )
            for t in q.stream():
                d = t.to_dict() or {}
                if d.get("completed") or d.get("done"):
                    continue
                out.append({"id": t.id, "title": d.get("title") or d.get("text") or "(無題)",
                            "due": d.get("dueDate") or d.get("due"),
                            "assignee": d.get("assignee")})
        except Exception as e:
            logger.warning("[briefing.todos] failed: %s", e)
    return out


def build_pre_meeting_text(uid: str, account_id: str) -> Optional[str]:
    """Compose the briefing for a single user. Returns None if there is
    nothing to say (no events, no related sessions, no open todos)."""
    events = _fetch_today_events(uid) if uid else []
    todos = _open_todos(account_id)

    if not events and not todos:
        return None

    lines = ["📅 今日のブリーフィング"]
    if events:
        lines.append("")
        lines.append("▼ 本日の会議")
        for ev in events:
            t = ev.get("start") or ""
            t_short = t[11:16] if isinstance(t, str) and len(t) >= 16 else (t or "")
            lines.append(f"・{t_short} {ev.get('summary')}")
            related = _related_past_sessions(account_id, ev.get("summary") or "")
            for r in related:
                lines.append(f"   関連: {r['title']}")
    if todos:
        lines.append("")
        lines.append("▼ 未完了 TODO")
        for t in todos:
            due = t.get("due")
            due_str = f" (期限: {due})" if due else ""
            asg = t.get("assignee")
            asg_str = f" / 担当: {asg}" if asg else ""
            lines.append(f"・{t.get('title')}{due_str}{asg_str}")
    lines.append("")
    lines.append("詳細は DeepNote アプリで確認できます。")
    return "\n".join(lines)


def deliver_pre_meeting(account_id: str) -> int:
    """Fan out the briefing to every linked bot DM for this account.
    Returns count of DMs sent."""
    if not account_id:
        return 0
    sent = 0
    try:
        for provider, coll in (("line", "lineLinks"), ("slack", "slackLinks")):
            q = db.collection(coll).where("accountId", "==", account_id).limit(50)
            for snap in q.stream():
                d = snap.to_dict() or {}
                uid = d.get("deepnoteUid") or ""
                text = build_pre_meeting_text(uid, account_id)
                if not text:
                    continue
                try:
                    if provider == "line":
                        from app.services import line_messaging
                        if line_messaging.is_configured():
                            line_messaging.push(snap.id, [line_messaging.text_message(text)])
                            sent += 1
                    else:
                        team_id = d.get("teamId") or d.get("workspaceId") or ""
                        if team_id:
                            from app.services.integrations import slack_client
                            slack_client.post_message(team_id=team_id, channel=snap.id, text=text)
                            sent += 1
                except Exception as e:
                    logger.warning("[briefing.deliver] %s/%s failed: %s", provider, snap.id, e)
    except Exception as e:
        logger.warning("[briefing.deliver] outer failed: %s", e)
    return sent


# ──────────────────────────────────────────────────────────────────────
# Post-meeting follow-up
# ──────────────────────────────────────────────────────────────────────

def deliver_session_followup(session_id: str, account_id: str) -> int:
    """24h after a session is summarised: surface still-open TODOs to
    the user's bot DM. Best-effort, DM-only."""
    if not session_id or not account_id:
        return 0
    try:
        s_snap = db.collection("sessions").document(session_id).get()
        if not s_snap.exists:
            return 0
        sd = s_snap.to_dict() or {}
    except Exception:
        return 0
    title = sd.get("title") or "(無題)"
    open_items: List[Dict[str, Any]] = []
    try:
        q = (
            db.collection("accounts").document(account_id)
            .collection("todos")
            .where("sessionId", "==", session_id).limit(20)
        )
        for t in q.stream():
            d = t.to_dict() or {}
            if d.get("completed") or d.get("done") or d.get("status") in ("done", "completed"):
                continue
            open_items.append({"title": d.get("title") or d.get("text") or "(無題)",
                                "due": d.get("dueDate") or d.get("due"),
                                "assignee": d.get("assignee")})
    except Exception as e:
        logger.warning("[followup.todos] failed: %s", e)
        return 0

    if not open_items:
        return 0

    lines = [f"🔔 昨日の会議「{title}」の未完了 TODO {len(open_items)} 件"]
    for it in open_items[:8]:
        due = it.get("due")
        due_str = f" (期限: {due})" if due else ""
        asg = it.get("assignee")
        asg_str = f" / 担当: {asg}" if asg else ""
        lines.append(f"・{it.get('title')}{due_str}{asg_str}")
    text = "\n".join(lines)

    sent = 0
    for provider, coll in (("line", "lineLinks"), ("slack", "slackLinks")):
        try:
            q = db.collection(coll).where("accountId", "==", account_id).limit(50)
            for snap in q.stream():
                d = snap.to_dict() or {}
                if not d.get("notifyOnSummaryReady") and not d.get("dmDigestOnSummary"):
                    continue
                if provider == "line":
                    from app.services import line_messaging
                    if line_messaging.is_configured():
                        line_messaging.push(snap.id, [line_messaging.text_message(text)])
                        sent += 1
                else:
                    team_id = d.get("teamId") or d.get("workspaceId") or ""
                    if team_id:
                        from app.services.integrations import slack_client
                        slack_client.post_message(team_id=team_id, channel=snap.id, text=text)
                        sent += 1
        except Exception as e:
            logger.warning("[followup.deliver] %s failed: %s", provider, e)
    return sent


# ──────────────────────────────────────────────────────────────────────
# Share-target suggestions
# ──────────────────────────────────────────────────────────────────────

def suggest_share_targets(account_id: str, session_id: str) -> List[Dict[str, Any]]:
    """Suggest where the user might share this session, ranked by past
    behaviour. Phase D heuristics:
      - workspace destinations the same account has shared *similar*
        sessions to before (title keyword overlap)
      - destinations seen in any sharedToWorkspaceTeams entry on this
        account are surfaced even without overlap
    Returns a list of ``{type, key, score, reason}`` records.
    """
    if not account_id or not session_id:
        return []
    try:
        snap = db.collection("sessions").document(session_id).get()
        if not snap.exists:
            return []
        target = snap.to_dict() or {}
    except Exception:
        return []
    target_title = target.get("title") or ""
    target_tags = target.get("autoTags") or []
    q_terms = [t for t in target_title.split() if len(t) >= 2] + list(target_tags)
    if not q_terms:
        q_terms = [target_title[:8]] if target_title else []

    score: Dict[str, int] = {}
    reason: Dict[str, str] = {}
    try:
        q = (
            db.collection("sessions")
            .where("ownerAccountId", "==", account_id)
            .limit(60).stream()
        )
        for s in q:
            d = s.to_dict() or {}
            if s.id == session_id:
                continue
            ws_keys = d.get("sharedToWorkspaceTeams") or []
            if not ws_keys:
                continue
            t = d.get("title") or ""
            overlap = sum(t.count(term) for term in q_terms)
            for k in ws_keys:
                score[k] = score.get(k, 0) + max(1, overlap)
                if overlap and k not in reason:
                    reason[k] = f"類似タイトルの会議をこの送信先に共有した実績"
                elif k not in reason:
                    reason[k] = "過去にこの送信先へ共有した実績"
    except Exception as e:
        logger.warning("[share_suggest] history scan failed: %s", e)

    out = []
    for k, sc in sorted(score.items(), key=lambda kv: -kv[1])[:5]:
        kind = "slack" if k.startswith("slack:") else ("line" if k.startswith("line:") else "unknown")
        out.append({"type": kind, "key": k, "score": sc, "reason": reason.get(k, "")})
    return out
