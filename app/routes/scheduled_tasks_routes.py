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

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser
from app.services import scheduled_tasks as _st

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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("")
def list_tasks(current_user: CurrentUser = Depends(get_current_user)):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    return {"tasks": _st.list_for(account_id)}


@router.patch("/{task_id}")
def update_task(task_id: str, req: ScheduledTaskPatch, current_user: CurrentUser = Depends(get_current_user)):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    res = _st.update(account_id, task_id, req.model_dump(exclude_none=True))
    if res is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    return res


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: str, current_user: CurrentUser = Depends(get_current_user)):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    if not _st.delete(account_id, task_id):
        raise HTTPException(status_code=404, detail="task_not_found")
    return None


@internal_router.post("/scheduler/tick", include_in_schema=False)
async def scheduler_tick(
    request: Request,
    x_deepnote_internal_token: Optional[str] = Header(None, alias="X-DeepNote-Internal-Token"),
):
    """Cloud Scheduler entry point. Should be called every 5-10 min.
    Fans out due scheduled_tasks via DM-only delivery in Phase B.
    """
    expected = os.environ.get("INTERNAL_SCHEDULER_SECRET")
    if expected and x_deepnote_internal_token != expected:
        raise HTTPException(status_code=401, detail="bad_internal_token")

    rows = _st.find_due()
    fired = 0
    failures = 0
    for account_id, task in rows:
        try:
            _dispatch(account_id, task)
            _st.mark_run(account_id, task.get("taskId") or "", success=True)
            fired += 1
        except Exception as e:
            logger.warning("[scheduler.tick] dispatch failed acct=%s task=%s: %s",
                           account_id, task.get("taskId"), e)
            try:
                _st.mark_run(account_id, task.get("taskId") or "", success=False, message=str(e))
            except Exception:
                pass
            failures += 1
    return {"due": len(rows), "fired": fired, "failures": failures}


def _dispatch(account_id: str, task: Dict[str, Any]) -> None:
    """Render the task's payload and send it. DM-only delivery."""
    task_type = (task.get("type") or "").lower()

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

    channel = (task.get("channel") or "").lower()
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
