
import firebase_admin
from firebase_admin import auth, credentials
import requests
import time
import sys
import json

# Configuration from print_curl_command.py
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk" 
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TEST_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2" 

# Japan PM Office (3m) - Should be open?
VIDEO_URL = "https://www.youtube.com/watch?v=M7FIvfx5J10" 
LANGUAGE = "ja"

def get_id_token():
    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("classnote-api-key.json")
            firebase_admin.initialize_app(cred)
        except:
            # Fallback if no key file, might fail in local env if not authenticated
            try:
                firebase_admin.initialize_app()
            except ValueError:
                pass

    try:
        custom_token = auth.create_custom_token(TEST_UID)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
        return None

    # Exchange for ID Token
    exchange_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}"
    payload = {"token": custom_token, "returnSecureToken": True}
    resp = requests.post(exchange_url, json=payload)
    if resp.status_code != 200:
        print(f"[AUTH ERROR] Exchange failed: {resp.text}")
        return None
    return resp.json()["idToken"]

def main():
    print(f"--- Verify YouTube Import (Real API) ---")
    print(f"Video: {VIDEO_URL} ({LANGUAGE})")
    
    token = get_id_token()
    if not token:
        print("Failed to get ID Token. Aborting.")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # 1. POST /imports/youtube
    print("\n1. Requesting Import...")
    data = {
        "url": VIDEO_URL,
        "mode": "lecture",
        "language": LANGUAGE,
        "title": "Verify YouTube Import"
    }
    resp = requests.post(f"{BASE_URL}/imports/youtube", json=data, headers=headers)
    if resp.status_code != 200:
        print(f"❌ Failed: {resp.status_code} {resp.text}")
        return
    
    res_json = resp.json()
    session_id = res_json["sessionId"]
    print(f"✅ Created Session: {session_id}")
    print(f"   Status: {res_json.get('transcriptStatus')}")

    # 2. Poll for Completion
    print("\n2. Polling for Completion (Timeout 180s)...")
    start_time = time.time()
    while time.time() - start_time < 180:
        r = requests.get(f"{BASE_URL}/sessions/{session_id}", headers=headers)
        if r.status_code != 200:
            print(f"❌ Polling Failed: {r.status_code}")
            break
        
        sess = r.json()
        status = sess.get("status")
        transcript = sess.get("transcriptText")
        
        # Check success criteria
        # New logic: status can be "failed" or changed to "recording_finished"
        # Or check length of transcript
        ts_len = len(transcript) if transcript else 0
        
        sys.stdout.write(f"\r   [{int(time.time()-start_time)}s] Status: {status} | Transcript Len: {ts_len}")
        sys.stdout.flush()

        if status == "failed":
            print(f"\n❌ Import Failed: {sess.get('errorMessage')}")
            break
        
        if transcript and len(transcript) > 10 and status != "queued":
            print(f"\n✅ Success! Transcript generated.")
            print("-" * 40)
            print(transcript[:500] + "..." if len(transcript) > 500 else transcript)
            print("-" * 40)
            return

        time.sleep(5)
    
    print("\n❌ Timeout waiting for transcript.")

if __name__ == "__main__":
    main()
