"""DeepNote Scheduled Tasks REST surface.

Routes:
    POST   /v1/scheduled-tasks                 create
    GET    /v1/scheduled-tasks                 list (caller's account)
    PATCH  /v1/scheduled-tasks/{taskId}        update
    DELETE /v1/scheduled-tasks/{taskId}        delete

    POST   /internal/scheduler/tick            Cloud Scheduler entry —
                                                processes due tasks and
                                                fires DM notifications
                                                (non-DM destinations
                                                require explicit human
                                                opt-in via Smart Share
                                                Lv3 — Phase B+).

Auth: user routes require Firebase ID token; ``/internal/scheduler/tick``
is unauthenticated (callable by Cloud Scheduler in-VPC). For now we
gate it on a header ``X-DeepNote-Internal-Token`` against the existing
internal-tasks shared secret if set; otherwise we accept all requests
behind Cloud Run IAM (deploy with ``--no-allow-unauthenticated`` if
strict isolation needed).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser
from app.services import scheduled_tasks as _st
from app.services import notifications as _notif

logger = logging.getLogger("app.routes.scheduled_tasks")

router = APIRouter(prefix="/v1/scheduled-tasks", tags=["Scheduled Tasks"])
internal_router = APIRouter(prefix="/internal", tags=["Internal Tasks"])


class ScheduledTaskRequest(BaseModel):
    type: Optional[str] = "custom"
    channel: Optional[str] = "slack"
    destination: Optional[Dict[str, Any]] = None
    rrule: str = Field(..., description="RFC 5545 subset, e.g. 'FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0'")
    timezone: Optional[str] = "Asia/Tokyo"
    enabled: Optional[bool] = True
    filters: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None


class ScheduledTaskPatch(BaseModel):
    type: Optional[str] = None
    channel: Optional[str] = None
    destination: Optional[Dict[str, Any]] = None
    rrule: Optional[str] = None
    timezone: Optional[str] = None
    enabled: Optional[bool] = None
    filters: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None


@router.post("")
def create_task(req: ScheduledTaskRequest, current_user: CurrentUser = Depends(get_current_user)):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    try:
        return _st.create(account_id, body=req.model_dump(exclude_none=True))
    except _st.InvalidRRuleError as e:
        raise HTTPException(status_code=400,
                            detail={"code": "invalid_rrule", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("")
def list_tasks(current_user: CurrentUser = Depends(get_current_user)):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    return {"tasks": _st.list_for(account_id)}


@router.get("/{task_id}")
def get_task(task_id: str, current_user: CurrentUser = Depends(get_current_user)):
    """Single-task fetch (V-037 planned → stable). Account-scoped: a
    user cannot read another account's task."""
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    items = _st.list_for(account_id)
    for t in items:
        if t.get("taskId") == task_id:
            return t
    raise HTTPException(status_code=404, detail="task_not_found")


@router.patch("/{task_id}")
def update_task(task_id: str, req: ScheduledTaskPatch, current_user: CurrentUser = Depends(get_current_user)):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    try:
        res = _st.update(account_id, task_id, req.model_dump(exclude_none=True))
    except _st.InvalidRRuleError as e:
        raise HTTPException(status_code=400,
                            detail={"code": "invalid_rrule", "message": str(e)})
    if res is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    return res


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: str, current_user: CurrentUser = Depends(get_current_user)):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    if not _st.delete(account_id, task_id):
        raise HTTPException(status_code=404, detail="task_not_found")
    return None


# ──────────────────────────────────────────────────────────────────────
# /internal/scheduler/tick — Cloud Scheduler entry point
# ──────────────────────────────────────────────────────────────────────
#
# Hard rules (V-037):
#   1. Fail-closed: if INTERNAL_SCHEDULER_SECRET is missing in env,
#      reject with 503 (service is misconfigured, not "open by default").
#   2. Strict header check: caller must present
#      X-Internal-Scheduler-Secret (legacy X-DeepNote-Internal-Token
#      still accepted for the existing Cloud Scheduler job, gated by
#      same secret).
#   3. Silent fail forbidden: any error from find_due is surfaced as
#      ``errorCode=scheduler_due_query_failed`` with HTTP 500. The
#      caller (Cloud Scheduler) re-tries on 5xx and surfaces the alert.
#   4. Per-task lease: try_acquire_lease must succeed before dispatch.
#      Without a lease, the tick reports the task as ``skipped`` and
#      moves on. lastRunSlot blocks duplicate dispatch for the same
#      runSlot if a previous tick already advanced it.
#   5. Structured ops_events: 11 events emitted via _emit_op so the
#      operator dashboard can see exactly what happened per task.

