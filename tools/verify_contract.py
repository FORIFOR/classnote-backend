
import firebase_admin
from firebase_admin import auth, credentials
import requests
import sys

# Config
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TEST_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk"

# Known Session with Summary Completed
SESSION_ID = "lecture-1767568426636-4e11be" 

def get_id_token():
    try:
        cred = credentials.Certificate("classnote-api-key.json")
        try: firebase_admin.get_app()
        except: firebase_admin.initialize_app(cred)
    except:
        try: firebase_admin.get_app()
        except: firebase_admin.initialize_app()

    try:
        custom_token = auth.create_custom_token(TEST_UID).decode('utf-8')
    except:
        custom_token = auth.create_custom_token(TEST_UID) # it might be bytes or str depending on version
        if isinstance(custom_token, bytes): custom_token = custom_token.decode('utf-8')
        
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}"
    resp = requests.post(url, json={"token": custom_token, "returnSecureToken": True})
    return resp.json().get("idToken")

def verify():
    token = get_id_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    print("\n--- 1. GET /sessions/{id} ---")
    resp = requests.get(f"{BASE_URL}/sessions/{SESSION_ID}", headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        print("✅ Status 200")
        print(f"summaryStatus: {data.get('summaryStatus')}")
        print(f"quizStatus:    {data.get('quizStatus')}")
        
        # Check if fields exist
        required = ["id", "title", "summaryStatus", "quizStatus"]
        missing = [f for f in required if f not in data]
        if missing: print(f"❌ Missing fields: {missing}")
        else: print("✅ Required fields present")
    else:
        print(f"❌ Failed: {resp.status_code} {resp.text}")

    print("\n--- 2. GET /sessions/{id}/artifacts/summary (Derived) ---")
    resp = requests.get(f"{BASE_URL}/sessions/{SESSION_ID}/artifacts/summary", headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        print("✅ Status 200")
        print(f"status: {data.get('status')}")
        
        # Check Status Enum
        valid_statuses = ["pending", "running", "completed", "failed"]
        if data.get("status") in valid_statuses:
            print(f"✅ Status '{data.get('status')}' is valid Enum")
        else:
            print(f"❌ Status '{data.get('status')}' INVALID (Expected: {valid_statuses})")
    else:
        # It might be 404 if not exists, which is valid API behavior
        print(f"ℹ️ Response: {resp.status_code} (Might be 404 if not generated yet)")

    print("\n--- 3. POST /sessions/{id}/jobs (Dry Run / Test) ---")
    # We won't actually trigger a heavy job, maybe just check if Endpoint exists or returns 400 for bad input?
    # Or trigger a 'quiz' with count=1
    payload = {
        "type": "quiz",
        "params": {"count": 1},
        "idempotencyKey": "test-verify-contract-" + SESSION_ID
    }
    resp = requests.post(f"{BASE_URL}/sessions/{SESSION_ID}/jobs", json=payload, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        print("✅ Status 200")
        print(f"jobId: {data.get('jobId')}")
        print(f"status: {data.get('status')}")
        
        # Check JobResponse schema
        if "jobId" in data and "status" in data:
            print("✅ JobResponse Schema Valid (jobId, status)")
        else:
            print(f"❌ JobResponse Schema Invalid: {data}")
    else:
        print(f"❌ Failed: {resp.status_code} {resp.text}")

if __name__ == "__main__":
    verify()
