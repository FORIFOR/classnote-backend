"""Unit tests for app.services.line_link_tokens.

Uses an in-memory replacement for `db` so we can exercise issue/resolve/
consume without Firestore.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from app.services import line_link_tokens as llt


# ──────────────────────────────────────────────────────────────────────
# In-memory Firestore stand-in
# ──────────────────────────────────────────────────────────────────────

class _Snapshot:
    def __init__(self, ref: "_DocRef"):
        self.id = ref._doc_id
        self.reference = ref
        self._data = ref._data
        self.exists = ref._exists

    def to_dict(self):
        return dict(self._data) if self._data is not None else {}


class _DocRef:
    def __init__(self, store, collection, doc_id):
        self._store = store
        self._collection = collection
        self._doc_id = doc_id

    @property
    def _key(self):
        return (self._collection, self._doc_id)

    @property
    def _data(self):
        return self._store._docs.get(self._key)

    @property
    def _exists(self):
        return self._key in self._store._docs

    def get(self, transaction=None):
        return _Snapshot(self)

    def set(self, data: Dict[str, Any], merge: bool = False):
        if merge and self._key in self._store._docs:
            self._store._docs[self._key].update(data)
        else:
            self._store._docs[self._key] = dict(data)

    def update(self, data: Dict[str, Any]):
        self._store._docs.setdefault(self._key, {}).update(data)

    def delete(self):
        self._store._docs.pop(self._key, None)


class _Query:
    def __init__(self, store, collection):
        self._store = store
        self._collection = collection
        self._filters = []
        self._limit = None

    def where(self, *args, **kwargs):
        # Accept either positional (field, op, value) or filter= kwarg.
        if "filter" in kwargs:
            f = kwargs["filter"]
            self._filters.append((f.field_path, f.op_string, f.value))
        else:
            field, op, value = args
            self._filters.append((field, op, value))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        out = []
        for (col, doc_id), data in self._store._docs.items():
            if col != self._collection:
                continue
            ok = True
            for field, op, value in self._filters:
                v = data.get(field)
                if op == "==" and v != value:
                    ok = False
                    break
                if op == ">=" and (v is None or v < value):
                    ok = False
                    break
                if op == "<" and (v is None or not (v < value)):
                    ok = False
                    break
            if ok:
                out.append(_Snapshot(_DocRef(self._store, col, doc_id)))
                if self._limit and len(out) >= self._limit:
                    break
        return out


class _Collection:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return _DocRef(self._store, self._name, doc_id)

    def where(self, *args, **kwargs):
        q = _Query(self._store, self._name)
        return q.where(*args, **kwargs)


class _Transaction:
    """Minimal stand-in: just runs the inner function once."""

    def update(self, ref, data):
        ref.update(data)


class _DB:
    def __init__(self):
        self._docs = {}

    def collection(self, name):
        return _Collection(self, name)

    def transaction(self):
        return _Transaction()


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_db(monkeypatch):
    fdb = _DB()
    monkeypatch.setattr(llt, "db", fdb)

    # Patch firestore.transactional → no-op decorator (return the same fn).
    import app.services.line_link_tokens as mod

    class _StubFirestore:
        @staticmethod
        def transactional(fn):
            return fn

    monkeypatch.setattr(mod, "firestore", _StubFirestore)
    return fdb


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

def test_issue_and_resolve_round_trip(fake_db):
    token = llt.issue(line_user_id="U1", line_source_type="user")
    assert isinstance(token, str) and len(token) >= 32
    data = llt.resolve(token)
    assert data["lineUserId"] == "U1"
    assert data["usedAt"] is None


def test_resolve_rejects_unknown_token(fake_db):
    with pytest.raises(llt.TokenError) as excinfo:
        llt.resolve("does-not-exist")
    assert excinfo.value.status == 404


def test_resolve_rejects_expired_token(fake_db):
    token = llt.issue(line_user_id="U2")
    # Push expiresAt into the past.
    fake_db._docs[(llt.LINK_TOKENS_COLLECTION, token)]["expiresAt"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    )
    with pytest.raises(llt.TokenError) as excinfo:
        llt.resolve(token)
    assert excinfo.value.status == 410


def test_consume_marks_token_used_and_records_link(fake_db):
    token = llt.issue(line_user_id="U3")
    result = llt.consume(token, deepnote_uid="uid-3", account_id="acct-3")
    assert result["lineUserId"] == "U3"
    rec = fake_db._docs[(llt.LINK_TOKENS_COLLECTION, token)]
    assert rec["usedAt"] is not None
    assert rec["linkedUid"] == "uid-3"
    link = fake_db._docs[(llt.USER_LINKS_COLLECTION, "U3")]
    assert link["deepnoteUid"] == "uid-3"
    assert link["accountId"] == "acct-3"


def test_consume_twice_raises_already_used(fake_db):
    token = llt.issue(line_user_id="U4")
    llt.consume(token, deepnote_uid="uid-4", account_id="acct-4")
    with pytest.raises(llt.TokenError) as excinfo:
        llt.consume(token, deepnote_uid="uid-4", account_id="acct-4")
    assert excinfo.value.status == 409


def test_consume_rejects_expired_token(fake_db):
    token = llt.issue(line_user_id="U5")
    fake_db._docs[(llt.LINK_TOKENS_COLLECTION, token)]["expiresAt"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    )
    with pytest.raises(llt.TokenError) as excinfo:
        llt.consume(token, deepnote_uid="uid-5", account_id="acct-5")
    assert excinfo.value.status == 410


def test_consume_requires_uid_and_account(fake_db):
    token = llt.issue(line_user_id="U6")
    with pytest.raises(llt.TokenError):
        llt.consume(token, deepnote_uid="", account_id="acct-6")
    with pytest.raises(llt.TokenError):
        llt.consume(token, deepnote_uid="uid-6", account_id="")


def test_get_link_returns_none_when_unknown(fake_db):
    assert llt.get_link("never-linked") is None


def test_get_link_returns_record_after_consume(fake_db):
    token = llt.issue(line_user_id="U7")
    llt.consume(token, deepnote_uid="uid-7", account_id="acct-7")
    link = llt.get_link("U7")
    assert link is not None
    assert link["deepnoteUid"] == "uid-7"
