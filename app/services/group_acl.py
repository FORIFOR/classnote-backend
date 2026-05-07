"""DeepNote bot — group / channel ACL & execution context resolution.

Phase 1: split ``requester`` / ``data_owner`` / ``billing_owner`` so a
group bot can serve a representative DeepNote account without giving
every group member the keys to that account's credit balance.

Public API (handlers must use this single helper)::

    ctx = group_acl.resolve_group_execution_context(
        provider="line", workspace_id=group_id,
        source_user_id=line_user_id, intent=cmd,
    )
    if isinstance(ctx, group_acl.Denied): reply(ctx.reason); return
    if isinstance(ctx, group_acl.RequireGroupConnect): reply(ctx.connect_hint); return
    # ctx.data_owner_uid / ctx.billing_owner_uid / ctx.is_admin / ...

Storage (see also ``docs/release-units/2026-05-07-bot-group-acl-PLAN.md``)::

    line_group_links/{group_id}            owner / billing UIDs
    line_group_acl/{group_id}/members/{u}  role + canRunPaidActions
    line_group_usage/{group_id}/days/{d}   per-day counters

Slack mirrors with ``slack_workspace_links/{team_id}/channels/{cid}`` etc.

Intent → tier classification is centralised here so the handlers stay
dumb. Tiers:
  ``public``  — read-only group surface, no billing impact
  ``private`` — personal info, refused in group context
  ``paid``    — credits debited from billing_owner, owner+admin only
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Union

from app.firebase import db

logger = logging.getLogger("app.services.group_acl")


# ──────────────────────────────────────────────────────────────────────
# Intent tiers
# ──────────────────────────────────────────────────────────────────────

PUBLIC_INTENTS = {
    "help", "greeting", "latest", "decisions", "assets",
    "pdf", "docx", "pptx",
    "auto_share_deprecated", "notify_status", "digest_status",
}
PRIVATE_INTENTS = {"credit", "todos"}
PAID_INTENTS = {"assistant_qna"}


def classify_tier(intent: str) -> Literal["public", "private", "paid", "unknown"]:
    if intent in PUBLIC_INTENTS:
        return "public"
    if intent in PRIVATE_INTENTS:
        return "private"
    if intent in PAID_INTENTS:
        return "paid"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────
# Storage helpers
# ──────────────────────────────────────────────────────────────────────

def _link_doc_ref(provider: str, workspace_id: str):
    if provider == "line":
        return db.collection("line_group_links").document(workspace_id)
    if provider == "slack":
        # workspace_id format for Slack is f"{team_id}:{channel_id}"
        return db.collection("slack_channel_links").document(workspace_id)
    raise ValueError(f"unknown provider: {provider}")


def _acl_member_ref(provider: str, workspace_id: str, source_user_id: str):
    base = _link_doc_ref(provider, workspace_id)
    return base.collection("members").document(source_user_id)


def _usage_doc_ref(provider: str, workspace_id: str, *, day: Optional[str] = None):
    if not day:
        day = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    return _link_doc_ref(provider, workspace_id).collection("usage").document(day)


# ──────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Denied:
    reason: str
    audit_outcome: str = "denied"


@dataclass
class RequireGroupConnect:
    connect_hint: str = (
        "このグループには DeepNote が接続されていません。\n"
        "個人チャットで DeepNote と連携した後、グループ内で「DeepNote 接続」と"
        "送信して代表アカウントを登録してください。"
    )
    audit_outcome: str = "no_group_link"


@dataclass
class ExecutionContext:
    provider: str
    workspace_id: str
    requester_source_user_id: str
    requester_deepnote_uid: Optional[str]
    data_owner_deepnote_uid: str
    data_owner_account_id: str
    billing_owner_deepnote_uid: str
    billing_owner_account_id: str
    role: str  # owner | admin | member
    is_owner: bool
    is_admin: bool
    intent: str
    tier: str
    audit_outcome: str = "ok"


Outcome = Union[Denied, RequireGroupConnect, ExecutionContext]


# ──────────────────────────────────────────────────────────────────────
# Defaults / env-overridable limits
# ──────────────────────────────────────────────────────────────────────

def _int_env(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def daily_limits() -> Dict[str, int]:
    return {
        "max_runs":           _int_env("GROUP_MAX_RUNS_PER_DAY", 30),
        "max_paid_runs":      _int_env("GROUP_MAX_PAID_RUNS_PER_DAY", 10),
        "max_artifacts":      _int_env("GROUP_MAX_ARTIFACTS_PER_DAY", 5),
        "max_runs_per_user":  _int_env("GROUP_MAX_RUNS_PER_USER_PER_DAY", 10),
    }


# ──────────────────────────────────────────────────────────────────────
# Group link / ACL CRUD
# ──────────────────────────────────────────────────────────────────────

def get_group_link(provider: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    snap = _link_doc_ref(provider, workspace_id).get()
    if not snap.exists:
        return None
    d = snap.to_dict() or {}
    return d if d.get("isActive", True) else None


def create_group_link(
    provider: str, workspace_id: str, *,
    owner_deepnote_uid: str, owner_account_id: str,
    created_by_source_user_id: str,
    group_name: str = "",
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    doc = {
        "provider": provider,
        "workspaceId": workspace_id,
        "groupName": group_name,
        "ownerDeepnoteUid": owner_deepnote_uid,
        "ownerAccountId": owner_account_id,
        "billingDeepnoteUid": owner_deepnote_uid,
        "billingAccountId": owner_account_id,
        "createdBySourceUserId": created_by_source_user_id,
        "mode": "owner_account",
        "isActive": True,
        "createdAt": now,
        "updatedAt": now,
    }
    _link_doc_ref(provider, workspace_id).set(doc)
    # creator gets owner role automatically
    set_member_role(provider, workspace_id, created_by_source_user_id,
                    role="owner", deepnote_uid=owner_deepnote_uid)
    return doc


def deactivate_group_link(provider: str, workspace_id: str) -> bool:
    ref = _link_doc_ref(provider, workspace_id)
    if not ref.get().exists:
        return False
    ref.update({"isActive": False, "updatedAt": datetime.now(timezone.utc)})
    return True


def get_member(provider: str, workspace_id: str, source_user_id: str) -> Optional[Dict[str, Any]]:
    snap = _acl_member_ref(provider, workspace_id, source_user_id).get()
    if not snap.exists:
        return None
    return snap.to_dict() or None


def set_member_role(
    provider: str, workspace_id: str, source_user_id: str, *,
    role: str, deepnote_uid: Optional[str] = None,
    added_by: Optional[str] = None,
) -> Dict[str, Any]:
    if role not in ("owner", "admin", "member"):
        raise ValueError(f"invalid role: {role}")
    now = datetime.now(timezone.utc)
    doc = {
        "sourceUserId": source_user_id,
        "role": role,
        "canRunPaidActions": role in ("owner", "admin"),
        "deepnoteUid": deepnote_uid,
        "addedAt": now,
        "addedBy": added_by,
    }
    _acl_member_ref(provider, workspace_id, source_user_id).set(doc, merge=True)
    return doc


def remove_member(provider: str, workspace_id: str, source_user_id: str) -> bool:
    ref = _acl_member_ref(provider, workspace_id, source_user_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def list_members(provider: str, workspace_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    out = []
    base = _link_doc_ref(provider, workspace_id)
    for snap in base.collection("members").limit(limit).stream():
        d = snap.to_dict() or {}
        d["sourceUserId"] = snap.id
        out.append(d)
    return out


# ──────────────────────────────────────────────────────────────────────
# Usage limiter
# ──────────────────────────────────────────────────────────────────────
#
# Chat-ops volumes are low (≤ ~30 events / group / day) so a simple
# read-modify-write is acceptable. If two cap-edge requests race we may
# undercharge by 1, never overcharge — the cap acts as a hard ceiling.

def check_and_increment_usage(
    provider: str, workspace_id: str, source_user_id: str, *, tier: str,
) -> Optional[str]:
    """Check daily caps and, on success, bump counters. Returns ``None``
    on success or a reason string (``paid_cap`` / ``runs_cap`` /
    ``user_cap``) when capped."""
    if tier not in ("public", "private", "paid"):
        return None  # unknown intents are not metered
    limits = daily_limits()
    ref = _usage_doc_ref(provider, workspace_id)
    try:
        snap = ref.get()
        d: Dict[str, Any] = snap.to_dict() if getattr(snap, "exists", False) else {}
    except Exception as e:
        logger.warning("[group_acl.usage] read failed: %s", e)
        d = {}

    runs = int(d.get("runCount", 0))
    paid = int(d.get("paidRunCount", 0))
    per_user = dict(d.get("perUser", {}) or {})
    u = dict(per_user.get(source_user_id) or {"runs": 0, "paid": 0})

    if tier == "paid" and paid >= limits["max_paid_runs"]:
        return "paid_cap"
    if runs >= limits["max_runs"]:
        return "runs_cap"
    if int(u.get("runs", 0)) >= limits["max_runs_per_user"]:
        return "user_cap"

    u["runs"] = int(u.get("runs", 0)) + 1
    if tier == "paid":
        u["paid"] = int(u.get("paid", 0)) + 1
    per_user[source_user_id] = u
    try:
        ref.set({
            "runCount": runs + 1,
            "paidRunCount": paid + (1 if tier == "paid" else 0),
            "perUser": per_user,
            "updatedAt": datetime.now(timezone.utc),
        }, merge=True)
    except Exception as e:
        logger.warning("[group_acl.usage] write failed: %s", e)
    return None


# ──────────────────────────────────────────────────────────────────────
# Public API: resolve_group_execution_context
# ──────────────────────────────────────────────────────────────────────

def resolve_group_execution_context(
    *,
    provider: str,
    workspace_id: str,
    source_user_id: str,
    intent: str,
) -> Outcome:
    """Single point where every group / channel handler asks "may this
    message do this thing right now, and if so as whom"?"""
    if not workspace_id or not source_user_id:
        return Denied("invalid_source", audit_outcome="bad_input")

    glink = get_group_link(provider, workspace_id)
    if not glink:
        return RequireGroupConnect()

    tier = classify_tier(intent)
    member = get_member(provider, workspace_id, source_user_id)

    # Soft auto-promote the requester to ``member`` if the link exists
    # but they have no ACL row yet — avoids dead-end UX where an active
    # group looks "broken" to a non-creator member.
    if not member:
        member = set_member_role(provider, workspace_id, source_user_id,
                                 role="member", added_by="auto_first_seen")

    is_owner = member.get("role") == "owner"
    is_admin = member.get("role") in ("owner", "admin")

    # private actions never run in groups
    if tier == "private":
        return Denied(
            "個人情報のため、グループでは表示できません。DeepNote bot との"
            "個人チャットでお試しください。",
            audit_outcome="blocked_private_in_group",
        )

    # paid actions require admin+
    if tier == "paid" and not is_admin:
        return Denied(
            "この操作は DeepNote のクレジットを消費するため、グループ管理者"
            "(owner / admin) のみ実行できます。\n"
            "管理者に依頼するか、個人チャットで DeepNote と直接やり取りしてください。",
            audit_outcome="blocked_paid_member",
        )

    # usage cap
    cap_hit = check_and_increment_usage(provider, workspace_id, source_user_id, tier=tier)
    if cap_hit == "paid_cap":
        return Denied(
            "本日のグループのクレジット消費上限に達しました。明日また"
            "お試しいただくか、管理者に上限緩和をご相談ください。",
            audit_outcome="cap_paid",
        )
    if cap_hit == "runs_cap":
        return Denied(
            "本日のグループ利用上限に達しました。明日またお試しください。",
            audit_outcome="cap_runs",
        )
    if cap_hit == "user_cap":
        return Denied(
            "本日あなたが個別に実行できる上限に達しました。明日また"
            "お試しください。",
            audit_outcome="cap_user",
        )

    return ExecutionContext(
        provider=provider,
        workspace_id=workspace_id,
        requester_source_user_id=source_user_id,
        requester_deepnote_uid=member.get("deepnoteUid"),
        data_owner_deepnote_uid=glink.get("ownerDeepnoteUid", ""),
        data_owner_account_id=glink.get("ownerAccountId", ""),
        billing_owner_deepnote_uid=glink.get("billingDeepnoteUid", ""),
        billing_owner_account_id=glink.get("billingAccountId", ""),
        role=member.get("role", "member"),
        is_owner=is_owner,
        is_admin=is_admin,
        intent=intent,
        tier=tier,
    )
