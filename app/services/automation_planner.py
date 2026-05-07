"""Natural-language → AutomationPlan converter (V-042 / PR2).

The planner is the only place we let an LLM touch automation. It
produces a JSON object that we constrain to ``AutomationPlan`` via
Pydantic. Anything the LLM can't articulate is captured in
``questions`` so the user can clarify in a follow-up turn instead of
the LLM guessing dangerous defaults.

Storage: ``automation_plans/{planId}`` (root collection, 7-day TTL).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.firebase import db
from app.services.automation_schema import (
    AutomationPlan,
    AutomationDestination,
    AutomationExecution,
    AutomationRequirements,
    NotifyTodoDigestAction,
    NotifyMeetingDigestAction,
    PreMeetingBriefingAction,
    CreateMailDraftAction,
    CreateShareDraftAction,
    CreateTodoReviewAction,
    ScheduleTrigger, ScheduleSpec,
)
from app.services.automation_safety import (
    apply_safety_rules,
    required_integrations_for,
    PROVIDER_INTEGRATIONS,
)
from app.services.integrations import store as _int_store

logger = logging.getLogger("app.services.automation_planner")

PLAN_COLLECTION = "automation_plans"
PLAN_TTL_DAYS = 7


# ──────────────────────────────────────────────────────────────────────
# LLM prompt
# ──────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
あなたは DeepNote Automation Planner です。
ユーザーの自然文を、必ず AutomationPlan JSON に変換してください。

ルール:
- 外部送信や共有は execution.requiresConfirmation=true を必ず付ける
- Gmail/Outlook は初期は下書き作成 (create_mail_draft) のみ
- Slack channel / LINE group には自動投稿しない
- 不明点があれば questions に入れ、confidence を 0.5 以下に下げる
- RRULE は RFC 5545 形式 (例: "RRULE:FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0")
- timezone が不明なら "Asia/Tokyo"
- 会議関連は DeepNote session / calendar_event を使う
- TODO 提案は最大3件を基本にする (maxCandidates=3)
- ユーザー確認なしに実行する前提のplanを作らない

action.type 一覧:
- notify_todo_digest        : 未完了 TODO の digest を通知 (filters)
- notify_meeting_digest     : 会議まとめ digest (filters.range = "last_week" 等)
- create_mail_draft         : Gmail/Outlook 下書き作成 (provider, source, tone)
- create_share_draft        : Slack/LINE 共有下書き (channel, source, format)
- create_todo_review        : 会議の TODO 候補レビュー (maxCandidates, minConfidence)
- pre_meeting_briefing      : 会議前ブリーフィング (lookbackDays)

trigger.type 一覧:
- schedule  : RRULE で時刻指定
- event     : session_ready / summary_completed / calendar_event_before 等

destination.channel 一覧:
- desktop / ios / line_dm / slack_dm / email / slack_channel / line_group

JSON 例 (ユーザー: 毎朝9時に未完了TODOを送って):
{
  "title": "毎朝TODO通知",
  "summary": "平日9:00に未完了TODOをDesktopへ通知します。",
  "trigger": {
    "type": "schedule",
    "schedule": {"kind": "rrule", "rrule": "RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0", "timezone": "Asia/Tokyo"}
  },
  "action": {"type": "notify_todo_digest", "filters": {"status": "open"}},
  "destination": {"channel": "desktop", "target": "self"},
  "execution": {"mode": "cloud", "requiresConfirmation": false, "maxRunsPerDay": 3},
  "requirements": {"missingIntegrations": [], "requiresPaidCredits": false, "warnings": []},
  "confidence": 0.92,
  "questions": []
}

必ず JSON のみ返答すること。
"""


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

async def plan_automation(
    *,
    account_id: str,
    user_id: str,
    text: str,
    surface: str = "desktop",
    timezone_name: str = "Asia/Tokyo",
) -> Dict[str, Any]:
    """Run the planner, validate, attach requirements, persist. Returns
    the stored record (with ``id`` and ``plan``).
    """
    raw = await _llm_generate_plan(text, timezone_name=timezone_name, surface=surface)
    plan = AutomationPlan.model_validate(raw)
    plan = apply_safety_rules(plan)
    plan = _attach_requirements(account_id, plan)

    plan_id = f"plan_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    record = {
        "id": plan_id,
        "accountId": account_id,
        "userId": user_id,
        "sourceText": text,
        "surface": surface,
        "status": "draft",
        "plan": plan.model_dump(),
        "createdAt": now,
        "updatedAt": now,
        "expiresAt": now + timedelta(days=PLAN_TTL_DAYS),
    }
    db.collection(PLAN_COLLECTION).document(plan_id).set(record)
    logger.info("[automation.plan] created plan_id=%s account=%s text_len=%d",
                plan_id, account_id[:8] + "…", len(text))
    return record


