"""Short-lived link tokens for Slack ↔ DeepNote account linking.

Mirrors app.services.line_link_tokens with team_id / slack_user_id keys.

Storage:
  slack_link_tokens/{token}
    teamId, slackUserId, slackChannelId, expiresAt, usedAt, createdAt,
    linkedUid, linkedAccountId
  slack_user_links/{team_id}:{slack_user_id}
    deepnoteUid, accountId, linkedAt, teamId, slackUserId
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

from app.firebase import db

logger = logging.getLogger("app.services.slack_link_tokens")

LINK_TOKENS_COLLECTION = "slack_link_tokens"
USER_LINKS_COLLECTION = "slack_user_links"

TOKEN_TTL_SECONDS = 600
REUSE_WINDOW_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _link_doc(token: str):
    return db.collection(LINK_TOKENS_COLLECTION).document(token)


def _user_link_id(team_id: str, slack_user_id: str) -> str:
    return f"{team_id}:{slack_user_id}"


def _user_link_doc(team_id: str, slack_user_id: str):
    return db.collection(USER_LINKS_COLLECTION).document(_user_link_id(team_id, slack_user_id))


class TokenError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def issue(*, team_id: str, slack_user_id: str, slack_channel_id: Optional[str] = None) -> str:
    cutoff = _now() - timedelta(seconds=REUSE_WINDOW_SECONDS)
    try:
        recent = (
            db.collection(LINK_TOKENS_COLLECTION)
            .where("teamId", "==", team_id)
            .where("slackUserId", "==", slack_user_id)
            .where("createdAt", ">=", cutoff)
            .limit(5)
            .stream()
        )
        for snap in recent:
            data = snap.to_dict() or {}
            if data.get("usedAt") is None and data.get("expiresAt") and data["expiresAt"] > _now():
                return snap.id
    except Exception as e:
        logger.debug("[slack_link_tokens] reuse lookup skipped: %s", e)

    token = secrets.token_urlsafe(32)
    payload = {
        "teamId": team_id,
        "slackUserId": slack_user_id,
        "slackChannelId": slack_channel_id,
        "expiresAt": _now() + timedelta(seconds=TOKEN_TTL_SECONDS),
        "usedAt": None,
        "createdAt": _now(),
        "linkedUid": None,
        "linkedAccountId": None,
    }
    _link_doc(token).set(payload)
    return token


def resolve(token: str) -> Dict[str, Any]:
    if not token:
        raise TokenError("token_missing", 400)
    snap = _link_doc(token).get()
    if not snap.exists:
        raise TokenError("token_unknown", 404)
    data = snap.to_dict() or {}
    if data.get("usedAt"):
        raise TokenError("token_already_used", 409)
    if data.get("expiresAt") and data["expiresAt"] < _now():
        raise TokenError("token_expired", 410)
    return data


def consume(token: str, *, deepnote_uid: str, account_id: str) -> Dict[str, Any]:
    if not token:
        raise TokenError("token_missing", 400)
    if not deepnote_uid or not account_id:
        raise TokenError("invalid_consumer", 400)

    ref = _link_doc(token)

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise TokenError("token_unknown", 404)
        data = snap.to_dict() or {}
        if data.get("usedAt"):
            raise TokenError("token_already_used", 409)
        if data.get("expiresAt") and data["expiresAt"] < _now():
            raise TokenError("token_expired", 410)
        tx.update(ref, {
            "usedAt": _now(),
            "linkedUid": deepnote_uid,
            "linkedAccountId": account_id,
        })
        return data

    transaction = db.transaction()
    data = _txn(transaction)

    team_id = data.get("teamId")
    slack_user_id = data.get("slackUserId")
    if team_id and slack_user_id:
        _user_link_doc(team_id, slack_user_id).set({
            "deepnoteUid": deepnote_uid,
            "accountId": account_id,
            "linkedAt": _now(),
            "teamId": team_id,
            "slackUserId": slack_user_id,
        }, merge=True)
        logger.info("[slack_link_tokens] linked team=%s user=%s uid=%s account=%s",
                    team_id, slack_user_id, deepnote_uid, account_id)

    return {
        "teamId": team_id,
        "slackUserId": slack_user_id,
        "slackChannelId": data.get("slackChannelId"),
        "deepnoteUid": deepnote_uid,
        "accountId": account_id,
    }


def get_link(team_id: str, slack_user_id: str) -> Optional[Dict[str, Any]]:
    if not team_id or not slack_user_id:
        return None
    snap = _user_link_doc(team_id, slack_user_id).get()
    if not snap.exists:
        return None
    return snap.to_dict() or None
