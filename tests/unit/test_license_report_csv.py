"""Unit test for monthly report CSV rendering (no Firestore needed)."""

from __future__ import annotations

import csv
import io

from app.services.license_report_service import REPORT_COLUMNS, rows_to_csv


def test_rows_to_csv_emits_header_and_rows_in_order() -> None:
    rows = [
        {
            "license_key": "DNLS-****-****-Q8AZ",
            "license_id": "lic_a",
            "partner_id": "life_select",
            "reseller_id": "next_standards",
            "customer_id": "LS-CUST-001",
            "status": "activated",
            "application_date": "2026-04-06",
            "activated_date": "2026-04-07",
            "billing_start_date": "2026-06-01",
            "cancelled_date": "",
            "free_months": "2",
            "billing_month": "2026-04",
        },
        {
            "license_key": "DNLS-****-****-BBBB",
            "license_id": "lic_b",
            "partner_id": "life_select",
            "reseller_id": "next_standards",
            "customer_id": "",
            "status": "cancelled",
            "application_date": "2026-04-10",
            "activated_date": "2026-04-12",
            "billing_start_date": "2026-06-01",
            "cancelled_date": "2026-04-20",
            "free_months": "2",
            "billing_month": "2026-04",
        },
    ]
    text = rows_to_csv(rows)
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert header == REPORT_COLUMNS
    body = list(reader)
    assert len(body) == 2
    # Column 0 is license_key, masked form.
    assert body[0][0] == "DNLS-****-****-Q8AZ"
    assert body[1][0] == "DNLS-****-****-BBBB"
    # cancelled_date column position is stable.
    cancelled_idx = REPORT_COLUMNS.index("cancelled_date")
    assert body[0][cancelled_idx] == ""
    assert body[1][cancelled_idx] == "2026-04-20"


def test_rows_to_csv_empty() -> None:
    text = rows_to_csv([])
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines == [",".join(REPORT_COLUMNS)]
