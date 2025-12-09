import sys
from unittest.mock import MagicMock

# Mock google.cloud modules to avoid ImportError
sys.modules["google.cloud"] = MagicMock()
sys.modules["google.cloud.firestore"] = MagicMock()
sys.modules["google.cloud.storage"] = MagicMock()
sys.modules["google.auth"] = MagicMock()
sys.modules["vertexai"] = MagicMock()
sys.modules["vertexai.generative_models"] = MagicMock()

# Mock env vars
import os
os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
sys.path.append(".")  # Add current dir to path

print("Attempting to import app.main...")

try:
    from app.main import app
    print("Successfully imported app.main")
    print("FastAPI app initialized:", app)
    
    # Check if OpenAPI schema can be generated
    print("Generating OpenAPI schema...")
    schema = app.openapi()
    print("OpenAPI schema generated successfully")
    
except Exception as e:
    print(f"FAILED to import app.main or generate schema: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
