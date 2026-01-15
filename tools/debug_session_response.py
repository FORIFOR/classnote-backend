
import firebase_admin
from firebase_admin import auth, credentials
import requests
import json
import os
import sys

# Configuration
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk"
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TARGET_UID = "qyp2anOl43RC94LSrsknE9cH0hA3" # Owner from dump
SESSION_ID = "lecture-1767485056140-1d2fc3"

def get_id_token():
    key_path = "classnote-api-key.json"
    cred = None
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        cred = credentials.ApplicationDefault()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
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

def check_api_names():
    try:
        token = get_id_token()
        headers = {"Authorization": f"Bearer {token}"}
        
        print(f"Fetching GET /sessions/{SESSION_ID} ...")
        resp = requests.get(f"{BASE_URL}/sessions/{SESSION_ID}", headers=headers)
        
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} - {resp.text}")
            return

        data = resp.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
        
    except Exception as e:
        print(f"Request Error: {e}")

if __name__ == "__main__":
    check_api_names()
