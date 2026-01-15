
import firebase_admin
from firebase_admin import auth, credentials
from google.cloud import firestore
import requests
import json
import os
import time

# Configuration
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk"
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TARGET_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"
SESSION_ID = "lecture-1767396222047-d14581" # Re-use existing session or create new

def get_id_token():
    key_path = "classnote-api-key.json"
    cred = None
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    try:
        custom_token = auth.create_custom_token(TARGET_UID)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
            
        exchange_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}"
        payload = {"token": custom_token, "returnSecureToken": True}
        resp = requests.post(exchange_url, json=payload)
        resp.raise_for_status()
        return resp.json()["idToken"]
    except Exception as e:
        print(f"Token error: {e}")
        raise e

def verify_fix():
    token = get_id_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    print(f"Triggering Summary for {SESSION_ID}...")
    resp = requests.post(f"{BASE_URL}/sessions/{SESSION_ID}/jobs", json={"type": "summary", "idempotencyKey": f"fix_check_{int(time.time())}"}, headers=headers)
    
    if resp.status_code != 200:
        print(f"Failed to start job: {resp.status_code} - {resp.text}")
        return
        
    print(f"Job Started: {resp.json()}")
    job_id = resp.json().get("jobId")
    
    if not job_id:
        print("Error: No jobId returned!")
        return

    print(f"Monitoring Job ID: {job_id}")
    
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        db = firestore.Client(project=project_id, credentials=cred)
    else:
        db = firestore.Client(project=project_id)
        
    start_time = time.time()
    while time.time() - start_time < 120:
        job_doc = db.collection("sessions").document(SESSION_ID).collection("jobs").document(job_id).get()
        if job_doc.exists:
            status = job_doc.to_dict().get("status")
            print(f"Job Status: {status}")
            if status in ["completed", "failed", "succeeded"]:
                print(f"✅ Job reached terminal status: {status}")
                return
        time.sleep(2)
        
    print("Timeout waiting for job completion.")

if __name__ == "__main__":
    verify_fix()
