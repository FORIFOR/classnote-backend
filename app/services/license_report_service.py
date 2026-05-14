"""Monthly license report (CSV) generation.

Spec: ``deepnote-contracts/flows/monthly-license-report-flow.md``.

A row is included for a license when, **for the target JST month**:
- ``applicationDate`` falls within the month, OR
- the license was active at any point during the month (i.e. activated
  on or before month end, and not cancelled before month start).

The report is identifying information about who-was-billable-when, so it
intentionally includes both still-active and ``cancelled`` rows whose
billing month was the target one.

Plain license keys are NOT stored, so the CSV's ``license_key`` column
emits ``"<keyPrefix>-****-****-<keyLast4>"`` by default. The full plain
key is only present in the batch-creation CSV (one-shot, at issue time).
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional

from app.firebase import db

logger = logging.getLogger("app.services.license_report_service")

JST = timezone(timedelta(hours=9))

REPORT_COLUMNS = [
    "license_key",
    "license_id",
    "partner_id",
    "reseller_id",
    "customer_id",
    "status",
    "application_date",
    "activated_date",
    "billing_start_date",
    "cancelled_date",
    "free_months",
    "billing_month",
]


def _month_bounds(target_month: str) -> tuple[date, date]:
    """Return ``(month_start, month_end_exclusive)`` for ``YYYY-MM``."""
    y, m = target_month.split("-")
    yi, mi = int(y), int(m)
    start = date(yi, mi, 1)
    end = date(yi + 1, 1, 1) if mi == 12 else date(yi, mi + 1, 1)
    return start, end


def _as_date(v: Any) -> Optional[date]:
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


def collect_report_rows(*, partner_id: str, target_month: str) -> list[dict[str, Any]]:
    """Stream licenses for the partner and filter to the target month.

    Phase 1 implementation: query by partnerId, filter in Python. With
    realistic license counts (< 100k per partner) this is fine; we can
    move to a date-range index in Phase 2 when partners grow.
    """
    start, end = _month_bounds(target_month)
    rows: list[dict[str, Any]] = []

    q = db.collection("licenses").where("partnerId", "==", partner_id).stream()
    for snap in q:
        lic = snap.to_dict() or {}
        app_d = _as_date(lic.get("applicationDate"))
        cancel_d = _as_date(lic.get("cancelledDate")) or _as_date(lic.get("cancelledAt"))
        activated_d = _as_date(lic.get("activatedAt"))
        billing_d = _as_date(lic.get("billingStartDate"))

        # Active in target month?
        ever_activated_before_month_end = (
            (activated_d is not None and activated_d < end)
            or (app_d is not None and app_d < end)
        )
        not_cancelled_before_month = cancel_d is None or cancel_d >= start
        in_window = ever_activated_before_month_end and not_cancelled_before_month

        # Or: applicationDate within the window (covers issued-but-not-yet-
        # activated lots that should still be reported as billable per spec).
        applied_in_window = app_d is not None and start <= app_d < end

        if not (in_window or applied_in_window):
            continue

        prefix = lic.get("keyPrefix") or "DNLS"
        last4 = lic.get("keyLast4") or "????"
        masked_key = f"{prefix}-****-****-{last4}"

        rows.append(
            {
                "license_key": masked_key,
                "license_id": snap.id,
                "partner_id": lic.get("partnerId") or "",
                "reseller_id": lic.get("resellerId") or "",
                "customer_id": lic.get("customerId") or "",
                "status": lic.get("status") or "",
                "application_date": app_d.isoformat() if app_d else "",
                "activated_date": activated_d.isoformat() if activated_d else "",
                "billing_start_date": billing_d.isoformat() if billing_d else "",
                "cancelled_date": cancel_d.isoformat() if cancel_d else "",
                "free_months": str(lic.get("freeMonths", 2)),
                "billing_month": target_month,
            }
        )
    rows.sort(key=lambda r: (r["application_date"], r["license_id"]))
    return rows


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """Render rows to a CSV string with stable column order."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def generate_report(
    *,
    partner_id: str,
    target_month: str,
    created_by: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Build rows for the partner-month and persist a ``license_reports``
    doc. Returns ``(report_id, rows)``. The caller may either return rows
    inline as CSV or stash the CSV in GCS (Phase 2).
    """
    rows = collect_report_rows(partner_id=partner_id, target_month=target_month)
    report_id = f"report_{partner_id}_{target_month.replace('-', '_')}_{uuid.uuid4().hex[:6]}"
    doc = {
        "reportId": report_id,
        "partnerId": partner_id,
        "targetMonth": target_month,
        "status": "draft",
        "generatedAt": datetime.now(timezone.utc),
        "submittedAt": None,
        "rowCount": len(rows),
        "createdBy": created_by,
    }
    db.collection("license_reports").document(report_id).set(doc)
    logger.info(
        "[License] report generated: id=%s partner=%s month=%s rows=%d by=%s",
        report_id, partner_id, target_month, len(rows), created_by,
    )
    return report_id, rows


def get_report(report_id: str) -> Optional[dict[str, Any]]:
    snap = db.collection("license_reports").document(report_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()
