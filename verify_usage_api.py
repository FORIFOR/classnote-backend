
import sys
import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

# --- 1. Mock External Dependencies (Before imports) ---
# We need to mock google.cloud.firestore specifically because app.routes.users imports it
mock_firestore_module = MagicMock()
sys.modules["google.cloud"] = MagicMock()
sys.modules["google.cloud.firestore"] = mock_firestore_module
sys.modules["firebase_admin"] = MagicMock()
sys.modules["app.firebase"] = MagicMock()

# Mock Config/Env-dependent services
# app.services.cost_guard needs to be mocked BEFORE app.routes.users imports it
mock_cost_guard_module = MagicMock()
mock_cost_guard_service = MagicMock()
mock_cost_guard_module.cost_guard = mock_cost_guard_service
sys.modules["app.services.cost_guard"] = mock_cost_guard_module

# Also mock apple_service to avoid import errors in users.py
sys.modules["app.services.apple"] = MagicMock()

# --- 2. Import Target Router ---
# Now we can safely import app.routes.users
from app.routes.users import router
from app.dependencies import get_current_user, User
# from app.util_models import User # Wrong location

# --- 3. Setup Test App ---
app = FastAPI()
app.include_router(router)

# --- 4. Setup Mocks ---

# Mock User Document in Firestore
mock_user_doc = MagicMock()
mock_user_doc.exists = True
mock_user_doc.to_dict.return_value = {
    "plan": "free",
    "displayName": "Test User",
    "serverSessionCount": 2,
    "cloud_sessions_started": 0
}

# Mock Firestore DB lookups
# db.collection("users").document(uid).get()
mock_db = sys.modules["app.firebase"].db
mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc

# Mock CostGuard.get_usage_report (The core of what we are testing)
# We mock the return value directly to verify the API passes it through
expected_usage_report = {
    "limitSeconds": 1800.0,
    "usedSeconds": 120.0,
    "remainingSeconds": 1680.0,
    "sessionLimit": 3,
    "sessionsStarted": 1,
    "canStart": True,
    "reasonIfBlocked": None
}
mock_cost_guard_service.get_usage_report = AsyncMock(return_value=expected_usage_report)

# --- 5. Override Auth Dependency ---
def mock_get_current_user():
    user = MagicMock(spec=User)
    user.uid = "test-uid-123"
    user.email = "test@example.com"
    user.display_name = "Test User"
    return user

app.dependency_overrides[get_current_user] = mock_get_current_user

# --- 6. Run Test ---
def test_api():
    client = TestClient(app)
    print("Sending GET /users/me ...")
    response = client.get("/users/me")
    
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print("\n--- Response Body ---")
        # Pretty print subset
        print(f"UID: {data.get('uid')}")
        print(f"Plan: {data.get('plan')}")
        
        cloud = data.get("cloud")
        if cloud:
            print("\n[SUCCESS] 'cloud' object found:")
            print(cloud)
            
            # Verify values match our mock source
            if cloud["limitSeconds"] == 1800.0:
                print(">> Verification PASSED: Values match CostGuard mock.")
            else:
                print(">> Verification FAILED: Values mismatch.")
        else:
            print("\n[FAILURE] 'cloud' object is MISSING in response.")
    else:
        print(f"Error: {response.text}")

if __name__ == "__main__":
    test_api()