async def revise_automation_plan(
    *,
    plan_id: str,
    account_id: str,
    user_id: str,
    message: str,
) -> Dict[str, Any]:
    """Apply a free-text revision to the existing plan via the LLM.
    The result is written back as version v2 (just overwrites in
    place, since the plan is short-lived). Validation + safety rules
    re-run."""
    snap = db.collection(PLAN_COLLECTION).document(plan_id).get()
    if not snap.exists:
        raise PlanNotFoundError(plan_id)
    rec = snap.to_dict() or {}
    if rec.get("accountId") != account_id:
        raise PlanNotFoundError(plan_id)
    if rec.get("status") != "draft":
        raise PlanFinalizedError(plan_id, rec.get("status"))

    base_plan = rec.get("plan") or {}
    raw = await _llm_revise_plan(base_plan, message,
                                  timezone_name=rec.get("plan", {}).get("trigger", {}).get("schedule", {}).get("timezone", "Asia/Tokyo"))
    plan = AutomationPlan.model_validate(raw)
    plan = apply_safety_rules(plan)
    plan = _attach_requirements(account_id, plan)

    now = datetime.now(timezone.utc)
    rec["plan"] = plan.model_dump()
    rec["updatedAt"] = now
    db.collection(PLAN_COLLECTION).document(plan_id).set(rec)
    return rec


def get_plan(plan_id: str, *, account_id: str) -> Optional[Dict[str, Any]]:
    snap = db.collection(PLAN_COLLECTION).document(plan_id).get()
    if not snap.exists:
        return None
    rec = snap.to_dict() or {}
    if rec.get("accountId") != account_id:
        return None
    return rec


def mark_plan_confirmed(plan_id: str, *, task_id: str) -> None:
    db.collection(PLAN_COLLECTION).document(plan_id).update({
        "status": "confirmed",
        "confirmedTaskId": task_id,
        "updatedAt": datetime.now(timezone.utc),
    })


# ──────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────

class PlanNotFoundError(LookupError):
    pass


class PlanFinalizedError(RuntimeError):
    def __init__(self, plan_id: str, status: Optional[str]):
        super().__init__(f"plan {plan_id} already {status or 'finalised'}")


async def _llm_generate_plan(text: str, *, timezone_name: str, surface: str) -> Dict[str, Any]:
    """Call Gemini (or the project's existing LLM) to produce a plan
    JSON. The implementation tries the existing ``app.services.llm``
    helpers; if Gemini is unavailable in this environment (tests / dev
    without GOOGLE_CLOUD_PROJECT) we fall back to a rule-based stub
    so the planner is exercisable end-to-end without a live LLM."""
    user_prompt = (
        f"surface: {surface}\n"
        f"timezone: {timezone_name}\n"
        f"自然文: {text}\n"
        f"上のルールに従って AutomationPlan JSON だけを返してください。"
    )
    try:
        from app.services import llm as _llm
        out = await _llm.generate_json_async(  # type: ignore[attr-defined]
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema_hint=AutomationPlan.model_json_schema(),
        )
        if isinstance(out, dict):
            return out
    except Exception as e:
        logger.warning("[automation.plan.llm] failed, falling back: %s", e)

    # Fallback: rule-based heuristic so the endpoint is testable.
    return _stub_plan_from_text(text, timezone_name=timezone_name)


async def _llm_revise_plan(base_plan: Dict[str, Any], message: str,
                            *, timezone_name: str) -> Dict[str, Any]:
    user_prompt = (
        f"以下が現在の AutomationPlan です:\n```json\n"
        f"{json.dumps(base_plan, ensure_ascii=False, indent=2)}\n```\n\n"
        f"ユーザー修正リクエスト: {message}\n\n"
        f"timezone: {timezone_name}\n"
        f"上のルールに従って、修正後の AutomationPlan JSON だけを返してください。"
    )
    try:
        from app.services import llm as _llm
        out = await _llm.generate_json_async(  # type: ignore[attr-defined]
            system=SYSTEM_PROMPT, user=user_prompt,
            schema_hint=AutomationPlan.model_json_schema(),
        )
        if isinstance(out, dict):
            return out
    except Exception as e:
        logger.warning("[automation.revise.llm] failed, falling back: %s", e)

    # Fallback: passthrough — caller can re-revise
    return base_plan


