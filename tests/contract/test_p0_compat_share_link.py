"""
P0 #3 — `GET/POST /sessions/{sessionId}/share_link` shape contract.

Spec: deepnote-contracts/specs/openapi.v1.hotfix.yaml#/paths/
      ~1sessions~1{sessionId}~1share_link
Plan: deepnote-contracts/migration/backend-p0-compat-fix-plan.md §6.3

Done criteria fixed by this file:
  - response always has canonical key `url` (string, non-empty)
  - the following deprecated alias keys are emitted in parallel and point at
    the same canonical share URL:
        shareUrl, share_url, shareLink, share_link, link, publicUrl
  - identifier-style aliases (`token`, `shareToken`, `share_token`,
    `code`, `shareCode`, `share_code`) are tolerated but their semantics are
    NOT yet canonicalised — see TODO at the bottom of this module.
  - GET/POST same shape (api_route shares the handler).

Today the handler returns ONLY `{ "url": ... }`. Most alias-key tests below
are therefore expected to FAIL. They lock the P0 #3 done-criteria.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


SESSION_ID = "test-session-share-link"

URL_ALIAS_KEYS = (
    # canonical
    "url",
    # @deprecated camelCase / snake_case URL aliases (iOS 12-候補 decode)
    "shareUrl",
    "share_url",
    "shareLink",
    "share_link",
    "link",
    "publicUrl",
)

# Identifier / token-style alias keys. Plan §2 P0-3 marks these as TODO —
# semantics may differ from `url`. Tests below check presence-tolerance only.
IDENTIFIER_ALIAS_KEYS = (
    "token",
    "shareToken",
    "share_token",
    "code",
    "shareCode",
    "share_code",
)


def _install_share_link_firestore(monkeypatch) -> MagicMock:
    """Install a minimal `db` mock that lets `share.create_share_link` run.

    - `_resolve_session_local(session_id)` will see snapshot.exists == True
      with an ownership-friendly session doc.
    - `db.collection("shareLinks").where(...).stream()` returns no existing
      links so a new token is generated.
    """
    fake_db = MagicMock()

    session_snap = MagicMock()
    session_snap.id = SESSION_ID
    session_snap.exists = True
    session_snap.to_dict.return_value = {
        "ownerUid": "contract-test-uid",
        "ownerAccountId": "contract-test-account",
        "title": "Contract Test Session",
    }

    def _collection(name: str):
        col = MagicMock()
        if name == "shareLinks":
            col.where.return_value.stream.return_value = iter([])
            doc = MagicMock()
            col.document.return_value = doc
        else:
            col.document.return_value.get.return_value = session_snap
        return col

    fake_db.collection.side_effect = _collection

    monkeypatch.setattr("app.routes.share.db", fake_db, raising=False)
    return fake_db


@pytest.mark.anyio("asyncio")
async def test_share_link_post_returns_canonical_url(
    contract_client, auth_headers, monkeypatch
):
    _install_share_link_firestore(monkeypatch)
    resp = await contract_client.post(
        f"/sessions/{SESSION_ID}/share_link", headers=auth_headers
    )
    # 200 today (api_route handler). 404 means session resolver failed; print
    # body so failures are diagnosable.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "url" in body, "P0 #3: response missing canonical key `url`"
    assert isinstance(body["url"], str) and body["url"], (
        "P0 #3: `url` must be a non-empty string"
    )


@pytest.mark.anyio("asyncio")
async def test_share_link_post_emits_url_alias_keys(
    contract_client, auth_headers, monkeypatch
):
    """
    EXPECTED FAIL today.

    The hotfix requires these alias keys (URL-equivalent) to be emitted in
    parallel. iOS decodes whichever one it sees first in the 12-候補 list.
    """
    _install_share_link_firestore(monkeypatch)
    resp = await contract_client.post(
        f"/sessions/{SESSION_ID}/share_link", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    missing = [k for k in URL_ALIAS_KEYS if k not in body]
    assert not missing, (
        f"P0 #3: ShareLinkResponse missing URL-equivalent alias keys: {missing}. "
        "Spec requires parallel emit (shareUrl/share_url/shareLink/share_link/"
        "link/publicUrl) until 2026-11-06 sunset."
    )


@pytest.mark.anyio("asyncio")
async def test_share_link_url_aliases_share_same_value(
    contract_client, auth_headers, monkeypatch
):
    """All URL-equivalent alias keys MUST point at the same canonical URL."""
    _install_share_link_firestore(monkeypatch)
    resp = await contract_client.post(
        f"/sessions/{SESSION_ID}/share_link", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    canonical = body.get("url")
    mismatches = {
        k: body[k] for k in URL_ALIAS_KEYS
        if k in body and body[k] != canonical
    }
    assert not mismatches, (
        f"P0 #3: URL alias keys diverge from canonical url={canonical!r}: {mismatches}. "
        "All URL-equivalent aliases must share the same value."
    )


@pytest.mark.anyio("asyncio")
async def test_share_link_get_same_shape_as_post(
    contract_client, auth_headers, monkeypatch
):
    """GET and POST share the same handler (`api_route`) — shape parity."""
    _install_share_link_firestore(monkeypatch)
    post_resp = await contract_client.post(
        f"/sessions/{SESSION_ID}/share_link", headers=auth_headers
    )
    get_resp = await contract_client.get(
        f"/sessions/{SESSION_ID}/share_link", headers=auth_headers
    )
    assert post_resp.status_code == 200
    assert get_resp.status_code == 200
    # Key sets must match (values may diverge if a new token is minted).
    assert set(post_resp.json().keys()) == set(get_resp.json().keys()), (
        "P0 #3: GET and POST /share_link return divergent key sets"
    )


@pytest.mark.anyio("asyncio")
async def test_share_link_identifier_aliases_tolerated(
    contract_client, auth_headers, monkeypatch
):
    """
    Identifier/token aliases are tolerated but NOT asserted-required.

    Plan §10 TODO 3: it is not yet decided whether `token`/`shareToken`/
    `share_token`/`code`/`shareCode`/`share_code` carry the same semantics as
    `url` or are separate identifiers. This test only asserts that, IF a
    backend implementation chooses to emit any of them, the value must be a
    non-empty string. It MUST NOT regress the contract once decided.
    """
    _install_share_link_firestore(monkeypatch)
    resp = await contract_client.post(
        f"/sessions/{SESSION_ID}/share_link", headers=auth_headers
    )
    body = resp.json()
    bad = {
        k: body[k] for k in IDENTIFIER_ALIAS_KEYS
        if k in body and not (isinstance(body[k], str) and body[k])
    }
    assert not bad, (
        f"P0 #3: identifier alias keys present but invalid (must be non-empty "
        f"string when emitted): {bad}"
    )


@pytest.mark.anyio("asyncio")
async def test_share_link_does_not_remove_canonical_url(
    contract_client, auth_headers, monkeypatch
):
    """Add-only rule: `url` must never be removed even when aliases are added."""
    _install_share_link_firestore(monkeypatch)
    resp = await contract_client.post(
        f"/sessions/{SESSION_ID}/share_link", headers=auth_headers
    )
    body = resp.json()
    assert "url" in body, "P0 #3: canonical `url` removed from response — add-only rule violated"


# TODO(P1): Once §10 TODO 3 is resolved, replace `_identifier_aliases_tolerated`
# with a strict assertion. If `token`/`code` are confirmed equivalent to `url`,
# move them into URL_ALIAS_KEYS. If they are separate identifiers, add a
# dedicated `_share_link_identifier_aliases_share_same_value` test that pins
# them to a different canonical (e.g. share-code 6-digit). DO NOT delete this
# file's tolerance test until the decision is in.
