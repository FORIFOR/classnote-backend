
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import json

import sys
if len(sys.argv) > 1:
    SESSION_ID = sys.argv[1]
else:
    SESSION_ID = "lecture-1767419854920-f541d0"

def check_result():
    # Init Firestore
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

    print(f"Checking session {SESSION_ID}...")
    doc = db.collection("sessions").document(SESSION_ID).get()
    
    if not doc.exists:
        print("Session not found.")
        return

    data = doc.to_dict()
    
    print("\n--- Summary Markdown ---")
    print(data.get("summaryMarkdown", "(No summary data)"))
    
    print("\n--- Quiz Data ---")
    print(json.dumps(data.get("quiz", {}), indent=2, ensure_ascii=False))
    
    print("\n--- Job Statuses ---")
    jobs = db.collection("sessions").document(SESSION_ID).collection("jobs").stream()
    for j in jobs:
        jd = j.to_dict()
        print(f"Job {j.id}: {jd.get('type')} - {jd.get('status')} (Error: {jd.get('errorReason')})")

if __name__ == "__main__":
    check_result()
