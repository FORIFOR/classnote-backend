"""Tests for Phase 5 (login fallback), Phase 6 (export bridge), Phase 8 (settings)."""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.dependencies import CurrentUser, get_current_user
from app.services import line_link_tokens, slack_link_tokens


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def fb_env(monkeypatch):
    monkeypatch.setenv("FIREBASE_WEB_API_KEY", "fb-key")
    monkeypatch.setenv("FIREBASE_WEB_AUTH_DOMAIN", "test.firebaseapp.com")
    monkeypatch.setenv("FIREBASE_WEB_PROJECT_ID", "test-project")


@pytest.fixture
def fake_links(monkeypatch):
    state = {"line": [], "slack": []}

    def _line_resolve(token):
        for t in state["line"]:
            if t == token:
                return {"lineUserId": "U1"}
        raise line_link_tokens.TokenError("token_unknown", 404)

    def _slack_resolve(token):
        for t in state["slack"]:
            if t == token:
                return {"teamId": "T", "slackUserId": "U"}
        raise slack_link_tokens.TokenError("token_unknown", 404)

    monkeypatch.setattr(line_link_tokens, "resolve", _line_resolve)
    monkeypatch.setattr(slack_link_tokens, "resolve", _slack_resolve)
    return state


# ── Phase 5: login fallback ────────────────────────────────────────────

@pytest.mark.anyio
async def test_line_login_fallback_503_when_firebase_missing(fake_links, monkeypatch):
    monkeypatch.delenv("FIREBASE_WEB_API_KEY", raising=False)
    fake_links["line"].append("L1")
    async with _client() as c:
        r = await c.get("/integrations/line/login?token=L1")
    assert r.status_code == 503


@pytest.mark.anyio
async def test_line_login_fallback_renders_when_configured(fake_links, fb_env):
    fake_links["line"].append("L2")
    async with _client() as c:
        r = await c.get("/integrations/line/login?token=L2")
    assert r.status_code == 200
    assert "DeepNote と LINE の連携" in r.text
    assert "fb-key" in r.text  # config injected
    assert "/integrations/line/link-tokens/L2:consume" in r.text


@pytest.mark.anyio
async def test_slack_login_fallback_renders(fake_links, fb_env):
    fake_links["slack"].append("S1")
    async with _client() as c:
        r = await c.get("/integrations/slack/login?token=S1")
    assert r.status_code == 200
    assert "Slack" in r.text
    assert "/integrations/slack/link-tokens/S1:consume" in r.text


@pytest.mark.anyio
async def test_login_fallback_404_for_unknown_token(fake_links, fb_env):
    async with _client() as c:
        r = await c.get("/integrations/line/login?token=nope")
    assert r.status_code == 404


# ── Phase 6: export bridge ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_export_bridge_renders_when_configured(fb_env):
    async with _client() as c:
        r = await c.get("/sessions/sid-1/export?format=pdf")
    assert r.status_code == 200
    assert "sid-1" in r.text
    # session id and format are JS-injected; ensure both flow into the page
    assert '"sid-1"' in r.text or '<code>sid-1</code>' in r.text
    assert '"pdf"' in r.text or '>pdf<' in r.text


@pytest.mark.anyio
async def test_export_bridge_503_when_firebase_missing(monkeypatch):
    monkeypatch.delenv("FIREBASE_WEB_API_KEY", raising=False)
    async with _client() as c:
        r = await c.get("/sessions/sid/export?format=docx")
    assert r.status_code == 503


# ── Phase 8: settings ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_settings_html_renders(fb_env):
    async with _client() as c:
        r = await c.get("/integrations/me/settings")
    assert r.status_code == 200
    assert "DeepNote 連携設定" in r.text


@pytest.mark.anyio
async def test_links_endpoint_requires_auth():
    async with _client() as c:
        r = await c.get("/integrations/me/links")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_audit_endpoint_filters_by_uid(monkeypatch):
    fake_user = CurrentUser(uid="me", account_id="acct", provider=None,
                            phone_number=None, email="m@x.com")

    async def _dep():
        return fake_user
    app.dependency_overrides[get_current_user] = _dep

    # Stub Firestore query: pretend collection.where("deepnoteUid","==",me) returns 2 rows.
    from app.routes import bot_settings as bs

    class _Ref:
        def __init__(self, did, data): self.id = did; self._d = data
        def to_dict(self): return self._d
        @property
        def reference(self): return self
        def delete(self): pass

    class _Q:
        def where(self, *args, **kw): return self
        def limit(self, n): return self
        def stream(self):
            return [
                _Ref("a1", {"deepnoteUid": "me", "provider": "line", "command": "credit",
                            "outcome": "ok", "sourceType": "user", "at": 1}),
                _Ref("a2", {"deepnoteUid": "me", "provider": "slack", "command": "latest",
                            "outcome": "ok", "sourceType": "im", "at": 2}),
            ]

    class _Coll:
        def where(self, *args, **kw): return _Q()
        def document(self, doc_id): return _Ref(doc_id, None)

    class _DB:
        def collection(self, name): return _Coll()

    monkeypatch.setattr(bs, "db", _DB())

    try:
        async with _client() as c:
            r = await c.get("/integrations/me/audit?limit=10")
        assert r.status_code == 200
        items = r.json()["items"]
        assert {x["id"] for x in items} == {"a1", "a2"}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
