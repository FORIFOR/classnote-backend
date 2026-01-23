
import os
from google.cloud import speech_v2
from google.api_core.client_options import ClientOptions

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT") or "classnote-api"
REGION = "asia-northeast1"

def list_recognizers():
    api_endpoint = f"{REGION}-speech.googleapis.com"
    client_options = ClientOptions(api_endpoint=api_endpoint)
    client = speech_v2.SpeechClient(client_options=client_options)
    
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
