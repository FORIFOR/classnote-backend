"""Admin endpoints for the business license system.

All routes gated by ``get_admin_user`` (Firebase custom claim ``admin``
OR uid in ``ADMIN_UIDS`` env OR ``users/{uid}.isAdmin == true``).

Routes:
  POST   /v1/admin/license-batches                       create a batch
  GET    /v1/admin/license-batches                       list batches
  GET    /v1/admin/license-batches/{batch_id}/export.csv  one-shot CSV of plain keys (DB does not store them — only available when batch was just created via this same admin endpoint and returned in the response. The CSV export here re-emits the *masked* form from the DB.)
  GET    /v1/admin/licenses                              list licenses
  POST   /v1/admin/licenses/{license_id}:cancel          cancel one license
  POST   /v1/admin/license-reports:generate              build a monthly report
  GET    /v1/admin/license-reports/{report_id}/export.csv  download report CSV

Plain license keys cannot be re-emitted from the DB — they only exist
in the response of ``POST /license-batches`` (and the CSV that the CLI
``tools/licenses/generate_batch.py`` writes locally). The
``/export.csv`` endpoint on a batch therefore returns the *masked* keys
plus metadata, suitable for re-export / audit but not re-distribution.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.dependencies import AdminUser, get_admin_user
from app.firebase import db
from app.schemas.license import (
    BatchSummary,
    CancelLicenseRequest,
    CreateBatchRequest,
    CreateBatchResponse,
    GenerateReportRequest,
    LicenseListResponse,
    LicensePlan,
    LicenseStatus,
    LicenseSummary,
    ReportSummary,
)
from app.services.license_report_service import (
    collect_report_rows,
    generate_report,
    get_report,
    rows_to_csv,
)
from app.services.license_service import (
    LicenseError,
    LicenseNotFound,
    cancel_license,
    create_batch,
)

logger = logging.getLogger("app.routes.admin_licenses")
router = APIRouter(prefix="/v1/admin", tags=["Business License (Admin)"])


# ── Batches ────────────────────────────────────────────────────────────


@router.post("/license-batches", response_model=CreateBatchResponse)
async def admin_create_batch(
    body: CreateBatchRequest,
    admin: AdminUser = Depends(get_admin_user),
) -> CreateBatchResponse:
    """Create a batch of license keys.

    The plain keys are returned in the response **once**. The server does
    not store them; subsequent ``GET`` endpoints only return masked keys.
    """
    batch_id, plain_keys, batch_doc = create_batch(
        partner_id=body.partnerId,
        reseller_id=body.resellerId,
        plan=body.plan.value,
        count=body.count,
        free_months=body.freeMonths,
        memo=body.memo,
        created_by=admin.uid,
    )
    return CreateBatchResponse(
        batch=_batch_doc_to_summary(batch_doc),
        keys=plain_keys,
    )


@router.get("/license-batches", response_model=list[BatchSummary])
async def admin_list_batches(
    partner_id: Optional[str] = Query(None, alias="partnerId"),
    limit: int = Query(50, ge=1, le=500),
    admin: AdminUser = Depends(get_admin_user),
) -> list[BatchSummary]:
    q = db.collection("license_batches")
    if partner_id:
        q = q.where("partnerId", "==", partner_id)
    docs = q.limit(limit).stream()
    out: list[BatchSummary] = []
    for snap in docs:
        out.append(_batch_doc_to_summary(snap.to_dict() or {}))
    out.sort(key=lambda b: b.createdAt, reverse=True)
    return out


@router.get("/license-batches/{batch_id}/export.csv")
async def admin_export_batch_csv(
    batch_id: str,
    admin: AdminUser = Depends(get_admin_user),
) -> StreamingResponse:
    """Re-export a batch's masked keys + metadata as CSV.

    Plain keys are not stored, so this CSV is for audit / handover-record
    use, NOT for end-user distribution. Use the CSV returned by the
    ``POST /license-batches`` response (or ``tools/licenses/generate_batch.py``)
    for partner delivery.
    """
    batch_snap = db.collection("license_batches").document(batch_id).get()
    if not batch_snap.exists:
        raise HTTPException(status_code=404, detail={"error": "batch_not_found"})

    licenses = (
        db.collection("licenses").where("batchId", "==", batch_id).stream()
    )
    rows: list[dict[str, str]] = []
    for snap in licenses:
        lic = snap.to_dict() or {}
        prefix = lic.get("keyPrefix") or "DNLS"
        last4 = lic.get("keyLast4") or "????"
        rows.append(
            {
                "license_id": snap.id,
                "masked_key": f"{prefix}-****-****-{last4}",
                "partner_id": lic.get("partnerId") or "",
                "reseller_id": lic.get("resellerId") or "",
                "plan": lic.get("plan") or "",
                "status": lic.get("status") or "",
                "free_months": str(lic.get("freeMonths", "")),
                "issued_at": _iso(lic.get("issuedAt")),
                "activated_at": _iso(lic.get("activatedAt")),
                "cancelled_at": _iso(lic.get("cancelledAt")),
                "user_id": lic.get("userId") or "",
                "customer_id": lic.get("customerId") or "",
            }
        )
    rows.sort(key=lambda r: r["license_id"])

    cols = list(rows[0].keys()) if rows else [
        "license_id", "masked_key", "partner_id", "reseller_id",
        "plan", "status", "free_months", "issued_at", "activated_at",
        "cancelled_at", "user_id", "customer_id",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    csv_text = buf.getvalue()

    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{batch_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


# ── Individual licenses ────────────────────────────────────────────────


@router.get("/licenses", response_model=LicenseListResponse)
async def admin_list_licenses(
    partner_id: Optional[str] = Query(None, alias="partnerId"),
    batch_id: Optional[str] = Query(None, alias="batchId"),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=1000),
    admin: AdminUser = Depends(get_admin_user),
) -> LicenseListResponse:
    q = db.collection("licenses")
    if partner_id:
        q = q.where("partnerId", "==", partner_id)
    if batch_id:
        q = q.where("batchId", "==", batch_id)
    if status_filter:
        q = q.where("status", "==", status_filter)
    docs = q.limit(limit).stream()
    items: list[LicenseSummary] = []
    for snap in docs:
        items.append(_license_doc_to_summary(snap.to_dict() or {}))
    items.sort(key=lambda x: x.createdAt, reverse=True)
    return LicenseListResponse(items=items)


@router.post("/licenses/{license_id}:cancel", response_model=LicenseSummary)
async def admin_cancel_license(
    license_id: str,
    body: CancelLicenseRequest,
    admin: AdminUser = Depends(get_admin_user),
) -> LicenseSummary:
    try:
        result = cancel_license(
            license_id=license_id,
            cancelled_date=body.cancelledDate,
            reason=body.reason,
            cancelled_by=admin.uid,
        )
    except LicenseNotFound:
        raise HTTPException(status_code=404, detail={"error": "license_not_found"})
    except LicenseError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"error": exc.error_code})
    return _license_doc_to_summary(result)


# ── Monthly reports ────────────────────────────────────────────────────


@router.post("/license-reports:generate", response_model=ReportSummary)
async def admin_generate_report(
    body: GenerateReportRequest,
    admin: AdminUser = Depends(get_admin_user),
) -> ReportSummary:
    report_id, rows = generate_report(
        partner_id=body.partnerId,
        target_month=body.targetMonth,
        created_by=admin.uid,
    )
    doc = get_report(report_id) or {}
    return _report_doc_to_summary(doc)


@router.get("/license-reports/{report_id}/export.csv")
async def admin_export_report_csv(
    report_id: str,
    admin: AdminUser = Depends(get_admin_user),
) -> StreamingResponse:
    doc = get_report(report_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={"error": "report_not_found"})
    # Re-collect rows so the CSV is always fresh against the current
    # Firestore state — Phase 2 will optionally pin to a stored snapshot.
    rows = collect_report_rows(
        partner_id=doc["partnerId"], target_month=doc["targetMonth"]
    )
    csv_text = rows_to_csv(rows)
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{report_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _iso(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    return str(v)


def _batch_doc_to_summary(d: dict) -> BatchSummary:
    return BatchSummary(
        batchId=d.get("batchId", ""),
        partnerId=d.get("partnerId", ""),
        resellerId=d.get("resellerId"),
        plan=LicensePlan(d.get("plan", "business")),
        totalCount=int(d.get("totalCount", 0)),
        issuedCount=int(d.get("issuedCount", 0)),
        activatedCount=int(d.get("activatedCount", 0)),
        cancelledCount=int(d.get("cancelledCount", 0)),
        freeMonths=int(d.get("freeMonths", 2)),
        status=d.get("status", "created"),
        createdAt=d.get("createdAt"),
        exportedAt=d.get("exportedAt"),
        memo=d.get("memo"),
    )


def _license_doc_to_summary(d: dict) -> LicenseSummary:
    return LicenseSummary(
        licenseId=d.get("licenseId", ""),
        keyLast4=d.get("keyLast4", ""),
        keyPrefix=d.get("keyPrefix", "DNLS"),
        batchId=d.get("batchId", ""),
        partnerId=d.get("partnerId", ""),
        resellerId=d.get("resellerId"),
        customerId=d.get("customerId"),
        customerName=d.get("customerName"),
        plan=LicensePlan(d.get("plan", "business")),
        status=LicenseStatus(d.get("status", "unused")),
        issuedAt=d.get("issuedAt"),
        activatedAt=d.get("activatedAt"),
        cancelledAt=d.get("cancelledAt"),
        applicationDate=_date_or_none(d.get("applicationDate")),
        billingStartDate=_date_or_none(d.get("billingStartDate")),
        userId=d.get("userId"),
        userEmail=d.get("userEmail"),
        freeMonths=int(d.get("freeMonths", 2)),
        createdAt=d.get("createdAt"),
        updatedAt=d.get("updatedAt"),
    )


def _report_doc_to_summary(d: dict) -> ReportSummary:
    return ReportSummary(
        reportId=d.get("reportId", ""),
        partnerId=d.get("partnerId", ""),
        targetMonth=d.get("targetMonth", ""),
        status=d.get("status", "draft"),
        generatedAt=d.get("generatedAt"),
        submittedAt=d.get("submittedAt"),
        rowCount=int(d.get("rowCount", 0)),
        createdBy=d.get("createdBy"),
    )


def _date_or_none(v):
    from datetime import date, datetime
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None
