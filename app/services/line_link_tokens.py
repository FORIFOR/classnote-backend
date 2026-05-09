"""Short-lived link tokens for LINE ↔ DeepNote account linking.

Storage:
  line_link_tokens/{token}
    lineUserId       : str
    lineGroupId      : str | None
    lineSourceType   : "user" | "group" | "room"
    expiresAt        : datetime    (TTL 10 min)
    usedAt           : datetime | None
    createdAt        : datetime
    linkedUid        : str | None  (set on consume)
    linkedAccountId  : str | None  (set on consume)

  line_user_links/{lineUserId}
    deepnoteUid      : str
    accountId        : str
    linkedAt         : datetime
    lineSourceType   : "user" | "group" | "room"

Design notes:
  - Tokens are 32-byte url-safe (≈43 chars), single-use, 10-minute TTL.
  - consume() runs in a Firestore transaction so two concurrent consumes
    cannot both succeed (mirrors oauth_state_store.consume).
  - issue() reuses an active token if one was created within the last
    60 seconds for the same lineUserId — prevents bot spam from issuing
    many tokens per minute.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

from app.firebase import db

logger = logging.getLogger("app.services.line_link_tokens")

LINK_TOKENS_COLLECTION = "line_link_tokens"
USER_LINKS_COLLECTION = "line_user_links"

TOKEN_TTL_SECONDS = 600        # 10 minutes
REUSE_WINDOW_SECONDS = 60      # reuse an unused token if created within 60s


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _doc(token: str):
    return db.collection(LINK_TOKENS_COLLECTION).document(token)


def issue(
    *,
    line_user_id: str,
    line_group_id: Optional[str] = None,
    line_source_type: str = "user",
) -> str:
    """Mint a short-lived link token. Reuses an active token from the same
    line_user_id if one was created in the last REUSE_WINDOW_SECONDS and
    has not been consumed."""
    cutoff = _now() - timedelta(seconds=REUSE_WINDOW_SECONDS)
    try:
        recent = (
            db.collection(LINK_TOKENS_COLLECTION)
            .where("lineUserId", "==", line_user_id)
            .where("createdAt", ">=", cutoff)
            .limit(5)
            .stream()
        )
        for snap in recent:
            data = snap.to_dict() or {}
            if data.get("usedAt") is None and data.get("expiresAt") and data["expiresAt"] > _now():
                return snap.id
    except Exception as e:
        # Composite index might be missing; fall through to fresh issue.
        logger.debug("[line_link_tokens] reuse lookup skipped: %s", e)

    token = secrets.token_urlsafe(32)
    payload = {
        "lineUserId": line_user_id,
        "lineGroupId": line_group_id,
        "lineSourceType": line_source_type,
        "expiresAt": _now() + timedelta(seconds=TOKEN_TTL_SECONDS),
        "usedAt": None,
        "createdAt": _now(),
        "linkedUid": None,
        "linkedAccountId": None,
    }
    _doc(token).set(payload)
    return token


class TokenError(Exception):
    """Raised when a token is unknown / expired / already used."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def resolve(token: str) -> Dict[str, Any]:
    """Return the token record without consuming it.

    Raises TokenError on unknown / expired / already-used.
    """
    if not token:
        raise TokenError("token_missing", 400)
    snap = _doc(token).get()
    if not snap.exists:
        raise TokenError("token_unknown", 404)
    data = snap.to_dict() or {}
    if data.get("usedAt"):
        raise TokenError("token_already_used", 409)
    if data.get("expiresAt") and data["expiresAt"] < _now():
        raise TokenError("token_expired", 410)
    return data


def consume(token: str, *, deepnote_uid: str, account_id: str) -> Dict[str, Any]:
    """Atomically mark token as used and persist the line ↔ deepnote link.

    Raises TokenError on unknown / expired / already-used / mismatch.
    """
    if not token:
        raise TokenError("token_missing", 400)
    if not deepnote_uid or not account_id:
        raise TokenError("invalid_consumer", 400)

    ref = _doc(token)

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

    line_user_id = data.get("lineUserId")
    if line_user_id:
        db.collection(USER_LINKS_COLLECTION).document(line_user_id).set({
            "deepnoteUid": deepnote_uid,
            "accountId": account_id,
            "linkedAt": _now(),
            "lineSourceType": data.get("lineSourceType", "user"),
        }, merge=True)
        logger.info("[line_link_tokens] linked lineUserId=%s uid=%s account=%s",
                    line_user_id, deepnote_uid, account_id)

    return {
        "lineUserId": line_user_id,
        "lineGroupId": data.get("lineGroupId"),
        "lineSourceType": data.get("lineSourceType", "user"),
        "deepnoteUid": deepnote_uid,
        "accountId": account_id,
    }


def get_link(line_user_id: str) -> Optional[Dict[str, Any]]:
    """Return {deepnoteUid, accountId, linkedAt, lineSourceType} or None."""
    if not line_user_id:
        return None
    snap = db.collection(USER_LINKS_COLLECTION).document(line_user_id).get()
    if not snap.exists:
        return None
    return snap.to_dict() or None


def delete_link(line_user_id: str) -> bool:
    """Remove the LINE userId ↔ DeepNote account link.

    Returns True if a link existed and was deleted, False if no link
    was present. Used by the LINE bot 1:1 ``ログアウト`` command and
    by ``DELETE /integrations/me/links/line``.
    """
    if not line_user_id:
        return False
    ref = db.collection(USER_LINKS_COLLECTION).document(line_user_id)
    snap = ref.get()
    if not snap.exists:
        return False
    ref.delete()
    return True


def cleanup_expired(limit: int = 200) -> int:
    """Best-effort sweeper for expired token records (call from a cron)."""
    now = _now()
    deleted = 0
    query = db.collection(LINK_TOKENS_COLLECTION).where("expiresAt", "<", now).limit(limit)
    for snap in query.stream():
        snap.reference.delete()
        deleted += 1
    return deleted
