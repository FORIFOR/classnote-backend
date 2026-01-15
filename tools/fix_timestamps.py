
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import datetime

SESSION_ID = "lecture-1767485056140-1d2fc3"

def fix_timestamps():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"Fixing Timestamps for {SESSION_ID}...")
    doc_ref = db.collection("sessions").document(SESSION_ID)
    
    now = datetime.datetime.now(datetime.timezone.utc)
    
    updates = {
        "summaryUpdatedAt": now,
        "quizUpdatedAt": now,
        "updatedAt": now # Also touch root updated at
    }
    
    doc_ref.set(updates, merge=True)
    print(f"Updated timestamps to {now.isoformat()}")

if __name__ == "__main__":
    fix_timestamps()
