"""Unit tests for ``calculate_billing_start_date``.

Spec: deepnote-contracts/product/business-license.md →
  "申込月より最大2か月無料" / "有料期間分は翌月末支払い対象".
For free_months=2 the application month and the following month are
free; the first chargeable month is month #3 (the +2 month).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.license_service import calculate_billing_start_date


@pytest.mark.parametrize(
    "app_date,free_months,expected",
    [
        # Spec example: 2026-04-06 → 2026-06-01.
        (date(2026, 4, 6), 2, date(2026, 6, 1)),
        # Year boundary: Nov + 2 = Jan next year.
        (date(2026, 11, 15), 2, date(2027, 1, 1)),
        # Year boundary: Dec + 2 = Feb next year.
        (date(2026, 12, 31), 2, date(2027, 2, 1)),
        # Zero free months → application month is already chargeable;
        # billing starts on the 1st of the application month.
        (date(2026, 4, 6), 0, date(2026, 4, 1)),
        # Single free month.
        (date(2026, 4, 6), 1, date(2026, 5, 1)),
        # Long-tail: 6 free months from May should land in November.
        (date(2026, 5, 1), 6, date(2026, 11, 1)),
        # 12 months free wraps to same month next year.
        (date(2026, 5, 1), 12, date(2027, 5, 1)),
    ],
)
def test_calculate_billing_start_date(
    app_date: date, free_months: int, expected: date
) -> None:
    assert calculate_billing_start_date(app_date, free_months=free_months) == expected


def test_calculate_billing_start_date_rejects_negative() -> None:
    with pytest.raises(ValueError):
        calculate_billing_start_date(date(2026, 4, 6), free_months=-1)
