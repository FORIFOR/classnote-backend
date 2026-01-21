#!/usr/bin/env python3
"""
Verification Script: Check GCS bucket for leftover files.
"""
import os
from google.cloud import storage

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "classnote-x-dev")
BUCKET_NAME = f"{PROJECT_ID}-audio"  # Standard naming convention

client = storage.Client(project=PROJECT_ID)

print("=" * 60)
print("  GCS BUCKET VERIFICATION")
print("=" * 60)

try:
    bucket = client.bucket(BUCKET_NAME)
    if not bucket.exists():
        print(f"  Bucket '{BUCKET_NAME}' does not exist.")
    else:
        blobs = list(bucket.list_blobs(max_results=50))
        print(f"  Bucket: {BUCKET_NAME}")
        print(f"  Files found: {len(blobs)}")
        
        if blobs:
            print("\n  Files:")
            for blob in blobs:
                print(f"    - {blob.name} ({blob.size / 1024:.1f} KB)")
        else:
            print("\n  ✅ BUCKET IS CLEAN (no files)")
except Exception as e:
    print(f"  Error: {e}")

# Also check for other potential buckets
OTHER_BUCKETS = [
    f"{PROJECT_ID}-sessions",
    f"{PROJECT_ID}-uploads",
    "classnote-audio",
]

print("\n" + "-" * 60)
print("  Checking other potential buckets...")
for bname in OTHER_BUCKETS:
    try:
        b = client.bucket(bname)
        if b.exists():
            blobs = list(b.list_blobs(max_results=10))
            if blobs:
                print(f"  ⚠️  {bname}: {len(blobs)} files")
            else:
                print(f"  ✅ {bname}: CLEAN")
        else:
            print(f"  ❓ {bname}: does not exist")
    except Exception as e:
        print(f"  ❓ {bname}: {e}")

print("=" * 60)
