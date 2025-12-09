import os
import google.auth.transport.requests
import google.oauth2.service_account
import requests
import json

KEY_PATH = "classnote-api-key.json"
PROJECT = "classnote-x-dev"
LOCATION = "asia-northeast1"
MODEL = "gemini-1.5-flash"

if not os.path.exists(KEY_PATH):
    print(f"Error: {KEY_PATH} not found.")
    exit(1)

print(f"Authenticating with {KEY_PATH}...")
creds = google.oauth2.service_account.Credentials.from_service_account_file(
    KEY_PATH,
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
auth_req = google.auth.transport.requests.Request()
creds.refresh(auth_req)
token = creds.token

url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent"

print(f"Target URL: {url}")
print("Sending request...")

resp = requests.post(
    url,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]}
)

print(f"Status: {resp.status_code}")
try:
    print(json.dumps(resp.json(), indent=2))
except:
    print(resp.text)
