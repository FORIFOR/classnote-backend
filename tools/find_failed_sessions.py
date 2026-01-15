
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import datetime

def find_failed_sessions():
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
        try:
            firebase_admin.initialize_app(cred)
        except:
            pass
    else:
        try:
            firebase_admin.initialize_app()
        except:
            pass

    db = firestore.Client()
    
    print("Searching for recent failed sessions (last 24h)...")
    
    # We can't easily query "summaryStatus == failed" AND "updatedAt" without composite index.
    # So we'll fetch recent sessions and filter client-side.
    
    sessions = db.collection("sessions")\
        .where("ownerUid", "==", "qyp2anOl43RC94LSrsknE9cH0hA3")\
        .limit(20)\
        .stream()
        
    found = 0
    for s in sessions:
        d = s.to_dict()
        sid = s.id
        s_status = d.get("summaryStatus")
        s_error = d.get("summaryError")
        q_status = d.get("quizStatus")
        q_error = d.get("quizError")
        
        # Show ALL sessions for context, highlighting issues
        print(f"[{sid}] {d.get('createdAt')}")
        print(f"    User: {d.get('ownerUid')} | Title: {d.get('title')}")
        print(f"    Summary: {s_status} (Err: {s_error})")
        print(f"    Quiz:    {q_status} (Err: {q_error})")
        print(f"    Transcript Len: {len(d.get('transcriptText') or '')}")
        
        # Check Jobs Subcollection
        jobs = s.reference.collection("jobs").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(3).stream()
        for j in jobs:
            jd = j.to_dict()
            print(f"      - Job[{jd.get('type')}]: {jd.get('status')} (ID: {j.id})")
        print("-" * 40)
        found += 1

    if found == 0:
        print("\nNo failed sessions found in the last 50 entries.")

if __name__ == "__main__":
    find_failed_sessions()
