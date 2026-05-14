"""Business-license service — redeem, cancel, lookup, billing-date math.

Firestore layout (see ``deepnote-contracts/data/license-model.md``):
  ``license_batches/{batchId}``  — generated lots
  ``licenses/{licenseId}``       — individual keys (keyHash unique)
  ``license_reports/{reportId}`` — monthly partner reports (PR2)

Plain license keys are never stored. ``keyHash`` (sha256 of normalised key)
is the lookup index. The redeem path performs a Firestore transactional
update so two clients racing on the same key cannot both win.

Plan effect:
- On successful redeem we set ``accounts/{accountId}.plan = "business"``
  (preserving the existing ``previousPlan``), set ``users/{uid}.plan``
  for the redeeming user, and write ``accounts.entitlements.businessLicense``
  as the structured entitlement record. On cancel we restore previousPlan.
- Existing paid-plan gates that test ``plan in ("basic", "standard")``
  now also accept ``"business"`` — see the matching grep edits in
  ``app/routes/sessions.py`` and friends.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional

from google.cloud import firestore as gcf

from app.firebase import db
from app.services.license_key_service import (
    hash_license_key,
    last4_of_key,
    normalise_license_key,
)

logger = logging.getLogger("app.services.license_service")

JST = timezone(timedelta(hours=9))


# ─── Errors (mapped to HTTP in routes/licenses.py) ─────────────────────


class LicenseError(Exception):
    """Base for license operation failures with an HTTP-friendly code."""

    http_status: int = 400
    error_code: str = "license_error"

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.error_code)
        self.message = message or self.error_code
        self.details = details


class InvalidLicenseKey(LicenseError):
    http_status = 400
    error_code = "invalid_license_key"


class LicenseNotFound(LicenseError):
    http_status = 404
    error_code = "license_not_found"


class LicenseAlreadyUsed(LicenseError):
    http_status = 409
    error_code = "license_already_used"


class LicenseUnavailable(LicenseError):
    """Cancelled / disabled / expired — terminal states."""

    http_status = 410
    error_code = "license_unavailable"


# ─── Billing date math ────────────────────────────────────────────────


def calculate_billing_start_date(application_date: date, free_months: int = 2) -> date:
    """Day-1 of the first PAID month, given a JST application date.

    Examples (free_months=2):
        2026-04-06 → 2026-06-01   (April and May free, June chargeable)
        2026-12-15 → 2027-02-01   (Dec and Jan free, Feb chargeable)

    Semantics: ``free_months`` counts the application month and the
    following ``free_months - 1`` whole months as free. The first
    chargeable month starts on the 1st of (application_month + free_months).
    This matches the contract spec example "2026-04-06 申込 →
    2026-06-01 有料開始".
    """
    if free_months < 0:
        raise ValueError("free_months must be >= 0")
    year = application_date.year
    month = application_date.month + free_months
    while month > 12:
        year += 1
        month -= 12
    return date(year, month, 1)


def _today_jst() -> date:
    return datetime.now(JST).date()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─── Dataclass shipped back to route handlers ──────────────────────────


@dataclass
class RedeemResult:
    license_id: str
    plan: str
    partner_id: Optional[str]
    reseller_id: Optional[str]
    organization_name: Optional[str]
    activated_at: datetime
    application_date: date
    billing_start_date: date
    free_months: int
    key_last4: str


@dataclass
class MeLicense:
    status: str
    license_id: Optional[str] = None
    plan: Optional[str] = None
    partner_id: Optional[str] = None
    reseller_id: Optional[str] = None
    organization_name: Optional[str] = None
    activated_at: Optional[datetime] = None
    application_date: Optional[date] = None
    billing_start_date: Optional[date] = None
    cancelled_at: Optional[datetime] = None
    free_months: Optional[int] = None
    key_last4: Optional[str] = None


# ─── Redeem ────────────────────────────────────────────────────────────


def _find_license_ref_by_hash(key_hash: str):
    """Return ``(doc_ref, snapshot)`` for the unique license matching the
    hash. None if no match. ``keyHash`` has a single-field index.
    """
    q = db.collection("licenses").where("keyHash", "==", key_hash).limit(1).stream()
    for snap in q:
        return db.collection("licenses").document(snap.id), snap
    return None, None


def redeem_license(
    *,
    user_id: str,
    account_id: str,
    user_email: Optional[str],
    raw_key: str,
    device_id: Optional[str] = None,
) -> RedeemResult:
    """Redeem a license for ``user_id``.

    Caller is responsible for ensuring ``user_id`` is the authenticated
    Firebase UID. ``account_id`` is the canonical account container that
    will receive the entitlement and the elevated plan.

    Raises one of the ``LicenseError`` subclasses on failure; the routes
    layer maps these to HTTP statuses.
    """
    norm = normalise_license_key(raw_key)
    if not norm or not norm.startswith("DNLS") or len(norm) < 12:
        raise InvalidLicenseKey("license key format invalid")

    key_hash = hash_license_key(raw_key)
    doc_ref, _ = _find_license_ref_by_hash(key_hash)
    if doc_ref is None:
        raise LicenseNotFound()

    account_ref = db.collection("accounts").document(account_id)
    user_ref = db.collection("users").document(user_id)

    @gcf.transactional
    def _txn(txn: gcf.Transaction) -> dict[str, Any]:
        # IMPORTANT: Firestore transactions require ALL reads before ANY
        # writes. Read both license + account up front; do all writes
        # afterwards. Violating this raises ReadAfterWriteError at commit.
        snap = doc_ref.get(transaction=txn)
        if not snap.exists:
            raise LicenseNotFound()
        acc_snap = account_ref.get(transaction=txn)

        data = snap.to_dict() or {}
        status = data.get("status", "unused")
        existing_user = data.get("userId")

        if status in ("cancelled", "disabled", "expired"):
            raise LicenseUnavailable(f"license status={status}")

        if status == "activated":
            if existing_user and existing_user != user_id:
                raise LicenseAlreadyUsed()
            # Same user re-redeem — idempotent success.
        else:
            # unused / issued → now activating.
            pass

        now_utc = _now_utc()
        today_jst = _today_jst()

        free_months = int(data.get("freeMonths", 2))
        application_date = data.get("applicationDate") or today_jst
        if isinstance(application_date, datetime):
            application_date = application_date.date()
        billing_start = data.get("billingStartDate") or calculate_billing_start_date(
            application_date, free_months=free_months
        )
        if isinstance(billing_start, datetime):
            billing_start = billing_start.date()

        license_updates: dict[str, Any] = {
            "status": "activated",
            "userId": user_id,
            "userEmail": user_email,
            "activatedAt": data.get("activatedAt") or now_utc,
            "applicationDate": application_date.isoformat(),
            "billingStartDate": billing_start.isoformat(),
            "updatedAt": now_utc,
        }
        if device_id:
            license_updates["lastActivationDeviceId"] = device_id
        # Mark issuedAt as "now" if the partner-side issue step was skipped
        # for direct-to-user lots — gives the monthly report a value.
        if not data.get("issuedAt"):
            license_updates["issuedAt"] = now_utc

        plan = data.get("plan") or "business"

        # ── all writes from here on (no further reads) ─────────────────
        txn.update(doc_ref, license_updates)

        # Elevate account.plan and stash the previous one, but only the
        # first time we promote to business (so re-redeem doesn't lose
        # the original previousPlan).
        acc_data = acc_snap.to_dict() or {}
        acc_update: dict[str, Any] = {
            "entitlements.businessLicense": {
                "active": True,
                "licenseId": snap.id,
                "plan": plan,
                "partnerId": data.get("partnerId"),
                "resellerId": data.get("resellerId"),
                "activatedAt": now_utc,
                "applicationDate": application_date.isoformat(),
                "billingStartDate": billing_start.isoformat(),
                "freeMonths": free_months,
                "keyLast4": data.get("keyLast4"),
            },
            "updatedAt": now_utc,
            "planUpdatedAt": now_utc,
        }
        if acc_data.get("plan") != plan:
            acc_update["previousPlan"] = acc_data.get("plan")
            acc_update["plan"] = plan

        txn.set(account_ref, acc_update, merge=True)

        # Mirror plan on the user doc so legacy reads see the new tier.
        txn.set(
            user_ref,
            {
                "plan": plan,
                "licenseId": snap.id,
                "licenseStatus": "active",
                "planUpdatedAt": now_utc,
                "updatedAt": now_utc,
            },
            merge=True,
        )

        return {
            "license_id": snap.id,
            "plan": plan,
            "partner_id": data.get("partnerId"),
            "reseller_id": data.get("resellerId"),
            "organization_name": data.get("customerName") or data.get("organizationName"),
            "activated_at": license_updates["activatedAt"],
            "application_date": application_date,
            "billing_start_date": billing_start,
            "free_months": free_months,
            "key_last4": data.get("keyLast4") or last4_of_key(raw_key),
        }

    result = _txn(db.transaction())
    logger.info(
        "[License] redeem ok: user=%s account=%s license=%s plan=%s partner=%s",
        user_id, account_id, result["license_id"], result["plan"], result.get("partner_id"),
    )
    return RedeemResult(**result)


# ─── /v1/me/license ────────────────────────────────────────────────────


def get_my_license(*, user_id: str, account_id: str) -> MeLicense:
    """Resolve the current end-user-facing license state.

    Resolution order: account entitlement > user.licenseId > none.
    """
    acc_snap = db.collection("accounts").document(account_id).get()
    acc = acc_snap.to_dict() or {}
    ent = (acc.get("entitlements") or {}).get("businessLicense") or {}
    license_id = ent.get("licenseId")

    if not license_id:
        # Fallback: user-doc may still record it (older path).
        user_snap = db.collection("users").document(user_id).get()
        user_data = user_snap.to_dict() or {}
        license_id = user_data.get("licenseId")

    if not license_id:
        return MeLicense(status="inactive")

    lic_snap = db.collection("licenses").document(license_id).get()
    if not lic_snap.exists:
        logger.warning("[License] /me/license dangling licenseId=%s for user=%s", license_id, user_id)
        return MeLicense(status="inactive")

    lic = lic_snap.to_dict() or {}
    status = lic.get("status", "unused")

    # Render status to the narrower public enum.
    public_status: str
    if status == "activated":
        public_status = "active"
    elif status == "cancelled":
        public_status = "cancelled"
    elif status == "disabled":
        public_status = "disabled"
    elif status == "expired":
        public_status = "expired"
    else:
        public_status = "inactive"

    return MeLicense(
        status=public_status,
        license_id=license_id,
        plan=lic.get("plan", "business"),
        partner_id=lic.get("partnerId"),
        reseller_id=lic.get("resellerId"),
        organization_name=lic.get("customerName") or lic.get("organizationName"),
        activated_at=lic.get("activatedAt"),
        application_date=_to_date(lic.get("applicationDate")),
        billing_start_date=_to_date(lic.get("billingStartDate")),
        cancelled_at=lic.get("cancelledAt"),
        free_months=int(lic.get("freeMonths", 2)),
        key_last4=lic.get("keyLast4"),
    )


def _to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


# ─── Admin ops ─────────────────────────────────────────────────────────


def create_batch(
    *,
    partner_id: str,
    reseller_id: Optional[str],
    plan: str,
    count: int,
    free_months: int,
    memo: Optional[str],
    created_by: str,
) -> tuple[str, list[str], dict[str, Any]]:
    """Create a batch and ``count`` licenses. Returns
    ``(batch_id, plain_keys, batch_doc_dict)``. Plain keys are visible
    only here — never re-readable from Firestore.
    """
    from app.services.license_key_service import generate_license_keys, last4_of_key

    if count < 1:
        raise ValueError("count must be >= 1")

    plain_keys = generate_license_keys(count)
    batch_id = f"batch_{datetime.now(JST).strftime('%Y%m%d')}_{partner_id}_{uuid.uuid4().hex[:8]}"

    now = _now_utc()
    batch_doc = {
        "batchId": batch_id,
        "partnerId": partner_id,
        "resellerId": reseller_id,
        "plan": plan,
        "totalCount": count,
        "issuedCount": 0,
        "activatedCount": 0,
        "cancelledCount": 0,
        "freeMonths": free_months,
        "status": "created",
        "createdAt": now,
        "createdBy": created_by,
        "exportedAt": None,
        "memo": memo,
    }
    db.collection("license_batches").document(batch_id).set(batch_doc)

    # Bulk-write licenses in chunks of 400 (Firestore batched-writes cap = 500).
    for i in range(0, len(plain_keys), 400):
        chunk = plain_keys[i : i + 400]
        wb = db.batch()
        for key in chunk:
            from app.services.license_key_service import hash_license_key as _hash
            key_hash = _hash(key)
            doc_id = f"lic_{uuid.uuid4().hex[:20]}"
            wb.set(
                db.collection("licenses").document(doc_id),
                {
                    "licenseId": doc_id,
                    "keyHash": key_hash,
                    "keyPrefix": key.split("-", 1)[0],
                    "keyLast4": last4_of_key(key),
                    "batchId": batch_id,
                    "partnerId": partner_id,
                    "resellerId": reseller_id,
                    "customerId": None,
                    "customerName": None,
                    "plan": plan,
                    "status": "unused",
                    "issuedAt": None,
                    "activatedAt": None,
                    "cancelledAt": None,
                    "disabledAt": None,
                    "userId": None,
                    "userEmail": None,
                    "applicationDate": None,
                    "billingStartDate": None,
                    "freeMonths": free_months,
                    "createdAt": now,
                    "updatedAt": now,
                },
            )
        wb.commit()

    logger.info(
        "[License] batch created: id=%s partner=%s count=%d plan=%s by=%s",
        batch_id, partner_id, count, plan, created_by,
    )
    return batch_id, plain_keys, batch_doc


def cancel_license(
    *,
    license_id: str,
    cancelled_date: Optional[date],
    reason: Optional[str],
    cancelled_by: str,
) -> dict[str, Any]:
    """Admin cancel. Sets license.status=cancelled and clears the
    user's entitlement (and restores previousPlan) if a user holds it.
    """
    lic_ref = db.collection("licenses").document(license_id)

    @gcf.transactional
    def _txn(txn: gcf.Transaction) -> dict[str, Any]:
        # IMPORTANT: Firestore transactions require ALL reads before ANY
        # writes. Read license + (if held) user + account up front; do
        # all writes afterwards.
        snap = lic_ref.get(transaction=txn)
        if not snap.exists:
            raise LicenseNotFound()
        data = snap.to_dict() or {}
        if data.get("status") in ("cancelled", "disabled", "expired"):
            return data

        user_id = data.get("userId")
        user_ref = None
        user_data: dict[str, Any] = {}
        acc_ref = None
        acc_data: dict[str, Any] = {}
        if user_id:
            user_ref = db.collection("users").document(user_id)
            user_snap = user_ref.get(transaction=txn)
            user_data = user_snap.to_dict() or {}
            account_id = user_data.get("accountId")
            if account_id:
                acc_ref = db.collection("accounts").document(account_id)
                acc_snap = acc_ref.get(transaction=txn)
                acc_data = acc_snap.to_dict() or {}

        now_utc = _now_utc()
        eff_date = (cancelled_date or _today_jst()).isoformat()

        # ── all writes from here on (no further reads) ─────────────────
        txn.update(
            lic_ref,
            {
                "status": "cancelled",
                "cancelledAt": now_utc,
                "cancelledDate": eff_date,
                "cancelledReason": reason,
                "cancelledBy": cancelled_by,
                "updatedAt": now_utc,
            },
        )

        if user_ref is not None:
            if acc_ref is not None:
                prev = acc_data.get("previousPlan") or "free"
                txn.set(
                    acc_ref,
                    {
                        "entitlements.businessLicense": {
                            "active": False,
                            "licenseId": snap.id,
                            "cancelledAt": now_utc,
                        },
                        "plan": prev,
                        "previousPlan": acc_data.get("plan"),
                        "planUpdatedAt": now_utc,
                        "updatedAt": now_utc,
                    },
                    merge=True,
                )
            txn.set(
                user_ref,
                {
                    "licenseStatus": "cancelled",
                    "plan": (
                        user_data.get("previousPlan") or "free"
                        if (user_data.get("plan") == "business")
                        else user_data.get("plan")
                    ),
                    "planUpdatedAt": now_utc,
                    "updatedAt": now_utc,
                },
                merge=True,
            )

        return {**data, "status": "cancelled", "cancelledAt": now_utc, "cancelledDate": eff_date}

    result = _txn(db.transaction())
    logger.info("[License] cancel: id=%s by=%s date=%s", license_id, cancelled_by, cancelled_date)
    return result
