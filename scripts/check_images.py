#!/usr/bin/env python3
"""
Verification Script: Check for image notes in Firestore.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import firestore

if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

print("=" * 50)
print("  FIRESTORE IMAGE CHECK")
print("=" * 50)

sessions = db.collection("sessions").stream()
found_images = 0

for session in sessions:
    data = session.to_dict()
    image_notes = data.get("imageNotes", [])
    
    if image_notes:
        print(f"\nSession: {session.id} ({data.get('title', 'No Title')})")
        for img in image_notes:
            status = img.get("status", "unknown")
            print(f"  - Image ID: {img.get('id')}")
            print(f"    Status:   {status}")
            print(f"    Path:     {img.get('storagePath')}")
            print(f"    Created:  {img.get('createdAt')}")
            found_images += 1

print("\n" + "=" * 50)
if found_images == 0:
    print("  ✅ NO IMAGES FOUND IN DB")
else:
    print(f"  📸 FOUND {found_images} IMAGES IN DB")
print("=" * 50)
