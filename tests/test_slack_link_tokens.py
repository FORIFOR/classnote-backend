"""Unit tests for app.services.slack_link_tokens (in-memory Firestore stub)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from app.services import slack_link_tokens as slt


# In-memory Firestore stand-in (mirrors test_line_link_tokens). ────────

class _Snapshot:
    def __init__(self, ref):
        self.id = ref._doc_id
        self.reference = ref
        self._data = ref._data
        self.exists = ref._exists
    def to_dict(self):
        return dict(self._data) if self._data is not None else {}


class _DocRef:
    def __init__(self, store, collection, doc_id):
        self._store = store; self._collection = collection; self._doc_id = doc_id
    @property
    def _key(self): return (self._collection, self._doc_id)
    @property
    def _data(self): return self._store._docs.get(self._key)
    @property
    def _exists(self): return self._key in self._store._docs
    def get(self, transaction=None): return _Snapshot(self)
    def set(self, data, merge=False):
        if merge and self._key in self._store._docs:
            self._store._docs[self._key].update(data)
        else:
            self._store._docs[self._key] = dict(data)
    def update(self, data):
        self._store._docs.setdefault(self._key, {}).update(data)
    def delete(self):
        self._store._docs.pop(self._key, None)


class _Query:
    def __init__(self, store, collection):
        self._store = store; self._collection = collection
        self._filters = []; self._limit = None
    def where(self, *args, **kwargs):
        if "filter" in kwargs:
            f = kwargs["filter"]
            self._filters.append((f.field_path, f.op_string, f.value))
        else:
            self._filters.append(args)
        return self
    def limit(self, n): self._limit = n; return self
    def stream(self):
        out = []
        for (col, doc_id), data in self._store._docs.items():
            if col != self._collection: continue
            ok = True
            for field, op, value in self._filters:
                v = data.get(field)
                if op == "==" and v != value: ok = False; break
                if op == ">=" and (v is None or v < value): ok = False; break
                if op == "<" and (v is None or not (v < value)): ok = False; break
            if ok:
                out.append(_Snapshot(_DocRef(self._store, col, doc_id)))
                if self._limit and len(out) >= self._limit: break
        return out


class _Collection:
    def __init__(self, store, name):
        self._store = store; self._name = name
    def document(self, doc_id): return _DocRef(self._store, self._name, doc_id)
    def where(self, *args, **kwargs):
        return _Query(self._store, self._name).where(*args, **kwargs)


class _Transaction:
    def update(self, ref, data): ref.update(data)


class _DB:
    def __init__(self): self._docs = {}
    def collection(self, name): return _Collection(self, name)
    def transaction(self): return _Transaction()


@pytest.fixture
def fake_db(monkeypatch):
    fdb = _DB()
    monkeypatch.setattr(slt, "db", fdb)

    class _StubFirestore:
        @staticmethod
        def transactional(fn): return fn
    monkeypatch.setattr(slt, "firestore", _StubFirestore)
    return fdb


# Tests ───────────────────────────────────────────────────────────────

def test_issue_and_resolve(fake_db):
    token = slt.issue(team_id="T1", slack_user_id="U1", slack_channel_id="C1")
    data = slt.resolve(token)
    assert data["teamId"] == "T1"
    assert data["slackUserId"] == "U1"
    assert data["slackChannelId"] == "C1"


def test_resolve_unknown_returns_404(fake_db):
    with pytest.raises(slt.TokenError) as e:
        slt.resolve("nope")
    assert e.value.status == 404


def test_resolve_expired_returns_410(fake_db):
    token = slt.issue(team_id="T", slack_user_id="U")
    fake_db._docs[(slt.LINK_TOKENS_COLLECTION, token)]["expiresAt"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    with pytest.raises(slt.TokenError) as e:
        slt.resolve(token)
    assert e.value.status == 410


def test_consume_persists_user_link(fake_db):
    token = slt.issue(team_id="T", slack_user_id="U")
    slt.consume(token, deepnote_uid="uid", account_id="acct")
    link = slt.get_link("T", "U")
    assert link is not None
    assert link["deepnoteUid"] == "uid"
    assert link["accountId"] == "acct"
    assert link["teamId"] == "T"


def test_consume_twice_409(fake_db):
    token = slt.issue(team_id="T", slack_user_id="U")
    slt.consume(token, deepnote_uid="uid", account_id="acct")
    with pytest.raises(slt.TokenError) as e:
        slt.consume(token, deepnote_uid="uid", account_id="acct")
    assert e.value.status == 409


def test_consume_expired_410(fake_db):
    token = slt.issue(team_id="T", slack_user_id="U")
    fake_db._docs[(slt.LINK_TOKENS_COLLECTION, token)]["expiresAt"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    with pytest.raises(slt.TokenError) as e:
        slt.consume(token, deepnote_uid="uid", account_id="acct")
    assert e.value.status == 410


def test_consume_requires_uid_and_account(fake_db):
    token = slt.issue(team_id="T", slack_user_id="U")
    with pytest.raises(slt.TokenError):
        slt.consume(token, deepnote_uid="", account_id="acct")
    with pytest.raises(slt.TokenError):
        slt.consume(token, deepnote_uid="uid", account_id="")


def test_get_link_unknown_returns_none(fake_db):
    assert slt.get_link("T", "neverlinked") is None
