
import requests
import json
import os
import sys
import firebase_admin
from firebase_admin import credentials, auth
import uuid

API_BASE_URL = os.environ.get("SERVICE_URL", "https://classnote-api-mur5rvqgga-an.a.run.app")

def get_id_token():
    # Reuse valid token logic from test_image_upload.py
    api_key = None
    try:
        with open("tools/print_curl_command.py", "r") as f:
            import re
            content = f.read()
            match = re.search(r'API_KEY = "([^"]+)"', content)
            if match:
                api_key = match.group(1)
    except Exception:
        pass
        
    if not api_key:
        api_key = os.environ.get("API_KEY")
    
    if not api_key:
        print("skipped: API_KEY not set")
        sys.exit(0)
    
    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("classnote-api-key.json")
            firebase_admin.initialize_app(cred)
        except:
            firebase_admin.initialize_app()

    test_uid = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"
    try:
        custom_token = auth.create_custom_token(test_uid)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
    except Exception as e:
         print(f"Auth error: {e}")
         return None, None

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={api_key}"
    resp = requests.post(url, json={"token": custom_token, "returnSecureToken": True})
    if resp.status_code != 200:
        print(f"Auth exchange failed: {resp.text}")
        sys.exit(1)
        
    return resp.json()["idToken"], test_uid

def test_audio_url():
    print("--- Starting Audio URL Test ---")
    token, uid = get_id_token()
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Create Session
    print("[1] Creating Session...")
    sess_resp = requests.post(f"{API_BASE_URL}/sessions", json={"title":"AudioTest","mode":"lecture"}, headers=headers)
    if sess_resp.status_code != 201:
        print(f"FAIL: Create session {sess_resp.text}")
        return
    session_id = sess_resp.json()["id"]
    print(f"Session ID: {session_id}")

    # 2. Mock Audio Status (We assume Backend doesn't validate GCS existence for get_audio_url request if status is set?)
    # Wait, we can't update session audioPath via public API easily.
    # The `audio_url` endpoint relies on `audioPath` being in the doc.
    # How to set it?
    # `POST /sessions/{id}/audio/commit`? (Phase 1)
    # Or `POST /imports/youtube` (which sets audioPath).
    
    # Let's use `audio/commit` flow to look legitimate.
    # Step A: Prepare
    print("[2] Preparing Upload...")
    prep_resp = requests.post(
        f"{API_BASE_URL}/sessions/{session_id}/audio:prepareUpload",
        json={"filename": "test.m4a", "contentType": "audio/mp4", "sizeBytes": 100},
        headers=headers
    )
    if prep_resp.status_code != 200:
        print(f"FAIL: Prepare {prep_resp.text}")
        return
    prep_data = prep_resp.json()
    # Step B: Commit
    gcs_path = prep_data["storagePath"] # Was storagePath in API
    
    import hashlib
    content = b"fakeaudio"
    sha = hashlib.sha256(content).hexdigest()
    
    upload_url = prep_data["uploadUrl"]
    print(f"   Uploading dummy content to {upload_url[:50]}...")
    put_resp = requests.put(upload_url, data=content, headers={"Content-Type": "audio/mp4"})
    if put_resp.status_code not in [200, 201]:
        print(f"FAIL: Upload {put_resp.text}")
        return
        
    print("[3] Committing Audio...")
    commit_resp = requests.post(
        f"{API_BASE_URL}/sessions/{session_id}/audio:commit",
        json={
            "gcsPath": gcs_path,
            "expectedSizeBytes": len(content),
            "expectedPayloadSha256": sha
        },
        headers=headers
    )
    if commit_resp.status_code != 200:
        print(f"FAIL: Commit {commit_resp.text}")
        return

    # 3. GET audio_url
    print("[4] Requesting Audio URL...")
    url_resp = requests.get(f"{API_BASE_URL}/sessions/{session_id}/audio_url", headers=headers)
    
    if url_resp.status_code == 200:
        print("PASS: Got Audio URL")
        print(json.dumps(url_resp.json(), indent=2))
    else:
        print(f"FAIL: {url_resp.status_code} {url_resp.text}")

if __name__ == "__main__":
    test_audio_url()
