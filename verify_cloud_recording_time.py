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
    total_cloud_sec = 0.0
    count = 0
    issues_found = 0
    
    print(f"{'User ID':<30} | {'Date':<12} | {'Rec Sec':<10} | {'Cloud Sec':<10} | {'Note':<10}")
    print("-" * 85)
    
    for doc in docs:
        count += 1
        data = doc.to_dict()
        uid = data.get("user_id", "unknown")
        date = data.get("date", "unknown")
        rec_sec = data.get("total_recording_sec", 0.0)
        cloud_sec = data.get("total_recording_cloud_sec", 0.0)
        
        note = ""
        if cloud_sec > rec_sec:
            note = "ISSUE"
            issues_found += 1

        total_sec += rec_sec
        total_cloud_sec += cloud_sec
        print(f"{uid:<30} | {date:<12} | {rec_sec:<10.1f} | {cloud_sec:<10.1f} | {note}")
        
    print("-" * 85)
    print(f"Total Documents: {count}")
    if issues_found > 0:
        print(f"WARNING: Found {issues_found} documents where cloud recording time > total recording time.")
    else:
        print("OK: No inconsistencies found between cloud and total recording time.")
        
    print(f"Grand Total Recording Time: {total_sec:.2f} seconds ({total_sec/60:.2f} minutes)")
    print(f"Grand Total Cloud Recording Time: {total_cloud_sec:.2f} seconds ({total_cloud_sec/60:.2f} minutes)")

if __name__ == "__main__":
    check_usage()
