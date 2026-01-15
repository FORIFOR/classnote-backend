
import requests
import json
import os
import sys
import firebase_admin
from firebase_admin import credentials, auth

API_BASE_URL = os.environ.get("SERVICE_URL", "https://classnote-api-mur5rvqgga-an.a.run.app")
# Use the same token fetching logic as test_asset_flow.py if possible, or simple hardcode if we have a way.
# Let's borrow the token logic from `set_custom_claims.py` style or just use `verify_api_e2e.py` utils.
# For simplicity, assuming running in same environment as other tools.

def get_id_token():
    # Scrape API KEY from print_curl_command.py
    api_key = None
    try:
        with open("tools/print_curl_command.py", "r") as f:
            import re
            content = f.read()
            match = re.search(r'API_KEY = "([^"]+)"', content)
            if match:
                api_key = match.group(1)
    except Exception as e:
        print(f"Failed to load API KEY from tools: {e}")
        
    if not api_key:
        api_key = os.environ.get("API_KEY")

    if not api_key:
        print("skipped: API_KEY not set")
        sys.exit(0)
    
    # Init Firebase Admin
    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("classnote-api-key.json")
            firebase_admin.initialize_app(cred)
        except:
            firebase_admin.initialize_app()

    # Create Custom Token for Test User
    test_uid = "test-user-image-upload"
    try:
        custom_token = auth.create_custom_token(test_uid)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
    except Exception as e:
         print(f"Auth error: {e}")
         print("Ensure classnote-api-key.json exists.")
         return None, None

    # Exchange for ID Token
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={api_key}"
    resp = requests.post(url, json={"token": custom_token, "returnSecureToken": True})
    if resp.status_code != 200:
        print(f"Auth exchange failed: {resp.text}")
        sys.exit(1)
        
    return resp.json()["idToken"], test_uid

def test_image_upload():
    print("--- Starting Image Upload Test ---")
    
    # 1. Auth
    try:
        token, uid = get_id_token()
        print(f"Authenticated as {uid}")
    except Exception as e:
        # Fallback if API_KEY missing (local dev env maybe?)
        print(f"Auth error: {e}")
        return

    headers = {"Authorization": f"Bearer {token}"}

    # 2. Create Session
    print("[1] Creating Session...")
    sess_resp = requests.post(
        f"{API_BASE_URL}/sessions", 
        json={"title": "ImageTest", "mode": "lecture", "status": "録音中"},
        headers=headers
    )
    if sess_resp.status_code != 201:
        print(f"FAIL: Create session failed {sess_resp.text}")
        return
    session_id = sess_resp.json()["id"]
    print(f"Session ID: {session_id}")

    # 3. Get Upload URL
    print("[2] Requesting Upload URL...")
    req_body = {"contentType": "image/jpeg"}
    url_resp = requests.post(
        f"{API_BASE_URL}/sessions/{session_id}/image_notes/upload_url",
        json=req_body,
        headers=headers
    )
    if url_resp.status_code != 200:
        print(f"FAIL: Get Upload URL failed {url_resp.status_code} {url_resp.text}")
        return
    
    data = url_resp.json()
    upload_url = data["uploadUrl"]
    image_id = data["imageId"]
    print(f"Got Upload URL for {image_id}")

    # 4. Perform Upload
    print("[3] Uploading Dummy Image...")
    # Create dummy image data
    dummy_img = b"fake_image_content"
    put_resp = requests.put(upload_url, data=dummy_img, headers={"Content-Type": "image/jpeg"})
    
    if put_resp.status_code not in [200, 201]:
        # GCS XML API returns 200 or 201 usually
        print(f"FAIL: Upload failed {put_resp.status_code} {put_resp.text}")
        return
    
    print("Upload Success")

    # 5. List Image Notes (Verify Persistence)
    print("[4] Verifying Listing...")
    list_resp = requests.get(
        f"{API_BASE_URL}/sessions/{session_id}/image_notes",
        headers=headers
    )
    if list_resp.status_code != 200:
        print(f"FAIL: List failed {list_resp.text}")
        return
        
    items = list_resp.json()
    found = any(i["id"] == image_id for i in items)
    if found:
        print("PASS: Image found in list")
    else:
        print("FAIL: Image NOT found in list")
        print(json.dumps(items, indent=2))

if __name__ == "__main__":
    if "API_KEY" not in os.environ:
        # Try to load from .env if possible or warn
        # For this environment, we know API_KEY was used in previous tests.
        pass
    test_image_upload()
