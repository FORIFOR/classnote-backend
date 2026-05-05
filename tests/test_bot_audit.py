"""Tests for app.services.bot_audit + integration into LINE / Slack handlers."""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services import bot_audit, line_link_tokens, line_messaging, line_briefing
from app.services import slack_link_tokens, slack_briefing
from app.services.integrations import slack_client


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def captured_audit(monkeypatch):
    rows = []

    def _record(**kw):
        rows.append(kw)

    monkeypatch.setattr(bot_audit, "record", _record)
    return rows


@pytest.fixture
def line_configured(monkeypatch):
    monkeypatch.setattr(line_messaging, "is_configured", lambda: True)
    monkeypatch.setattr(line_messaging, "verify_signature", lambda *, body, header_signature: True)
    monkeypatch.setattr(line_messaging, "reply", lambda token, msgs: None)


@pytest.fixture
def line_links(monkeypatch):
    state = {"links": {}, "issued": []}
    monkeypatch.setattr(line_link_tokens, "issue",
                        lambda *, line_user_id, line_group_id=None, line_source_type="user":
                            (state["issued"].append({"token": f"t-{line_user_id}", "lineUserId": line_user_id}),
                             f"t-{line_user_id}")[1])
    monkeypatch.setattr(line_link_tokens, "get_link",
                        lambda lu: state["links"].get(lu))
    return state


@pytest.fixture
def slack_configured(monkeypatch):
    monkeypatch.setattr(slack_client, "is_configured", lambda: True)
    monkeypatch.setattr(slack_client, "verify_signature", lambda *, body, timestamp, signature: True)
    monkeypatch.setattr(slack_client, "post_message",
                        lambda *, team_id, channel, text, thread_ts=None: None)


@pytest.fixture
def slack_links(monkeypatch):
    state = {"links": {}, "issued": []}
    monkeypatch.setattr(slack_link_tokens, "issue",
                        lambda *, team_id, slack_user_id, slack_channel_id=None:
                            (state["issued"].append({"team": team_id, "user": slack_user_id}),
                             f"t-{team_id}-{slack_user_id}")[1])
    monkeypatch.setattr(slack_link_tokens, "get_link",
                        lambda team, user: state["links"].get((team, user)))
    return state


# ── audit row schema ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_line_dm_unlinked_records_audit(line_configured, line_links, captured_audit):
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt", "source": {"type": "user", "userId": "U1"},
        "message": {"type": "text", "text": "ヘルプ"},
    }]}).encode()
    async with _client() as c:
        await c.post("/integrations/line/webhook", content=body,
                     headers={"X-Line-Signature": "x"})
    assert captured_audit, "audit not recorded"
    row = captured_audit[-1]
    assert row["provider"] == "line"
    assert row["source_type"] == "user"
    assert row["source_user_id"] == "U1"
    assert row["outcome"] == "unlinked"


@pytest.mark.anyio
async def test_line_group_credit_records_private_blocked(line_configured, line_links, captured_audit):
    """Phase 7: credit in group → blocked_private_in_group (was previously unsupported)."""
    line_links["links"]["U2"] = {"accountId": "acct", "deepnoteUid": "uid"}
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt",
        "source": {"type": "group", "groupId": "G", "userId": "U2"},
        "message": {"type": "text", "text": "クレジット"},
    }]}).encode()
    async with _client() as c:
        await c.post("/integrations/line/webhook", content=body,
                     headers={"X-Line-Signature": "x"})
    row = captured_audit[-1]
    assert row["outcome"] == "blocked_private_in_group"
    assert row["source_type"] == "group"
    assert row["command"] == "credit"


@pytest.mark.anyio
async def test_line_linked_credit_records_ok(line_configured, line_links, captured_audit, monkeypatch):
    line_links["links"]["U3"] = {"accountId": "acct-3", "deepnoteUid": "uid-3"}
    monkeypatch.setattr(line_briefing, "get_credit_summary", lambda acc: {
        "plan": "free", "remaining": 1, "monthlyLimit": 10, "topupCredits": 0,
        "used": 9, "unlimited": False,
    })
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt", "source": {"type": "user", "userId": "U3"},
        "message": {"type": "text", "text": "クレジット"},
    }]}).encode()
    async with _client() as c:
        await c.post("/integrations/line/webhook", content=body,
                     headers={"X-Line-Signature": "x"})
    row = captured_audit[-1]
    assert row["outcome"] == "ok"
    assert row["command"] == "credit"
    assert row["account_id"] == "acct-3"
    assert row["deepnote_uid"] == "uid-3"


@pytest.mark.anyio
async def test_slack_channel_credit_records_private_blocked(slack_configured, slack_links, captured_audit):
    """Phase 7: credit @mention in channel → blocked_private_in_group."""
    body = json.dumps({"type": "event_callback", "team_id": "T",
        "event": {"type": "app_mention", "channel_type": "channel", "channel": "C",
                  "user": "U", "text": "<@BOT> credit", "ts": "1.0"}}).encode()
    async with _client() as c:
        await c.post("/integrations/slack/events", content=body)
    row = captured_audit[-1]
    assert row["provider"] == "slack"
    assert row["source_type"] == "channel"
    assert row["outcome"] == "blocked_private_in_group"
    assert row["command"] == "credit"


@pytest.mark.anyio
async def test_slack_dm_linked_records_ok(slack_configured, slack_links, captured_audit, monkeypatch):
    slack_links["links"][("T", "U")] = {"accountId": "acct", "deepnoteUid": "uid",
                                         "teamId": "T", "slackUserId": "U"}
    monkeypatch.setattr(slack_briefing, "get_credit_summary", lambda acc: {
        "plan": "standard", "remaining": 5, "monthlyLimit": 100, "topupCredits": 0,
        "used": 95, "unlimited": False,
    })
    body = json.dumps({"type": "event_callback", "team_id": "T",
        "event": {"type": "message", "channel_type": "im", "channel": "D",
                  "user": "U", "text": "クレジット", "ts": "1.0"}}).encode()
    async with _client() as c:
        await c.post("/integrations/slack/events", content=body)
    row = captured_audit[-1]
    assert row["provider"] == "slack"
    assert row["source_type"] == "im"
    assert row["outcome"] == "ok"
    assert row["command"] == "credit"
    assert row["team_id"] == "T"
