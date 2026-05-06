"""
P0 compatibility contract tests — shared fixtures.

These fixtures intentionally do NOT touch production code. They:
  - reuse the global `tests/conftest.py` Google Cloud module mocks (auto-loaded
    because pytest discovers parent `conftest.py` automatically)
  - install a FastAPI `dependency_overrides` shim so contract tests can hit
    auth-protected paths without a real Firebase ID token
  - expose a `contract_client` fixture (httpx AsyncClient) for the OpenAPI
    `specs/openapi.v1.hotfix.yaml` shape assertions
  - expose a `loaded_hotfix_spec` fixture that lazily reads the hotfix OpenAPI
    so individual tests can compare required keys against the spec without
    duplicating literal lists

Run from repo root:
    pytest tests/contract/ -v

The skeleton tests are EXPECTED TO FAIL today. Their job is to lock the P0
fix done-criteria; backend implementation lands in a follow-up PR.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

# Importing app.main is deferred to inside fixtures so the parent
# `tests/conftest.py` mock_pkg() bootstrap runs first.


CONTRACTS_REPO_CANDIDATES = [
    Path.home() / "Projects" / "deepnote-contracts",
    Path(__file__).resolve().parents[3] / "deepnote-contracts",
]

HOTFIX_SPEC_RELPATH = Path("specs") / "openapi.v1.hotfix.yaml"


def _find_hotfix_spec_path() -> Path | None:
    for root in CONTRACTS_REPO_CANDIDATES:
        candidate = root / HOTFIX_SPEC_RELPATH
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture(scope="session")
def hotfix_spec_path() -> Path | None:
    return _find_hotfix_spec_path()


@pytest.fixture(scope="session")
def loaded_hotfix_spec(hotfix_spec_path: Path | None) -> dict[str, Any] | None:
    """Return parsed OpenAPI hotfix spec, or None if `deepnote-contracts/` is not
    available locally. Tests that need it should `pytest.skip(...)` when None."""
    if hotfix_spec_path is None:
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    return yaml.safe_load(hotfix_spec_path.read_text())


@pytest.fixture
def app_with_auth_override() -> Iterator[Any]:
    """
    Boot the FastAPI app with `get_current_user` overridden to a fake
    `CurrentUser` so contract tests focus on response shape, not Firebase Auth.

    Yields the FastAPI `app` instance with overrides installed; tears them
    down on exit.
    """
    from app.main import app
    from app.dependencies import CurrentUser, get_current_user

    fake_user = CurrentUser(
        uid="contract-test-uid",
        account_id="contract-test-account",
        provider="google.com",
        phone_number=None,
        email="contract-test@example.com",
        display_name="Contract Test User",
        photo_url=None,
        has_custom_claims=False,
    )

    def _fake_get_current_user() -> CurrentUser:
        return fake_user

    app.dependency_overrides[get_current_user] = _fake_get_current_user
    try:
        yield app
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def contract_client(app_with_auth_override) -> Any:
    """
    httpx AsyncClient pointed at the in-process FastAPI app, with auth bypass
    installed via `app_with_auth_override`. Use this in contract tests.
    """
    from httpx import AsyncClient, ASGITransport

    transport = ASGITransport(app=app_with_auth_override)
    async with AsyncClient(transport=transport, base_url="http://contract-test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Stand-in for Authorization header. The actual token is unused because
    `contract_client` overrides `get_current_user`, but some routes still
    require the header to be present syntactically."""
    return {"Authorization": "Bearer contract-test-token"}


@pytest.fixture
def patched_firestore(monkeypatch) -> MagicMock:
    """
    Returns a `db` mock that swallows Firestore reads/writes used by
    `compat_aliases.alias_users_bootstrap` and `share.create_share_link`.

    Tests can override return values per call; default behaviour is "document
    does not exist" so handlers fall through to default values.
    """
    fake_db = MagicMock()

    # Default snapshot: doc does not exist; to_dict() is a real empty dict so
    # Pydantic validators that read fields (e.g. DeleteRequestResponse) get
    # plain string fallbacks instead of nested MagicMocks.
    fake_snapshot = MagicMock()
    fake_snapshot.exists = False
    fake_snapshot.to_dict.return_value = {}

    fake_db.collection.return_value.document.return_value.get.return_value = fake_snapshot
    fake_db.collection.return_value.where.return_value.stream.return_value = iter([])

    # `users.get_deletion_status` reads users/{uid} → snap.exists check first.
    # Returning exists=False sends it down the "already done" branch which
    # produces a clean Pydantic-valid response, avoiding the MagicMock leak
    # into DeleteRequestResponse fields.
    monkeypatch.setattr("app.routes.users.db", fake_db, raising=False)
    monkeypatch.setattr("app.routes.compat_aliases.db", fake_db, raising=False)
    monkeypatch.setattr("app.routes.share.db", fake_db, raising=False)
    monkeypatch.setattr("app.routes.folders.db", fake_db, raising=False)
    return fake_db
