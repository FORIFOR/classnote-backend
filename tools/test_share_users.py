
import requests
import json
import os
import sys
import firebase_admin
from firebase_admin import credentials, auth, firestore

API_BASE_URL = os.environ.get("SERVICE_URL", "https://classnote-api-mur5rvqgga-an.a.run.app")

# Reuse logic from test_audio_url.py for token
def get_id_token(target_uid):
    api_key = None
    try:
        with open("tools/print_curl_command.py", "r") as f:
            import re
            content = f.read()
            match = re.search(r'API_KEY = "([^"]+)"', content)
            if match:
                api_key = match.group(1)
    except: pass
    
    if not api_key:
        api_key = os.environ.get("API_KEY")

    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("classnote-api-key.json")
            firebase_admin.initialize_app(cred)
        except:
            firebase_admin.initialize_app()
            
    try:
        custom_token = auth.create_custom_token(target_uid)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
    except Exception as e:
         print(f"Auth error: {e}")
         return None

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={api_key}"
    resp = requests.post(url, json={"token": custom_token, "returnSecureToken": True})
    return resp.json().get("idToken")

def test_share_users():
    print("--- Starting Share Users Test ---")
    
    # 1. Setup User 1 (Owner)
    uid1 = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"
    token1 = get_id_token(uid1)
    h1 = {"Authorization": f"Bearer {token1}"}
    
    # 2. Setup User 2 (Sharer - we mock this by just using a random UID we inject into Firestore)
    # We need to CREATE a User Doc for User 2 so the API can fetch it.
    # Since we have Admin SDK initialized here locally (if key exists), we can write to Firestore for setup.
    # Or just rely on upgrade script having set up User 1.
    # We can share with User 1 (Self-share?) to test fetching.
    
    # Let's create a session as User 1
    print("[1] Creating Session...")
    sess_resp = requests.post(f"{API_BASE_URL}/sessions", json={"title":"ShareTest","mode":"lecture"}, headers=h1)
    if sess_resp.status_code != 201:
        print(f"FAIL: Create {sess_resp.text}")
        return
    session_id = sess_resp.json()["id"]
    print(f"Session: {session_id}")
    
    # 3. Add User 1 to sharedWith list (Backdoor via direct DB update? Or proper API?)
    # Proper API: `PUT /sessions/{id}/members/{uid}` or `POST /sessions/{id}/invite`?
    # `POST /invite` works.
    # But we can't invite ourselves if we are owner.
    # Let's invite a fake UID "fake-user-123".
    # And we need to CREATE a User Doc for "fake-user-123" so fetching works.
    
    # Direct DB Write using Admin SDK (Simulating existing user)
    try:
        db = firestore.client()
        fake_uid = "fake-user-123"
        print("[2] Creating Fake User Doc...")
        db.collection("users").document(fake_uid).set({
            "displayName": "Fake User",
            "username": "fakeuser",
            "photoUrl": "http://example.com/fake.jpg",
            "isShareable": True
        })
        
        # Now Share (Invite)
        print("[3] Adding Member (Invite)...")
        mem_resp = requests.post(
            f"{API_BASE_URL}/sessions/{session_id}/share:invite",
            json={"userId": fake_uid, "role": "viewer"},
            headers=h1
        )
        if mem_resp.status_code != 200:
            print(f"FAIL: Invite Member {mem_resp.text}")
            return
            
    except Exception as e:
        print(f"Direct DB setup failed (Key missing?): {e}")
        return

    # 4. Call GET shared_with_users
    print("[4] Fetching Shared Users...")
    share_resp = requests.get(f"{API_BASE_URL}/sessions/{session_id}/shared_with_users", headers=h1)
    
    if share_resp.status_code == 200:
        data = share_resp.json()
        print(json.dumps(data, indent=2))
        # Verify
        found = False
        for u in data:
            if u["uid"] == "fake-user-123":
                found = True
                if u.get("username") == "fakeuser":
                    print("PASS: Username correct")
                else:
                    print("FAIL: Username missing/wrong")
        if found:
            print("PASS: Fake user found")
        else:
            print("FAIL: Fake user not in list")
    else:
        print(f"FAIL: {share_resp.status_code} {share_resp.text}")

if __name__ == "__main__":
    test_share_users()
