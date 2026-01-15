import os
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

# Initialize with local key
key_path = "classnote-api-key.json"
if os.path.exists(key_path):
    cred = credentials.Certificate(key_path)
    firebase_admin.initialize_app(cred)
else:
    print("No key file found!")
    exit(1)

db = firestore.client()

def check_usage():
    print("Querying user_daily_usage...")
    docs = db.collection("user_daily_usage").stream()
    
    total_sec = 0.0
    count = 0
    
    print(f"{'User ID':<30} | {'Date':<12} | {'Sessions':<8} | {'Rec Sec':<10}")
    print("-" * 70)
    
    for doc in docs:
        count += 1
        data = doc.to_dict()
        uid = data.get("user_id", "unknown")
        date = data.get("date", "unknown")
        sessions = data.get("session_count", 0)
        rec_sec = data.get("total_recording_sec", 0.0)
        
        total_sec += rec_sec
        print(f"{uid:<30} | {date:<12} | {sessions:<8} | {rec_sec:<10.1f}")
        
    print("-" * 70)
    print(f"Total Documents: {count}")
    print(f"Grand Total Recording Time: {total_sec:.2f} seconds ({total_sec/60:.2f} minutes)")

if __name__ == "__main__":
    check_usage()
