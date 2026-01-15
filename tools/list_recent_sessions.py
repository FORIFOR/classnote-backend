
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import datetime

import sys

def list_recent_sessions(target_uid=None, title_filter=None):
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    try:
         db = firestore.Client(project=project_id)
    except:
         db = firestore.Client()
         
    print("Fetching last 10 sessions...")
    # Note: Querying across all sessions requires index or simple collection group query if specific permissions.
    # But usually 'sessions' is a root collection? Yes.
    
    query = db.collection("sessions").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(50)
    sessions = query.stream()
    
    print(f"\n{'ID':<30} | {'Created':<25} | {'User':<30} | {'Title'}")
    print("-" * 120)
    
    count = 0
    for s in sessions:
        d = s.to_dict()
        uid = d.get("ownerUid") or d.get("userId") or d.get("ownerUserId")
        title = d.get("title", "No Title")
        created = d.get("createdAt")
        
        # Filtering (Manual because Firestore queries are limited)
        if target_uid and uid != target_uid:
            continue
        if title_filter and title_filter not in title:
            continue
            
        print(f"{s.id:<30} | {str(created):<25} | {uid:<30} | {title}")
        count += 1
        if count >= 10: # Limit output to 10 *after* filtering
            break
            
    if count == 0:
        print("No matching sessions found.")
    print("-" * 120)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", help="Filter by User ID")
    parser.add_argument("--title", help="Filter by Title (substring)")
    args = parser.parse_args()
    
    list_recent_sessions(args.uid, args.title)
