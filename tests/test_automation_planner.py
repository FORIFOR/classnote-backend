"""Automation Planner / Safety / Compiler unit tests (V-042 / PR2)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.services.automation_schema import (
    AutomationPlan, ScheduleTrigger, ScheduleSpec,
    NotifyTodoDigestAction, CreateMailDraftAction,
    CreateShareDraftAction, AutomationDestination, AutomationExecution,
    AutomationRequirements,
)
from app.services import automation_safety as _safety
from app.services import automation_planner as _planner
from app.services import automation_compiler as _compiler


# ──────────────────────────────────────────────────────────────────────
# Schema validation
# ──────────────────────────────────────────────────────────────────────

def test_schema_minimal_plan_round_trip():
    p = AutomationPlan(
        title="t", summary="s",
        trigger=ScheduleTrigger(schedule=ScheduleSpec(
            rrule="RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            timezone="Asia/Tokyo")),
        action=NotifyTodoDigestAction(),
    )
    d = p.model_dump()
    p2 = AutomationPlan.model_validate(d)
    assert p2.action.type == "notify_todo_digest"
    assert p2.trigger.type == "schedule"


def test_schema_rejects_unknown_action():
    bad = {
        "title": "t", "summary": "s",
        "trigger": {"type": "schedule", "schedule": {
            "kind": "rrule", "rrule": "RRULE:FREQ=DAILY", "timezone": "Asia/Tokyo"}},
        "action": {"type": "totally_made_up"},
    }
    with pytest.raises(Exception):
        AutomationPlan.model_validate(bad)


# ──────────────────────────────────────────────────────────────────────
# Safety rules
# ──────────────────────────────────────────────────────────────────────

def _base_plan(action, destination_channel="desktop"):
    return AutomationPlan(
        title="t", summary="s",
        trigger=ScheduleTrigger(schedule=ScheduleSpec(
            rrule="RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            timezone="Asia/Tokyo")),
        action=action,
        destination=AutomationDestination(channel=destination_channel),
    )


def test_external_channel_forces_confirmation():
    plan = _base_plan(NotifyTodoDigestAction(), destination_channel="slack_channel")
    plan = _safety.apply_safety_rules(plan)
    assert plan.execution.requiresConfirmation is True
    assert plan.execution.confirmationChannel == "desktop"
    assert any("外部チャンネル" in w for w in plan.requirements.warnings)


def test_create_mail_draft_always_requires_confirmation():
    plan = _base_plan(CreateMailDraftAction(provider="gmail"))
    plan = _safety.apply_safety_rules(plan)
    assert plan.execution.requiresConfirmation is True
    assert any("メール下書き" in w for w in plan.requirements.warnings)


def test_default_max_runs_per_day_is_3():
    plan = _base_plan(NotifyTodoDigestAction())
    plan = _safety.apply_safety_rules(plan)
    assert plan.execution.maxRunsPerDay == 3


def test_required_integrations_for_share_draft():
    plan = _base_plan(CreateShareDraftAction(channel="slack"),
                     destination_channel="slack_dm")
    needed = _safety.required_integrations_for(plan)
    assert "slack" in needed


def test_required_integrations_for_pre_meeting_briefing():
    from app.services.automation_schema import PreMeetingBriefingAction
    plan = _base_plan(PreMeetingBriefingAction(lookbackDays=14))
    needed = _safety.required_integrations_for(plan)
    assert "google_or_microsoft_calendar" in needed


# ──────────────────────────────────────────────────────────────────────
# Planner stub fallback (LLM not available)
# ──────────────────────────────────────────────────────────────────────

def test_stub_plan_picks_daily_for_morning():
    out = _planner._stub_plan_from_text("毎朝9時に未完了TODOを送って", timezone_name="Asia/Tokyo")
    assert out["trigger"]["schedule"]["rrule"].startswith("RRULE:FREQ=DAILY")
    assert "BYHOUR=9" in out["trigger"]["schedule"]["rrule"]
    assert out["action"]["type"] == "notify_todo_digest"


def test_stub_plan_picks_weekly_monday():
    out = _planner._stub_plan_from_text("毎週月曜9時に先週の会議まとめをSlackに", timezone_name="Asia/Tokyo")
    rrule = out["trigger"]["schedule"]["rrule"]
    assert "FREQ=WEEKLY" in rrule
    assert "BYDAY=MO" in rrule


def test_stub_plan_picks_mail_draft_for_email_keyword():
    out = _planner._stub_plan_from_text("会議後にメールの下書きを作って", timezone_name="Asia/Tokyo")
    assert out["action"]["type"] == "create_mail_draft"
    assert out["action"]["provider"] == "gmail"


# ──────────────────────────────────────────────────────────────────────
# Compiler
# ──────────────────────────────────────────────────────────────────────

def test_compiler_refuses_when_missing_integrations(monkeypatch):
    plan = _base_plan(CreateMailDraftAction(provider="gmail"))
    plan = _safety.apply_safety_rules(plan)
    plan.requirements.missingIntegrations = ["gmail"]
    with pytest.raises(_compiler.CompileError) as exc:
        _compiler.confirm_plan_to_scheduled_task(
            account_id="a", user_id="u", plan=plan, plan_id="p1",
        )
    assert exc.value.code == "missing_integrations"


def test_compiler_refuses_when_questions_open(monkeypatch):
    plan = _base_plan(NotifyTodoDigestAction())
    plan = _safety.apply_safety_rules(plan)
    from app.services.automation_schema import AutomationQuestion
    plan.questions = [AutomationQuestion(
        field="time", question="何時にしますか?", options=["9:00", "10:00"]
    )]
    with pytest.raises(_compiler.CompileError) as exc:
        _compiler.confirm_plan_to_scheduled_task(
            account_id="a", user_id="u", plan=plan, plan_id="p2",
        )
    assert exc.value.code == "plan_has_open_questions"


def test_compiler_legacy_type_mapping():
    plan = _base_plan(NotifyTodoDigestAction())
    assert _compiler._legacy_type(plan) == "daily_todo_digest"
    plan = _base_plan(CreateMailDraftAction(provider="gmail"))
    assert _compiler._legacy_type(plan) == "create_mail_draft"


def test_compiler_writes_to_firestore(monkeypatch):
    plan = _base_plan(NotifyTodoDigestAction())
    plan = _safety.apply_safety_rules(plan)

    captured: list = []
    fake_doc = MagicMock()
    fake_doc.set = lambda payload: captured.append(payload)
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.collection.return_value.document.return_value = fake_doc
    monkeypatch.setattr(_compiler, "db", fake_db)

    res = _compiler.confirm_plan_to_scheduled_task(
        account_id="acct-A", user_id="uid-A", plan=plan, plan_id="plan-1",
    )
    assert res["taskId"].startswith("st_")
    assert res["accountId"] == "acct-A"
    assert res["sourcePlanId"] == "plan-1"
    assert res["enabled"] is True
    assert res["nextRunAt"] is not None
    assert captured and captured[0]["taskId"] == res["taskId"]
    assert captured[0]["automationPlan"]["action"]["type"] == "notify_todo_digest"
    assert captured[0]["v2"] is True
