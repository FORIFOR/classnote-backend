import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import storage
try:
    from app.firebase import AUDIO_BUCKET_NAME
except ImportError:
    # Fallback if app imports fail (env vars might be needed)
    AUDIO_BUCKET_NAME = os.getenv("AUDIO_BUCKET_NAME", "classnote-x-audio")

def configure_lifecycle(bucket_name):
    print(f"Configuring lifecycle for bucket: {bucket_name}")
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    
    # Define Rule: Delete objects older than 30 days (Audio Only)
    # Applied to the entire audio bucket as per requirement.
    rules = [
        {
            "action": {"type": "Delete"},
            "condition": {"age": 30}
        }
    ]
    
    bucket.lifecycle_rules = rules
    bucket.patch()
    
    print("Lifecycle rules updated successfully:")
    for rule in bucket.lifecycle_rules:
        print(rule)

if __name__ == "__main__":
    if not AUDIO_BUCKET_NAME:
        print("Error: AUDIO_BUCKET_NAME not found")
        sys.exit(1)
        
    configure_lifecycle(AUDIO_BUCKET_NAME)
