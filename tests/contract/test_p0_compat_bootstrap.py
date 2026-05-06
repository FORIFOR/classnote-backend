"""
P0 #1 — `POST /users/bootstrap` shape contract.

Spec: deepnote-contracts/specs/openapi.v1.hotfix.yaml#/paths/~1users~1bootstrap
Plan: deepnote-contracts/migration/backend-p0-compat-fix-plan.md §6.1

Done criteria fixed by this file:
  - response always has `plan` (string)
  - `plan == "free"` when `accounts/{id}.plan` is null
  - `featureGates` always present and contains the 6 required Booleans
  - canonical response keys (uid / accountId / providers / hasUsername / etc.)
    never disappear

These tests are EXPECTED TO RUN against the in-process app (auth bypass via
`tests/contract/conftest.py::contract_client`). Initial state may be partial
PASS — that is fine; the file's purpose is to lock the contract.
"""
from __future__ import annotations

import pytest

REQUIRED_FEATURE_GATES = (
    "cloudStt",
    "summarization",
    "quiz",
    "cloudSync",
    "export",
    "share",
)

REQUIRED_TOP_LEVEL_KEYS = (
    "uid",
    "accountId",
    "plan",
    "hasUsername",
    "providers",
    "needsPhoneVerification",
    "needsSnsLogin",
    "suspended",
    "canonicalized",
    "claimsRefreshRequired",
)


@pytest.mark.anyio("asyncio")
async def test_bootstrap_returns_plan_key(contract_client, auth_headers, patched_firestore):
    """`plan` MUST be present in the bootstrap response."""
    resp = await contract_client.post("/users/bootstrap", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "plan" in body, "P0 #1: bootstrap response missing required key `plan`"
    assert isinstance(body["plan"], str)


@pytest.mark.anyio("asyncio")
async def test_bootstrap_plan_defaults_to_free_when_account_plan_missing(
    contract_client, auth_headers, patched_firestore
):
    """When `accounts/{id}.plan` is null/missing the server MUST default to "free"."""
    resp = await contract_client.post("/users/bootstrap", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("plan") == "free", (
        "P0 #1: missing accounts/{id}.plan must collapse to plan=='free' "
        "(see backend-p0-compat-fix-plan.md §2 P0-1)"
    )


@pytest.mark.anyio("asyncio")
async def test_bootstrap_feature_gates_present(contract_client, auth_headers, patched_firestore):
    """`featureGates` block MUST exist."""
    resp = await contract_client.post("/users/bootstrap", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "featureGates" in body, "P0 #1: bootstrap response missing `featureGates`"
    assert isinstance(body["featureGates"], dict)


@pytest.mark.anyio("asyncio")
async def test_bootstrap_feature_gates_six_booleans(contract_client, auth_headers, patched_firestore):
    """All 6 Boolean feature gates required by iOS Decodable MUST be present."""
    resp = await contract_client.post("/users/bootstrap", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    gates = resp.json().get("featureGates") or {}
    missing = [k for k in REQUIRED_FEATURE_GATES if k not in gates]
    wrong_type = [k for k in REQUIRED_FEATURE_GATES if k in gates and not isinstance(gates[k], bool)]
    assert not missing, f"P0 #1: featureGates missing keys: {missing}"
    assert not wrong_type, f"P0 #1: featureGates wrong type for: {wrong_type}"


@pytest.mark.anyio("asyncio")
async def test_bootstrap_canonical_top_level_keys_present(
    contract_client, auth_headers, patched_firestore
):
    """All canonical (non-deprecated) top-level keys MUST be present.

    Lock against silent removal — even if a future refactor switches to a
    Pydantic model, these names are wire-contract for the iOS BootstrapResponse.
    """
    resp = await contract_client.post("/users/bootstrap", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in body]
    assert not missing, f"P0 #1: bootstrap response missing canonical keys: {missing}"


@pytest.mark.anyio("asyncio")
async def test_bootstrap_legacy_alias_keys_not_removed(
    contract_client, auth_headers, patched_firestore
):
    """
    Existing client-facing keys (e.g. `displayName`, `username`, `photoUrl`,
    `provider`, `cacheValidUntil`, `previousAccountId`) must remain in the
    response shape — even when null. The hotfix scope is **add-only**, never
    remove.
    """
    resp = await contract_client.post("/users/bootstrap", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    legacy_optional_keys = (
        "displayName",
        "username",
        "photoUrl",
        "provider",
        "previousAccountId",
        "cacheValidUntil",
    )
    missing = [k for k in legacy_optional_keys if k not in body]
    assert not missing, (
        f"P0 #1: optional/legacy keys disappeared from bootstrap response: {missing}. "
        "Add-only rule violated — see backend-p0-compat-fix-plan.md §4.2"
    )


@pytest.mark.anyio("asyncio")
async def test_bootstrap_spec_required_keys_match(loaded_hotfix_spec):
    """
    Sanity check: REQUIRED_TOP_LEVEL_KEYS in this file aligns with the
    BootstrapResponse `required` array in the hotfix OpenAPI. If the spec
    grows, this test fails so the test file is updated with it.
    """
    if loaded_hotfix_spec is None:
        pytest.skip("deepnote-contracts not reachable from this checkout")
    schema = (
        loaded_hotfix_spec.get("components", {})
        .get("schemas", {})
        .get("BootstrapResponse")
    )
    if not schema or "required" not in schema:
        pytest.skip(
            "openapi.v1.hotfix.yaml does not yet declare "
            "components.schemas.BootstrapResponse.required — drift check skipped"
        )
    required_in_spec = set(schema["required"])
    declared = set(REQUIRED_TOP_LEVEL_KEYS)
    assert declared == required_in_spec, (
        f"REQUIRED_TOP_LEVEL_KEYS drift vs hotfix spec: "
        f"only-in-test={declared - required_in_spec}, "
        f"only-in-spec={required_in_spec - declared}"
    )
