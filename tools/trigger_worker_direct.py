
import requests
import json
import os
import firebase_admin
from firebase_admin import credentials

# Configuration
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
SESSION_ID = "lecture-1767485056140-1d2fc3" # The failed session

def trigger_worker():
    url = f"{BASE_URL}/internal/tasks/summarize"
    payload = {
        "sessionId": SESSION_ID,
        "idempotencyKey": "manual_trigger_debug"
    }
    
    print(f"Triggering Worker Direct: {url}")
    print(f"Payload: {payload}")
    
    # Note: If protected by check_auth, this might fail, but tasks.py usually trusts internal or validates token if present.
    # Current tasks.py does not enforce auth on this endpoint based on my previous read.
    resp = requests.post(url, json=payload)
    
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")

if __name__ == "__main__":
    trigger_worker()
