"""Persistence layer for OAuth tokens (Firestore).

Storage layout:
  users/{uid}/integrations/{provider}
    - provider: "google" | "microsoft"
    - status: "connected" | "revoked" | "error"
    - scope: full granted scope string from provider
    - accessTokenCipher: Fernet ciphertext (encrypted)
    - refreshTokenCipher: Fernet ciphertext (encrypted, may be empty)
    - tokenType: "Bearer"
    - expiresAt: datetime
    - accountEmail: human-readable identifier (provider returns)
    - accountId: provider-side stable id (sub / oid)
    - createdAt / updatedAt
    - lastError, lastErrorAt (optional)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.firebase import db
from app.services import token_crypto

logger = logging.getLogger("app.services.integrations.store")


def _doc_ref(uid: str, provider: str):
    return db.collection("users").document(uid).collection("integrations").document(provider)


def save_tokens(
    *,
    uid: str,
    provider: str,
    access_token: str,
    refresh_token: Optional[str],
    expires_in: Optional[int],
    scope: Optional[str],
    token_type: Optional[str] = "Bearer",
    account_email: Optional[str] = None,
    account_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    if not token_crypto.is_configured():
        raise RuntimeError("token_crypto not configured")
    expires_at = None
    if expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 30)

    payload: Dict[str, Any] = {
        "provider": provider,
        "status": "connected",
        "scope": scope,
        "accessTokenCipher": token_crypto.encrypt(access_token),
        "tokenType": token_type or "Bearer",
        "expiresAt": expires_at,
        "accountEmail": account_email,
        "accountId": account_id,
        "updatedAt": datetime.now(timezone.utc),
        "lastError": None,
        "lastErrorAt": None,
    }
    if refresh_token:
        payload["refreshTokenCipher"] = token_crypto.encrypt(refresh_token)
    if extra:
        payload.update(extra)

    ref = _doc_ref(uid, provider)
    if not ref.get().exists:
        payload["createdAt"] = datetime.now(timezone.utc)
    ref.set(payload, merge=True)


def update_access_token(
    *,
    uid: str,
    provider: str,
    access_token: str,
    expires_in: Optional[int],
    scope: Optional[str] = None,
) -> None:
    expires_at = None
    if expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 30)
    payload = {
        "accessTokenCipher": token_crypto.encrypt(access_token),
        "expiresAt": expires_at,
        "updatedAt": datetime.now(timezone.utc),
        "lastError": None,
        "lastErrorAt": None,
    }
    if scope:
        payload["scope"] = scope
    _doc_ref(uid, provider).set(payload, merge=True)


def load(uid: str, provider: str) -> Optional[Dict[str, Any]]:
    snap = _doc_ref(uid, provider).get()
    if snap.exists:
        return snap.to_dict() or {}
    if provider == "google":
        migrated = _migrate_legacy_google(uid)
        if migrated is not None:
            return migrated
    return None


def _migrate_legacy_google(uid: str) -> Optional[Dict[str, Any]]:
    """Read users/{uid}.googleCalendarTokens (legacy plaintext), encrypt and
    persist into users/{uid}/integrations/google. The legacy field is NOT
    deleted here; a separate migration job removes it.

    Returns the new-shape dict (as if read from the new path) on success, or
    None if no legacy data / token_crypto not configured / migration failed.
    """
    if not token_crypto.is_configured():
        return None
    try:
        user_doc = db.collection("users").document(uid).get()
    except Exception as e:
        logger.warning("[integrations.store] legacy google read failed for %s: %s", uid, e)
        return None
    if not user_doc.exists:
        return None
    legacy = (user_doc.to_dict() or {}).get("googleCalendarTokens")
    if not legacy:
        return None

    access = legacy.get("accessToken")
    refresh = legacy.get("refreshToken")
    expires_at = legacy.get("expiresAt")
    if not access:
        return None

    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "provider": "google",
        "status": "connected",
        "scope": "https://www.googleapis.com/auth/calendar.events",
        "accessTokenCipher": token_crypto.encrypt(access),
        "tokenType": "Bearer",
        "expiresAt": expires_at,
        "accountEmail": None,
        "accountId": None,
        "createdAt": now,
        "updatedAt": now,
        "lastError": None,
        "lastErrorAt": None,
        "migratedFromLegacy": True,
    }
    if refresh:
        payload["refreshTokenCipher"] = token_crypto.encrypt(refresh)
    try:
        _doc_ref(uid, "google").set(payload, merge=True)
    except Exception as e:
        logger.warning("[integrations.store] legacy google migration write failed for %s: %s", uid, e)
        return None
    return payload


def get_decrypted_tokens(uid: str, provider: str) -> Optional[Dict[str, Any]]:
    """Return {access, refresh, expiresAt, scope} or None."""
    data = load(uid, provider)
    if not data:
        return None
    if data.get("status") != "connected":
        return None
    cipher_access = data.get("accessTokenCipher")
    cipher_refresh = data.get("refreshTokenCipher")
    if not cipher_access:
        return None
    return {
        "accessToken": token_crypto.decrypt(cipher_access),
        "refreshToken": token_crypto.decrypt(cipher_refresh) if cipher_refresh else None,
        "expiresAt": data.get("expiresAt"),
        "scope": data.get("scope"),
        "accountEmail": data.get("accountEmail"),
        "accountId": data.get("accountId"),
    }


def mark_error(uid: str, provider: str, reason: str) -> None:
    _doc_ref(uid, provider).set({
        "lastError": reason[:500],
        "lastErrorAt": datetime.now(timezone.utc),
        "updatedAt": datetime.now(timezone.utc),
    }, merge=True)


def revoke(uid: str, provider: str) -> None:
    _doc_ref(uid, provider).set({
        "status": "revoked",
        "accessTokenCipher": "",
        "refreshTokenCipher": "",
        "updatedAt": datetime.now(timezone.utc),
    }, merge=True)
