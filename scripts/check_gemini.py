import os
import sys

sys.path.append(os.getcwd())

# Setup Envs
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/Users/horioshuuhei/Projects/classnote-api/classnote-api-key.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "classnote-x-dev"
os.environ["VERTEX_REGION"] = "asia-northeast1"

MODELS = [
    "gemini-1.5-flash",
    "gemini-2.0-flash-exp",
    "gemini-2.0-flash-lite-preview-02-05", # Try specific preview
    "gemini-1.5-flash-002",
]

sys.path.append(os.getcwd())
# Re-import to ensure env vars are picked up if module caches (not issue here as script runs once)
try:
    from app.gemini_client import summarize_transcript
except ImportError:
    # Quick fix if import fails due to path issues in some envs
    pass

import vertexai
from vertexai.generative_models import GenerativeModel

vertexai.init(project="classnote-x-dev", location="us-central1")

for model_name in MODELS:
    print(f"--- Testing {model_name} ---")
    try:
        model = GenerativeModel(model_name)
        response = model.generate_content("Hello")
        print(f"✅ Success with {model_name}!")
        print(response.text[:50])
        break
    except Exception as e:
        print(f"❌ Failed {model_name}: {e}")

