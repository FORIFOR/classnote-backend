
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os

SESSION_ID = "lecture-1767485056140-1d2fc3"

def dump_jobs():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"Dumping Jobs for {SESSION_ID}...")
    jobs = db.collection("sessions").document(SESSION_ID).collection("jobs").stream()
    
    for j in jobs:
        d = j.to_dict()
        print(f"Job {j.id}: {d.get('type')} - {d.get('status')} (Error: {d.get('errorReason')})")

if __name__ == "__main__":
    dump_jobs()
