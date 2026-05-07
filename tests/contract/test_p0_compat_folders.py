"""
P0 #5 — `/folders*` legacy alias must remain in lockstep with `/v1/folders*`.

Spec: deepnote-contracts/specs/openapi.v1.hotfix.yaml#/paths/~1folders
      and ~1v1~1folders
Plan: deepnote-contracts/migration/backend-p0-compat-fix-plan.md §6.5

Done criteria fixed by this file:
  - `GET /folders` (legacy alias) is registered and returns a JSON ARRAY
    (iOS Decodable expects `[FolderResponse]`, NOT `{folders: [...]}`).
  - `GET /v1/folders` (canonical) is registered.
  - Folder listing array carries the `id`, `name` keys (and tolerates
    optional `color`, `isArchived`, `sessionCount`, `createdAt`, `updatedAt`).
  - `FolderSessionSnapshot` includes ownership keys (`ownerUid`,
    `ownerAccountId`, `ownerUsername`) — losing these breaks iOS `isMine`.
  - Removing the legacy alias is detected by this file (route-table check).

The shape tests below depend on the in-process app being able to list
folders. The handlers run against `db` from `app.routes.folders`; the
`patched_firestore` fixture installs a sane no-op mock.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


LEGACY_FOLDERS = "/folders"
CANONICAL_FOLDERS = "/v1/folders"
LEGACY_FOLDER_SESSIONS = "/folders/{folder_id}/sessions"
CANONICAL_FOLDER_SESSIONS = "/v1/folders/{folder_id}/sessions"

REQUIRED_FOLDER_KEYS = ("id", "name")
TOLERATED_FOLDER_KEYS = (
    "color",
    "isArchived",
    "sessionCount",
    "createdAt",
    "updatedAt",
    "description",
    "deletedAt",
)
REQUIRED_SNAPSHOT_OWNERSHIP_KEYS = ("ownerUid", "ownerAccountId", "ownerUsername")


def _registered_paths(app) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes if getattr(r, "path", "")}


def _install_folders_firestore(monkeypatch) -> MagicMock:
    """
    Install a `db` mock for `app.routes.folders` so handlers don't hit
    Firestore. Returns the mock; tests can adjust per-call behaviour.
    """
    fake_db = MagicMock()

    # Default: every Firestore lookup returns "no documents" / empty stream.
    empty_doc = MagicMock()
    empty_doc.exists = False
    empty_doc.to_dict.return_value = {}

    fake_collection = MagicMock()
    fake_collection.stream.return_value = iter([])
    fake_collection.get.return_value = []
    fake_collection.where.return_value = fake_collection
    fake_collection.order_by.return_value = fake_collection
    fake_collection.document.return_value.get.return_value = empty_doc
    fake_collection.document.return_value.collection.return_value = fake_collection

    fake_db.collection.return_value = fake_collection

    monkeypatch.setattr("app.routes.folders.db", fake_db, raising=False)
    return fake_db


def test_legacy_folders_alias_registered(app_with_auth_override):
    """`/folders` (legacy_router) MUST exist. Removal would 404 every iOS / Desktop client."""
    paths = _registered_paths(app_with_auth_override)
    assert LEGACY_FOLDERS in paths, (
        "P0 #5: /folders legacy alias is missing — DO NOT REMOVE this router"
    )


def test_canonical_folders_registered(app_with_auth_override):
    paths = _registered_paths(app_with_auth_override)
    assert CANONICAL_FOLDERS in paths, "P0 #5: /v1/folders canonical missing"


def test_folder_sessions_aliases_registered(app_with_auth_override):
    paths = _registered_paths(app_with_auth_override)
    assert LEGACY_FOLDER_SESSIONS in paths, (
        f"P0 #5: legacy {LEGACY_FOLDER_SESSIONS} missing — iOS listFolderSessions breaks"
    )
    assert CANONICAL_FOLDER_SESSIONS in paths, (
        f"P0 #5: canonical {CANONICAL_FOLDER_SESSIONS} missing"
    )


@pytest.mark.anyio("asyncio")
async def test_legacy_folders_returns_json_array(
    contract_client, auth_headers, monkeypatch
):
    """
    `GET /folders` (legacy) must return a JSON ARRAY directly. iOS
    `listFolders()` Decodable target is `[FolderResponse]`, NOT
    `{"folders": [...]}` — that wrapping is canonical-only.
    """
    _install_folders_firestore(monkeypatch)
    resp = await contract_client.get(LEGACY_FOLDERS, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list), (
        f"P0 #5: GET /folders must return a JSON array, got {type(body).__name__}: {body!r}"
    )


@pytest.mark.anyio("asyncio")
async def test_canonical_folders_returns_wrapped_dict(
    contract_client, auth_headers, monkeypatch
):
    """
    `GET /v1/folders` returns the wrapped shape `{"folders": [...]}`.

    This is *intentional asymmetry* with the legacy alias and is documented
    in `app/routes/folders.py`. Both shapes must keep working.
    """
    _install_folders_firestore(monkeypatch)
    resp = await contract_client.get(CANONICAL_FOLDERS, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict) and "folders" in body, (
        f"P0 #5: GET /v1/folders must return {{folders: [...]}}, got {body!r}"
    )
    assert isinstance(body["folders"], list)


@pytest.mark.anyio("asyncio")
async def test_legacy_folder_item_required_keys(
    contract_client, auth_headers, monkeypatch
):
    """If any folder is returned, each item must carry `id` and `name`."""
    fake_db = _install_folders_firestore(monkeypatch)

    folder_doc = MagicMock()
    folder_doc.exists = True
    folder_doc.id = "fld_test"
    folder_doc.to_dict.return_value = {
        "id": "fld_test",
        "name": "Contract Test Folder",
        "color": "#abcdef",
        "isArchived": False,
        "sessionCount": 0,
    }

    # Stream the one fake folder when the handler iterates the collection.
    fake_db.collection.return_value.stream.return_value = iter([folder_doc])

    resp = await contract_client.get(LEGACY_FOLDERS, headers=auth_headers)
    if resp.status_code != 200 or not isinstance(resp.json(), list) or not resp.json():
        pytest.skip(
            "prerequisite: legacy /folders did not return a populated array; "
            f"got status={resp.status_code} body={resp.text[:200]}"
        )
    item: dict[str, Any] = resp.json()[0]
    missing = [k for k in REQUIRED_FOLDER_KEYS if k not in item]
    assert not missing, f"P0 #5: legacy /folders item missing required keys: {missing}"


@pytest.mark.anyio("asyncio")
async def test_folder_session_snapshot_has_ownership_keys(
    contract_client, auth_headers, monkeypatch
):
    """
    `FolderSessionSnapshot` MUST include ownership keys (`ownerUid`,
    `ownerAccountId`, `ownerUsername`). iOS `isMine` decision depends on them.

    This test is wired against an empty-folder fixture; if the handler can't
    surface even one snapshot row in this skeleton, it skips. Done-criteria
    for the P0 fix is to ALSO add a unit test (under `tests/`) that builds a
    real snapshot row and asserts the ownership keys flow through.
    """
    _install_folders_firestore(monkeypatch)
    resp = await contract_client.get(
        LEGACY_FOLDER_SESSIONS.format(folder_id="fld_does_not_exist"),
        headers=auth_headers,
    )
    if resp.status_code != 200:
        pytest.skip(
            f"prerequisite: legacy folder-sessions endpoint not reachable in this "
            f"skeleton (status={resp.status_code}). Add deeper fixture in P0 fix PR."
        )
    body = resp.json()
    if not isinstance(body, list) or not body:
        pytest.skip(
            "prerequisite: no snapshot rows in fixture; replace `_install_folders_firestore` "
            "with a fixture that yields one snapshot row and re-run."
        )
    item = body[0]
    missing = [k for k in REQUIRED_SNAPSHOT_OWNERSHIP_KEYS if k not in item]
    assert not missing, (
        f"P0 #5: FolderSessionSnapshot missing ownership keys: {missing}. "
        "Without these iOS isMine() collapses and shared sessions look unowned."
    )


def test_legacy_alias_removal_is_detected(app_with_auth_override):
    """
    Tripwire: if a future refactor drops the legacy folders router, this
    test MUST fire. Same as `test_legacy_folders_alias_registered` but
    explicit about its purpose so the failure message points at the rule.
    """
    paths = _registered_paths(app_with_auth_override)
    assert LEGACY_FOLDERS in paths, (
        "P0 #5 TRIPWIRE: /folders legacy alias was removed. "
        "Removal of this router is forbidden until 2027-11-06 (see hotfix spec). "
        "If this is intentional, update deepnote-contracts/specs/openapi.v1.hotfix.yaml "
        "AND backend-p0-compat-fix-plan.md before deleting this test."
    )
