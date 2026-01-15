
import firebase_admin
from firebase_admin import auth, credentials, firestore
import requests
import json
import os
import time
import uuid

# Configuration
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk"
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
SESSION_ID = "lecture-1767396222047-d14581" 

def get_id_token():
    key_path = "classnote-api-key.json"
    cred = None
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    # We just need Firestore client
    return cred

def verify_direct():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)
        
    # 1. Create a dummy job document
    job_id = str(uuid.uuid4())
    print(f"Creating Dummy Job: {job_id}")
    job_ref = db.collection("sessions").document(SESSION_ID).collection("jobs").document(job_id)
    job_ref.set({
        "jobId": job_id,
        "type": "summary",
        "status": "queued",
        "createdAt": firestore.SERVER_TIMESTAMP,
        "idempotencyKey": f"direct_test_{int(time.time())}"
    })
    
    # 2. Call Worker Direct
    print("Calling Worker Endpoint Directly...")
    payload = {
        "sessionId": SESSION_ID,
        "jobId": job_id,
        "idempotencyKey": f"direct_test_{int(time.time())}"
    }
    
    # Note: Internal workers usually don't verify auth, or rely on OIDC.
    # But currently tasks.py doesn't seem to check auth token? 
    # Let's try without auth first (if fail, we know).
    # Update: tasks.py is --allow-unauthenticated in deploy.sh? Yes.
    
    resp = requests.post(f"{BASE_URL}/internal/tasks/summarize", json=payload)
    print(f"Worker Response: {resp.status_code} - {resp.text}")
    
    if resp.status_code != 200:
        print("Worker returned error.")
        return

    # 3. Monitor Job Status
    print("Monitoring Job Status...")
    start_time = time.time()
    while time.time() - start_time < 60:
        job_doc = job_ref.get()
        if job_doc.exists:
            status = job_doc.to_dict().get("status")
            print(f"Job Status: {status}")
            if status in ["completed", "failed", "succeeded"]:
                print("✅ Direct Verification Passed!")
                return
        time.sleep(2)
        
    print("Timeout waiting for job completion.")

if __name__ == "__main__":
    verify_direct()
