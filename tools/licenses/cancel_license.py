"""Cancel a single license by license_id or by license key.

Usage:
    # by id
    python -m tools.licenses.cancel_license --license-id lic_xxx --cancelled-date 2026-05-31
    # by raw key (hashed internally)
    python -m tools.licenses.cancel_license --license-key DNLS-7K4P-X9M2-Q8AZ --cancelled-date 2026-05-31
"""

from __future__ import annotations

import argparse
import sys
from datetime import date


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cancel a license (admin op).")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--license-id", help="licenses/{id} document id")
    grp.add_argument("--license-key", help="Plain license key (will be hashed)")
    parser.add_argument(
        "--cancelled-date",
        help="JST effective cancel date YYYY-MM-DD (default: today)",
        default=None,
    )
    parser.add_argument("--reason", default=None, help="Free-text reason")
    parser.add_argument("--cancelled-by", default="cli", help="Audit identity")
    args = parser.parse_args(argv)

    from app.firebase import db
    from app.services.license_key_service import hash_license_key
    from app.services.license_service import cancel_license

    if args.license_id:
        license_id = args.license_id
    else:
        # Resolve via keyHash.
        key_hash = hash_license_key(args.license_key)
        snaps = list(
            db.collection("licenses").where("keyHash", "==", key_hash).limit(1).stream()
        )
        if not snaps:
            print("[fail] license_not_found", file=sys.stderr)
            return 1
        license_id = snaps[0].id

    cancelled_date = (
        date.fromisoformat(args.cancelled_date) if args.cancelled_date else None
    )
    cancel_license(
        license_id=license_id,
        cancelled_date=cancelled_date,
        reason=args.reason,
        cancelled_by=args.cancelled_by,
    )
    print(f"[ok] cancelled license_id={license_id}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
