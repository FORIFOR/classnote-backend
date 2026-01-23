#!/usr/bin/env python3
"""
Migration Script: Add ownerAccountId to existing sessions.

This script:
1. Finds all sessions where ownerAccountId is missing
2. Resolves the accountId from ownerUid/ownerUserId via uid_links
3. Updates the session with ownerAccountId

Usage:
    # Dry run (default) - shows what would be updated
    python tools/migrate_sessions_owner_account_id.py

    # Execute migration
    python tools/migrate_sessions_owner_account_id.py --execute

    # Limit batch size
    python tools/migrate_sessions_owner_account_id.py --execute --limit 1000
"""

import argparse
import sys
import os
from datetime import datetime, timezone

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Initialize Firebase before importing db
import firebase_admin
if not firebase_admin._apps:
    firebase_admin.initialize_app()

from app.firebase import db


def resolve_account_id_for_uid(uid: str) -> str | None:
    """
    Resolve uid -> accountId using uid_links or users collection.
    Returns None if not resolvable (should not happen for legitimate users).
    """
    # 1. Check uid_links (primary)
    link_doc = db.collection("uid_links").document(uid).get()
    if link_doc.exists:
        account_id = link_doc.to_dict().get("accountId")
        if account_id:
            return account_id

    # 2. Check users/{uid}.accountId (legacy)
    user_doc = db.collection("users").document(uid).get()
    if user_doc.exists:
        account_id = user_doc.to_dict().get("accountId")
        if account_id:
            return account_id

    return None


def migrate_sessions(dry_run: bool = True, limit: int = 10000):
    """
    Migrate sessions to add ownerAccountId.

    Args:
        dry_run: If True, only print what would be done
        limit: Maximum number of sessions to process
    """
    print(f"=== Session Migration: Add ownerAccountId ===")
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    print(f"Limit: {limit}")
    print()

    # Find sessions without ownerAccountId
    # Note: Firestore doesn't support "field does not exist" queries directly
    # We query all sessions and filter in code
    sessions_ref = db.collection("sessions")

    total_checked = 0
    needs_migration = 0
    migrated = 0
    failed = 0
    already_has_account_id = 0
    unresolvable = 0

    batch = db.batch()
    batch_count = 0
    batch_size = 500  # Firestore batch limit

    print("Scanning sessions...")

    # Stream all sessions (can be optimized with pagination for very large datasets)
    for doc in sessions_ref.limit(limit).stream():
        total_checked += 1
        data = doc.to_dict()
        session_id = doc.id

        # Skip if already has ownerAccountId
        if data.get("ownerAccountId"):
            already_has_account_id += 1
            continue

        needs_migration += 1

        # Get owner uid
        owner_uid = data.get("ownerUid") or data.get("ownerUserId") or data.get("userId")
        if not owner_uid:
            print(f"  [SKIP] {session_id}: No owner uid found")
            unresolvable += 1
            continue

        # Resolve accountId
        account_id = resolve_account_id_for_uid(owner_uid)
        if not account_id:
            print(f"  [UNRESOLVABLE] {session_id}: Cannot resolve accountId for uid={owner_uid}")
            unresolvable += 1
            continue

        if dry_run:
            print(f"  [WOULD UPDATE] {session_id}: ownerUid={owner_uid} -> ownerAccountId={account_id}")
            migrated += 1
        else:
            try:
                batch.update(doc.reference, {
                    "ownerAccountId": account_id,
                    "migratedAt": datetime.now(timezone.utc),
                    "migrationNote": "added_owner_account_id"
                })
                batch_count += 1
                migrated += 1

                # Commit batch when full
                if batch_count >= batch_size:
                    batch.commit()
                    print(f"  [COMMITTED] Batch of {batch_count} sessions")
                    batch = db.batch()
                    batch_count = 0

            except Exception as e:
                print(f"  [ERROR] {session_id}: {e}")
                failed += 1

        if total_checked % 1000 == 0:
            print(f"  Progress: {total_checked} checked...")

    # Commit remaining
    if not dry_run and batch_count > 0:
        batch.commit()
        print(f"  [COMMITTED] Final batch of {batch_count} sessions")

    # Summary
    print()
    print("=== Migration Summary ===")
    print(f"Total sessions checked: {total_checked}")
    print(f"Already has ownerAccountId: {already_has_account_id}")
    print(f"Needed migration: {needs_migration}")
    print(f"{'Would migrate' if dry_run else 'Migrated'}: {migrated}")
    print(f"Unresolvable (no uid_links): {unresolvable}")
    print(f"Failed: {failed}")

    if dry_run and migrated > 0:
        print()
        print("Run with --execute to apply changes")


def create_uid_links_for_users(dry_run: bool = True, limit: int = 10000):
    """
    Create missing uid_links documents for users that have accountId.
    This ensures resolve_account_id_for_uid will work for all users.
    """
    print(f"\n=== Creating Missing uid_links ===")
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")

    users_ref = db.collection("users")
    created = 0
    already_exists = 0
    no_account_id = 0

    for doc in users_ref.limit(limit).stream():
        uid = doc.id
        data = doc.to_dict()
        account_id = data.get("accountId")

        if not account_id:
            no_account_id += 1
            continue

        # Check if uid_links exists
        link_doc = db.collection("uid_links").document(uid).get()
        if link_doc.exists:
            already_exists += 1
            continue

        if dry_run:
            print(f"  [WOULD CREATE] uid_links/{uid} -> accountId={account_id}")
            created += 1
        else:
            db.collection("uid_links").document(uid).set({
                "uid": uid,
                "accountId": account_id,
                "linkedAt": datetime.now(timezone.utc),
                "linkedVia": "migration_repair"
            })
            created += 1

    print(f"\nuid_links Summary:")
    print(f"  Already exists: {already_exists}")
    print(f"  No accountId in user: {no_account_id}")
    print(f"  {'Would create' if dry_run else 'Created'}: {created}")


def main():
    parser = argparse.ArgumentParser(description="Migrate sessions to add ownerAccountId")
    parser.add_argument("--execute", action="store_true", help="Actually execute the migration (default is dry run)")
    parser.add_argument("--limit", type=int, default=10000, help="Maximum number of sessions to process")
    parser.add_argument("--create-links", action="store_true", help="Also create missing uid_links")

    args = parser.parse_args()
    dry_run = not args.execute

    if args.create_links:
        create_uid_links_for_users(dry_run=dry_run, limit=args.limit)
        print()

    migrate_sessions(dry_run=dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
