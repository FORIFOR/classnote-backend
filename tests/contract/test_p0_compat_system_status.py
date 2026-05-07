"""
P0 #2 — `GET /system/status` and `GET /system/config` shape contract.

Spec: deepnote-contracts/specs/openapi.v1.hotfix.yaml#/paths/~1system~1status
      deepnote-contracts/specs/openapi.v1.hotfix.yaml#/paths/~1system~1config
Plan: deepnote-contracts/migration/backend-p0-compat-fix-plan.md §6.2

Done criteria fixed by this file:
  - `/system/status` always returns `mode` (string from the SystemStatus enum).
  - In the absence of an active drain/maintenance flag, `mode == "normal"`.
  - When the drain mode env-var is set the response surfaces `mode == "drain"`.
  - `/system/config` always returns `status` and `generatedAt`.
  - Both endpoints must remain reachable WITHOUT bearer auth (iOS calls them
    pre-login).

These tests run in-process. The current handlers live in
`app/routes/compat_aliases.py::alias_system_status` and `alias_system_config`.
NOTE: today `alias_system_config` does NOT emit `status`/`generatedAt`, so the
matching test below is expected to FAIL until the P0 fix lands.
"""
from __future__ import annotations

import pytest

VALID_SYSTEM_STATUS_MODES = {"normal", "notice", "degraded", "maintenance", "force_update"}
# Spec enum subset — both `notice` and `force_update` are server-only extensions
# carried by the existing handler; the OpenAPI hotfix spec lists
# {normal, degraded, drain, maintenance}. The intersection that MUST be
# representable is captured below.
SPEC_SYSTEM_STATUS_MODES = {"normal", "degraded", "drain", "maintenance"}

VALID_APP_CONFIG_STATUSES = {"ok", "degraded", "maintenance"}


@pytest.mark.anyio("asyncio")
async def test_system_status_returns_mode_key(contract_client):
    resp = await contract_client.get("/system/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "mode" in body, "P0 #2: /system/status response missing required key `mode`"
    assert isinstance(body["mode"], str)


@pytest.mark.anyio("asyncio")
async def test_system_status_mode_is_normal_by_default(contract_client, monkeypatch):
    """When no SYSTEM_STATUS_MODE env override is set, default mode == "normal"."""
    monkeypatch.delenv("SYSTEM_STATUS_MODE", raising=False)
    resp = await contract_client.get("/system/status")
    assert resp.status_code == 200
    assert resp.json().get("mode") == "normal", (
        "P0 #2: default `mode` must be 'normal' (see hotfix spec SystemStatus.mode)"
    )


@pytest.mark.anyio("asyncio")
async def test_system_status_drain_mode_surfaces(contract_client, monkeypatch):
    """If SYSTEM_STATUS_MODE=drain is set, the response must reflect it.

    The current handler whitelists {normal, notice, degraded, maintenance,
    force_update}; "drain" needs to be added so iOS can stop scheduling work
    when SRE drains the service. This test is expected to FAIL today.
    """
    monkeypatch.setenv("SYSTEM_STATUS_MODE", "drain")
    resp = await contract_client.get("/system/status")
    assert resp.status_code == 200
    assert resp.json().get("mode") == "drain", (
        "P0 #2: SYSTEM_STATUS_MODE=drain must surface as mode=='drain'. "
        "Update VALID_MODES in alias_system_status to include 'drain'."
    )


@pytest.mark.anyio("asyncio")
async def test_system_status_mode_falls_back_to_normal_for_unknown(
    contract_client, monkeypatch
):
    """Unknown mode strings collapse to 'normal' (existing behaviour)."""
    monkeypatch.setenv("SYSTEM_STATUS_MODE", "bogus-mode")
    resp = await contract_client.get("/system/status")
    assert resp.status_code == 200
    assert resp.json().get("mode") == "normal"


@pytest.mark.anyio("asyncio")
async def test_system_status_mode_in_known_enum(contract_client, monkeypatch):
    """`mode` is always a value in the union of server enum and spec enum."""
    monkeypatch.delenv("SYSTEM_STATUS_MODE", raising=False)
    resp = await contract_client.get("/system/status")
    body = resp.json()
    assert body["mode"] in (VALID_SYSTEM_STATUS_MODES | SPEC_SYSTEM_STATUS_MODES)


@pytest.mark.anyio("asyncio")
async def test_system_status_does_not_require_auth(contract_client):
    """`/system/status` must respond 200 WITHOUT Authorization header."""
    resp = await contract_client.get("/system/status")
    assert resp.status_code == 200, (
        "P0 #2: /system/status must be reachable without bearer auth. "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.anyio("asyncio")
async def test_system_config_has_status_and_generated_at(contract_client):
    """
    EXPECTED FAIL until P0 fix lands.

    The current `alias_system_config` returns `{platform, maintenance,
    minSupportedVersion, features}` — it does NOT emit `status` or
    `generatedAt`, both of which iOS AppConfig.swift requires for Decodable.
    """
    resp = await contract_client.get("/system/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "status" in body, "P0 #2: /system/config response missing `status`"
    assert "generatedAt" in body, "P0 #2: /system/config response missing `generatedAt`"
    assert body["status"] in VALID_APP_CONFIG_STATUSES, (
        f"P0 #2: /system/config `status` must be one of {VALID_APP_CONFIG_STATUSES}, "
        f"got {body['status']!r}"
    )
    assert isinstance(body["generatedAt"], str) and body["generatedAt"], (
        "P0 #2: /system/config `generatedAt` must be a non-empty ISO8601 string"
    )


@pytest.mark.anyio("asyncio")
async def test_system_config_does_not_require_auth(contract_client):
    resp = await contract_client.get("/system/config")
    assert resp.status_code == 200


@pytest.mark.anyio("asyncio")
async def test_system_config_legacy_keys_not_removed(contract_client):
    """Add-only rule: existing keys remain even after `status`/`generatedAt`
    land. Lock against accidental removal of `platform`, `maintenance`,
    `minSupportedVersion`, `features`."""
    resp = await contract_client.get("/system/config")
    body = resp.json()
    legacy_keys = ("platform", "maintenance", "minSupportedVersion", "features")
    missing = [k for k in legacy_keys if k not in body]
    assert not missing, (
        f"P0 #2: /system/config legacy keys disappeared: {missing}. "
        "Add-only rule violated."
    )
