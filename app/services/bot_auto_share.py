"""Phase 7+ — automatic group/channel sharing toggle.

Background:
    The Phase 7 group-bot mode required users to flip an explicit
    "share to this workspace" flag on every session before LINE / Slack
    bots in that workspace could surface it. Discoverability was poor
    and the bot felt broken. This service adds an opt-in **auto-share**
    setting: once a user enables it for a workspace from the bot DM,
    every newly summarised session is automatically tagged with the
    workspace key on completion.

Storage:
    The setting lives on the existing bot link document so we don't
    need a new collection:

        users/{lineLinks|slackLinks}/{linkUserId}.autoShareToWorkspaces:
            ["slack:T123", "line:G456", ...]

    Multiple users in the same account can each opt in independently;
    the union of their lists is what the session finalize hook uses.

Privacy guarantees:
    - Only newly summarised sessions are touched (existing sessions are
      never retroactively shared without the user's explicit move).
    - We only append, never remove, ``sharedToWorkspaceTeams`` entries.
    - Disable is immediate: future sessions stop being shared, but
      already-shared sessions keep their flag (user can unshare via the
      iOS / web UI if they want a full revocation).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from app.firebase import db

logger = logging.getLogger("app.services.bot_auto_share")

LINE_LINKS = "lineLinks"
SLACK_LINKS = "slackLinks"


def _link_collection(provider: str) -> Optional[str]:
    if provider == "line":
        return LINE_LINKS
    if provider == "slack":
        return SLACK_LINKS
    return None


def _link_doc_id(provider: str, source_user_id: str, team_id: Optional[str] = None) -> str:
    """Slack uses a composite (team, user) key in some places; line is just user.
    The existing tokens services key purely on the source user id, so we
    follow that convention here."""
    return source_user_id


def enable(provider: str, source_user_id: str, workspace_key: str) -> bool:
    """Add ``workspace_key`` to the link doc's ``autoShareToWorkspaces`` list.
    Idempotent. Returns True iff the value was newly added.
    """
    coll = _link_collection(provider)
    if not coll or not source_user_id or not workspace_key:
        return False
    ref = db.collection(coll).document(_link_doc_id(provider, source_user_id))
    snap = ref.get()
    if not snap.exists:
        logger.warning("[auto_share.enable] no link doc for %s/%s", provider, source_user_id)
        return False
    data = snap.to_dict() or {}
    cur = list(data.get("autoShareToWorkspaces") or [])
    if workspace_key in cur:
        return False
    cur.append(workspace_key)
    ref.update({"autoShareToWorkspaces": cur})
    return True


def disable(provider: str, source_user_id: str, workspace_key: str) -> bool:
    """Remove ``workspace_key`` from the link doc's ``autoShareToWorkspaces``.
    Idempotent. Returns True iff the value was actually removed.
    Note: existing sessions already tagged remain tagged.
    """
    coll = _link_collection(provider)
    if not coll or not source_user_id or not workspace_key:
        return False
    ref = db.collection(coll).document(_link_doc_id(provider, source_user_id))
    snap = ref.get()
    if not snap.exists:
        return False
    data = snap.to_dict() or {}
    cur = list(data.get("autoShareToWorkspaces") or [])
    if workspace_key not in cur:
        return False
    cur = [w for w in cur if w != workspace_key]
    ref.update({"autoShareToWorkspaces": cur})
    return True


def is_enabled(provider: str, source_user_id: str, workspace_key: str) -> bool:
    coll = _link_collection(provider)
    if not coll or not source_user_id or not workspace_key:
        return False
    snap = db.collection(coll).document(_link_doc_id(provider, source_user_id)).get()
    if not snap.exists:
        return False
    cur = (snap.to_dict() or {}).get("autoShareToWorkspaces") or []
    return workspace_key in cur


def workspaces_for_account(account_id: str) -> List[str]:
    """Return the union of all ``autoShareToWorkspaces`` entries across
    every bot link doc that resolves to this account. Used by the
    summarise hook to decide which workspace keys to append to a new
    session's ``sharedToWorkspaceTeams``.
    """
    if not account_id:
        return []
    seen: set[str] = set()
    for coll in (LINE_LINKS, SLACK_LINKS):
        try:
            q = db.collection(coll).where("accountId", "==", account_id).limit(50)
            for snap in q.stream():
                d = snap.to_dict() or {}
                for w in (d.get("autoShareToWorkspaces") or []):
                    if isinstance(w, str) and w:
                        seen.add(w)
        except Exception as e:
            logger.warning("[auto_share.workspaces_for_account] %s lookup failed: %s", coll, e)
    return sorted(seen)


def apply_to_session(session_id: str, owner_account_id: str) -> List[str]:
    """Best-effort: append the account's auto-share workspaces to the
    given session's ``sharedToWorkspaceTeams``. Returns the list of
    keys actually added (may be empty). Safe to call repeatedly.
    """
    workspaces = workspaces_for_account(owner_account_id)
    if not workspaces:
        return []
    try:
        sess_ref = db.collection("sessions").document(session_id)
        snap = sess_ref.get()
        if not snap.exists:
            return []
        data = snap.to_dict() or {}
        existing = list(data.get("sharedToWorkspaceTeams") or [])
        added = [w for w in workspaces if w not in existing]
        if not added:
            return []
        sess_ref.update({"sharedToWorkspaceTeams": existing + added})
        logger.info(
            "[auto_share.apply_to_session] session=%s account=%s added=%s",
            session_id, owner_account_id, added,
        )
        return added
    except Exception as e:
        logger.warning("[auto_share.apply_to_session] failed for %s: %s", session_id, e)
        return []
