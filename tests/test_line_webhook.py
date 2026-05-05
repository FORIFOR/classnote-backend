"""HTTP-level tests for /integrations/line/* endpoints.

Stubs:
  - app.services.line_messaging.is_configured / verify_signature / reply
  - app.services.line_link_tokens (issue / get_link / resolve / consume)
  - app.services.line_briefing.* (credit / latest / todos / decisions)

Auth-protected `consume` route uses dependency_overrides for get_current_user.
"""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.dependencies import CurrentUser, get_current_user
from app.services import line_link_tokens, line_messaging, line_briefing


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(line_messaging, "is_configured", lambda: True)
    monkeypatch.setattr(line_messaging, "verify_signature", lambda *, body, header_signature: True)
    sent = []
    monkeypatch.setattr(line_messaging, "reply", lambda token, msgs: sent.append(("reply", token, msgs)))
    monkeypatch.setattr(line_messaging, "push", lambda to, msgs: sent.append(("push", to, msgs)))
    return sent


@pytest.fixture
def fake_links(monkeypatch):
    state = {"links": {}, "issued": []}

    def _issue(*, line_user_id, line_group_id=None, line_source_type="user"):
        token = f"tok-{line_user_id}-{len(state['issued'])}"
        state["issued"].append({"token": token, "lineUserId": line_user_id})
        return token

    def _get_link(line_user_id):
        return state["links"].get(line_user_id)

    def _resolve(token):
        for issued in state["issued"]:
            if issued["token"] == token:
                return {
                    "lineUserId": issued["lineUserId"],
                    "lineSourceType": "user",
                    "expiresAt": None,
                    "usedAt": None,
                }
        raise line_link_tokens.TokenError("token_unknown", 404)

    def _consume(token, *, deepnote_uid, account_id):
        for issued in state["issued"]:
            if issued["token"] == token:
                state["links"][issued["lineUserId"]] = {
                    "deepnoteUid": deepnote_uid,
                    "accountId": account_id,
                    "lineSourceType": "user",
                }
                return {
                    "lineUserId": issued["lineUserId"],
                    "lineSourceType": "user",
                    "deepnoteUid": deepnote_uid,
                    "accountId": account_id,
                }
        raise line_link_tokens.TokenError("token_unknown", 404)

    monkeypatch.setattr(line_link_tokens, "issue", _issue)
    monkeypatch.setattr(line_link_tokens, "get_link", _get_link)
    monkeypatch.setattr(line_link_tokens, "resolve", _resolve)
    monkeypatch.setattr(line_link_tokens, "consume", _consume)
    return state


# ──────────────────────────────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_webhook_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(line_messaging, "is_configured", lambda: False)
    body = json.dumps({"events": []}).encode()
    async with _client() as c:
        r = await c.post("/integrations/line/webhook", content=body,
                         headers={"X-Line-Signature": "x"})
    assert r.status_code == 503
    assert r.json()["detail"] == "line_messaging_not_configured"


