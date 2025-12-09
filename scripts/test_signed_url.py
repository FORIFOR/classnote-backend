import os
import sys
from google.cloud import storage
from datetime import timedelta

# Setup Envs
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/Users/horioshuuhei/Projects/classnote-api/classnote-api-key.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "classnote-x-dev"

def test_sign_url(bucket_name, blob_name):
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        
        print(f"Generating URL for gs://{bucket_name}/{blob_name}...")
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=1),
            method="GET",
        )
        print("Success!")
        print(url)
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    test_sign_url("classnote-x-audio", "sessions/lecture-1765081270856-54858c/audio.raw")