def _stub_plan_from_text(text: str, *, timezone_name: str) -> Dict[str, Any]:
    """Rule-based plan synthesis used when the LLM is unavailable.
    Covers the most-common patterns from the user's spec examples so
    the planner endpoint is end-to-end testable without Gemini.
    """
    t = text.lower()
    plan = AutomationPlan(
        title="自動化(自動生成)",
        summary=f"自然文「{text[:60]}」から生成されたデフォルト Plan。LLM 不在のため精度は低めです。",
        trigger=ScheduleTrigger(schedule=ScheduleSpec(
            rrule="RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            timezone=timezone_name,
        )),
        action=NotifyTodoDigestAction(filters={"status": "open"}),
        destination=AutomationDestination(channel="desktop", target="self"),
        execution=AutomationExecution(mode="cloud", requiresConfirmation=False),
        requirements=AutomationRequirements(),
        confidence=0.4,
        questions=[],
    )

    # Pattern matching
    if "毎朝" in text or "毎日" in t or "daily" in t:
        rrule = "RRULE:FREQ=DAILY"
    elif "毎週月" in text or "monday" in t:
        rrule = "RRULE:FREQ=WEEKLY;BYDAY=MO"
    elif "毎週" in text or "weekly" in t:
        rrule = "RRULE:FREQ=WEEKLY"
    else:
        rrule = "RRULE:FREQ=DAILY"

    # hour
    for h, hint in ((9, "9時"), (8, "8時"), (10, "10時"), (17, "17時"), (18, "18時")):
        if hint in text:
            rrule += f";BYHOUR={h};BYMINUTE=0"
            break
    else:
        rrule += ";BYHOUR=9;BYMINUTE=0"
    plan.trigger = ScheduleTrigger(schedule=ScheduleSpec(rrule=rrule, timezone=timezone_name))

    # action
    if "メール" in text or "mail" in t or "gmail" in t:
        plan.action = CreateMailDraftAction(provider="gmail", source="latest_session", tone="polite")
        plan.title = "会議メール下書き作成"
        plan.summary = "会議内容から Gmail 下書きを作成します(送信はしません)。"
    elif "slack" in t and ("dm" in t or "ダイレクト" in text):
        plan.action = CreateShareDraftAction(channel="slack", source="latest_session", format="summary")
        plan.destination = AutomationDestination(channel="slack_dm", target="self")
        plan.title = "Slack DM へのまとめ送信"
        plan.summary = "会議まとめを Slack DM 用に整形します。"
    elif "slack" in t:
        plan.action = NotifyMeetingDigestAction(filters={"range": "last_week"})
        plan.destination = AutomationDestination(channel="slack_dm", target="self")
        plan.title = "週次会議まとめ"
    elif "会議" in text or "meeting" in t:
        plan.action = NotifyMeetingDigestAction(filters={"range": "last_week"})
        plan.title = "会議まとめ通知"
    elif "ブリーフィング" in text or "briefing" in t or "前" in text:
        plan.action = PreMeetingBriefingAction(lookbackDays=30)
        plan.title = "会議前ブリーフィング"
    elif "todo" in t or "タスク" in text or "やること" in text:
        plan.action = NotifyTodoDigestAction(filters={"status": "open"})
        plan.title = "TODO digest"

    return plan.model_dump()


def _attach_requirements(account_id: str, plan: AutomationPlan) -> AutomationPlan:
    """Look up which integrations the plan needs and which the user
    actually has, then populate ``requirements.missingIntegrations``."""
    needed = required_integrations_for(plan)
    missing: List[str] = []

    # Slack / LINE: integrations live elsewhere (line_user_links / slack_user_links)
    # — we lazily check via the existing chat-bot store. For V-042 PR2 we
    # only check the OAuth-based providers (google / microsoft); slack /
    # line connectivity is left to PR3 dispatcher to surface.
    for need in needed:
        if need == "google_mail":
            rec = _int_store.load(_resolve_uid(account_id), "google")
            scopes = (rec or {}).get("scopes") or []
            if not any("gmail.compose" in s or "gmail.modify" in s for s in scopes):
                missing.append("gmail")
        elif need == "microsoft_mail":
            rec = _int_store.load(_resolve_uid(account_id), "microsoft")
            scopes = (rec or {}).get("scopes") or []
            ls = {x.lower() for x in scopes}
            if "mail.readwrite" not in ls:
                missing.append("outlook_mail")
        elif need == "google_or_microsoft_calendar":
            rec_g = _int_store.load(_resolve_uid(account_id), "google") or {}
            rec_m = _int_store.load(_resolve_uid(account_id), "microsoft") or {}
            has_cal = (
                any("calendar" in s for s in (rec_g.get("scopes") or []))
                or any("calendars.read" in (s or "").lower() for s in (rec_m.get("scopes") or []))
            )
            if not has_cal:
                missing.append("calendar")
    plan.requirements.missingIntegrations = sorted(set(missing))
    return plan


def _resolve_uid(account_id: str) -> str:
    """Heuristic: in current data model, integrations are keyed by uid
    (Firebase). Map account_id → uid via accounts/{aid}.ownerUid if
    present, else fall back to account_id itself (which works in
    single-uid-per-account setups).
    """
    try:
        snap = db.collection("accounts").document(account_id).get()
        if snap.exists:
            d = snap.to_dict() or {}
            return d.get("ownerUid") or d.get("uid") or account_id
    except Exception:
        pass
    return account_id
