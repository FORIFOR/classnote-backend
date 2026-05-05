"""Unit tests for PR D — credits_reservations service."""
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────
# Fake Firestore minimal shim (single reservation doc)
# ─────────────────────────────────────────────


class _FakeSnap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDoc:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    def get(self):
        return _FakeSnap(self._store.get(self._key))

    def set(self, payload, merge=False):
        if merge and self._key in self._store:
            merged = dict(self._store[self._key])
            merged.update(payload)
            self._store[self._key] = merged
        else:
            self._store[self._key] = dict(payload)

    def update(self, payload):
        base = dict(self._store.get(self._key) or {})
        for k, v in payload.items():
            base[k] = v
        self._store[self._key] = base


class _FakeSubcoll:
    def __init__(self, store, prefix):
        self._store = store
        self._prefix = prefix

    def document(self, doc_id):
        return _FakeDoc(self._store, f"{self._prefix}/{doc_id}")


class _FakeSessionDoc:
    def __init__(self, store, sid):
        self._store = store
        self._sid = sid

    def collection(self, name):
        return _FakeSubcoll(self._store, f"sessions/{self._sid}/{name}")


class _FakeSessions:
    def __init__(self, store):
        self._store = store

    def document(self, sid):
        return _FakeSessionDoc(self._store, sid)


class _FakeDB:
    def __init__(self):
        self._store = {}

    def collection(self, name):
        assert name == "sessions"
        return _FakeSessions(self._store)


@pytest.fixture
def fake_db():
    return _FakeDB()


@pytest.fixture
def fake_credits():
    mock = MagicMock()
    mock.can_consume.return_value = (True, {"remaining": 100})
    mock.consume.return_value = {"remaining": 98}
    mock.refund.return_value = None
    return mock


def _patch_all(fake_db, fake_credits):
    return [
        patch("app.services.credits_reservations.db", fake_db),
        patch("app.services.credits_reservations.ai_credits", fake_credits),
    ]


def _enter(ctxs):
    for c in ctxs:
        c.__enter__()


def _exit(ctxs):
    for c in reversed(ctxs):
        c.__exit__(None, None, None)


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────


class TestReserve:
    def test_requires_session_id(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            with pytest.raises(ValueError):
                cr.reserve(
                    session_id="",
                    account_id="a1",
                    uid="u1",
                    plan={"summary": True},
                    client_request_id="req1",
                    operation_id="op1",
                )
        finally:
            _exit(ctxs)

    def test_requires_client_request_id(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            with pytest.raises(ValueError):
                cr.reserve(
                    session_id="s1",
                    account_id="a1",
                    uid="u1",
                    plan={"summary": True},
                    client_request_id="",
                    operation_id="op1",
                )
        finally:
            _exit(ctxs)

    def test_reserve_consumes_each_feature(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            result = cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={"summary": True, "quiz": True},
                client_request_id="req1",
                operation_id="op1",
            )
            assert result["reservationId"] == "req1"
            assert result["status"] == "reserved"
            assert result["totalAmount"] == 2
            assert set(result["items"].keys()) == {"summary", "quiz"}
            # One can_consume and two consume calls
            fake_credits.can_consume.assert_called_once_with("a1", 2)
            assert fake_credits.consume.call_count == 2
            modes = [c.args[2] for c in fake_credits.consume.call_args_list]
            assert "summary_generated" in modes
            assert "quiz_generated" in modes
        finally:
            _exit(ctxs)

    def test_reserve_is_idempotent_on_client_request_id(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            first = cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={"summary": True},
                client_request_id="req1",
                operation_id="op1",
            )
            fake_credits.consume.reset_mock()
            second = cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={"summary": True},
                client_request_id="req1",
                operation_id="op2",
            )
            assert second["reservationId"] == first["reservationId"]
            # No double-consume on replay
            fake_credits.consume.assert_not_called()
        finally:
            _exit(ctxs)

    def test_insufficient_credits_raises_and_skips_consume(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        fake_credits.can_consume.return_value = (False, {"remaining": 0})
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            with pytest.raises(cr.InsufficientCreditsError):
                cr.reserve(
                    session_id="s1",
                    account_id="a1",
                    uid="u1",
                    plan={"summary": True, "quiz": True},
                    client_request_id="req1",
                    operation_id="op1",
                )
            fake_credits.consume.assert_not_called()
        finally:
            _exit(ctxs)

    def test_consume_failure_rolls_back_prior_consumes(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        # First consume ok, second raises
        fake_credits.consume.side_effect = [{"remaining": 99}, RuntimeError("boom")]
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            with pytest.raises(cr.ReservationError):
                cr.reserve(
                    session_id="s1",
                    account_id="a1",
                    uid="u1",
                    plan={"summary": True, "quiz": True},
                    client_request_id="req1",
                    operation_id="op1",
                )
            fake_credits.refund.assert_called_once()
        finally:
            _exit(ctxs)

    def test_empty_plan_creates_zero_reservation(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            result = cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={},
                client_request_id="req1",
                operation_id="op1",
            )
            assert result["totalAmount"] == 0
            assert result["items"] == {}
            fake_credits.consume.assert_not_called()
        finally:
            _exit(ctxs)


class TestCommit:
    def test_commit_single_feature_marks_it(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={"summary": True, "quiz": True},
                client_request_id="req1",
                operation_id="op1",
            )
            cr.commit(session_id="s1", reservation_id="req1", feature="summary")
            data = cr._get_existing("s1", "req1")
            assert data["items"]["summary"]["status"] == "committed"
            assert data["items"]["quiz"]["status"] == "reserved"
            # Not all committed yet
            assert data["status"] == "reserved"
        finally:
            _exit(ctxs)

    def test_commit_all_transitions_status(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={"summary": True},
                client_request_id="req1",
                operation_id="op1",
            )
            cr.commit(session_id="s1", reservation_id="req1")
            data = cr._get_existing("s1", "req1")
            assert data["status"] == "committed"
            assert data["items"]["summary"]["status"] == "committed"
        finally:
            _exit(ctxs)

    def test_commit_missing_reservation_raises(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            with pytest.raises(cr.ReservationError):
                cr.commit(session_id="s1", reservation_id="nope")
        finally:
            _exit(ctxs)


class TestRelease:
    def test_release_refunds_pending_features(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={"summary": True, "quiz": True},
                client_request_id="req1",
                operation_id="op1",
            )
            cr.release(session_id="s1", reservation_id="req1", reason="test")
            assert fake_credits.refund.call_count == 2
            data = cr._get_existing("s1", "req1")
            assert data["status"] == "released"
            assert data["items"]["summary"]["status"] == "released"
            assert data["items"]["quiz"]["status"] == "released"
        finally:
            _exit(ctxs)

    def test_release_skips_committed(self, fake_db, fake_credits):
        from app.services import credits_reservations as cr
        ctxs = _patch_all(fake_db, fake_credits)
        _enter(ctxs)
        try:
            cr.reserve(
                session_id="s1",
                account_id="a1",
                uid="u1",
                plan={"summary": True, "quiz": True},
                client_request_id="req1",
                operation_id="op1",
            )
            cr.commit(session_id="s1", reservation_id="req1", feature="summary")
            fake_credits.refund.reset_mock()
            cr.release(session_id="s1", reservation_id="req1")
            # only quiz should be refunded
            fake_credits.refund.assert_called_once()
            refund_args = fake_credits.refund.call_args
            assert refund_args.args[2] == "quiz_generated"
        finally:
            _exit(ctxs)
