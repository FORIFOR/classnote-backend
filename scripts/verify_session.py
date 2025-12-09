import os
import sys
from google.cloud import firestore

# Ensure project is set for local execution
os.environ["GOOGLE_CLOUD_PROJECT"] = "classnote-x-dev"

def check_session(session_id):
    db = firestore.Client()
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    
    print(f"Checking session: {session_id}")
    print(f"Exists: {doc.exists}")
    
    if doc.exists:
        data = doc.to_dict()
        transcript = data.get("transcriptText")
        print(f"Status: {data.get('status')}")
        print(f"AudioPath: {data.get('audioPath')}")
        if transcript:
            print(f"Transcript (first 100 chars): {transcript[:100]}...")
        else:
            print("Transcript: [None]")
            
        print(f"Summary Status: {data.get('summaryStatus')}")
        print(f"Quiz Status: {data.get('quizStatus')}")
    else:
        print("Document not found.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        s_id = sys.argv[1]
    else:
        s_id = "lecture-1765040595523" # Default from logs
    check_session(s_id)
