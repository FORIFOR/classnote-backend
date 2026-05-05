"""Legacy googleCalendarTokens lazy-migration tests.

Verifies that integrations.store.load(uid, "google") falls back to the legacy
users/{uid}.googleCalendarTokens map field, encrypts it with token_crypto, and
persists into users/{uid}/integrations/google. The legacy field MUST NOT be
deleted by this migration.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _setup_crypto(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    yield


def _seed_legacy(uid: str, access: str, refresh: str | None = None):
    from app.firebase import db

    body = {
        "accessToken": access,
        "expiresAt": datetime.now(timezone.utc),
    }
    if refresh:
        body["refreshToken"] = refresh
    db.collection("users").document(uid).set({"googleCalendarTokens": body})


def test_load_falls_back_and_migrates_legacy_google():
    from app.services.integrations import store

    uid = "legacy-uid-1"
    _seed_legacy(uid, "old-access", "old-refresh")

    data = store.load(uid, "google")
    assert data is not None
    assert data.get("status") == "connected"
    assert data.get("migratedFromLegacy") is True
    # Tokens are stored encrypted (cipher), not plaintext
    assert data.get("accessTokenCipher") not in (None, "old-access")
    assert data.get("refreshTokenCipher") not in (None, "old-refresh")


def test_decrypted_tokens_after_migration():
    from app.services.integrations import store

    uid = "legacy-uid-2"
    _seed_legacy(uid, "another-access", "another-refresh")

    bundle = store.get_decrypted_tokens(uid, "google")
    assert bundle is not None
    assert bundle["accessToken"] == "another-access"
    assert bundle["refreshToken"] == "another-refresh"


def test_legacy_field_not_deleted_after_migration():
    from app.firebase import db
    from app.services.integrations import store

    uid = "legacy-uid-3"
    _seed_legacy(uid, "still-here", "and-here")
    _ = store.load(uid, "google")

    user_doc = db.collection("users").document(uid).get()
    assert user_doc.exists
    body = user_doc.to_dict() or {}
    assert "googleCalendarTokens" in body
    assert body["googleCalendarTokens"].get("accessToken") == "still-here"


def test_no_migration_when_no_legacy_data():
    from app.services.integrations import store

    assert store.load("uid-with-nothing", "google") is None


def test_migration_only_for_google():
    from app.services.integrations import store

    uid = "legacy-uid-4"
    _seed_legacy(uid, "google-only", "google-only-r")
    # microsoft must not migrate from googleCalendarTokens
    assert store.load(uid, "microsoft") is None
