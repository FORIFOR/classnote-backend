"""AutomationPlan Pydantic schema (V-042 / PR2).

Used by:
  - automation_planner.py — LLM output validation
  - automation_safety.py  — apply hard-rule transforms before confirm
  - automation_compiler.py — convert confirmed plan into a row in
                             ``scheduled_tasks/{taskId}``

The schema is the single source of truth. LLMs are constrained to emit
exactly this shape — anything else is rejected at validation. The LLM
NEVER executes; the dispatcher does.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────
# Trigger
# ──────────────────────────────────────────────────────────────────────

class ScheduleSpec(BaseModel):
    kind: Literal["rrule"] = "rrule"
    rrule: str
    timezone: str = "Asia/Tokyo"


class ScheduleTrigger(BaseModel):
    type: Literal["schedule"] = "schedule"
    schedule: ScheduleSpec


class EventTrigger(BaseModel):
    type: Literal["event"] = "event"
    event: Literal[
        "session_ready",
        "summary_completed",
        "todo_candidate_created",
        "calendar_event_before",
        "calendar_event_ended",
    ]
    offsetMinutes: Optional[int] = None


AutomationTrigger = Union[ScheduleTrigger, EventTrigger]


# ──────────────────────────────────────────────────────────────────────
# Action
# ──────────────────────────────────────────────────────────────────────

class NotifyTodoDigestAction(BaseModel):
    type: Literal["notify_todo_digest"] = "notify_todo_digest"
    filters: Dict[str, Any] = Field(default_factory=dict)


class NotifyMeetingDigestAction(BaseModel):
    type: Literal["notify_meeting_digest"] = "notify_meeting_digest"
    filters: Dict[str, Any] = Field(default_factory=dict)


class CreateMailDraftAction(BaseModel):
    type: Literal["create_mail_draft"] = "create_mail_draft"
    provider: Literal["gmail", "outlook"]
    source: Literal[
        "latest_session", "selected_session", "calendar_event_session",
    ] = "latest_session"
    includeLinks: bool = True
    includePdf: bool = False
    tone: Literal["polite", "short", "formal"] = "polite"


class CreateShareDraftAction(BaseModel):
    type: Literal["create_share_draft"] = "create_share_draft"
    channel: Literal["slack", "line"]
    source: Literal["latest_session", "selected_session"] = "latest_session"
    format: Literal["summary", "decisions", "todos", "full"] = "summary"


class CreateTodoReviewAction(BaseModel):
    type: Literal["create_todo_review"] = "create_todo_review"
    maxCandidates: int = 3
    minConfidence: float = 0.7


class PreMeetingBriefingAction(BaseModel):
    type: Literal["pre_meeting_briefing"] = "pre_meeting_briefing"
    source: Literal["calendar"] = "calendar"
    lookbackDays: int = 30


AutomationAction = Union[
    NotifyTodoDigestAction,
    NotifyMeetingDigestAction,
    CreateMailDraftAction,
    CreateShareDraftAction,
    CreateTodoReviewAction,
    PreMeetingBriefingAction,
]


# ──────────────────────────────────────────────────────────────────────
# Destination, execution, requirements
# ──────────────────────────────────────────────────────────────────────

class AutomationDestination(BaseModel):
    channel: Literal[
        "desktop", "ios",
        "line_dm", "slack_dm", "email",
        "slack_channel", "line_group",
    ] = "desktop"
    target: Optional[str] = "self"


class AutomationExecution(BaseModel):
    mode: Literal["cloud", "local_desktop"] = "cloud"
    requiresConfirmation: bool = False
    confirmationChannel: Optional[
        Literal["desktop", "ios", "line_dm", "slack_dm"]
    ] = None
    maxRunsPerDay: Optional[int] = None


class AutomationRequirements(BaseModel):
    missingIntegrations: List[str] = Field(default_factory=list)
    requiresPaidCredits: bool = False
    warnings: List[str] = Field(default_factory=list)


class AutomationQuestion(BaseModel):
    field: str
    question: str
    options: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# AutomationPlan (root)
# ──────────────────────────────────────────────────────────────────────

class AutomationPlan(BaseModel):
    title: str
    summary: str
    trigger: AutomationTrigger
    action: AutomationAction
    destination: AutomationDestination = Field(default_factory=AutomationDestination)
    execution: AutomationExecution = Field(default_factory=AutomationExecution)
    requirements: AutomationRequirements = Field(default_factory=AutomationRequirements)
    confidence: float = Field(default=0.5, ge=0, le=1)
    questions: List[AutomationQuestion] = Field(default_factory=list)
