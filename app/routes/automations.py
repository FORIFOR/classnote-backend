"""DeepNote Automation Planner REST surface (V-042 / PR2).

Routes:
    POST /v1/automations:plan
    POST /v1/automations/{planId}:revise
    POST /v1/automations:confirm
    GET  /v1/automations/{planId}     (helper for fetching a draft)

The planner produces an AutomationPlan; the user reviews it on the
client UI; only after explicit confirm does the plan get compiled
into a ``scheduled_tasks`` row that the existing tick pipeline picks up.
LLM never executes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser
from app.services import automation_planner as _planner
from app.services import automation_compiler as _compiler
from app.services.automation_schema import AutomationPlan

logger = logging.getLogger("app.routes.automations")

router = APIRouter(prefix="/v1/automations", tags=["Automations"])


class PlanRequest(BaseModel):
    text: str
    surface: Optional[str] = "desktop"
    timezone: Optional[str] = "Asia/Tokyo"


class PlanResponseEnvelope(BaseModel):
    planId: str
    title: str
    summary: str
    plan: Dict[str, Any]
    expiresAt: Optional[Any] = None


class ReviseRequest(BaseModel):
    message: str


class ConfirmRequest(BaseModel):
    planId: str


class ConfirmResponse(BaseModel):
    taskId: str
    status: str
    nextRunAt: Optional[Any] = None


@router.post(":plan", response_model=PlanResponseEnvelope)
async def plan_automation(
    body: PlanRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail={"code": "text_required"})
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    rec = await _planner.plan_automation(
        account_id=account_id,
        user_id=current_user.uid,
        text=body.text,
        surface=body.surface or "desktop",
        timezone_name=body.timezone or "Asia/Tokyo",
    )
    plan = rec["plan"]
    return PlanResponseEnvelope(
        planId=rec["id"],
        title=plan.get("title", ""),
        summary=plan.get("summary", ""),
        plan=plan,
        expiresAt=rec.get("expiresAt"),
    )


@router.post("/{plan_id}:revise", response_model=PlanResponseEnvelope)
async def revise_automation(
    plan_id: str,
    body: ReviseRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    if not (body.message or "").strip():
        raise HTTPException(status_code=400, detail={"code": "message_required"})
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    try:
        rec = await _planner.revise_automation_plan(
            plan_id=plan_id, account_id=account_id,
            user_id=current_user.uid, message=body.message,
        )
    except _planner.PlanNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "plan_not_found"})
    except _planner.PlanFinalizedError as e:
        raise HTTPException(status_code=409, detail={"code": "plan_finalized",
                                                       "message": str(e)})
    plan = rec["plan"]
    return PlanResponseEnvelope(
        planId=rec["id"], title=plan.get("title", ""),
        summary=plan.get("summary", ""), plan=plan,
        expiresAt=rec.get("expiresAt"),
    )


@router.get("/{plan_id}", response_model=PlanResponseEnvelope)
def get_plan(
    plan_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    rec = _planner.get_plan(plan_id, account_id=account_id)
    if not rec:
        raise HTTPException(status_code=404, detail={"code": "plan_not_found"})
    plan = rec["plan"]
    return PlanResponseEnvelope(
        planId=rec["id"], title=plan.get("title", ""),
        summary=plan.get("summary", ""), plan=plan,
        expiresAt=rec.get("expiresAt"),
    )


@router.post(":confirm", response_model=ConfirmResponse)
def confirm_automation(
    body: ConfirmRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    rec = _planner.get_plan(body.planId, account_id=account_id)
    if not rec:
        raise HTTPException(status_code=404, detail={"code": "plan_not_found"})
    if rec.get("status") != "draft":
        raise HTTPException(status_code=409, detail={"code": "plan_not_draft",
                                                       "status": rec.get("status")})
    plan = AutomationPlan.model_validate(rec["plan"])
    try:
        task = _compiler.confirm_plan_to_scheduled_task(
            account_id=account_id, user_id=current_user.uid,
            plan=plan, plan_id=body.planId,
        )
    except _compiler.CompileError as e:
        raise HTTPException(status_code=409,
                            detail={"code": e.code, **e.detail})
    _planner.mark_plan_confirmed(body.planId, task_id=task["taskId"])
    return ConfirmResponse(
        taskId=task["taskId"],
        status="enabled",
        nextRunAt=task.get("nextRunAt"),
    )
