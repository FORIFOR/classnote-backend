#!/usr/bin/env python3
"""
Cleanup Script: Delete ALL users and their data from Firestore.
Run from project root with: python3 scripts/cleanup_all_users.py

WARNING: This is a destructive operation. All user data will be permanently deleted.
"""
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud import storage

# Initialize Firebase Admin
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

def delete_collection(coll_ref, batch_size=100):
    """Delete all documents in a collection."""
    docs = coll_ref.limit(batch_size).stream()
    deleted = 0
    for doc in docs:
        # Recursively delete subcollections
        for subcol in doc.reference.collections():
            delete_collection(subcol)
        doc.reference.delete()
        deleted += 1
    if deleted >= batch_size:
        return delete_collection(coll_ref, batch_size)
    return deleted

def delete_user_data(uid: str):
    """Delete all data for a single user."""
    print(f"  Deleting user: {uid}")
    
    # 1. Delete user document and subcollections
    user_ref = db.collection("users").document(uid)
    for subcol_name in ["subscriptions", "consents", "monthly_usage", "sessionMeta"]:
        subcol = user_ref.collection(subcol_name)
        delete_collection(subcol)
    user_ref.delete()
    
    # 2. Delete sessions owned by this user
    sessions = db.collection("sessions").where("userId", "==", uid).stream()
    for session in sessions:
        session_ref = session.reference
        # Delete subcollections (jobs, etc.)
        for subcol in session_ref.collections():
            delete_collection(subcol)
        session_ref.delete()
        print(f"    Deleted session: {session.id}")
    
    # 3. Delete from Firebase Auth (optional - may fail if user doesn't exist)
    try:
        auth.delete_user(uid)
        print(f"    Deleted auth record for: {uid}")
    except Exception as e:
        print(f"    Auth deletion skipped: {e}")

def cleanup_orphan_collections():
    """Delete orphan data not associated with users."""
    print("\nCleaning up orphan collections...")
    
    # Delete shareCodes
    delete_collection(db.collection("shareCodes"))
    print("  Deleted shareCodes")
    
    # Delete username_claims
    delete_collection(db.collection("username_claims"))
    print("  Deleted username_claims")

def main():
    print("=" * 60)
    print("  CLEANUP: Deleting ALL user data from Firestore")
    print("=" * 60)
    
    # Get all users
    users = list(db.collection("users").stream())
    print(f"\nFound {len(users)} users to delete.\n")
    
    if not users:
        print("No users found. Database is already clean.")
        return
    
    for user_doc in users:
        uid = user_doc.id
        delete_user_data(uid)
    
    cleanup_orphan_collections()
    
    print("\n" + "=" * 60)
    print("  CLEANUP COMPLETE")
    print("=" * 60)
    print(f"Deleted {len(users)} users and all associated data.")

if __name__ == "__main__":
    main()
