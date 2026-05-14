"""Pydantic schemas for the business-license / bulk-license system (Phase 1).

Spec: ``deepnote-contracts/api/license-endpoints.md`` and ``data/license-model.md``.
This module defines only the wire shapes for routes; persistence is
``app/services/license_service.py``.

License key format: ``DNLS-XXXX-XXXX-XXXX`` (16 chars + 3 dashes).
Plain keys are never stored in Firestore — only ``keyHash`` and ``keyLast4``.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ─── Enumerations ──────────────────────────────────────────────────────


class LicenseStatus(str, Enum):
    """Lifecycle of a single license key.

    ``unused``     - generated but not yet handed to a partner
    ``issued``     - handed to partner / reseller / corp customer
    ``activated``  - end user has redeemed it in the app
    ``cancelled``  - cancelled by partner / customer (still counts for the
                     month it was active in for monthly billing report)
    ``expired``    - past expiry date (rare; Phase 1 doesn't auto-expire)
    ``disabled``   - admin force-disable (fraud / mis-issuance)
    """

    UNUSED = "unused"
    ISSUED = "issued"
    ACTIVATED = "activated"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    DISABLED = "disabled"


class LicensePlan(str, Enum):
    """Initial Phase-1 plan tier. Adding new tiers is non-breaking."""

    BUSINESS = "business"


# ─── User-facing endpoints ─────────────────────────────────────────────


class RedeemLicenseRequest(BaseModel):
    """``POST /v1/licenses:redeem`` request body."""

    licenseKey: str = Field(
        ...,
        min_length=8,
        max_length=64,
        description="The plain license key entered by the end user. "
        "Whitespace and case are normalised server-side.",
    )
    deviceId: Optional[str] = Field(
        None,
        max_length=128,
        description="Optional device fingerprint for audit. Not required.",
    )


class RedeemLicenseResponse(BaseModel):
    """``POST /v1/licenses:redeem`` 200 response."""

    status: Literal["active"] = "active"
    plan: LicensePlan
    licenseId: str
    partnerId: Optional[str] = None
    resellerId: Optional[str] = None
    organizationName: Optional[str] = None
    activatedAt: datetime
    applicationDate: date
    billingStartDate: date
    freeMonths: int


class MeLicenseResponse(BaseModel):
    """``GET /v1/me/license`` response.

    When the caller has no license, ``status='inactive'`` is returned with
    all the other fields ``None``. This avoids 404 noise on every poll.
    """

    status: Literal["active", "inactive", "cancelled", "expired", "disabled"]
    plan: Optional[LicensePlan] = None
    licenseId: Optional[str] = None
    partnerId: Optional[str] = None
    resellerId: Optional[str] = None
    organizationName: Optional[str] = None
    activatedAt: Optional[datetime] = None
    applicationDate: Optional[date] = None
    billingStartDate: Optional[date] = None
    cancelledAt: Optional[datetime] = None
    freeMonths: Optional[int] = None
    # Last 4 chars of the redeemed key, shown in UI for user reassurance.
    keyLast4: Optional[str] = None


# ─── Admin endpoints ───────────────────────────────────────────────────


class CreateBatchRequest(BaseModel):
    """``POST /v1/admin/license-batches`` request body."""

    partnerId: str = Field(..., min_length=1, max_length=64)
    resellerId: Optional[str] = Field(None, max_length=64)
    plan: LicensePlan = LicensePlan.BUSINESS
    count: int = Field(..., ge=1, le=10000)
    freeMonths: int = Field(2, ge=0, le=12)
    memo: Optional[str] = Field(None, max_length=500)


class BatchSummary(BaseModel):
    batchId: str
    partnerId: str
    resellerId: Optional[str] = None
    plan: LicensePlan
    totalCount: int
    issuedCount: int = 0
    activatedCount: int = 0
    cancelledCount: int = 0
    freeMonths: int
    status: str
    createdAt: datetime
    exportedAt: Optional[datetime] = None
    memo: Optional[str] = None


class CreateBatchResponse(BaseModel):
    batch: BatchSummary
    # The plain keys are returned ONCE here (same content as the CSV export
    # below). They are not retrievable after this response — only the CSV
    # export at batch-create time has them.
    keys: list[str]


class LicenseSummary(BaseModel):
    licenseId: str
    keyLast4: str
    keyPrefix: str
    batchId: str
    partnerId: str
    resellerId: Optional[str] = None
    customerId: Optional[str] = None
    customerName: Optional[str] = None
    plan: LicensePlan
    status: LicenseStatus
    issuedAt: Optional[datetime] = None
    activatedAt: Optional[datetime] = None
    cancelledAt: Optional[datetime] = None
    applicationDate: Optional[date] = None
    billingStartDate: Optional[date] = None
    userId: Optional[str] = None
    userEmail: Optional[str] = None
    freeMonths: int
    createdAt: datetime
    updatedAt: datetime


class LicenseListResponse(BaseModel):
    items: list[LicenseSummary]
    nextCursor: Optional[str] = None


class CancelLicenseRequest(BaseModel):
    cancelledDate: Optional[date] = Field(
        None,
        description="Effective cancel date in JST. Defaults to today if omitted.",
    )
    reason: Optional[str] = Field(None, max_length=500)


class GenerateReportRequest(BaseModel):
    partnerId: str = Field(..., min_length=1, max_length=64)
    targetMonth: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}$",
        description="YYYY-MM in JST. Licenses whose applicationDate "
        "falls within this month or are still active during it are included.",
    )


class ReportSummary(BaseModel):
    reportId: str
    partnerId: str
    targetMonth: str
    status: str
    generatedAt: datetime
    submittedAt: Optional[datetime] = None
    rowCount: int
    createdBy: Optional[str] = None
