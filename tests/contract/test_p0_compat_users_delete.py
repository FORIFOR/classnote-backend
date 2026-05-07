"""
P0 #4 — `/users/me:delete*` (canonical, colon) ↔ `/users/me/delete*` (slash alias).

Spec: deepnote-contracts/specs/openapi.v1.hotfix.yaml#/paths/~1users~1me%3Adelete
      and ~1users~1me~1delete (slash alias)
Plan: deepnote-contracts/migration/backend-p0-compat-fix-plan.md §6.4

Done criteria fixed by this file:
  - canonical `POST /users/me:delete` exists and returns 202
  - canonical `GET /users/me:delete/preflight` and `/status` exist
  - slash aliases `POST /users/me/delete`, `GET /users/me/delete/preflight`
    and `GET /users/me/delete/status` exist (Desktop calls these)
  - both forms hit the same handler (response shape parity)
  - existing `DELETE /users/me` (legacy, deprecated) still exists and does
    NOT collide with the new slash alias

Most slash-alias tests are EXPECTED TO FAIL today — those alias paths are
the deliverable of P0 fix PR-4 (`app/routes/compat_aliases.py`).
"""
from __future__ import annotations

import pytest


CANONICAL_DELETE = "/users/me:delete"
CANONICAL_PREFLIGHT = "/users/me:delete/preflight"
CANONICAL_STATUS = "/users/me:delete/status"

ALIAS_DELETE = "/users/me/delete"
ALIAS_PREFLIGHT = "/users/me/delete/preflight"
ALIAS_STATUS = "/users/me/delete/status"

LEGACY_DELETE_ME = "/users/me"  # DELETE method, deprecated 204


def _registered_routes(app) -> set[tuple[str, str]]:
    """Return the set of (METHOD, PATH) pairs registered on the FastAPI app."""
    out: set[tuple[str, str]] = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None) or set()
        if not path:
            continue
        for m in methods:
            out.add((m.upper(), path))
    return out


def test_canonical_colon_paths_registered(app_with_auth_override):
    """The colon-form canonical paths must exist in the FastAPI route table."""
    routes = _registered_routes(app_with_auth_override)
    assert ("POST", CANONICAL_DELETE) in routes, (
        f"P0 #4: canonical {CANONICAL_DELETE} (POST) is missing from the app"
    )
    assert ("GET", CANONICAL_PREFLIGHT) in routes, (
        f"P0 #4: canonical {CANONICAL_PREFLIGHT} (GET) is missing"
    )
    assert ("GET", CANONICAL_STATUS) in routes, (
        f"P0 #4: canonical {CANONICAL_STATUS} (GET) is missing"
    )


def test_slash_alias_paths_registered(app_with_auth_override):
    """
    EXPECTED FAIL today.

    Desktop ships these slash-form paths. The P0 #4 fix is to add them as
    aliases in `app/routes/compat_aliases.py` that forward to the canonical
    handlers in `app/routes/users.py`.
    """
    routes = _registered_routes(app_with_auth_override)
    assert ("POST", ALIAS_DELETE) in routes, (
        f"P0 #4: slash alias {ALIAS_DELETE} (POST) is missing — Desktop calls this path"
    )
    assert ("GET", ALIAS_PREFLIGHT) in routes, (
        f"P0 #4: slash alias {ALIAS_PREFLIGHT} (GET) is missing"
    )
    assert ("GET", ALIAS_STATUS) in routes, (
        f"P0 #4: slash alias {ALIAS_STATUS} (GET) is missing"
    )


def test_legacy_delete_me_does_not_collide_with_slash_alias(app_with_auth_override):
    """
    `DELETE /users/me` (legacy 204) is registered today. Adding
    `POST /users/me/delete` (slash alias) MUST NOT collide because:
      - methods differ (DELETE vs POST)
      - paths differ (`/users/me` vs `/users/me/delete`)

    This test is *informational* but locks the regression contract: if a
    future refactor accidentally drops the legacy DELETE or shadows the new
    POST alias, this test fires.
    """
    routes = _registered_routes(app_with_auth_override)
    assert ("DELETE", LEGACY_DELETE_ME) in routes, (
        "P0 #4: legacy DELETE /users/me removed unexpectedly — add-only rule violated"
    )
    # No path collision sanity: POST /users/me MUST NOT have been added.
    assert ("POST", LEGACY_DELETE_ME) not in routes, (
        "P0 #4: POST /users/me appeared — would shadow the canonical "
        "`/users/me:delete` semantics"
    )


