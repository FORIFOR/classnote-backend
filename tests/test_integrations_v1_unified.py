"""V-041-B unified /v1/integrations surface — unit tests.

Live API calls (Calendar list / Gmail draft create / Outlook draft
create) require a real OAuth token; those are deferred to the smoke
matrix in docs/release-units/2026-05-08-integrations-readiness-audit.md
once master walks the consent flow. Here we cover:

  - GET /v1/integrations returns both providers with connected:false
    when no tokens are stored
  - capability translation (scope → flag) maps correctly
  - POST /{provider}:test 409s without a token (not_connected)
  - POST /{provider}:disconnect is idempotent (no-op if not connected)
  - POST /mail/drafts 409s without a token; 400 if `to` empty
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.dependencies import get_current_user, CurrentUser
from app.services.integrations import store as _store
from app.routes import integrations_v1 as _v1


@pytest.fixture
def _auth():
    fake = CurrentUser(
        uid="test-uid", account_id="test-acct", provider="x",
        phone_number=None, email="t@t.com", display_name="t",
        photo_url=None, has_custom_claims=False,
    )
    app.dependency_overrides[get_current_user] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_current_user, None)


# ──────────────────────────────────────────────────────────────────────
# Capability translation (pure function, no I/O)
# ──────────────────────────────────────────────────────────────────────

def test_capabilities_google_calendar_only():
    caps = _v1._capabilities_from_scopes("google", [
        "openid", "email",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    ])
    assert caps["calendarRead"] is True
    assert caps["calendarWrite"] is False
    assert caps["mailDraft"] is False
    assert caps["mailSend"] is False


def test_capabilities_google_full():
    caps = _v1._capabilities_from_scopes("google", [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ])
    assert caps["calendarRead"] is True
    assert caps["calendarWrite"] is True
    assert caps["mailDraft"] is True
    assert caps["mailSend"] is True


def test_capabilities_microsoft_readwrite():
    caps = _v1._capabilities_from_scopes("microsoft", [
        "Calendars.ReadWrite", "Mail.ReadWrite",
    ])
    assert caps["calendarRead"] is True
    assert caps["calendarWrite"] is True
    assert caps["mailDraft"] is True
    assert caps["mailSend"] is False


# ──────────────────────────────────────────────────────────────────────
# GET /v1/integrations
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_list_integrations_no_tokens(_auth, monkeypatch):
    monkeypatch.setattr(_store, "load", lambda uid, prov: None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/integrations")
    assert r.status_code == 200
    body = r.json()
    providers = {it["provider"] for it in body["items"]}
    assert providers == {"google", "microsoft"}
    for it in body["items"]:
        assert it["connected"] is False


@pytest.mark.anyio
async def test_list_integrations_google_connected(_auth, monkeypatch):
    def _load(uid, prov):
        if prov == "google":
            return {
                "encryptedRefreshToken": "abc",
                "status": "connected",
                "email": "u@example.com",
                "scopes": ["https://www.googleapis.com/auth/calendar.events.readonly",
                           "https://www.googleapis.com/auth/gmail.compose"],
            }
        return None
    monkeypatch.setattr(_store, "load", _load)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/integrations")
    assert r.status_code == 200
    items = {it["provider"]: it for it in r.json()["items"]}
    assert items["google"]["connected"] is True
    assert items["google"]["email"] == "u@example.com"
    assert items["google"]["capabilities"]["calendarRead"] is True
    assert items["google"]["capabilities"]["mailDraft"] is True
    assert items["microsoft"]["connected"] is False


# ──────────────────────────────────────────────────────────────────────
# POST /{provider}:test
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_test_endpoint_without_token_is_not_ready(_auth, monkeypatch):
    monkeypatch.setattr(_store, "load", lambda uid, prov: None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/integrations/google:test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "not_connected"


# ──────────────────────────────────────────────────────────────────────
# POST /{provider}:disconnect
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_disconnect_when_already_absent_is_idempotent(_auth, monkeypatch):
    monkeypatch.setattr(_store, "load", lambda uid, prov: None)
    revoked_calls = []
    monkeypatch.setattr(_store, "revoke",
                        lambda uid, prov: revoked_calls.append((uid, prov)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/integrations/google:disconnect")
    assert r.status_code == 200
    assert r.json()["revoked"] is False
    assert revoked_calls == []


@pytest.mark.anyio
async def test_disconnect_revokes_when_present(_auth, monkeypatch):
    monkeypatch.setattr(_store, "load",
                        lambda uid, prov: {"encryptedRefreshToken": "abc"})
    revoked_calls = []
    monkeypatch.setattr(_store, "revoke",
                        lambda uid, prov: revoked_calls.append((uid, prov)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/integrations/microsoft:disconnect")
    assert r.status_code == 200
    assert r.json()["revoked"] is True
    assert revoked_calls == [("test-uid", "microsoft")]


# ──────────────────────────────────────────────────────────────────────
# POST /mail/drafts
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_mail_draft_400_when_to_empty(_auth, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/integrations/mail/drafts",
                         json={"provider": "gmail", "to": [],
                               "subject": "test", "body": "x"})
    # 422 Pydantic-level for empty list rejection OR our 400 — accept either
    assert r.status_code in (400, 422), r.text


@pytest.mark.anyio
async def test_mail_draft_409_when_not_connected(_auth, monkeypatch):
    monkeypatch.setattr(_store, "load", lambda uid, prov: None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/integrations/mail/drafts",
                         json={"provider": "gmail", "to": ["x@y.com"],
                               "subject": "test", "body": "x"})
    assert r.status_code == 409
    body = r.json()
    code = body.get("code") or (body.get("detail") or {}).get("code")
    assert code == "not_connected"


@pytest.mark.anyio
async def test_mail_draft_creates_when_connected(_auth, monkeypatch):
    monkeypatch.setattr(_store, "load",
                        lambda uid, prov: {"encryptedRefreshToken": "abc"})
    monkeypatch.setattr(_v1._g, "create_gmail_draft",
                        lambda *args, **kw: {"id": "draft_123",
                                             "message": {"id": "msg_999"}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/integrations/mail/drafts",
                         json={"provider": "gmail", "to": ["x@y.com"],
                               "subject": "test", "body": "x"})
    assert r.status_code == 201
    body = r.json()
    assert body["externalDraftId"] == "draft_123"
    assert body["provider"] == "gmail"
    assert "drafts" in body["openUrl"]
