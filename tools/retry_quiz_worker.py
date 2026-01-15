
import requests
import json
import os
import time

# Configuration
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
SESSION_ID = "lecture-1767485056140-1d2fc3"
JOB_ID = "bd34aa56-c221-4ba7-94fc-9703a573e357" # Existing queued job

def retry_quiz():
    print(f"Retrying Quiz Job {JOB_ID} for Session {SESSION_ID}...")
    
    payload = {
        "sessionId": SESSION_ID,
        "jobId": JOB_ID,
        "count": 5,
        "idempotencyKey": f"retry_{int(time.time())}"
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/internal/tasks/quiz", json=payload)
        print(f"Worker Response: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    retry_quiz()
