"""OAuth state store with single-use Firestore persistence + HMAC signature.

Why both signature and store?
  - HMAC catches replay/forgery without DB lookups for malformed states
  - Firestore record gives us single-use semantics + automatic CSRF blocking
    when the same state is replayed.

Issue:
  - state value:  base64url(uid|provider|nonce|expires|scope_hash) + "." + sig
  - Firestore doc /oauth_state/{nonce}: { uid, provider, returnTo, scopeSet,
                                          expiresAt, consumedAt(optional) }

Validate:
  - verify HMAC
  - load Firestore doc by nonce, check not consumed, mark consumed atomically
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

logger = logging.getLogger("app.services.oauth_state_store")

STATE_TTL_SECONDS = int(os.environ.get("OAUTH_STATE_TTL_SECONDS", "600"))
COLLECTION = "oauth_state"


def _state_secret(provider: str) -> bytes:
    if provider == "google":
        env_key = "GOOGLE_OAUTH_STATE_SECRET"
    elif provider == "microsoft":
        env_key = "MICROSOFT_OAUTH_STATE_SECRET"
    else:
        env_key = f"{provider.upper()}_OAUTH_STATE_SECRET"
    raw = os.environ.get(env_key) or os.environ.get("OAUTH_STATE_SECRET")
    if not raw:
        raise RuntimeError(f"State secret not configured (env: {env_key})")
    return raw.encode("utf-8")


def _hash_scope(scope_set: str) -> str:
    return hashlib.sha256(scope_set.encode("utf-8")).hexdigest()[:16]


def _sign(message: str, provider: str) -> str:
    sig = hmac.new(_state_secret(provider), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue(*, uid: str, provider: str, return_to: str, scope_set: str) -> str:
    """Mint a single-use state value, persisted in Firestore."""
    nonce = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS)
    scope_hash = _hash_scope(scope_set)

    payload = f"{uid}|{provider}|{nonce}|{int(expires_at.timestamp())}|{scope_hash}"
    encoded = _b64encode(payload.encode("utf-8"))
    sig = _sign(encoded, provider)
    state = f"{encoded}.{sig}"

    db.collection(COLLECTION).document(nonce).set({
        "uid": uid,
        "provider": provider,
        "returnTo": return_to,
        "scopeHash": scope_hash,
        "expiresAt": expires_at,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "consumedAt": None,
    })
    return state


def consume(state: str, *, expected_provider: str) -> Dict[str, Any]:
    """Verify + atomically mark consumed. Returns the original payload dict.

    Raises ValueError if invalid / expired / already used.
    """
    if not state or "." not in state:
        raise ValueError("malformed_state")
    encoded, sig = state.rsplit(".", 1)
    expected_sig = _sign(encoded, expected_provider)
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("bad_signature")
    try:
        payload = _b64decode(encoded).decode("utf-8")
        uid, provider, nonce, exp_str, scope_hash = payload.split("|")
        expires_at = datetime.fromtimestamp(int(exp_str), tz=timezone.utc)
    except Exception:
        raise ValueError("malformed_payload")

    if provider != expected_provider:
        raise ValueError("provider_mismatch")
    if datetime.now(timezone.utc) > expires_at:
        raise ValueError("expired")

    ref = db.collection(COLLECTION).document(nonce)

    @firestore.transactional
    def _consume(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise ValueError("unknown_nonce")
        data = snap.to_dict() or {}
        if data.get("consumedAt"):
            raise ValueError("already_consumed")
        if data.get("uid") != uid or data.get("provider") != provider:
            raise ValueError("payload_mismatch")
        if data.get("scopeHash") != scope_hash:
            raise ValueError("scope_mismatch")
        tx.update(ref, {"consumedAt": firestore.SERVER_TIMESTAMP})
        return data

    transaction = db.transaction()
    data = _consume(transaction)
    return {
        "uid": uid,
        "provider": provider,
        "nonce": nonce,
        "returnTo": data.get("returnTo"),
        "scopeHash": scope_hash,
        "expiresAt": expires_at,
    }


def cleanup_expired(limit: int = 200) -> int:
    """Best-effort sweeper for expired records (call from a cron / cleanup task)."""
    now = datetime.now(timezone.utc)
    deleted = 0
    query = db.collection(COLLECTION).where("expiresAt", "<", now).limit(limit)
    for snap in query.stream():
        snap.reference.delete()
        deleted += 1
    return deleted
