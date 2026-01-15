
import firebase_admin
from firebase_admin import auth, credentials
import requests
import json
import os
import time

# Configuration
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk"
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TEST_UID = "verification-bot-user"

def get_id_token():
    key_path = "classnote-api-key.json"
    cred = None
    
    if os.path.exists(key_path):
        print(f"Using key file: {key_path}")
        cred = credentials.Certificate(key_path)
    else:
        print("Key file not found, trying default credentials...")
        cred = credentials.ApplicationDefault()

    try:
        # Delete existing app if any to force re-init
        if firebase_admin._apps:
             firebase_admin.delete_app(firebase_admin.get_app())
        
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Init failed: {e}")
        raise e
    
    print(f"Minting custom token for {TEST_UID}...")
    try:
        custom_token = auth.create_custom_token(TEST_UID)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
    except Exception as e:
         print(f"Failed to create custom token: {e}")
         raise e

    print("Exchanging for ID token...")
    exchange_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}"
    payload = {"token": custom_token, "returnSecureToken": True}
    resp = requests.post(exchange_url, json=payload)
    if resp.status_code != 200:
        print(f"Token exchange failed: {resp.text}")
        exit(1)
    
    id_token = resp.json()["idToken"]
    print("Got ID Token.")
    return id_token

def run_checks(token):
    headers = {"Authorization": f"Bearer {token}"}
    
    # 1. GET /sessions
    print("\n--- 1. Testing GET /sessions ---")
    resp = requests.get(f"{BASE_URL}/sessions", headers=headers)
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        sessions = resp.json()
        print(f"Found {len(sessions)} sessions.")
    else:
        print(f"Error: {resp.text}")
        return

    # 2. CREATE Session
    print("\n--- 2. Testing POST /sessions ---")
    payload = {
        "title": "Verification Session " + str(int(time.time())),
        "mode": "lecture"
    }
    resp = requests.post(f"{BASE_URL}/sessions", json=payload, headers=headers)
    print(f"Status: {resp.status_code}")
    if resp.status_code not in [200, 201]:
        print(f"Error creating session: {resp.text}")
        if sessions:
             session_id = sessions[0]['id']
             print(f"Fallback: Using existing Session ID: {session_id}")
        else:
             return
    else:
        session_data = resp.json()
        session_id = session_data["id"]
        print(f"Created Session ID: {session_id}")
    
    # 3. Trigger Summary
    print("\n--- 3. Testing POST /sessions/{id}/jobs (Summary) ---")
    # Need to update status/transcript first? 
    # Usually jobs require some content. Summary needs transcript.
    # Let's mock transcript first.
    
    # Update Transcript
    # Wait, /sessions/{id} PATCH doesn't update transcript directly in MVP?
    # It might need audio or direct update.
    # Let's check update endpoint.
    # Actually, let's try to trigger it and see if it fails due to "no transcript" or "success/queued".
    # Providing a basic transcript via 'transcriptText' if allowed in create? No.
    # We might need to use `transcribe` job or manually inject to DB if we were admin.
    # OR, we can try to call the job and expect it to "start" even if it fails later.
    
    job_payload = {"type": "summary"}
    resp = requests.post(f"{BASE_URL}/sessions/{session_id}/jobs", json=job_payload, headers=headers)
    print(f"Summary Job Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    
    # 4. Trigger Quiz
    print("\n--- 4. Testing POST /sessions/{id}/jobs (Quiz) ---")
    job_payload = {"type": "quiz", "params": {"count": 3}}
    resp = requests.post(f"{BASE_URL}/sessions/{session_id}/jobs", json=job_payload, headers=headers)
    print(f"Quiz Job Status: {resp.status_code}")
    print(f"Response: {resp.text}")

if __name__ == "__main__":
    try:
        token = get_id_token()
        run_checks(token)
    except Exception as e:
        print(f"Verification failed: {e}")
