"""HTTP-level tests for /integrations/slack/* endpoints."""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.dependencies import CurrentUser, get_current_user
from app.services import slack_link_tokens, slack_briefing
from app.services.integrations import slack_client


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(slack_client, "is_configured", lambda: True)
    monkeypatch.setattr(slack_client, "verify_signature",
                        lambda *, body, timestamp, signature: True)
    sent = []
    monkeypatch.setattr(slack_client, "post_message",
                        lambda *, team_id, channel, text, thread_ts=None:
                            sent.append({"team": team_id, "channel": channel,
                                         "text": text, "thread_ts": thread_ts}))
    return sent


@pytest.fixture
def fake_links(monkeypatch):
    state = {"links": {}, "issued": []}

    def _issue(*, team_id, slack_user_id, slack_channel_id=None):
        token = f"tok-{team_id}-{slack_user_id}-{len(state['issued'])}"
        state["issued"].append({"token": token, "teamId": team_id,
                                "slackUserId": slack_user_id,
                                "slackChannelId": slack_channel_id})
        return token

    def _get_link(team_id, slack_user_id):
        return state["links"].get((team_id, slack_user_id))

    def _resolve(token):
        for issued in state["issued"]:
            if issued["token"] == token:
                return {
                    "teamId": issued["teamId"],
                    "slackUserId": issued["slackUserId"],
                    "slackChannelId": issued["slackChannelId"],
                    "expiresAt": None,
                    "usedAt": None,
                }
        raise slack_link_tokens.TokenError("token_unknown", 404)

    def _consume(token, *, deepnote_uid, account_id):
        for issued in state["issued"]:
            if issued["token"] == token:
                state["links"][(issued["teamId"], issued["slackUserId"])] = {
                    "deepnoteUid": deepnote_uid,
                    "accountId": account_id,
                    "teamId": issued["teamId"],
                    "slackUserId": issued["slackUserId"],
                }
                return {
                    "teamId": issued["teamId"],
                    "slackUserId": issued["slackUserId"],
                    "slackChannelId": issued["slackChannelId"],
                    "deepnoteUid": deepnote_uid,
                    "accountId": account_id,
                }
        raise slack_link_tokens.TokenError("token_unknown", 404)

    monkeypatch.setattr(slack_link_tokens, "issue", _issue)
    monkeypatch.setattr(slack_link_tokens, "get_link", _get_link)
    monkeypatch.setattr(slack_link_tokens, "resolve", _resolve)
    monkeypatch.setattr(slack_link_tokens, "consume", _consume)
    return state


# ── URL verification & signature ─────────────────────────────────────

@pytest.mark.anyio
async def test_events_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(slack_client, "is_configured", lambda: False)
    body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()
    async with _client() as c:
        r = await c.post("/integrations/slack/events", content=body)
    assert r.status_code == 503


