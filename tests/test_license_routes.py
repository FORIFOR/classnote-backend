"""Route-level smoke tests for the user-facing license endpoints.

These do not touch Firestore — `redeem_license` / `get_my_license` are
monkeypatched. The point is to verify:
  - HTTP status mapping for each service-layer exception
  - response-body shape on success
  - response shape when the user has no license (status='inactive')

Service-layer correctness (key normalisation, hashing, billing-start
date) is covered by the dedicated unit tests in
`tests/unit/test_license_*.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import CurrentUser, get_current_user
from app.main import app
from app.routes import licenses as licenses_route
from app.services import license_service


@pytest.fixture
def _auth():
    fake = CurrentUser(
        uid="test-uid",
        account_id="test-acct",
        provider="google.com",
        phone_number=None,
        email="t@t.com",
        display_name="t",
        photo_url=None,
        has_custom_claims=False,
    )
    app.dependency_overrides[get_current_user] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_current_user, None)


# ── POST /v1/licenses:redeem ────────────────────────────────────────


@pytest.mark.anyio
async def test_redeem_success_returns_business_plan(_auth, monkeypatch):
    """Happy path: a valid key elevates the user to `business`."""

    def _fake_redeem(**kwargs):
        return license_service.RedeemResult(
            license_id="lic_abc",
            plan="business",
            partner_id="life_select",
            reseller_id="next_standards",
            organization_name="ACME Co.",
            activated_at=datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc),
            application_date=date(2026, 4, 6),
            billing_start_date=date(2026, 6, 1),
            free_months=2,
        )

    monkeypatch.setattr(licenses_route, "redeem_license", _fake_redeem)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        res = await c.post(
            "/v1/licenses:redeem",
            json={"licenseKey": "DNLS-AAAA-BBBB-CCCC"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "active"
    assert body["plan"] == "business"
    assert body["licenseId"] == "lic_abc"
    assert body["partnerId"] == "life_select"
    assert body["billingStartDate"] == "2026-06-01"
    assert body["freeMonths"] == 2


@pytest.mark.parametrize(
    "exception,expected_status,expected_code",
    [
        # `error_code` is the class attribute carried into the response,
        # not the message arg — terminal states all share
        # `license_unavailable` for 410.
        (license_service.InvalidLicenseKey(), 400, "invalid_license_key"),
        (license_service.LicenseNotFound(), 404, "license_not_found"),
        (license_service.LicenseAlreadyUsed(), 409, "license_already_used"),
        (license_service.LicenseUnavailable(), 410, "license_unavailable"),
    ],
)
@pytest.mark.anyio
async def test_redeem_error_status_mapping(
    _auth, monkeypatch, exception, expected_status, expected_code
):
    """Each service-layer exception maps to the documented HTTP code.

    The UI is told to surface a single generic message for the 400/404/
    409/410 cluster, but the response body must still carry the precise
    `error` slug so support / logs can distinguish them.
    """

    def _raises(**kwargs):
        raise exception

    monkeypatch.setattr(licenses_route, "redeem_license", _raises)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        res = await c.post(
            "/v1/licenses:redeem",
            json={"licenseKey": "DNLS-AAAA-BBBB-CCCC"},
        )

    assert res.status_code == expected_status
    assert res.json()["detail"]["error"] == expected_code


# ── GET /v1/me/license ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_me_license_active(_auth, monkeypatch):
    def _fake_get_my_license(**kwargs):
        return license_service.MeLicense(
            status="active",
            plan="business",
            license_id="lic_abc",
            partner_id="life_select",
            reseller_id="next_standards",
            organization_name="ACME Co.",
            activated_at=datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc),
            application_date=date(2026, 4, 6),
            billing_start_date=date(2026, 6, 1),
            cancelled_at=None,
            free_months=2,
            key_last4="Q8AZ",
        )

    monkeypatch.setattr(licenses_route, "get_my_license", _fake_get_my_license)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        res = await c.get("/v1/me/license")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "active"
    assert body["plan"] == "business"
    assert body["licenseId"] == "lic_abc"
    assert body["keyLast4"] == "Q8AZ"


@pytest.mark.anyio
async def test_me_license_inactive_when_unredeemed(_auth, monkeypatch):
    """No license redeemed yet → status='inactive', all other fields
    null. Spec calls out that this endpoint never 404s so the client
    can poll it without conditional branching."""

    def _fake_get_my_license(**kwargs):
        return license_service.MeLicense(
            status="inactive",
            plan=None,
            license_id=None,
            partner_id=None,
            reseller_id=None,
            organization_name=None,
            activated_at=None,
            application_date=None,
            billing_start_date=None,
            cancelled_at=None,
            free_months=None,
            key_last4=None,
        )

    monkeypatch.setattr(licenses_route, "get_my_license", _fake_get_my_license)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        res = await c.get("/v1/me/license")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "inactive"
    assert body["plan"] is None
    assert body["licenseId"] is None
