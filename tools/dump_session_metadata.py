
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import json
import datetime

SESSION_ID = "lecture-1767485056140-1d2fc3"

def date_converter(o):
    if isinstance(o, datetime.datetime):
        return o.isoformat()

def check_full_metadata():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"--- Full Metadata for {SESSION_ID} ---")
    doc = db.collection("sessions").document(SESSION_ID).get()
    
    if not doc.exists:
        print("Session not found")
        return

    data = doc.to_dict()
    print(f"Root Status: {data.get('status')}")
    print(f"Summary Status: {data.get('summaryStatus')}")
    print(f"Summary Error: {data.get('summaryError')}")
    print(f"Quiz Status: {data.get('quizStatus')}")
    print(f"Quiz Error: {data.get('quizError')}")
    print(f"Transcript Status: {data.get('transcriptStatus')}")
    print(f"Transcript Error: {data.get('transcriptError')}")
    print(f"Diarization Status: {data.get('diarizationStatus')}")
    print(f"Diarization Error: {data.get('diarizationError')}")
    print(f"Playlist Status: {data.get('playlistStatus')}")
    print(f"Playlist Error: {data.get('playlistError')}")

if __name__ == "__main__":
    check_full_metadata()