@pytest.mark.anyio
async def test_events_401_on_bad_signature(monkeypatch):
    monkeypatch.setattr(slack_client, "is_configured", lambda: True)
    monkeypatch.setattr(slack_client, "verify_signature",
                        lambda *, body, timestamp, signature: False)
    async with _client() as c:
        r = await c.post("/integrations/slack/events", content=b"{}",
                         headers={"X-Slack-Signature": "v0=bad",
                                  "X-Slack-Request-Timestamp": "0"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_url_verification_returns_challenge(configured):
    body = json.dumps({"type": "url_verification", "challenge": "CHAL-123"}).encode()
    async with _client() as c:
        r = await c.post("/integrations/slack/events", content=body)
    assert r.status_code == 200
    assert r.text == "CHAL-123"


# ── DM events ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_dm_unlinked_replies_with_connect_url(configured, fake_links):
    body = json.dumps({
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message", "channel_type": "im",
            "channel": "D1", "user": "U1", "text": "ヘルプ", "ts": "1.0",
        }
    }).encode()
    async with _client() as c:
        r = await c.post("/integrations/slack/events", content=body)
    assert r.status_code == 200
    assert configured  # something posted
    posted = configured[-1]
    assert posted["team"] == "T1" and posted["channel"] == "D1"
    assert "/integrations/slack/connect?token=" in posted["text"]
    assert "DeepNote" in posted["text"]


@pytest.mark.anyio
async def test_dm_linked_credit_command(configured, fake_links, monkeypatch):
    fake_links["links"][("T2", "U2")] = {"deepnoteUid": "uid-2", "accountId": "acct-2",
                                         "teamId": "T2", "slackUserId": "U2"}
    monkeypatch.setattr(slack_briefing, "get_credit_summary", lambda acct: {
        "plan": "standard", "remaining": 7, "monthlyLimit": 100,
        "topupCredits": 0, "used": 93, "unlimited": False,
    })
    body = json.dumps({
        "type": "event_callback",
        "team_id": "T2",
        "event": {
            "type": "message", "channel_type": "im",
            "channel": "D2", "user": "U2", "text": "クレジット", "ts": "1.0",
        }
    }).encode()
    async with _client() as c:
        r = await c.post("/integrations/slack/events", content=body)
    assert r.status_code == 200
    text = configured[-1]["text"]
    assert "あなたのDeepNoteアカウント" in text and "7" in text and "100" in text


@pytest.mark.anyio
async def test_channel_mention_returns_unsupported(configured, fake_links):
    """Public channel mentions must NEVER expose personal data."""
    fake_links["links"][("T3", "U3")] = {"deepnoteUid": "uid-3", "accountId": "acct-3",
                                         "teamId": "T3", "slackUserId": "U3"}
    body = json.dumps({
        "type": "event_callback",
        "team_id": "T3",
        "event": {
            "type": "app_mention", "channel_type": "channel",
            "channel": "C3", "user": "U3", "text": "<@BOT> クレジット", "ts": "1.0",
        }
    }).encode()
    async with _client() as c:
        r = await c.post("/integrations/slack/events", content=body)
    assert r.status_code == 200
    text = configured[-1]["text"]
    assert "未対応" in text
    assert "あなたのDeepNoteアカウント" not in text


@pytest.mark.anyio
async def test_bot_message_is_ignored(configured, fake_links):
    """We must never reply to other bots / our own posts (loop prevention)."""
    body = json.dumps({
        "type": "event_callback",
        "team_id": "T4",
        "event": {
            "type": "message", "channel_type": "im",
            "channel": "D4", "user": "U4", "text": "クレジット",
            "bot_id": "B999", "ts": "1.0",
        }
    }).encode()
    async with _client() as c:
        r = await c.post("/integrations/slack/events", content=body)
    assert r.status_code == 200
    assert configured == []


# ── Link token API ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_resolve_link_token_minimal_disclosure(fake_links):
    fake_links["issued"].append({"token": "tok-T-U-0", "teamId": "T",
                                 "slackUserId": "U", "slackChannelId": "C"})
    async with _client() as c:
        r = await c.get("/integrations/slack/link-tokens/tok-T-U-0")
    assert r.status_code == 200
    body = r.json()
    assert body["teamId"] == "T" and body["slackUserId"] == "U"
    assert "deepnoteUid" not in body and "accountId" not in body


@pytest.mark.anyio
async def test_consume_requires_auth(fake_links):
    async with _client() as c:
        r = await c.post("/integrations/slack/link-tokens/anything:consume")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_consume_persists_link(fake_links):
    fake_user = CurrentUser(uid="uid-x", account_id="acct-x", provider=None,
                            phone_number=None, email="x@example.com")

    async def _dep():
        return fake_user
    app.dependency_overrides[get_current_user] = _dep
    try:
        fake_links["issued"].append({"token": "tok-X-0", "teamId": "Tx",
                                     "slackUserId": "Ux", "slackChannelId": "Cx"})
        async with _client() as c:
            r = await c.post("/integrations/slack/link-tokens/tok-X-0:consume")
        assert r.status_code == 200
        body = r.json()
        assert body["linked"] is True and body["teamId"] == "Tx"
        assert fake_links["links"][("Tx", "Ux")]["deepnoteUid"] == "uid-x"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── Connect HTML ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_connect_inapp_returns_copy_page(fake_links):
    fake_links["issued"].append({"token": "tok-Tc-0", "teamId": "Tc",
                                 "slackUserId": "Uc", "slackChannelId": None})
    async with _client() as c:
        r = await c.get("/integrations/slack/connect?token=tok-Tc-0",
                        headers={"User-Agent": "Mozilla/5.0 Slack/22.0.0"})
    assert r.status_code == 200
    assert "URLをコピー" in r.text


@pytest.mark.anyio
async def test_connect_invalid_token(fake_links):
    async with _client() as c:
        r = await c.get("/integrations/slack/connect?token=does-not-exist")
    assert r.status_code == 400
    assert "有効期限" in r.text or "無効" in r.text
