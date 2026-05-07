"""Phase 7 — group / channel shared-data tests.

Verifies:
  - linked speaker + sharedToWorkspaceTeams hit → returns shared session
  - linked speaker, no shared session → "no shared data" message
  - any group context with credit / TODO → "private rejected" (never leaks)
  - unlinked speaker in group → unsupported notice (never leaks)
"""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services import line_link_tokens, line_messaging, slack_link_tokens
from app.services import group_shared_briefing
from app.services.integrations import slack_client


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def line_ready(monkeypatch):
    monkeypatch.setattr(line_messaging, "is_configured", lambda: True)
    monkeypatch.setattr(line_messaging, "verify_signature",
                        lambda *, body, header_signature: True)
    sent = []
    monkeypatch.setattr(line_messaging, "reply",
                        lambda token, msgs: sent.append(msgs[0]["text"]))
    return sent


@pytest.fixture
def slack_ready(monkeypatch):
    monkeypatch.setattr(slack_client, "is_configured", lambda: True)
    monkeypatch.setattr(slack_client, "verify_signature",
                        lambda *, body, timestamp, signature: True)
    sent = []
    monkeypatch.setattr(slack_client, "post_message",
                        lambda *, team_id, channel, text, thread_ts=None:
                            sent.append(text))
    return sent


# ── LINE group ─────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_line_group_credit_request_is_rejected(line_ready, monkeypatch):
    monkeypatch.setattr(line_link_tokens, "get_link",
                        lambda u: {"accountId": "acct", "deepnoteUid": "uid"})
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt",
        "source": {"type": "group", "groupId": "G", "userId": "U"},
        "message": {"type": "text", "text": "クレジット"},
    }]}).encode()
    async with _client() as c:
        await c.post("/integrations/line/webhook", content=body,
                     headers={"X-Line-Signature": "x"})
    text = line_ready[-1]
    assert "クレジット残量" in text and "個人情報" in text
    assert "あなたのDeepNoteアカウント" not in text


@pytest.mark.anyio
async def test_line_group_decisions_returns_shared_data(line_ready, monkeypatch):
    monkeypatch.setattr(line_link_tokens, "get_link",
                        lambda u: {"accountId": "acct", "deepnoteUid": "uid"})
    monkeypatch.setattr(group_shared_briefing, "get_recent_shared_decisions",
                        lambda acc, ws, *, limit=3: ["決定A", "決定B"])
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt",
        "source": {"type": "group", "groupId": "G", "userId": "U"},
        "message": {"type": "text", "text": "決定事項"},
    }]}).encode()
    async with _client() as c:
        await c.post("/integrations/line/webhook", content=body,
                     headers={"X-Line-Signature": "x"})
    text = line_ready[-1]
    assert "決定A" in text and "決定B" in text


@pytest.mark.anyio
async def test_line_group_decisions_no_shared_data(line_ready, monkeypatch):
    monkeypatch.setattr(line_link_tokens, "get_link",
                        lambda u: {"accountId": "acct", "deepnoteUid": "uid"})
    monkeypatch.setattr(group_shared_briefing, "get_recent_shared_decisions",
                        lambda acc, ws, *, limit=3: [])
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt",
        "source": {"type": "group", "groupId": "G", "userId": "U"},
        "message": {"type": "text", "text": "決定事項"},
    }]}).encode()
    async with _client() as c:
        await c.post("/integrations/line/webhook", content=body,
                     headers={"X-Line-Signature": "x"})
    text = line_ready[-1]
    assert "共有された会議データが見つかりませんでした" in text


@pytest.mark.anyio
async def test_line_group_unlinked_user_returns_unsupported(line_ready, monkeypatch):
    monkeypatch.setattr(line_link_tokens, "get_link", lambda u: None)
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt",
        "source": {"type": "group", "groupId": "G", "userId": "Uu"},
        "message": {"type": "text", "text": "決定事項"},
    }]}).encode()
    async with _client() as c:
        await c.post("/integrations/line/webhook", content=body,
                     headers={"X-Line-Signature": "x"})
    text = line_ready[-1]
    assert "未対応" in text
    assert "あなたのDeepNoteアカウント" not in text


# ── Slack channel ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_slack_channel_credit_request_rejected(slack_ready, monkeypatch):
    monkeypatch.setattr(slack_link_tokens, "get_link",
                        lambda team, user: {"accountId": "a", "deepnoteUid": "u",
                                             "teamId": team, "slackUserId": user})
    body = json.dumps({"type": "event_callback", "team_id": "T",
        "event": {"type": "app_mention", "channel_type": "channel", "channel": "C",
                  "user": "U", "text": "<@BOT> credit", "ts": "1.0"}}).encode()
    async with _client() as c:
        await c.post("/integrations/slack/events", content=body)
    text = slack_ready[-1]
    assert "個人情報" in text


@pytest.mark.anyio
async def test_slack_channel_decisions_returns_shared(slack_ready, monkeypatch):
    monkeypatch.setattr(slack_link_tokens, "get_link",
                        lambda team, user: {"accountId": "a", "deepnoteUid": "u",
                                             "teamId": team, "slackUserId": user})
    monkeypatch.setattr(group_shared_briefing, "get_recent_shared_decisions",
                        lambda acc, ws, *, limit=3: ["共有決定X"])
    body = json.dumps({"type": "event_callback", "team_id": "T",
        "event": {"type": "app_mention", "channel_type": "channel", "channel": "C",
                  "user": "U", "text": "<@BOT> 決定事項", "ts": "1.0"}}).encode()
    async with _client() as c:
        await c.post("/integrations/slack/events", content=body)
    text = slack_ready[-1]
    assert "共有決定X" in text


@pytest.mark.anyio
async def test_slack_non_mention_in_channel_is_ignored(slack_ready, monkeypatch):
    """Plain channel messages (not @mention) must NOT trigger any reply."""
    body = json.dumps({"type": "event_callback", "team_id": "T",
        "event": {"type": "message", "channel_type": "channel", "channel": "C",
                  "user": "U", "text": "クレジット", "ts": "1.0"}}).encode()
    async with _client() as c:
        await c.post("/integrations/slack/events", content=body)
    assert slack_ready == []
