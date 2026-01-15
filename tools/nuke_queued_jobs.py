
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os

SESSION_ID = "lecture-1767485056140-1d2fc3"
TARGET_JOBS = [
    "b321579c-d1e2-4a96-a3bd-597d3572f2dc",
    "e55f6ddc-5d52-4dc1-a8da-5132efefdd8d"
]

def nuke_jobs():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"Nuking Queued Jobs for {SESSION_ID}...")
    
    for jid in TARGET_JOBS:
        print(f"Force-completing job {jid}...")
        db.collection("sessions").document(SESSION_ID).collection("jobs").document(jid).update({
            "status": "completed",
            "ForceCompletedBy": "SupportCleanup"
        })

if __name__ == "__main__":
    nuke_jobs()
