
import os
import json
from google.cloud import speech_v2
from google.api_core.client_options import ClientOptions
from google.oauth2 import service_account

PROJECT_ID = "classnote-x-dev"
REGION = "asia-northeast1"
KEY_PATH = "classnote-api-key.json"

def list_recognizers():
    if os.path.exists(KEY_PATH):
        creds = service_account.Credentials.from_service_account_file(KEY_PATH)
    else:
        print("Key file not found")
        return

    api_endpoint = f"{REGION}-speech.googleapis.com"
    client_options = ClientOptions(api_endpoint=api_endpoint)
    client = speech_v2.SpeechClient(credentials=creds, client_options=client_options)
    
    parent = f"projects/{PROJECT_ID}/locations/{REGION}"
    print(f"Listing recognizers in {parent}...")
    
    try:
        recognizers = client.list_recognizers(parent=parent)
        for r in recognizers:
            print(f"- {r.name}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    list_recognizers()
