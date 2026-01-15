
import firebase_admin
from firebase_admin import auth, credentials
import requests
import os
import sys

# Configuration
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk"
# Using the deployed Cloud Run URL confirmed in previous step
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TEST_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2" 
SESSION_ID = "lecture-1767568426636-4e11be" # Confirmed existing session

def get_id_token():
    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("classnote-api-key.json")
            firebase_admin.initialize_app(cred)
        except:
            firebase_admin.initialize_app()

    try:
        custom_token = auth.create_custom_token(TEST_UID)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
    except Exception as e:
        print(f"Error minting token: {e}")
        return None

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}"
    resp = requests.post(url, json={"token": custom_token, "returnSecureToken": True})
    if resp.status_code != 200:
        print(f"Auth failed: {resp.text}")
        return None
    return resp.json()["idToken"]

def test_share():
    print(f"Target Base URL: {BASE_URL}")
    
    # 1. Check AASA
    print("\n--- Checking AASA (Universal Links Config) ---")
    try:
        aasa_resp = requests.get(f"{BASE_URL}/.well-known/apple-app-site-association")
        if aasa_resp.status_code == 200:
            print("✅ AASA Found:")
            print(aasa_resp.text)
        else:
            print(f"❌ AASA Failed: {aasa_resp.status_code}")
    except Exception as e:
        print(f"❌ AASA Request Error: {e}")

    # 2. Convert Session to Share Link
    print("\n--- Generating Share Link ---")
    token = get_id_token()
    if not token:
        print("Skipping link generation due to auth failure.")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{BASE_URL}/sessions/{SESSION_ID}/share_link"
    
    # It supports GET or POST? share.py says methods=["GET", "POST"]
    # Let's use POST to create/ensure
    resp = requests.post(url, headers=headers)
    
    if resp.status_code == 200:
        data = resp.json()
        share_url = data.get("url")
        print(f"✅ Generated Link: {share_url}")
        
        if share_url.startswith(BASE_URL):
             print("   -> Looks CORRECT (Matches API Domain)")
        else:
             print("   -> Looks SUSPICIOUS (Does not match API Domain)")
             
    else:
        print(f"❌ Share Link Failed: {resp.status_code} {resp.text}")

if __name__ == "__main__":
    test_share()
