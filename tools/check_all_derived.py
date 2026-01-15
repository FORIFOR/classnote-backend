
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os

SESSION_ID = "lecture-1767485056140-1d2fc3"

def check_all_derived():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"Checking ALL Derived Docs for {SESSION_ID}...")
    docs = db.collection("sessions").document(SESSION_ID).collection("derived").stream()
    
    found_any_fail = False
    for doc in docs:
        d = doc.to_dict()
        status = d.get('status')
        err = d.get('errorReason')
        print(f"Derived '{doc.id}': Status={status}, Error={err}")
        if status == 'failed' or err:
            found_any_fail = True

    if not found_any_fail:
        print("ALL DERIVED DOCS ARE CLEAN.")

if __name__ == "__main__":
    check_all_derived()
