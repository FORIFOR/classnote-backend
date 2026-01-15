import sys
import os
import unittest.mock

# Mock google-cloud packages to avoid environment errors locally
sys.modules["google.cloud"] = unittest.mock.MagicMock()
sys.modules["google.cloud.firestore"] = unittest.mock.MagicMock()
sys.modules["firebase_admin"] = unittest.mock.MagicMock()
sys.modules["firebase_admin.firestore"] = unittest.mock.MagicMock()

print("Attempting to import app.routes.usage...")
try:
    from app.routes import usage
    print("SUCCESS: app.routes.usage imported.")
except ImportError as e:
    print(f"FAILED: ImportError: {e}")
except Exception as e:
    print(f"FAILED: Exception: {e}")
    import traceback
    traceback.print_exc()

print("\nAttempting to import app.main...")
try:
    from app import main
    if hasattr(main, 'usage_router_available'):
        print(f"app.main.usage_router_available = {main.usage_router_available}")
    else:
        print("app.main.usage_router_available not found (old version?)")
except Exception as e:
    print(f"FAILED: app.main import error: {e}")
    import traceback
    traceback.print_exc()
