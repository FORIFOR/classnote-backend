#!/usr/bin/env python3
"""
Verification Script: Check if Firestore is clean.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import credentials, firestore

if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

# Collections to check
COLLECTIONS = [
    "users",
    "sessions", 
    "shareCodes",
    "username_claims",
]

print("=" * 50)
print("  DATABASE VERIFICATION")
print("=" * 50)

total_docs = 0
for coll_name in COLLECTIONS:
    docs = list(db.collection(coll_name).limit(10).stream())
    count = len(docs)
    total_docs += count
    
    status = "✅ CLEAN" if count == 0 else f"⚠️  {count} docs found"
    print(f"  {coll_name}: {status}")
    
    if count > 0:
        for doc in docs:
            print(f"      - {doc.id}")

print("=" * 50)
if total_docs == 0:
    print("  ✅ DATABASE IS CLEAN")
else:
    print(f"  ⚠️  FOUND {total_docs} ORPHAN DOCUMENTS")
print("=" * 50)