_SECRET_HEADER_CANONICAL = "X-Internal-Scheduler-Secret"
_SECRET_HEADER_LEGACY = "X-DeepNote-Internal-Token"


def _emit_op(event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit a single structured scheduler ops event. We do not block on
    Firestore writes here — the line goes to stdlib logging which Cloud
    Run automatically forwards to Cloud Logging in JSON form. A future
    PR can also append to a Firestore ``ops_events`` collection if
    needed for in-app dashboards."""
    payload = {"event": event, **fields}
    if level == "warning":
        logger.warning("[scheduler] %s", json.dumps(payload, default=str, ensure_ascii=False))
    elif level == "error":
        logger.error("[scheduler] %s", json.dumps(payload, default=str, ensure_ascii=False))
    else:
        logger.info("[scheduler] %s", json.dumps(payload, default=str, ensure_ascii=False))


@internal_router.post("/scheduler/tick", include_in_schema=False)
async def scheduler_tick(
    request: Request,
    x_internal_scheduler_secret: Optional[str] = Header(None, alias=_SECRET_HEADER_CANONICAL),
    x_deepnote_internal_token: Optional[str] = Header(None, alias=_SECRET_HEADER_LEGACY),
):
    """Cloud Scheduler entry point. Should be called every 5-10 min."""
    expected = os.environ.get("INTERNAL_SCHEDULER_SECRET")
    if not expected:
        # Fail-closed (V-037): refusing to run is the safe default. The
        # operator must explicitly inject a secret for cron to function.
        _emit_op("scheduler_tick_rejected", level="warning",
                 reason="secret_not_configured")
        raise HTTPException(
            status_code=503,
            detail={"code": "scheduler_secret_not_configured",
                    "message": "INTERNAL_SCHEDULER_SECRET env not set on this server."},
        )
    presented = x_internal_scheduler_secret or x_deepnote_internal_token
    if not presented:
        _emit_op("scheduler_tick_rejected", level="warning", reason="missing_header")
        raise HTTPException(
            status_code=401,
            detail={"code": "missing_internal_token",
                    "message": f"{_SECRET_HEADER_CANONICAL} header required."},
        )
    if presented != expected:
        _emit_op("scheduler_tick_rejected", level="warning", reason="bad_secret")
        raise HTTPException(status_code=401, detail={"code": "bad_internal_token"})

    tick_holder_id = f"tick_{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc)
    _emit_op("scheduler_tick_started", holderId=tick_holder_id, ts=started_at)

    try:
        rows = _st.find_due()
    except _st.SchedulerDueQueryError as e:
        _emit_op("scheduler_due_query_failed", level="error",
                 holderId=tick_holder_id, error=str(e))
        # 500 so Cloud Scheduler retries and the alert surfaces.
        raise HTTPException(
            status_code=500,
            detail={"code": "scheduler_due_query_failed", "message": str(e)},
        )

    scanned = len(rows)
    dispatched = 0
    skipped = 0
    errors = 0

    for account_id, task in rows:
        task_id = task.get("taskId") or ""
        run_slot = task.get("nextRunAt")
        if run_slot is None:
            skipped += 1
            _emit_op("scheduled_task_dispatch_skipped", holderId=tick_holder_id,
                     accountId=account_id, taskId=task_id, reason="next_run_at_null")
            continue

        # 1. Acquire lease — duplicate-skip if another tick beat us or
        # lastRunSlot already covers this runSlot.
        if not _st.try_acquire_lease(account_id, task_id,
                                     run_slot=run_slot,
                                     lease_holder_id=tick_holder_id):
            skipped += 1
            _emit_op("scheduled_task_duplicate_skipped",
                     holderId=tick_holder_id,
                     accountId=account_id, taskId=task_id, runSlot=run_slot)
            continue
        _emit_op("scheduled_task_leased", holderId=tick_holder_id,
                 accountId=account_id, taskId=task_id, runSlot=run_slot)

        # 2. Dispatch — wrapped so a single failure cannot poison the loop.
        _emit_op("scheduled_task_dispatch_started", holderId=tick_holder_id,
                 accountId=account_id, taskId=task_id,
                 type=task.get("type"), channel=task.get("channel"))
        try:
            _dispatch(account_id, task)
            _st.mark_run(account_id, task_id, success=True, run_slot=run_slot)
            dispatched += 1
            _emit_op("scheduled_task_dispatch_succeeded",
                     holderId=tick_holder_id,
                     accountId=account_id, taskId=task_id, runSlot=run_slot)
            _emit_op("scheduled_task_next_run_advanced",
                     holderId=tick_holder_id,
                     accountId=account_id, taskId=task_id)
        except Exception as e:
            errors += 1
            _emit_op("scheduled_task_dispatch_failed", level="warning",
                     holderId=tick_holder_id,
                     accountId=account_id, taskId=task_id, error=str(e))
            try:
                _st.mark_run(account_id, task_id, success=False,
                             message=str(e), run_slot=run_slot)
            except Exception as _e:
                logger.warning("[scheduler] mark_run after failure failed: %s", _e)

    _emit_op("scheduler_tick_succeeded", holderId=tick_holder_id,
             scanned=scanned, dispatched=dispatched,
             skipped=skipped, errors=errors,
             durationMs=int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000))
    return {
        "scanned": scanned,
        "due": scanned,           # back-compat alias
        "dispatched": dispatched,
        "skipped": skipped,
        "errors": errors,
        "fired": dispatched,      # back-compat alias
        "failures": errors,       # back-compat alias
    }


def _count_open_todos(account_id: str) -> Optional[int]:
    """Cheap count of open TODOs for the digest body. Returns None on
    Firestore failure so the dispatcher can fall back to a generic body
    text without breaking the tick."""
    try:
        from app.services.assistant_briefing import _open_todos as _ot
        rows = _ot(account_id, limit=20)
        return len(rows)
    except Exception as e:
        logger.warning("[dispatcher] open todo count failed: %s", e)
        return None


def _count_recent_sessions(account_id: str, *, days: int = 7) -> Optional[int]:
    """Count of sessions in the last ``days`` for the meeting digest."""
    try:
        from datetime import datetime, timedelta, timezone
        from app.firebase import db as _db
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        n = 0
        for _ in (_db.collection("sessions")
                  .where("ownerAccountId", "==", account_id)
                  .where("createdAt", ">=", cutoff)
                  .limit(50)
                  .stream()):
            n += 1
        return n
    except Exception as e:
        logger.warning("[dispatcher] recent sessions count failed: %s", e)
        return None


def _build_desktop_notification(task: Dict[str, Any]) -> Dict[str, str]:
    """Render title/body for a desktop-channel notification based on
    task type. AI is intentionally NOT used here — this path runs
    every minute and must stay free of LLM latency/cost. Dynamic
    counts come from cheap Firestore reads; on failure we fall back
    to a generic body text.
    """
    t = (task.get("type") or "").lower()
    label = (task.get("label") or "").strip()
    plan = task.get("automationPlan") or {}
    plan_title = (plan.get("title") or "").strip()
    account_id = task.get("accountId") or ""
    if t == "daily_todo_digest" or t == "daily_open_todos":
        n = _count_open_todos(account_id) if account_id else None
        if n is None:
            body = label or "未完了TODOがあります。Desktop で確認してください。"
        elif n == 0:
            body = "未完了TODOはありません。"
        else:
            body = f"未完了TODOが{n}件あります。Desktop で確認してください。"
        return {"title": "今日のTODO", "body": body}
    if t == "weekly_meeting_digest":
        n = _count_recent_sessions(account_id, days=7) if account_id else None
        if n is None:
            body = label or "先週の会議まとめが届きました。Desktop で確認してください。"
        elif n == 0:
            body = "先週の会議はありません。"
        else:
            body = f"先週の会議は{n}件です。Desktop で確認してください。"
        return {"title": "先週の会議まとめ", "body": body}
    if t == "session_followup":
        return {"title": "会議の要約が完了しました",
                "body": label or "要約・決定事項・TODOを確認できます。"}
    if t == "pre_meeting_briefing":
        return {"title": "次の会議のブリーフィング",
                "body": label or "前回までの決定事項と未完了TODOを確認できます。"}
    if t == "smart_share_prompt":
        return {"title": "共有候補の会議があります",
                "body": label or "Desktop で内容を確認して共有してください。"}
    # ── V-042 AutomationPlan-derived task types (PR3 minimum) ──
    if t == "create_mail_draft":
        return {"title": plan_title or "メール下書きの作成確認",
                "body": "会議内容からメール下書きを作成しますか? Desktop で確認してください。"}
    if t == "create_share_draft":
        return {"title": plan_title or "共有下書きの作成確認",
                "body": "Slack / LINE 用の共有下書きを作成しますか?"}
    if t == "create_todo_review":
        return {"title": plan_title or "TODO 候補のレビュー",
                "body": "新しい TODO 候補があります。Desktop で承認/却下してください。"}
    if t == "automation":
        return {"title": plan_title or "自動化通知",
                "body": (plan.get("summary") or "DeepNote の自動化が実行されました。")[:200]}
    return {"title": label or "DeepNote 通知",
            "body": "DeepNote から自動通知が届きました。"}


def _dispatch(account_id: str, task: Dict[str, Any]) -> None:
    """Render the task's payload and send it. DM-only delivery.

    Desktop is the V-037 default. line/slack continue to behave as
    before for tasks created with those legacy channels.
    """
    task_type = (task.get("type") or "").lower()
    channel = (task.get("channel") or "").lower()
    task_id = task.get("taskId") or ""
    run_slot = task.get("nextRunAt")
    idempotency_key = (
        _st.build_run_slot_key(task_id, run_slot)
        if (task_id and run_slot) else None
    )

    # ── Desktop: write to notification_events. Idempotent on
    # (accountId, idempotencyKey).
    if channel == "desktop":
        # Carry accountId so _build_desktop_notification can do
        # cheap Firestore counts (TODOs, sessions) for richer body text.
        rendered_task = dict(task)
        rendered_task["accountId"] = account_id
        rendered = _build_desktop_notification(rendered_task)
        ev = _notif.create(
            account_id=account_id,
            notification_type=task_type or "system",
            title=rendered["title"],
            body=rendered["body"],
            source_task_id=task_id,
            idempotency_key=idempotency_key,
            delivery={"channel": "desktop", "status": "pending"},
        )
        if ev.get("_created"):
            _emit_op("notification_event_created",
                     accountId=account_id, taskId=task_id,
                     notificationId=ev["id"], runSlot=run_slot)
        else:
            _emit_op("scheduled_task_duplicate_skipped",
                     accountId=account_id, taskId=task_id,
                     runSlot=run_slot, reason="notification_idempotent")
        return

    # Phase D: dedicated task types delegate to assistant_briefing /
    # follow-up helpers. They handle their own DM fan-out and never
    # touch a public channel.
    if task_type == "pre_meeting_briefing":
        try:
            from app.services import assistant_briefing
            assistant_briefing.deliver_pre_meeting(account_id)
        except Exception as e:
            logger.warning("[scheduler.briefing] failed: %s", e)
        return
    if task_type == "session_followup":
        sid = (task.get("filters") or {}).get("sessionId") or ""
        if sid:
            try:
                from app.services import assistant_briefing
                assistant_briefing.deliver_session_followup(sid, account_id)
            except Exception as e:
                logger.warning("[scheduler.followup] failed: %s", e)
        return

    dest = task.get("destination") or {}
    output = task.get("output") or {}

    # Build text via existing briefing helpers.
    from app.services import line_briefing, slack_briefing
    parts = []
    if output.get("includeSummary", True):
        latest = (line_briefing.get_latest_session(account_id)
                  if channel == "line"
                  else slack_briefing.get_latest_session(account_id))
        if latest:
            parts.append(f"📝 最新の会議: {latest.get('title') or '(無題)'}")
            summ = (latest.get("summary") or "").strip().splitlines()
            if summ:
                parts.append(summ[0][:300])
    if output.get("includeTodos", True):
        todos = (line_briefing.get_recent_todos(account_id, limit=3)
                 if channel == "line"
                 else slack_briefing.get_recent_todos(account_id, limit=3))
        if todos:
            parts.append("\n▼ TODO")
            for t in todos[:3]:
                parts.append(f"・{t.get('title') or t.get('text') or ''}")
    if output.get("includeDecisions", True):
        decisions = (line_briefing.get_latest_decisions(account_id)
                     if channel == "line"
                     else slack_briefing.get_latest_decisions(account_id))
        if decisions:
            parts.append("\n▼ 決定事項")
            for d in decisions[:3]:
                parts.append(f"・{d}")
    text = "\n".join(parts) or "今回の会議サマリーはまだ生成されていません。"

    if channel == "line":
        line_user_id = dest.get("lineUserId")
        if not line_user_id:
            return  # group destinations require Lv3 confirm
        from app.services import line_messaging
        if line_messaging.is_configured():
            line_messaging.push(line_user_id, [line_messaging.text_message(text)])
            return
    if channel == "slack":
        slack_user_id = dest.get("slackUserId")
        team_id = dest.get("teamId") or dest.get("workspaceId")
        if not slack_user_id or not team_id:
            return  # channel destinations require Lv3 confirm
        from app.services.integrations import slack_client
        slack_client.post_message(team_id=team_id, channel=slack_user_id, text=text)
        return
