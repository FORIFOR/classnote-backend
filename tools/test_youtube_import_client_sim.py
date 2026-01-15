
import requests
import json
import time
import os
import firebase_admin
from firebase_admin import auth, credentials

# --- Configuration ---
# BASE_URL = "http://localhost:8080" # Local
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TEST_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2" 
API_KEY_PATH = "classnote-api-key.json"

# Mock Transcript (Simulating Client Fetch)
VIDEO_URL = "https://www.youtube.com/watch?v=M7FIvfx5J10"
TRANSCRIPT_TEXT = """
[00:00:00] みなさん、こんにちは。
[00:00:05] 本日は、新しいプロジェクトについて説明します。
[00:00:10] このプロジェクトは、効率的な学習を支援するためのものです。
[00:00:20] 具体的には、AIを活用して自動的に要約とクイズを作成します。
"""

def get_id_token():
    if not firebase_admin._apps:
        cred = credentials.Certificate(API_KEY_PATH) if os.path.exists(API_KEY_PATH) else None
        firebase_admin.initialize_app(cred)
    
    # Create Custom Token -> Exchange for ID Token
    custom_token = auth.create_custom_token(TEST_UID)
    
    # Exchange custom token for ID token using Firebase Auth REST API (requires Web API Key)
    # Since we might not have Web API Key easily, we assume the user has a valid ID Token 
    # OR we use a helper if available.
    # For now, let's try to grab one from a local helper or environment variable.
    # Simpler: Use the print_curl_command logic if possible, or just assume we have one.
    
    # Actually, simpler approach for this script:
    # Use the `print_curl_command.py` logic if available.
    # But since I don't want to depend on external keys for this script if possible...
    # I'll paste the logic from print_curl_command.py
    
    # To properly exchange, we need the Web API Key.
    # If not available, we can't easily get ID Token without client SDK.
    # I'll rely on the existing logic in `tools/print_curl_command.py` which seems to have an API KEY.
    # Wait, `print_curl_command.py` has `API_KEY = "..."`. I can copy it.
    pass

# Hardcoded from print_curl_command.py (User provided this file previously)
WEB_API_KEY = "AIzaSy..." # I need to check print_curl_command.py content again to get the key.
# I will check it in a moment. For now, I'll assume I can read it.

def get_id_token_real():
    # Use the existing tool logic
    import subprocess
    result = subprocess.run(["python3", "tools/print_curl_command.py"], capture_output=True, text=True)
    # The output format is: curl -H "Authorization: Bearer <TOKEN>" ...
    # Parse valid token
    import re
    match = re.search(r"Bearer ([a-zA-Z0-9.\-_]+)", result.stdout)
    if match:
        return match.group(1)
    raise ValueError("Could not get ID Token from tools/print_curl_command.py")

def test_client_import():
    print(f"--- Verify YouTube Import (Client Sim) ---")
    
    token = get_id_token_real()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # 1. Import with Transcript
    print(f"\n1. Requesting Import with Transcript...")
    payload = {
        "url": VIDEO_URL,
        "mode": "lecture",
        "language": "ja",
        "transcriptText": TRANSCRIPT_TEXT,
        "transcriptLang": "ja",
        "source": "youtube_captions"
    }
    
    res = requests.post(f"{BASE_URL}/imports/youtube", json=payload, headers=headers)
    if res.status_code != 200:
        print(f"❌ Failed: {res.status_code} {res.text}")
        return

    data = res.json()
    session_id = data.get("sessionId")
    print(f"✅ Created Session: {session_id}")
    print(f"   Status: {data.get('transcriptStatus')}") # processing?
    
    # 2. Poll for Status (Expecting 'recording_finished' or later)
    print(f"\n2. Polling for Completion (Timeout 60s)...")
    for _ in range(30):
        time.sleep(2)
        res_s = requests.get(f"{BASE_URL}/sessions/{session_id}", headers=headers)
        if res_s.status_code != 200:
            print(f"   Get Session Failed: {res_s.status_code}")
            continue
            
        sess = res_s.json()
        status = sess.get("status")
        summary_status = sess.get("summaryStatus")
        quiz_status = sess.get("quizStatus")
        trans_len = len(sess.get("transcriptText") or "")
        
        playlist_status = sess.get("playlistStatus")
        
        print(f"   Status: {status} | Summary: {summary_status} | Quiz: {quiz_status} | Playlist: {playlist_status}")
        
        if trans_len > 0 and summary_status != "pending" and summary_status != "running" and playlist_status != "pending" and playlist_status != "running":
             # Success Condition: Transcript Saved AND Summary/Playlist DONE
             print(f"✅ Success! Transcript saved and tasks triggered.")
             print(f"   Summary Status: {summary_status}")
             print(f"   Quiz Status: {quiz_status}")
             print(f"   Playlist Status: {playlist_status}")
             print(f"   Playlist Content: {json.dumps(sess.get('playlist') or [], ensure_ascii=False)[:100]}...")
             return
             
    print("❌ Timeout waiting for tasks.")

if __name__ == "__main__":
    test_client_import()
