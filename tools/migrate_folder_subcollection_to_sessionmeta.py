"""Migrate legacy folder linkage from subcollection to sessionMeta.

Background:
    Older client builds wrote folder→session linkage to
    ``users/{uid}/folders/{fid}/sessions/{sid}`` (with snapshot fields).
    The current backend reads from ``users/{uid}/sessionMeta/{sid}.folderId``
    so any session whose linkage lives only in the subcollection appears
    in 0 folders despite being assigned by the user.

What this script does:
    1. For every user, scan ``users/{uid}/folders/*/sessions/*`` (skipping
       soft-deleted folders).
    2. For each (sid, fid) found there, read the matching
       ``users/{uid}/sessionMeta/{sid}`` document.
       - If sessionMeta is missing → create it with folderId=fid.
       - If sessionMeta exists with folderId == None → set folderId=fid.
       - If sessionMeta exists with folderId == different fid →
         **skip** (treat the explicit user move via the new API as
         authoritative).
    3. Emit a report.

Safe to run repeatedly. Use ``--dry-run`` first.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill sessionMeta folderId from legacy folder subcollection")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned changes without writing")
    parser.add_argument("--limit-users", type=int, default=0,
                        help="Stop after processing this many users (0 = no limit)")
    parser.add_argument("--only-uid", type=str, default=None,
                        help="Process a single uid only (debugging)")
    args = parser.parse_args()

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if not project_id:
        print("ERROR: GOOGLE_CLOUD_PROJECT not set", file=sys.stderr)
        return 2

    from google.cloud import firestore
    db = firestore.Client(project=project_id)

    if args.only_uid:
        uid_iter = [db.collection("users").document(args.only_uid).get()]
    else:
        uid_iter = db.collection("users").stream()

    total_users = 0
    users_with_links = 0
    total_planned = 0
    total_created = 0
    total_updated = 0
    total_skipped_diff = 0
    total_already_ok = 0
    failures = 0

    for u in uid_iter:
        if not u.exists:
            continue
        uid = u.id
        total_users += 1
        if args.limit_users and total_users > args.limit_users:
            break

        folders = list(db.collection("users").document(uid).collection("folders").stream())
        if not folders:
            continue

        sub_links: dict[str, tuple[str, dict]] = {}  # sid -> (fid, sub_doc_data)
        for f in folders:
            fdata = f.to_dict() or {}
            if fdata.get("deletedAt"):
                continue
            sub_iter = (
                db.collection("users").document(uid)
                .collection("folders").document(f.id)
                .collection("sessions").stream()
            )
            for s in sub_iter:
                sub_links.setdefault(s.id, (f.id, s.to_dict() or {}))

        if not sub_links:
            continue
        users_with_links += 1

        meta_col = db.collection("users").document(uid).collection("sessionMeta")
        u_planned = u_created = u_updated = u_skipped = u_ok = 0
        for sid, (fid, sub_data) in sub_links.items():
            try:
                meta_ref = meta_col.document(sid)
                meta_snap = meta_ref.get()
                now = datetime.now(timezone.utc)
                if not meta_snap.exists:
                    u_planned += 1
                    if not args.dry_run:
                        meta_ref.set({
                            "sessionId": sid,
                            "folderId": fid,
                            "isPinned": False,
                            "isArchived": False,
                            "role": "OWNER",
                            "createdAt": sub_data.get("createdAt") or now,
                            "updatedAt": now,
                            "organizationUpdatedAt": now,
                            "lastOpenedAt": None,
                            "migratedFromFolderSubcollection": True,
                            "migratedAt": now,
                        })
                    u_created += 1
                    continue

                md = meta_snap.to_dict() or {}
                cur_fid = md.get("folderId")
                if cur_fid is None:
                    u_planned += 1
                    if not args.dry_run:
                        meta_ref.update({
                            "folderId": fid,
                            "updatedAt": now,
                            "organizationUpdatedAt": now,
                            "migratedFromFolderSubcollection": True,
                            "migratedAt": now,
                        })
                    u_updated += 1
                elif cur_fid == fid:
                    u_ok += 1
                else:
                    # User explicitly moved it elsewhere via /sessions/{id}/organization
                    # PUT — respect the new schema.
                    u_skipped += 1
            except Exception as e:
                failures += 1
                print(f"  [FAIL] uid={uid} sid={sid}: {e}")

        if u_planned or u_skipped:
            print(f"uid={uid:30} sub_links={len(sub_links):4} planned={u_planned} created={u_created} updated={u_updated} skipped_diff={u_skipped} already_ok={u_ok}")
        total_planned += u_planned
        total_created += u_created
        total_updated += u_updated
        total_skipped_diff += u_skipped
        total_already_ok += u_ok

    print()
    print("=== summary ===")
    print(f"users scanned:         {total_users}")
    print(f"users w/ legacy links: {users_with_links}")
    print(f"planned writes:        {total_planned}  (create: {total_created}, update: {total_updated})")
    print(f"skipped (diff folder): {total_skipped_diff}")
    print(f"already in sync:       {total_already_ok}")
    print(f"failures:              {failures}")
    print(f"mode:                  {'DRY RUN (no writes)' if args.dry_run else 'WRITE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
