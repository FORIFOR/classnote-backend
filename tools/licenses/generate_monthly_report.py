"""Generate a partner-month report CSV.

Usage:
    python -m tools.licenses.generate_monthly_report \
        --partner life_select \
        --month 2026-05 \
        --out /tmp/lifeselect_monthly_report_2026_05.csv
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a monthly partner license report.")
    parser.add_argument("--partner", required=True, help="Partner ID, e.g. life_select")
    parser.add_argument("--month", required=True, help="Target month YYYY-MM (JST)")
    parser.add_argument("--out", required=True, help="Output CSV path (will be overwritten)")
    parser.add_argument("--created-by", default="cli", help="Audit identity")
    args = parser.parse_args(argv)

    from app.services.license_report_service import generate_report, rows_to_csv

    report_id, rows = generate_report(
        partner_id=args.partner,
        target_month=args.month,
        created_by=args.created_by,
    )
    csv_text = rows_to_csv(rows)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(csv_text)

    print(f"[ok] report generated")
    print(f"     report_id : {report_id}")
    print(f"     partner   : {args.partner}")
    print(f"     month     : {args.month}")
    print(f"     row_count : {len(rows)}")
    print(f"     csv_path  : {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
