import sys
import os

# Mock google-cloud packages not available locally
import unittest.mock
sys.modules["google.cloud"] = unittest.mock.MagicMock()
sys.modules["google.cloud.firestore"] = unittest.mock.MagicMock()
sys.modules["firebase_admin"] = unittest.mock.MagicMock()
sys.modules["firebase_admin.firestore"] = unittest.mock.MagicMock()

try:
    from app import main
    print("Import successful")
except Exception as e:
    print(f"Import failed: {e}")
    import traceback
    traceback.print_exc()
