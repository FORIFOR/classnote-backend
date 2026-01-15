
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os

SESSION_ID = "lecture-1767485056140-1d2fc3"

def check_derived():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"Checking Derived Summary for {SESSION_ID}...")
    doc_ref = db.collection("sessions").document(SESSION_ID).collection("derived").document("summary")
    doc = doc_ref.get()
    
    if doc.exists:
        data = doc.to_dict()
        print(f"Derived Summary Status: {data.get('status')}")
        print(f"Derived Summary Error: {data.get('errorReason')}")
        print(f"Derived UpdatedAt: {data.get('updatedAt')}")
    else:
        print("Derived Summary Document NOT FOUND")

if __name__ == "__main__":
    check_derived()
