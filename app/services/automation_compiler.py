"""Compile a confirmed AutomationPlan into a row in scheduled_tasks.

Called by ``POST /v1/automations:confirm`` after the user explicitly
agrees. Refuses to compile if the plan still has unresolved
requirements (missing integrations, low confidence asking questions).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.firebase import db
from app.services.automation_schema import AutomationPlan
from app.services.scheduled_tasks import compute_next_run

logger = logging.getLogger("app.services.automation_compiler")


class CompileError(RuntimeError):
    def __init__(self, code: str, detail: Dict[str, Any]):
        self.code = code
        self.detail = detail
        super().__init__(code)


def confirm_plan_to_scheduled_task(
    *,
    account_id: str,
    user_id: str,
    plan: AutomationPlan,
    plan_id: str,
) -> Dict[str, Any]:
    """Persist as a ``scheduled_tasks`` row in the legacy account-scoped
    subcollection (``accounts/{aid}/scheduled_tasks/{taskId}``) so the
    existing tick + dispatcher pipeline picks it up unchanged.
    """
    if plan.requirements.missingIntegrations:
        raise CompileError("missing_integrations", {
            "missing": plan.requirements.missingIntegrations,
        })
    if plan.questions:
        raise CompileError("plan_has_open_questions", {
            "questions": [q.model_dump() for q in plan.questions],
        })

    # nextRunAt only meaningful for schedule triggers
    next_run: Optional[datetime] = None
    rrule: Optional[str] = None
    timezone_name = "Asia/Tokyo"
    if plan.trigger.type == "schedule":
        rrule = plan.trigger.schedule.rrule
        timezone_name = plan.trigger.schedule.timezone
        next_run = compute_next_run(rrule, tzname=timezone_name)
        if next_run is None:
            raise CompileError("invalid_rrule", {"rrule": rrule})

    task_id = f"st_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)

    # The legacy schema fields keep dispatch + audit pipelines working;
    # the v2 fields preserve the full AutomationPlan shape so future
    # editors can show "edit plan" UX without losing context.
    doc: Dict[str, Any] = {
        "taskId": task_id,
        "accountId": account_id,
        "createdBy": user_id,
        "sourcePlanId": plan_id,
        "type": _legacy_type(plan),
        "channel": plan.destination.channel,
        "destination": plan.destination.model_dump(),
        "rrule": rrule or "",
        "timezone": timezone_name,
        "enabled": True,
        "filters": getattr(plan.action, "filters", None) or {},
        "output": {
            "includeSummary": True,
            "includeTodos": True,
            "includeDecisions": True,
            "attachPdf": False,
        },
        "lastRunAt": None,
        "nextRunAt": next_run,
        "createdAt": now,
        "updatedAt": now,
        # ── V-042 plan preservation ──
        "automationPlan": plan.model_dump(),
        "automationPlanId": plan_id,
        "v2": True,
    }
    db.collection("accounts").document(account_id).collection("scheduled_tasks").document(task_id).set(doc)
    logger.info("[automation.compile] task_id=%s plan_id=%s account=%s",
                task_id, plan_id, account_id[:8] + "…")
    return doc


def _legacy_type(plan: AutomationPlan) -> str:
    """Map the AutomationPlan action.type onto the existing
    ``scheduled_tasks.type`` enum that scheduled_task_dispatcher.py
    already understands. New plans whose action has no legacy match
    are stored as ``automation`` and dispatched via the new
    AutomationAction path (see scheduled_tasks_routes._dispatch).
    """
    t = plan.action.type
    if t == "notify_todo_digest":
        return "daily_todo_digest"
    if t == "notify_meeting_digest":
        return "weekly_meeting_digest"
    if t == "pre_meeting_briefing":
        return "pre_meeting_briefing"
    if t == "create_mail_draft":
        return "create_mail_draft"
    if t == "create_share_draft":
        return "create_share_draft"
    if t == "create_todo_review":
        return "create_todo_review"
    return "automation"
