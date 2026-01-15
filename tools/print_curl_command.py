
import firebase_admin
from firebase_admin import auth, credentials
import requests
import os
import sys

# Configuration
API_KEY = "AIzaSyDdf_xue7WNYCFUcLVJCAiG-OUFupqyoTk" # From verify_summary_dummy.py
BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"
TEST_UID = "H2oQZPuK9EhnA9NUr6QqESNP6sa2" # Or usage owner if known

def get_id_token():
    # Init Firebase
    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("classnote-api-key.json")
            firebase_admin.initialize_app(cred)
        except:
            firebase_admin.initialize_app()

    # Mint Custom Token
    try:
        custom_token = auth.create_custom_token(TEST_UID)
        if isinstance(custom_token, bytes):
            custom_token = custom_token.decode('utf-8')
    except Exception as e:
        print(f"Error minting custom token: {e}")
        print("Ensure GOOGLE_APPLICATION_CREDENTIALS is set or classnote-api-key.json exists.")
        return None

    # Exchange for ID Token
    exchange_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}"
    payload = {"token": custom_token, "returnSecureToken": True}
    resp = requests.post(exchange_url, json=payload)
    if resp.status_code != 200:
        print(f"Error exchanging token: {resp.text}")
        return None
    return resp.json()["idToken"]

def print_command(session_id):
    token = get_id_token()
    if not token:
        return

    cmd = (
        f"curl -X POST '{BASE_URL}/sessions/{session_id}/jobs' \\\n"
        f"  -H 'Authorization: Bearer {token}' \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -d '{{\"type\": \"summary\"}}'"
    )
    print("\n# Copy and run this command:")
    print(cmd)
    print("\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 tools/print_curl_command.py <SESSION_ID>")
        # Default to the known working session for demo
        print("Example for known session:")
        print_command("lecture-1767568426636-4e11be")
    else:
        print_command(sys.argv[1])
