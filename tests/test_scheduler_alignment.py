"""Phase 2-B / V-037 — scheduler hardening + notifications API contract.

Coverage:
  - scheduled_tasks API: invalid RRULE rejection
  - /internal/scheduler/tick: fail-closed when secret unset, header
    enforcement, find_due exception propagation
  - /v1/notifications: account-scope isolation on list / markRead /
    markAllRead
  - dispatcher: desktop branch creates notification_event idempotently
  - Tests intentionally avoid the live Firestore client — services are
    monkeypatched at module level.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.dependencies import get_current_user, CurrentUser
from app.services import scheduled_tasks as _st
from app.services import notifications as _notif
from app.routes import scheduled_tasks_routes as _routes


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────

ACCT_A = "acct-A"
ACCT_B = "acct-B"


@pytest.fixture
def client(_auth_as_acct_a):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def _auth_as_acct_a():
    fake = CurrentUser(
        uid="uid-A", account_id=ACCT_A, provider="google.com",
        phone_number=None, email="a@example.com", display_name="A",
        photo_url=None, has_custom_claims=False,
    )
    app.dependency_overrides[get_current_user] = lambda: fake
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ──────────────────────────────────────────────────────────────────────
# 1. scheduled_tasks API — invalid RRULE rejection
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_create_invalid_rrule_returns_400(_auth_as_acct_a, monkeypatch):
    """grammatically broken rrule must be rejected at create time
    instead of stored with nextRunAt=null (V-037 strict mode)."""
    def _raise_invalid(account_id, *, body):
        raise _st.InvalidRRuleError("rrule could not be parsed: 'INVALID'")
    monkeypatch.setattr(_st, "create", _raise_invalid)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/scheduled-tasks", json={
            "type": "daily_todo_digest",
            "rrule": "INVALID",
            "destination": {"channel": "desktop", "target": "self"},
        })
    assert r.status_code == 400, r.text
    # main.py's exception handler flattens dict-detail into the body root.
    body = r.json()
    code = body.get("code") or (body.get("detail") or {}).get("code")
    assert code == "invalid_rrule", body


@pytest.mark.anyio
async def test_create_empty_rrule_returns_400(_auth_as_acct_a, monkeypatch):
    def _raise_empty(account_id, *, body):
        raise _st.InvalidRRuleError("rrule required")
    monkeypatch.setattr(_st, "create", _raise_empty)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/scheduled-tasks", json={
            "type": "daily_todo_digest",
            "rrule": "",
            "destination": {"channel": "desktop"},
        })
    assert r.status_code == 400


# ──────────────────────────────────────────────────────────────────────
# 2. /internal/scheduler/tick — fail-closed + header enforcement
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_tick_503_when_secret_unset(monkeypatch):
    monkeypatch.delenv("INTERNAL_SCHEDULER_SECRET", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick")
    assert r.status_code == 503
    body = r.json()
    code = body.get("code") or (body.get("detail") or {}).get("code")
    assert code == "scheduler_secret_not_configured", body


@pytest.mark.anyio
async def test_tick_401_when_header_missing(monkeypatch):
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick")
    assert r.status_code == 401
    body = r.json()
    code = body.get("code") or (body.get("detail") or {}).get("code")
    assert code == "missing_internal_token", body


@pytest.mark.anyio
async def test_tick_401_when_secret_mismatch(monkeypatch):
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick",
                         headers={"X-Internal-Scheduler-Secret": "wrong"})
    assert r.status_code == 401
    body = r.json()
    code = body.get("code") or (body.get("detail") or {}).get("code")
    assert code == "bad_internal_token", body


@pytest.mark.anyio
async def test_tick_propagates_due_query_error_as_500(monkeypatch):
    """Silent failure forbidden: SchedulerDueQueryError must become a
    visible HTTP 500 with code=scheduler_due_query_failed so Cloud
    Scheduler retries surface in the alerting pipeline."""
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    def _raise(*a, **kw):
        raise _st.SchedulerDueQueryError("FAILED_PRECONDITION: index missing")
    monkeypatch.setattr(_st, "find_due", _raise)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick",
                         headers={"X-Internal-Scheduler-Secret": "test-secret"})
    assert r.status_code == 500
    body = r.json()
    code = body.get("code") or (body.get("detail") or {}).get("code")
    assert code == "scheduler_due_query_failed", body


@pytest.mark.anyio
async def test_tick_with_zero_due_returns_clean_zero(monkeypatch):
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    monkeypatch.setattr(_st, "find_due", lambda *a, **kw: [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick",
                         headers={"X-Internal-Scheduler-Secret": "test-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] == 0
    assert body["dispatched"] == 0
    assert body["skipped"] == 0
    assert body["errors"] == 0


@pytest.mark.anyio
async def test_tick_legacy_header_still_accepted(monkeypatch):
    """The original Cloud Scheduler job sends X-DeepNote-Internal-Token;
    new code accepts both during the migration window."""
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    monkeypatch.setattr(_st, "find_due", lambda *a, **kw: [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick",
                         headers={"X-DeepNote-Internal-Token": "test-secret"})
    assert r.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# 3. tick → desktop dispatch → notification_event creation
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_tick_desktop_task_creates_notification_event(monkeypatch):
    """A desktop-channel task should result in a notification_event
    being created via the notifications service."""
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    run_slot = datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc)
    task = {
        "taskId": "st_test_1", "type": "daily_todo_digest",
        "channel": "desktop", "destination": {"channel": "desktop", "target": "self"},
        "nextRunAt": run_slot, "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    }
    monkeypatch.setattr(_st, "find_due", lambda *a, **kw: [(ACCT_A, task)])
    monkeypatch.setattr(_st, "try_acquire_lease", lambda *a, **kw: True)
    monkeypatch.setattr(_st, "mark_run", lambda *a, **kw: None)

    create_calls = []
    def _fake_create(**kwargs):
        create_calls.append(kwargs)
        return {**kwargs, "id": "notif_test_1", "_created": True}
    monkeypatch.setattr(_notif, "create", _fake_create)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick",
                         headers={"X-Internal-Scheduler-Secret": "test-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] == 1
    assert len(create_calls) == 1
    assert create_calls[0]["account_id"] == ACCT_A
    assert create_calls[0]["source_task_id"] == "st_test_1"
    assert create_calls[0]["notification_type"] == "daily_todo_digest"
    # Idempotency key must include the task id and runSlot.
    key = create_calls[0]["idempotency_key"]
    assert "st_test_1" in key
    assert "scheduled_task:" in key


@pytest.mark.anyio
async def test_tick_lease_failure_is_skipped_not_failure(monkeypatch):
    """If try_acquire_lease returns False (another tick / already done),
    the task counts as ``skipped`` not ``errors``."""
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    task = {
        "taskId": "st_test_2", "type": "daily_todo_digest",
        "channel": "desktop", "nextRunAt": datetime.now(timezone.utc),
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    }
    monkeypatch.setattr(_st, "find_due", lambda *a, **kw: [(ACCT_A, task)])
    monkeypatch.setattr(_st, "try_acquire_lease", lambda *a, **kw: False)

    create_calls = []
    monkeypatch.setattr(_notif, "create",
                        lambda **kw: create_calls.append(kw) or {"_created": True, "id": "x"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick",
                         headers={"X-Internal-Scheduler-Secret": "test-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] == 0
    assert body["skipped"] == 1
    assert len(create_calls) == 0


@pytest.mark.anyio
async def test_tick_skips_task_with_null_next_run(monkeypatch):
    monkeypatch.setenv("INTERNAL_SCHEDULER_SECRET", "test-secret")
    task = {"taskId": "st_test_3", "type": "daily_todo_digest",
            "channel": "desktop", "nextRunAt": None}
    monkeypatch.setattr(_st, "find_due", lambda *a, **kw: [(ACCT_A, task)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/internal/scheduler/tick",
                         headers={"X-Internal-Scheduler-Secret": "test-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] == 0
    assert body["skipped"] == 1


# ──────────────────────────────────────────────────────────────────────
# 4. /v1/notifications — account-scoped list / markRead / markAllRead
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_list_notifications_returns_own_only(_auth_as_acct_a, monkeypatch):
    captured = {}
    def _fake_list(account_id, *, unread=None, limit=50):
        captured["account_id"] = account_id
        return [{
            "id": "n1", "accountId": account_id,
            "type": "daily_todo_digest", "title": "t", "body": "b",
            "read": False, "createdAt": datetime.now(timezone.utc),
        }]
    monkeypatch.setattr(_notif, "list_for", _fake_list)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/notifications?unread=true")
    assert r.status_code == 200
    body = r.json()
    assert captured["account_id"] == ACCT_A
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == "n1"


@pytest.mark.anyio
async def test_mark_read_404_for_other_account(_auth_as_acct_a, monkeypatch):
    """mark_read returning False (cross-account or missing) must
    surface as 404 (do NOT leak existence with 403)."""
    monkeypatch.setattr(_notif, "mark_read", lambda *a, **kw: False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/notifications/n_other:markRead")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_mark_read_204_when_owned(_auth_as_acct_a, monkeypatch):
    monkeypatch.setattr(_notif, "mark_read", lambda *a, **kw: True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/notifications/n_owned:markRead")
    assert r.status_code == 204


@pytest.mark.anyio
async def test_mark_all_read_returns_count(_auth_as_acct_a, monkeypatch):
    monkeypatch.setattr(_notif, "mark_all_read", lambda *a, **kw: 7)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/notifications:markAllRead")
    assert r.status_code == 200
    assert r.json()["written"] == 7


# ──────────────────────────────────────────────────────────────────────
# 5. notifications service idempotency
# ──────────────────────────────────────────────────────────────────────

def test_notifications_create_idempotent_via_idempotency_key(monkeypatch):
    """If idempotencyKey already exists, create() must NOT write a new
    document — return the existing one with _created=False."""
    existing = {"id": "n_existing", "accountId": ACCT_A,
                "type": "daily_todo_digest", "title": "old", "body": "old",
                "read": False, "createdAt": datetime.now(timezone.utc),
                "idempotencyKey": "scheduled_task:st1:slot"}
    monkeypatch.setattr(_notif, "find_by_idempotency_key", lambda a, k: existing)

    db_writes = []
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.set = lambda payload: db_writes.append(payload)
    monkeypatch.setattr(_notif, "_coll", lambda: fake_db.collection.return_value)

    out = _notif.create(
        account_id=ACCT_A, notification_type="daily_todo_digest",
        title="new (should be ignored)", body="new",
        idempotency_key="scheduled_task:st1:slot",
    )
    assert out["id"] == "n_existing"
    assert out["_created"] is False
    assert db_writes == []  # NO new write
