
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
import os
import sys

SESSION_ID = "lecture-1767485056140-1d2fc3"

def fix_jobs():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)

    print(f"Fixing jobs for session {SESSION_ID}...")
    
    doc_ref = db.collection("sessions").document(SESSION_ID)
    doc = doc_ref.get()
    
    if not doc.exists:
        print("Session not found.")
        return

    data = doc.to_dict()
    summary_md = data.get("summaryMarkdown")
    quiz_data = data.get("quiz")
    
    jobs = doc_ref.collection("jobs").stream()
    
    for j in jobs:
        jd = j.to_dict()
        jid = j.id
        jtype = jd.get("type")
        jstatus = jd.get("status")
        
        if jstatus == "queued":
            if jtype == "summary":
                if summary_md:
                    print(f"-> Marking SUMMARY job {jid} as completed")
                    doc_ref.collection("jobs").document(jid).update({"status": "completed"})
                else:
                    print(f"-> Marking SUMMARY job {jid} as failed (No artifact)")
                    doc_ref.collection("jobs").document(jid).update({"status": "failed", "errorReason": "Stuck in queue, no result"})
            
            elif jtype == "quiz":
                if data.get("quizStatus") == "completed" or data.get("quizMarkdown"):
                     print(f"-> Marking QUIZ job {jid} as completed")
                     doc_ref.collection("jobs").document(jid).update({"status": "completed"})
                else:
                     print(f"-> Marking QUIZ job {jid} as failed (No artifact)")
                     doc_ref.collection("jobs").document(jid).update({"status": "failed", "errorReason": "Stuck in queue, no result"})

            elif jtype == "transcribe":
                if data.get("transcriptText"):
                    print(f"-> Marking TRANSCRIBE job {jid} as completed")
                    doc_ref.collection("jobs").document(jid).update({"status": "completed"})
                else:
                    print(f"-> Marking TRANSCRIBE job {jid} as failed")
                    doc_ref.collection("jobs").document(jid).update({"status": "failed"})

            elif jtype == "diarize":
                 if data.get("diarizedSegments") or data.get("segments") or data.get("transcriptText"):
                     print(f"-> Marking DIARIZE job {jid} as completed (Assuming implicit success)")
                     doc_ref.collection("jobs").document(jid).update({"status": "completed"})
                 else:
                     print(f"-> Marking DIARIZE job {jid} as failed")
                     doc_ref.collection("jobs").document(jid).update({"status": "failed"})
            
            else:
                print(f"-> Marking Unknown/Other job {jid} ({jtype}) as failed (Stuck)")
                doc_ref.collection("jobs").document(jid).update({"status": "failed", "errorReason": "Stuck cleanup"})
        else:
            print(f"Job {jid} is {jstatus}, skipping.")

if __name__ == "__main__":
    fix_jobs()
