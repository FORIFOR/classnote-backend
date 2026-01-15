
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
    
    print(f"Minting custom token for {TARGET_UID}...")
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

def poll_for_results(session_id, jobs_triggered):
    print("Polling for results (timeout 120s)...")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "classnote-x-dev"
    try:
        db = firestore.Client(project=project_id)
    except:
        db = firestore.Client()
        
    start_time = time.time()
    completed = set()
    
    while time.time() - start_time < 120:
        doc = db.collection("sessions").document(session_id).get()
        if not doc.exists:
            print("Session not found!")
            return
            
        data = doc.to_dict()
        
        summary = data.get("summaryMarkdown")
        quiz = data.get("quiz")
        
        if summary and "summary" not in completed:
            print(f"\n✅ Summary Generated ({len(summary)} chars):\n{summary[:100].replace(chr(10), ' ')}...\n")
            completed.add("summary")
            
        if quiz and "quiz" not in completed:
            print(f"\n✅ Quiz Generated (Count: {len(quiz.get('questions', [])) if isinstance(quiz, dict) else '?'})\n")
            completed.add("quiz")
            
        if "summary" in completed and "quiz" in completed:
            print("All requested jobs completed!")
            return

        time.sleep(5)
        
    print("Timeout reached.")

def run_trigger():
    try:
        token = get_id_token()
    except Exception as e:
        print(f"Failed to get auth token: {e}")
        return

    headers = {"Authorization": f"Bearer {token}"}
    
    print(f"Triggering Summary for {SESSION_ID}...")
    resp = requests.post(f"{BASE_URL}/sessions/{SESSION_ID}/jobs", json={"type": "summary"}, headers=headers)
    if resp.status_code == 200:
        print("Summary Job Queued.")
    else:
        print(f"Summary Start Failed: {resp.status_code} - {resp.text}")

    print(f"Triggering Quiz for {SESSION_ID}...")
    resp = requests.post(f"{BASE_URL}/sessions/{SESSION_ID}/jobs", json={"type": "quiz"}, headers=headers)
    if resp.status_code == 200:
        print("Quiz Job Queued.")
    else:
        print(f"Quiz Start Failed: {resp.status_code} - {resp.text}")
        
    poll_for_results(SESSION_ID, ["summary", "quiz"])

if __name__ == "__main__":
    run_trigger()
