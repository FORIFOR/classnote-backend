"""HMAC-signed single-use OAuth state for Slack workspace install.

Mirrors app.services.oauth_state_store but namespaced so a Slack state
cannot be replayed against the Google/Microsoft consume() path (each
provider has its own secret + collection).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from google.cloud import firestore

from app.firebase import db

logger = logging.getLogger("app.services.slack_oauth_state")

STATE_TTL_SECONDS = int(os.environ.get("SLACK_OAUTH_STATE_TTL_SECONDS", "600"))
COLLECTION = "slack_oauth_state"


def _secret() -> bytes:
    raw = os.environ.get("SLACK_OAUTH_STATE_SECRET") or os.environ.get("OAUTH_STATE_SECRET")
    if not raw:
        raise RuntimeError("SLACK_OAUTH_STATE_SECRET not configured")
    return raw.encode("utf-8")


def _sign(message: str) -> str:
    sig = hmac.new(_secret(), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue(*, return_to: str = "/", uid: str = "") -> str:
    nonce = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS)
    payload = f"slack|{nonce}|{int(expires_at.timestamp())}"
    encoded = _b64encode(payload.encode("utf-8"))
    sig = _sign(encoded)
    state = f"{encoded}.{sig}"

    db.collection(COLLECTION).document(nonce).set({
        "uid": uid or None,
        "returnTo": return_to,
        "expiresAt": expires_at,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "consumedAt": None,
    })
    return state


def consume(state: str) -> Dict[str, Any]:
    if not state or "." not in state:
        raise ValueError("malformed_state")
    encoded, sig = state.rsplit(".", 1)
    expected_sig = _sign(encoded)
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("bad_signature")
    try:
        payload = _b64decode(encoded).decode("utf-8")
        provider, nonce, exp_str = payload.split("|")
        expires_at = datetime.fromtimestamp(int(exp_str), tz=timezone.utc)
    except Exception:
        raise ValueError("malformed_payload")
    if provider != "slack":
        raise ValueError("provider_mismatch")
    if datetime.now(timezone.utc) > expires_at:
        raise ValueError("expired")

    ref = db.collection(COLLECTION).document(nonce)

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise ValueError("unknown_nonce")
        data = snap.to_dict() or {}
        if data.get("consumedAt"):
            raise ValueError("already_consumed")
        tx.update(ref, {"consumedAt": firestore.SERVER_TIMESTAMP})
        return data

    transaction = db.transaction()
    data = _txn(transaction)
    return {
        "uid": data.get("uid"),
        "returnTo": data.get("returnTo"),
        "nonce": nonce,
        "expiresAt": expires_at,
    }
