"""Phase 3 — scheduled digest endpoint + scheduler tests."""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services import digest_scheduler, line_briefing, line_messaging
from app.services.integrations import slack_client


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_run_morning_digests_503_without_token(monkeypatch):
    monkeypatch.delenv("DIGEST_INTERNAL_TOKEN", raising=False)
    async with _client() as c:
        r = await c.post("/internal/tasks/run_morning_digests")
    assert r.status_code == 503


@pytest.mark.anyio
async def test_run_morning_digests_401_on_bad_token(monkeypatch):
    monkeypatch.setenv("DIGEST_INTERNAL_TOKEN", "secret-1")
    async with _client() as c:
        r = await c.post("/internal/tasks/run_morning_digests",
                         headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_run_morning_digests_pushes_to_each_linked_user(monkeypatch):
    monkeypatch.setenv("DIGEST_INTERNAL_TOKEN", "secret-2")

    monkeypatch.setattr(digest_scheduler, "_line_links", lambda: [
        {"lineUserId": "U1", "accountId": "acct-1"},
        {"lineUserId": "U2", "accountId": "acct-2"},
    ])
    monkeypatch.setattr(digest_scheduler, "_slack_links", lambda: [
        {"teamId": "T1", "slackUserId": "S1", "accountId": "acct-3"},
    ])

    monkeypatch.setattr(line_briefing, "get_credit_summary", lambda acc: {
        "plan": "standard", "remaining": 50, "monthlyLimit": 100,
        "topupCredits": 0, "used": 50, "unlimited": False,
    })
    monkeypatch.setattr(line_briefing, "get_latest_session", lambda acc: {
        "id": f"sid-{acc}", "title": f"会議-{acc}", "summary": "ok",
    })
    monkeypatch.setattr(line_briefing, "get_recent_todos",
                        lambda acc, *, limit=3: [{"title": "t1"}, {"title": "t2"}])

    line_pushes = []
    monkeypatch.setattr(line_messaging, "push",
                        lambda to, msgs: line_pushes.append((to, msgs[0]["text"])))
    slack_posts = []
    monkeypatch.setattr(slack_client, "post_message",
                        lambda *, team_id, channel, text, thread_ts=None:
                            slack_posts.append((team_id, channel, text)))

    async with _client() as c:
        r = await c.post("/internal/tasks/run_morning_digests",
                         headers={"Authorization": "Bearer secret-2"})
    assert r.status_code == 200
    body = r.json()
    assert body["line"] == 2 and body["slack"] == 1 and body["failed"] == 0
    assert {x[0] for x in line_pushes} == {"U1", "U2"}
    assert slack_posts == [("T1", "S1", slack_posts[0][2])]
    digest_text = line_pushes[0][1]
    assert "おはようございます" in digest_text
    assert "クレジット" in digest_text
    assert "最新の会議" in digest_text


@pytest.mark.anyio
async def test_digest_failure_for_one_user_does_not_block_others(monkeypatch):
    monkeypatch.setenv("DIGEST_INTERNAL_TOKEN", "s3")
    monkeypatch.setattr(digest_scheduler, "_line_links", lambda: [
        {"lineUserId": "OK1", "accountId": "a1"},
        {"lineUserId": "FAIL", "accountId": "a2"},
        {"lineUserId": "OK2", "accountId": "a3"},
    ])
    monkeypatch.setattr(digest_scheduler, "_slack_links", lambda: [])
    monkeypatch.setattr(line_briefing, "get_credit_summary", lambda acc: None)
    monkeypatch.setattr(line_briefing, "get_latest_session", lambda acc: None)
    monkeypatch.setattr(line_briefing, "get_recent_todos", lambda acc, *, limit=3: [])

    def _push(to, msgs):
        if to == "FAIL":
            raise RuntimeError("boom")
    monkeypatch.setattr(line_messaging, "push", _push)

    async with _client() as c:
        r = await c.post("/internal/tasks/run_morning_digests",
                         headers={"Authorization": "Bearer s3"})
    assert r.status_code == 200
    body = r.json()
    assert body["line"] == 2  # OK1, OK2
    assert body["failed"] == 1
