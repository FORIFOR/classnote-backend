
import sys
import types
from unittest.mock import MagicMock

# Mock google libraries - careful not to clobber google namespace if possible
# But locally we might not have it.
sys.modules["google.cloud"] = MagicMock()
sys.modules["google.cloud.firestore"] = MagicMock()
sys.modules["google.cloud.storage"] = MagicMock()
sys.modules["google.cloud.speech"] = MagicMock()
sys.modules["google.auth"] = MagicMock()
sys.modules["google.auth.transport"] = MagicMock()
sys.modules["google.auth.transport.requests"] = MagicMock()
sys.modules["firebase_admin"] = MagicMock()
sys.modules["firebase_admin.auth"] = MagicMock()
sys.modules["firebase_admin.credentials"] = MagicMock()
sys.modules["firebase_admin.firestore"] = MagicMock()

# Mock protobuf if missing locally
try:
    import google.protobuf
except ImportError:
    sys.modules["google.protobuf"] = MagicMock()
    sys.modules["google.protobuf.timestamp_pb2"] = MagicMock()
    sys.modules["google.protobuf.duration_pb2"] = MagicMock()

# Mock app.firebase db
# We need to ensure app.firebase imports don't crash before we can mock db?
# No, app.firebase imports google.cloud.firestore, which is mocked.
# But inside app.firebase it does: db = firestore.Client()
# Our mock returns MagicMock, so db is MagicMock.

try:
    print("Attempting to import app.main with mocks...")
    import app.main
    print("SUCCESS: app.main imported with mocks")
except Exception as e:
    print(f"CRASH: {e}")
    import traceback
    traceback.print_exc()
