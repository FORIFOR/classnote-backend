
import requests
import json
import os
import time
import uuid
import sys
import argparse
from google.cloud import firestore

# Configuration
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TIMEOUT_SEC = 60

def get_db():
    key_path = "classnote-api-key.json"
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if os.path.exists(key_path):
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_file(key_path)
        if not project_id:
            project_id = cred.project_id
        return firestore.Client(project=project_id, credentials=cred)
    else:
        if not project_id:
            project_id = os.environ.get("GCP_PROJECT") or "classnote-x-dev"
        return firestore.Client(project=project_id)

def monitor_job(db, session_id, job_id, job_type):
    print(f"Monitoring Job {job_id}...")
    session_ref = db.collection("sessions").document(session_id)
    job_ref = session_ref.collection("jobs").document(job_id)
    
    start_time = time.time()
    while time.time() - start_time < TIMEOUT_SEC:
        job_doc = job_ref.get()
        if job_doc.exists:
            data = job_doc.to_dict()
            status = data.get("status")
            print(f"  Job Status: {status}")
            if status in ["completed", "failed"]:
                return status, data.get("errorReason")
        else:
            print("  Job document not found yet...")

        # session_doc check removed to force strict job tracking verification
        
        time.sleep(2)
    
    return "timeout", "Monitor timed out"

def run_test(session_id, job_type):
    job_id = str(uuid.uuid4())
    print(f"--- Starting Manual Test: {job_type.upper()} ---")
    print(f"Session ID: {session_id}")
    print(f"Generated Job ID: {job_id}")

    # 1. Prepare Payload
    payload = {
        "sessionId": session_id,
        "jobId": job_id,
        "idempotencyKey": f"manual_test_{int(time.time())}"
    }
    if job_type == "quiz":
        payload["count"] = 5

    # 2. Prepare job document BEFORE calling worker (Fix race condition)
    db = get_db()
    try:
        doc_ref = db.collection("sessions").document(session_id).collection("jobs").document(job_id)
        doc_ref.set({
            "status": "queued",
            "type": job_type,
            "createdAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"Job doc create error: {e}")
        return

    # 3. Call Worker Endpoint
    task_path = "summarize" if job_type == "summary" else job_type
    endpoint = f"{BASE_URL}/internal/tasks/{task_path}"
    print(f"Calling Endpoint: {endpoint}")
    
    try:
        resp = requests.post(endpoint, json=payload, timeout=120)
        print(f"Response: {resp.status_code}")
        print(f"Body: {resp.text}")
        if resp.status_code != 200:
            print(f"Worker Error: {resp.text}")
            # continue to monitor anyway
    except requests.exceptions.Timeout:
        print("Request timed out (Client side), but worker may be running. Proceeding to monitor.")
    except Exception as e:
        print(f"Request Exception: {e}")
        # continue to monitor anyway

    # 4. Monitor Result
    status, error = monitor_job(db, session_id, job_id, job_type)
    
    print("-" * 30)
    print(f"Final Status: {status}")
    if error:
        print(f"Error Reason: {error}")
    
    if status == "completed":
        print("✅ SUCCESS: Job completed successfully.")
    else:
        print("❌ FAILURE: Job failed or timed out.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manually trigger and monitor AI jobs")
    parser.add_argument("session_id", help="Target Session ID")
    parser.add_argument("type", choices=["summary", "quiz"], help="Job Type")
    
    args = parser.parse_args()
    run_test(args.session_id, args.type)
