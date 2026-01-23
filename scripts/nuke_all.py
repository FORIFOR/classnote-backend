#!/usr/bin/env python3
"""
Consolidated Nuke Script: Purge all data from Firestore, Storage, and Firebase Auth.
Usage: python3 scripts/nuke_all.py
"""
import os
import sys
import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud import storage

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

TARGET_COLLECTIONS = [
    "users",
    "accounts",
    "uid_links",
    "phone_numbers",
    "sessions",
    "session_members",
    "ops_events",
    "user_daily_usage",
    "usage_events",
    "system_stats",
    "shareCodes",
    "shareLinks",
    "idempotency_locks",
    "active_streams",
    "translations",
    "security_audit_logs",
    "usage_limits",
    "invitations",
    "feedback",
    "batch_jobs",
    "username_claims"
]

STORAGE_BUCKETS = [
    os.environ.get("AUDIO_BUCKET_NAME", "classnote-x-audio"),
    os.environ.get("MEDIA_BUCKET_NAME", "classnote-x-media"),
]

def init_firebase():
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()
    project_id = firebase_admin.get_app().project_id
    storage_client = storage.Client(project=project_id)
    return db, storage_client, project_id

def delete_collection(db, coll_ref, batch_size=50):
    """Recursively delete a collection and its subcollections."""
    docs = list(coll_ref.limit(batch_size).stream())
    if not docs:
        return 0

    deleted = 0
    for doc in docs:
        for sub_coll in doc.reference.collections():
            delete_collection(db, sub_coll, batch_size)
        doc.reference.delete()
        deleted += 1

    if len(docs) >= batch_size:
        return deleted + delete_collection(db, coll_ref, batch_size)
    return deleted

def nuke_firestore(db):
    print("\n--- Purging Firestore ---")
    total = 0
    for coll_name in TARGET_COLLECTIONS:
        print(f"  Clearing {coll_name}...")
        count = delete_collection(db, db.collection(coll_name))
        print(f"    Deleted {count} documents.")
        total += count
    print(f"Firestore clean. Total deleted: {total}")

def nuke_storage(storage_client):
    print("\n--- Purging Storage ---")
    for bucket_name in STORAGE_BUCKETS:
        print(f"  Emptying bucket: {bucket_name}...")
        try:
            bucket = storage_client.bucket(bucket_name)
            if not bucket.exists():
                print(f"    Bucket {bucket_name} does not exist.")
                continue
            blobs = list(bucket.list_blobs())
            if blobs:
                bucket.delete_blobs(blobs)
                print(f"    Deleted {len(blobs)} files.")
            else:
                print("    Bucket already empty.")
        except Exception as e:
            print(f"    Error: {e}")

def nuke_auth():
    print("\n--- Purging Firebase Auth ---")
    count = 0
    try:
        # Delete users in batches
        page = auth.list_users()
        while page:
            uids = [user.uid for user in page.users]
            if uids:
                auth.delete_users(uids)
                count += len(uids)
            page = page.get_next_page()
    except Exception as e:
        print(f"  Error during Auth nuke: {e}")
    print(f"Auth clean. Total users deleted: {count}")

def main():
    db, storage_client, project_id = init_firebase()
    print(f"TARGET PROJECT: {project_id}")
    print("WARNING: This will delete ALL data in Firestore, Storage, and Auth!")
    confirm = input(f"Type 'NUKE ALL' to confirm: ")
    if confirm != "NUKE ALL":
        print("Aborted.")
        return

    nuke_firestore(db)
    nuke_storage(storage_client)
    nuke_auth()
    print("\nEnvironment wiped clean.")

if __name__ == "__main__":
    main()
