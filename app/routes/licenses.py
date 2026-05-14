"""End-user license endpoints.

Two routes, both Firebase-authenticated:
  ``POST /v1/licenses:redeem``  — redeem a key, elevate to Business plan
  ``GET  /v1/me/license``       — read the current license state

Admin endpoints (batch generation, cancel, monthly report) live in
``app/routes/admin_licenses.py``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import CurrentUser, get_current_user
from app.schemas.license import (
    LicensePlan,
    MeLicenseResponse,
    RedeemLicenseRequest,
    RedeemLicenseResponse,
)
from app.services.license_service import (
    InvalidLicenseKey,
    LicenseAlreadyUsed,
    LicenseError,
    LicenseNotFound,
    LicenseUnavailable,
    get_my_license,
    redeem_license,
)

logger = logging.getLogger("app.routes.licenses")
router = APIRouter(prefix="/v1", tags=["Business License"])


@router.post("/licenses:redeem", response_model=RedeemLicenseResponse)
async def redeem(
    body: RedeemLicenseRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> RedeemLicenseResponse:
    """Redeem a business license key for the authenticated user.

    Idempotency: re-submitting the same valid key by the same user returns
    200 with the same data (does not toggle dates). A different user
    redeeming an already-activated key gets ``409 license_already_used``;
    purposely terse so the response cannot be used as an oracle for
    brute-forcing keys.
    """
    try:
        result = redeem_license(
            user_id=current_user.uid,
            account_id=current_user.account_id,
            user_email=getattr(current_user, "email", None),
            raw_key=body.licenseKey,
            device_id=body.deviceId,
        )
    except InvalidLicenseKey as exc:
        raise HTTPException(status_code=400, detail={"error": exc.error_code})
    except LicenseNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": exc.error_code})
    except LicenseAlreadyUsed as exc:
        raise HTTPException(status_code=409, detail={"error": exc.error_code})
    except LicenseUnavailable as exc:
        raise HTTPException(status_code=410, detail={"error": exc.error_code})
    except LicenseError as exc:  # pragma: no cover — defensive
        logger.warning("[License] redeem error: %s", exc)
        raise HTTPException(status_code=exc.http_status, detail={"error": exc.error_code})

    return RedeemLicenseResponse(
        plan=LicensePlan(result.plan),
        licenseId=result.license_id,
        partnerId=result.partner_id,
        resellerId=result.reseller_id,
        organizationName=result.organization_name,
        activatedAt=result.activated_at,
        applicationDate=result.application_date,
        billingStartDate=result.billing_start_date,
        freeMonths=result.free_months,
    )


@router.get("/me/license", response_model=MeLicenseResponse)
async def me_license(
    current_user: CurrentUser = Depends(get_current_user),
) -> MeLicenseResponse:
    """Return the calling user's current license state.

    Returns ``status='inactive'`` (200) when the user has not redeemed a
    license. Never 404s — that simplifies client polling and avoids
    leaking redeem state for unrelated users.
    """
    me = get_my_license(user_id=current_user.uid, account_id=current_user.account_id)
    return MeLicenseResponse(
        status=me.status,  # type: ignore[arg-type]
        plan=LicensePlan(me.plan) if me.plan else None,
        licenseId=me.license_id,
        partnerId=me.partner_id,
        resellerId=me.reseller_id,
        organizationName=me.organization_name,
        activatedAt=me.activated_at,
        applicationDate=me.application_date,
        billingStartDate=me.billing_start_date,
        cancelledAt=me.cancelled_at,
        freeMonths=me.free_months,
        keyLast4=me.key_last4,
    )
