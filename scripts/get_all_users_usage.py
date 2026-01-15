import os
import firebase_admin
from firebase_admin import credentials, firestore
from collections import defaultdict

# Initialize Firebase
if not firebase_admin._apps:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)

db = firestore.client()

def get_total_usage():
    print("Fetching documents from user_daily_usage...")
    docs = db.collection("user_daily_usage").stream()
    
    user_totals = defaultdict(float)
    user_session_counts = defaultdict(int)
    
    doc_count = 0
    for doc in docs:
        data = doc.to_dict()
        uid = data.get("user_id")
        if not uid:
            # Try to extract from doc_id if missing in body (legacy format)
            # doc_id is {uid}_{date}
            uid = doc.id.rsplit('_', 1)[0]
            
        recording_sec = data.get("total_recording_sec", 0)
        session_count = data.get("session_count", 0)
        
        user_totals[uid] += recording_sec
        user_session_counts[uid] += session_count
        doc_count += 1
        
    print(f"Processed {doc_count} daily logs.")
    print("-" * 50)
    print(f"{'User ID':<40} | {'Sessions':<10} | {'Total Recording (H:M:S)'}")
    print("-" * 50)
    
    # Sort by total recording time descending
    sorted_users = sorted(user_totals.items(), key=lambda x: x[1], reverse=True)
    
    for uid, total_sec in sorted_users:
        hours = int(total_sec // 3600)
        minutes = int((total_sec % 3600) // 60)
        seconds = int(total_sec % 60)
        time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        print(f"{uid:<40} | {user_session_counts[uid]:<10} | {time_str} ({total_sec:.1f}s)")

if __name__ == "__main__":
    get_total_usage()
