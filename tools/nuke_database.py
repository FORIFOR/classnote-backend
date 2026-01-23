
import os
import sys
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage

# --- CONFIGURATION ---
TARGET_COLLECTIONS = [
    "users",
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
    "batch_jobs", # Just in case
    # Add any other top-level collections here
]

STORAGE_PREFIXES = [
    "sessions/",
    "imports/",
]

def init_firebase():
    """Initialize Firebase Admin and GCS Client."""
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    
    if not project_id:
        print("ERROR: GOOGLE_CLOUD_PROJECT or GCP_PROJECT environment variable must be set.")
        sys.exit(1)

    print(f"Target Project: {project_id}")
    
    # Initialize Firestore
    try:
        firebase_admin.initialize_app()
        db = firestore.client()
    except Exception as e:
        print(f"Failed to init Firebase: {e}")
        sys.exit(1)
        
    # Initialize Storage
    storage_client = storage.Client(project=project_id)
    
    return db, storage_client, project_id

def recursive_delete_collection(db, coll_ref, batch_size=50):
    """Recursively delete a collection and its subcollections."""
    docs = list(coll_ref.limit(batch_size).stream())
    deleted = 0

    if not docs:
        return 0

    for doc in docs:
        # 1. Recursively delete subcollections
        # Note: listing subcollections requires one extra API call per doc if not known
        # But generic 'nuke' needs to discover them.
        for sub_coll in doc.reference.collections():
            recursive_delete_collection(db, sub_coll, batch_size)

        # 2. Delete the doc itself
        doc.reference.delete()
        deleted += 1

    # Recurse if there are more docs in this collection
    if len(docs) >= batch_size:
        return deleted + recursive_delete_collection(db, coll_ref, batch_size)
    
    return deleted

def nuke_firestore(db):
    print("\n--- NUKING FIRESTORE ---")
    total_deleted = 0
    for coll_name in TARGET_COLLECTIONS:
        print(f"Scanning collection: {coll_name}...")
        ref = db.collection(coll_name)
        count = recursive_delete_collection(db, ref)
        print(f"  Deleted {count} documents (and subcollections).")
        total_deleted += count
    print(f"Firestore Cleanup Complete. Total docs deleted: {total_deleted}")

def nuke_storage(storage_client):
    print("\n--- NUKING STORAGE ---")
    
    # Resolve Bucket Names from Env or Default
    audio_bucket_name = os.environ.get("AUDIO_BUCKET_NAME", "classnote-x-audio")
    media_bucket_name = os.environ.get("MEDIA_BUCKET_NAME", "classnote-x-media")
    
    buckets = [audio_bucket_name, media_bucket_name]
    
    for bucket_name in buckets:
        print(f"Scanning bucket: {bucket_name}...")
        try:
            bucket = storage_client.bucket(bucket_name)
            # Verify bucket exists
            if not bucket.exists():
                print(f"  Bucket {bucket_name} does not exist. Skipping.")
                continue

            for prefix in STORAGE_PREFIXES:
                blobs = list(bucket.list_blobs(prefix=prefix))
                if not blobs:
                    continue
                
                print(f"  Found {len(blobs)} files in {prefix}")
                
                # Batch delete (Delete in chunks if many)
                # GCS Python client delete_blobs equivalent? 
                # bucket.delete_blobs(blobs) is efficient
                bucket.delete_blobs(blobs)
                print(f"  Deleted {len(blobs)} files.")
                
        except Exception as e:
            print(f"  Error accessing bucket {bucket_name}: {e}")

    print("Storage Cleanup Complete.")

def main():
    print("==================================================")
    print("!!!            DANGER: NUKE DATABASE           !!!")
    print("==================================================")
    print("This script will PERMANENTLY DELETE all data in:")
    print(f"- Firestore Collections: {', '.join(TARGET_COLLECTIONS)}")
    print(f"- Storage Paths: {', '.join(STORAGE_PREFIXES)}")
    print("==================================================")
    
    db, storage_client, project_id = init_firebase()
    
    confirm = input(f"Type 'NUKE' to confirm deletion for project '{project_id}': ")
    if confirm != "NUKE":
        print("Aborted.")
        sys.exit(0)

    nuke_firestore(db)
    nuke_storage(storage_client)
    
    print("\n\nAll systems cleanly wiped.")

if __name__ == "__main__":
    main()
