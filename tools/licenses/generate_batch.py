"""Generate a license batch and write a delivery CSV of plain keys.

The plain keys are visible **only** at this generation time — they are
not stored in Firestore (only the keyHash is). Treat the output CSV
file as sensitive: deliver over an encrypted channel (Drive folder
shared with the partner, signed S3 URL, etc.) and delete the local copy
after delivery.

Usage:
    python -m tools.licenses.generate_batch \
        --partner life_select \
        --reseller next_standards \
        --count 1000 \
        --plan business \
        --free-months 2 \
        --memo "NEXT STANDARDS初回納品" \
        --out /tmp/lifeselect_2026_05_batch001.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a license batch + delivery CSV.")
    parser.add_argument("--partner", required=True, help="Partner ID, e.g. life_select")
    parser.add_argument("--reseller", default=None, help="Reseller ID (optional)")
    parser.add_argument("--plan", default="business", help="Plan name (default: business)")
    parser.add_argument("--count", type=int, required=True, help="Number of keys to generate")
    parser.add_argument("--free-months", type=int, default=2, help="Free months (default: 2)")
    parser.add_argument("--memo", default=None, help="Free-text memo on the batch")
    parser.add_argument("--out", required=True, help="Output CSV path (will be overwritten)")
    parser.add_argument(
        "--created-by",
        default="cli",
        help="Audit identity for the batch (CLI operator id / email).",
    )
    args = parser.parse_args(argv)

    # Imported lazily so --help works without Firestore creds.
    from app.services.license_service import create_batch

    batch_id, plain_keys, _batch_doc = create_batch(
        partner_id=args.partner,
        reseller_id=args.reseller,
        plan=args.plan,
        count=args.count,
        free_months=args.free_months,
        memo=args.memo,
        created_by=args.created_by,
    )

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["license_key", "partner_id", "reseller_id", "plan", "free_months", "batch_id"])
        for k in plain_keys:
            w.writerow([k, args.partner, args.reseller or "", args.plan, args.free_months, batch_id])

    print(f"[ok] generated {args.count} keys")
    print(f"     batch_id   : {batch_id}")
    print(f"     csv_path   : {args.out}")
    print(f"     partner    : {args.partner}")
    print(f"     reseller   : {args.reseller or '-'}")
    print(f"     plan       : {args.plan}")
    print(f"     free_months: {args.free_months}")
    print(f"     created_at : {datetime.now(timezone.utc).isoformat()}")
    print("[reminder] plain keys are in this CSV ONLY. Firestore stores only the hash.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