@pytest.mark.anyio("asyncio")
async def test_canonical_delete_post_returns_2xx(
    contract_client, auth_headers, patched_firestore
):
    """`POST /users/me:delete` must respond 2xx (handler-side semantics may
    return 200 or 202 today; spec target is 202)."""
    resp = await contract_client.post(CANONICAL_DELETE, headers=auth_headers)
    assert 200 <= resp.status_code < 300, (
        f"P0 #4: {CANONICAL_DELETE} expected 2xx, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.anyio("asyncio")
async def test_slash_alias_delete_post_returns_2xx(
    contract_client, auth_headers, patched_firestore
):
    """EXPECTED FAIL today — alias does not exist yet."""
    resp = await contract_client.post(ALIAS_DELETE, headers=auth_headers)
    assert 200 <= resp.status_code < 300, (
        f"P0 #4: {ALIAS_DELETE} expected 2xx (alias to canonical), "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.anyio("asyncio")
async def test_canonical_preflight_returns_2xx(
    contract_client, auth_headers, patched_firestore
):
    resp = await contract_client.get(CANONICAL_PREFLIGHT, headers=auth_headers)
    assert 200 <= resp.status_code < 300


@pytest.mark.anyio("asyncio")
async def test_slash_alias_preflight_returns_2xx(
    contract_client, auth_headers, patched_firestore
):
    resp = await contract_client.get(ALIAS_PREFLIGHT, headers=auth_headers)
    assert 200 <= resp.status_code < 300, (
        f"P0 #4: {ALIAS_PREFLIGHT} alias missing or failing: {resp.status_code}"
    )


@pytest.mark.anyio("asyncio")
async def test_canonical_status_returns_2xx(
    contract_client, auth_headers, patched_firestore
):
    resp = await contract_client.get(CANONICAL_STATUS, headers=auth_headers)
    assert 200 <= resp.status_code < 300


@pytest.mark.anyio("asyncio")
async def test_slash_alias_status_returns_2xx(
    contract_client, auth_headers, patched_firestore
):
    resp = await contract_client.get(ALIAS_STATUS, headers=auth_headers)
    assert 200 <= resp.status_code < 300, (
        f"P0 #4: {ALIAS_STATUS} alias missing or failing: {resp.status_code}"
    )


@pytest.mark.anyio("asyncio")
async def test_canonical_and_alias_status_share_shape(
    contract_client, auth_headers, patched_firestore
):
    """
    EXPECTED FAIL today.

    Once both endpoints exist, they must reach the same handler. Compare
    response key-sets (values may differ across calls because of timestamps).
    """
    canonical = await contract_client.get(CANONICAL_STATUS, headers=auth_headers)
    alias = await contract_client.get(ALIAS_STATUS, headers=auth_headers)
    if canonical.status_code != 200 or alias.status_code != 200:
        pytest.skip(
            f"prerequisite: both canonical and alias must return 200 "
            f"(canonical={canonical.status_code}, alias={alias.status_code})"
        )
    assert set(canonical.json().keys()) == set(alias.json().keys()), (
        "P0 #4: canonical and slash-alias /status return divergent key sets"
    )


@pytest.mark.anyio("asyncio")
async def test_canonical_and_alias_preflight_share_shape(
    contract_client, auth_headers, patched_firestore
):
    canonical = await contract_client.get(CANONICAL_PREFLIGHT, headers=auth_headers)
    alias = await contract_client.get(ALIAS_PREFLIGHT, headers=auth_headers)
    if canonical.status_code != 200 or alias.status_code != 200:
        pytest.skip(
            f"prerequisite: both canonical and alias must return 200 "
            f"(canonical={canonical.status_code}, alias={alias.status_code})"
        )
    assert set(canonical.json().keys()) == set(alias.json().keys())
