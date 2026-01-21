
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.firebase import db
from google.cloud import firestore

def fix_user_count(uid):
    print(f"Fixing serverSessionCount for {uid}...")
    
    # 1. Count Active Sessions
    sessions_ref = db.collection("sessions")
    # Owner check + active check
    query = (
        sessions_ref
        .where("ownerId", "==", uid)
        .where("deletedAt", "==", None)
    )
    
    docs = list(query.stream())
    actual_count = len(docs)
    print(f"Found {actual_count} active sessions for {uid}.")
    print(f"IDs: {[d.id for d in docs]}")
    
    # 2. Update User Doc
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        print("User not found!")
        return
        
    current_stored = user_doc.to_dict().get("serverSessionCount", "N/A")
    print(f"Stored count: {current_stored} -> Target: {actual_count}")
    
    if current_stored != actual_count:
        user_ref.update({
            "serverSessionCount": actual_count
        })
        print("Updated successfully.")
    else:
        print("Count already correct.")

if __name__ == "__main__":
    target_uid = "6QJ3VWIy3DMYwqF2wTUGNsmMcWz2"
    if len(sys.argv) > 1:
        target_uid = sys.argv[1]
    
    fix_user_count(target_uid)