@pytest.mark.anyio
async def test_webhook_401_on_bad_signature(monkeypatch):
    monkeypatch.setattr(line_messaging, "is_configured", lambda: True)
    monkeypatch.setattr(line_messaging, "verify_signature",
                        lambda *, body, header_signature: False)
    body = json.dumps({"events": []}).encode()
    async with _client() as c:
        r = await c.post("/integrations/line/webhook", content=body,
                         headers={"X-Line-Signature": "bad"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_signature"


@pytest.mark.anyio
async def test_webhook_unlinked_user_replies_with_connect_url(configured, fake_links):
    body = json.dumps({"events": [{
        "type": "message",
        "replyToken": "rt-1",
        "source": {"type": "user", "userId": "U1"},
        "message": {"type": "text", "text": "ヘルプ"},
    }]}).encode()
    async with _client() as c:
        r = await c.post("/integrations/line/webhook", content=body,
                         headers={"X-Line-Signature": "x"})
    assert r.status_code == 200
    assert configured  # at least one reply queued
    last = configured[-1]
    assert last[0] == "reply" and last[1] == "rt-1"
    text = last[2][0]["text"]
    assert "DeepNote" in text and "/integrations/line/connect?token=" in text
    assert "Safari" in text  # in-app browser hint


@pytest.mark.anyio
async def test_webhook_linked_user_credit_command(configured, fake_links, monkeypatch):
    fake_links["links"]["U2"] = {"deepnoteUid": "uid-2", "accountId": "acct-2",
                                  "lineSourceType": "user"}
    monkeypatch.setattr(line_briefing, "get_credit_summary", lambda acct: {
        "plan": "standard", "remaining": 42, "monthlyLimit": 100,
        "topupCredits": 0, "used": 58, "unlimited": False,
    })
    body = json.dumps({"events": [{
        "type": "message",
        "replyToken": "rt-2",
        "source": {"type": "user", "userId": "U2"},
        "message": {"type": "text", "text": "クレジット"},
    }]}).encode()
    async with _client() as c:
        r = await c.post("/integrations/line/webhook", content=body,
                         headers={"X-Line-Signature": "x"})
    assert r.status_code == 200
    text = configured[-1][2][0]["text"]
    assert "あなたのDeepNoteアカウント" in text
    assert "42" in text and "100" in text
    assert "standard" in text


@pytest.mark.anyio
async def test_webhook_group_message_returns_unsupported(configured, fake_links):
    body = json.dumps({"events": [{
        "type": "message",
        "replyToken": "rt-g",
        "source": {"type": "group", "groupId": "G1", "userId": "U3"},
        "message": {"type": "text", "text": "クレジット"},
    }]}).encode()
    async with _client() as c:
        r = await c.post("/integrations/line/webhook", content=body,
                         headers={"X-Line-Signature": "x"})
    assert r.status_code == 200
    text = configured[-1][2][0]["text"]
    assert "未対応" in text
    # Crucially, no credit data for U3 should leak.
    assert "あなたのDeepNoteアカウント" not in text


@pytest.mark.anyio
async def test_webhook_help_command_for_linked_user(configured, fake_links):
    fake_links["links"]["U4"] = {"deepnoteUid": "uid-4", "accountId": "acct-4",
                                  "lineSourceType": "user"}
    body = json.dumps({"events": [{
        "type": "message",
        "replyToken": "rt-4",
        "source": {"type": "user", "userId": "U4"},
        "message": {"type": "text", "text": "help"},
    }]}).encode()
    async with _client() as c:
        r = await c.post("/integrations/line/webhook", content=body,
                         headers={"X-Line-Signature": "x"})
    assert r.status_code == 200
    text = configured[-1][2][0]["text"]
    assert "クレジット" in text and "TODO" in text


# ──────────────────────────────────────────────────────────────────────
# Link token API
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_resolve_link_token_returns_only_line_metadata(fake_links):
    token = "tok-U9-0"
    fake_links["issued"].append({"token": token, "lineUserId": "U9"})
    async with _client() as c:
        r = await c.get(f"/integrations/line/link-tokens/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["lineUserId"] == "U9"
    # Must NOT expose any DeepNote-side identity.
    assert "deepnoteUid" not in body
    assert "accountId" not in body


@pytest.mark.anyio
async def test_resolve_link_token_404_unknown(fake_links):
    async with _client() as c:
        r = await c.get("/integrations/line/link-tokens/nope")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_consume_link_token_requires_auth(fake_links):
    async with _client() as c:
        r = await c.post("/integrations/line/link-tokens/anything:consume")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_consume_link_token_persists_link(fake_links):
    fake_user = CurrentUser(uid="uid-x", account_id="acct-x", provider=None,
                            phone_number=None, email="x@example.com")

    async def _dep():
        return fake_user
    app.dependency_overrides[get_current_user] = _dep
    try:
        token = "tok-U10-0"
        fake_links["issued"].append({"token": token, "lineUserId": "U10"})
        async with _client() as c:
            r = await c.post(f"/integrations/line/link-tokens/{token}:consume")
        assert r.status_code == 200
        body = r.json()
        assert body["linked"] is True
        assert body["lineUserId"] == "U10"
        assert fake_links["links"]["U10"]["deepnoteUid"] == "uid-x"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ──────────────────────────────────────────────────────────────────────
# Connect HTML
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_connect_html_inapp_browser_returns_copy_page(fake_links):
    token = "tok-U11-0"
    fake_links["issued"].append({"token": token, "lineUserId": "U11"})
    async with _client() as c:
        r = await c.get(f"/integrations/line/connect?token={token}",
                        headers={"User-Agent": "Mozilla/5.0 Line/13.0.0"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "URLをコピー" in r.text
    assert "Safari" in r.text and "Chrome" in r.text


@pytest.mark.anyio
async def test_connect_html_safari_redirects_when_frontend_set(fake_links, monkeypatch):
    monkeypatch.setenv("LINE_CONNECT_FRONTEND_URL", "https://app.deepnote.example/login")
    token = "tok-U12-0"
    fake_links["issued"].append({"token": token, "lineUserId": "U12"})
    async with _client() as c:
        r = await c.get(f"/integrations/line/connect?token={token}",
                        headers={"User-Agent": "Mozilla/5.0 Safari"},
                        follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith("https://app.deepnote.example/login")
    assert "lineToken=" in loc


@pytest.mark.anyio
async def test_connect_html_safari_returns_fallback_when_frontend_unset(fake_links, monkeypatch):
    monkeypatch.delenv("LINE_CONNECT_FRONTEND_URL", raising=False)
    token = "tok-U13-0"
    fake_links["issued"].append({"token": token, "lineUserId": "U13"})
    async with _client() as c:
        r = await c.get(f"/integrations/line/connect?token={token}",
                        headers={"User-Agent": "Mozilla/5.0 Safari"},
                        follow_redirects=False)
    assert r.status_code == 200
    assert "DeepNote" in r.text


@pytest.mark.anyio
async def test_connect_html_invalid_token_returns_error_page(fake_links):
    async with _client() as c:
        r = await c.get("/integrations/line/connect?token=does-not-exist",
                        headers={"User-Agent": "Mozilla/5.0 Safari"})
    assert r.status_code == 400
    assert "有効期限" in r.text or "無効" in r.text
