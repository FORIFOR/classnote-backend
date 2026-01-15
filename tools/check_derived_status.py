
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

    print(f"Checking Derived Docs for {SESSION_ID}...")
    
    # Derived Summary
    sum_doc = db.collection("sessions").document(SESSION_ID).collection("derived").document("summary").get()
    if sum_doc.exists:
        print(f"Derived Summary Status: {sum_doc.to_dict().get('status')}")
        print(f"Derived Summary Error: {sum_doc.to_dict().get('errorReason')}")
    else:
        print("Derived Summary: Not Found")

    # Derived Quiz
    quiz_doc = db.collection("sessions").document(SESSION_ID).collection("derived").document("quiz").get()
    if quiz_doc.exists:
        print(f"Derived Quiz Status: {quiz_doc.to_dict().get('status')}")
        print(f"Derived Quiz Error: {quiz_doc.to_dict().get('errorReason')}")
    else:
        print("Derived Quiz: Not Found")

if __name__ == "__main__":
    check_derived()
