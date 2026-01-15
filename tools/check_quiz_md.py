
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import sys

SESSION_ID = "lecture-1767485056140-1d2fc3"

def check_quiz():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"Checking Quiz for {SESSION_ID}...")
    doc = db.collection("sessions").document(SESSION_ID).get()
    
    if not doc.exists:
        print("Session not found")
        return

    data = doc.to_dict()
    print(f"QuizStatus: {data.get('quizStatus')}")
    qmd = data.get('quizMarkdown')
    if qmd:
        print(f"QuizMarkdown Length: {len(qmd)}")
        print("Preview: " + qmd[:100].replace("\n", " "))
    else:
        print("QuizMarkdown: None")

if __name__ == "__main__":
    check_quiz()
