"""Hard-rule transforms applied to every AutomationPlan.

Runs after Pydantic validation and after planner LLM output is parsed,
but before the plan is persisted. Adjusts the plan in place (or
returns a new immutable copy) so dangerous combinations cannot reach
the user-facing confirm step.

Rules (V-042):
  R1. External-channel destinations always require explicit confirmation
      and add a user-facing warning.
  R2. ``create_mail_draft`` always requires confirmation. We never
      create a draft directly from a fired tick; we surface a
      "create draft?" notification first.
  R3. ``create_share_draft`` for slack_channel / line_group is forbidden
      from auto-firing — the channel is forced to ``desktop`` for the
      confirmation step.
  R4. Daily run cap defaults to 3 if not set.
  R5. Auto-send mail or auto-share to public channels is impossible by
      construction (the dispatcher in scheduled_task_dispatcher.py
      only emits notifications for these action types; the actual send
      is gated on a separate user tap).
"""
from __future__ import annotations

import logging
from typing import List

from app.services.automation_schema import AutomationPlan

logger = logging.getLogger("app.services.automation_safety")


EXTERNAL_CHANNELS = {"slack_channel", "line_group", "email"}
PROVIDER_INTEGRATIONS = {
    "slack_dm": "slack",
    "slack_channel": "slack",
    "line_dm": "line",
    "line_group": "line",
}


def apply_safety_rules(plan: AutomationPlan) -> AutomationPlan:
    """Mutate plan in place to enforce hard rules; return same plan."""
    warnings: List[str] = list(plan.requirements.warnings)

    # R1
    if plan.destination.channel in EXTERNAL_CHANNELS:
        plan.execution.requiresConfirmation = True
        if plan.execution.confirmationChannel is None:
            plan.execution.confirmationChannel = "desktop"
        warnings.append(
            "外部チャンネルへの共有は自動送信せず、確認付きで実行します。"
        )

    # R2
    if plan.action.type == "create_mail_draft":
        plan.execution.requiresConfirmation = True
        warnings.append("メール下書きは確認後に作成します(自動送信はしません)。")

    # R3
    if plan.action.type == "create_share_draft" and plan.action.channel in (
        "slack", "line"
    ) and plan.destination.channel in EXTERNAL_CHANNELS:
        plan.execution.requiresConfirmation = True
        warnings.append(
            "公開チャンネル / グループへの共有下書きは、確認 (Desktop通知) 経由でのみ作成します。"
        )

    # R4 default cap
    if plan.execution.maxRunsPerDay is None:
        plan.execution.maxRunsPerDay = 3

    # Dedup warnings
    seen = set()
    deduped: List[str] = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            deduped.append(w)
    plan.requirements.warnings = deduped
    return plan


def required_integrations_for(plan: AutomationPlan) -> List[str]:
    """Return the integration providers this plan needs to execute.
    Used by ``attach_requirements`` to populate ``missingIntegrations``."""
    needed: List[str] = []
    # destination channel
    if plan.destination.channel in PROVIDER_INTEGRATIONS:
        needed.append(PROVIDER_INTEGRATIONS[plan.destination.channel])
    # action specifics
    if plan.action.type == "create_mail_draft":
        if plan.action.provider == "gmail":
            needed.append("google_mail")
        elif plan.action.provider == "outlook":
            needed.append("microsoft_mail")
    if plan.action.type == "pre_meeting_briefing":
        # either provider's calendar
        needed.append("google_or_microsoft_calendar")
    if plan.action.type == "create_share_draft":
        if plan.action.channel == "slack":
            needed.append("slack")
        elif plan.action.channel == "line":
            needed.append("line")
    # dedup
    return sorted({*needed})
