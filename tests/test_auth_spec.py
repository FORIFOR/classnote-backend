import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.main import app
from app.dependencies import get_current_user, CurrentUser, auth
from app.services.account import account_id_from_phone

client = TestClient(app)

# --- Mocks ---

@pytest.fixture
def mock_auth():
    with patch("app.dependencies.auth.verify_id_token") as mock:
        yield mock

@pytest.fixture
def mock_db():
    with patch("app.dependencies.db") as mock:
        yield mock

@pytest.fixture
def mock_account_db():
    with patch("app.routes.account.db") as mock:
        yield mock

@pytest.fixture
def mock_users_db():
    with patch("app.routes.users.db") as mock:
        yield mock

# --- 1) Normal Auth Case ---

def test_t1_valid_token_returns_200(mock_auth, mock_users_db):
    mock_auth.return_value = {
        "uid": "test_uid_001",
        "email": "test@example.com",
        "firebase": {"sign_in_provider": "google.com"}
    }
    # Mock /users/me DB lookup
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {"uid": "test_uid_001", "plan": "free"}
    mock_users_db.collection.return_value.document.return_value.get.return_value = mock_doc

    response = client.get("/users/me", headers={"Authorization": "Bearer valid_token"})
    assert response.status_code == 200
    data = response.json()
    assert data["uid"] == "test_uid_001"

def test_t2_auto_create_user(mock_auth, mock_users_db):
    mock_auth.return_value = {
        "uid": "new_user",
        "email": "new@example.com",
        "name": "New User",
        "picture": "http://pic",
        "firebase": {"sign_in_provider": "google.com"}
    }
    # First lookup fails (user doesn't exist)
    mock_doc_missing = MagicMock()
    mock_doc_missing.exists = False
    
    # Setup mock to return missing, then allow set
    mock_users_db.collection.return_value.document.return_value.get.return_value = mock_doc_missing
    
    # We can't easily assert Side Effects on "create" inside the route without more complex mocking of the route logic itself
    # But checking 200 OK implies it handled the "not found" branch by creating/returning defaults
    response = client.get("/users/me", headers={"Authorization": "Bearer valid_token"})
    assert response.status_code == 200
    assert response.json()["uid"] == "new_user"
    # Verify set called
    mock_users_db.collection.return_value.document.return_value.set.assert_called_once()

# --- 2) Abnormal Auth Case ---

def test_t4_no_header_returns_401():
    response = client.get("/users/me")
    assert response.status_code == 401

def test_t5_broken_bearer_returns_401(mock_auth):
    from app.dependencies import InvalidIdTokenError
    mock_auth.side_effect = InvalidIdTokenError("Invalid token")
    response = client.get("/users/me", headers={"Authorization": "Bearer broken"})
    assert response.status_code == 401

def test_t6_expired_token_returns_401(mock_auth):
    from app.dependencies import ExpiredIdTokenError
    mock_auth.side_effect = ExpiredIdTokenError("Expired")
    response = client.get("/users/me", headers={"Authorization": "Bearer expired"})
    assert response.status_code == 401

# --- 3) Account Separation ---

def test_t8_provider_separation(mock_auth, mock_users_db):
    # Case A: Google
    mock_auth.return_value = {"uid": "google_123", "firebase": {"sign_in_provider": "google.com"}}
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {"uid": "google_123"}
    mock_users_db.collection.return_value.document.return_value.get.return_value = mock_doc
    
    resp_google = client.get("/users/me", headers={"Authorization": "Bearer tok_g"})
    assert resp_google.json()["uid"] == "google_123"

    # Case B: Apple
    mock_auth.return_value = {"uid": "apple_456", "firebase": {"sign_in_provider": "apple.com"}}
    # Update mock for new UID
    mock_doc.to_dict.return_value = {"uid": "apple_456"}
    
    resp_apple = client.get("/users/me", headers={"Authorization": "Bearer tok_a"})
    assert resp_apple.json()["uid"] == "apple_456"
    assert resp_apple.json()["uid"] != resp_google.json()["uid"]

# --- 5) Phone Verification (Account Logic) ---

def test_t12_unverified_returns_flag(mock_auth, mock_users_db):
    mock_auth.return_value = {"uid": "u1", "firebase": {"sign_in_provider": "google.com"}} # No phone
    
    # Mock user doc exists but no link
    mock_user_doc = MagicMock()
    mock_user_doc.exists = True
    mock_user_doc.to_dict.return_value = {"uid": "u1", "createdAt": "..."}
    
    # Mock uid_link missing
    mock_link_doc = MagicMock()
    mock_link_doc.exists = False
    
    # We need to target the collection calls carefully. 
    # users.py does: 
    # 1. uid_links.document(uid).get()
    # 2. users.document(uid).get()
    
    def side_effect_col(name):
        m = MagicMock()
        if name == "uid_links":
            m.document.return_value.get.return_value = mock_link_doc
        elif name == "users":
            m.document.return_value.get.return_value = mock_user_doc
        return m
    
    mock_users_db.collection.side_effect = side_effect_col

    response = client.get("/users/me", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    assert response.json().get("needsPhoneVerification") is True

def test_t13_verify_phone_link(mock_auth, mock_account_db):
    # This tests POST /me/phone:link
    # Token MUST have phone_number
    mock_auth.return_value = {
        "uid": "u1", 
        "phone_number": "+819012345678",
        "firebase": {"sign_in_provider": "phone"}
    }
    
    # Mock DB transactions for linking
    # We expect Account creation or fetch
    
    response = client.post("/me/phone:link", headers={"Authorization": "Bearer tok"})
    
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "accountId" in response.json()
    

# --- 4) Authentication & Authorization (Session Access) ---

@pytest.fixture
def mock_sessions_db():
    with patch("app.dependencies.db") as mock: # Patching db used in dependencies
        yield mock

def test_t9_read_own_session(mock_auth, mock_sessions_db):
    # Setup: User "u1" linked to Account "a1"
    mock_auth.return_value = {"uid": "u1", "firebase": {}}
    
    # Mock Session Data: Owned by "u1" (Legacy) works naturally
    # But let's test the "Account Match" logic which is newer/stricter
    session_data = {
        "ownerUserId": "u1", 
        "ownerAccountId": "a1", 
        "title": "My Session"
    }
    
    # Dependency: ensure_can_view -> db.uid_links...
    mock_link_doc = MagicMock()
    mock_link_doc.exists = True
    mock_link_doc.to_dict.return_value = {"accountId": "a1"}
    
    # Session Doc fetch inside route
    mock_session_doc = MagicMock()
    mock_session_doc.exists = True
    mock_session_doc.to_dict.return_value = session_data
    
    # We need to mock the DB structure carefully or test the dependency directly.
    # Testing the route GET /sessions/{id} is better E2E simulation.
    
    # DB Layout: 
    # 1. sessions.document(sid).get() -> returns session_data
    # 2. ensure_can_view -> checks uid_links if needed
    
    # IMPORTANT: We need deeper mocking for `app.dependencies.db` vs `app.routes.sessions.db`
    # They should be the same object if imported from app.firebase.
    
    def side_effect(col_name):
        m = MagicMock()
        if col_name == "sessions":
            m.document.return_value.get.return_value = mock_session_doc
        elif col_name == "uid_links":
            m.document.return_value.get.return_value = mock_link_doc
        elif col_name == "users":
            u = MagicMock()
            u.exists = True
            m.document.return_value.get.return_value = u
        return m
    
    mock_sessions_db.collection.side_effect = side_effect

    with patch("app.routes.sessions.db", mock_sessions_db):
        response = client.get("/sessions/s1", headers={"Authorization": "Bearer tok"})
        
    # Since we mocked session and auth, it should pass
    # Note: depends on logic order. User u1 matches ownerUserId u1, so it might skip account check.
    # To test account check specifically, we'd make ownerUserId different but ownerAccountId same.
    assert response.status_code == 200

def test_t10_read_other_session_403(mock_auth, mock_sessions_db):
    # User "u2" tries to read "s1" owned by "u1" (Account a1)
    mock_auth.return_value = {"uid": "u2", "firebase": {}}
    
    session_data = {
        "ownerUserId": "u1", 
        "ownerAccountId": "a1", 
        "visibility": "private"
    }
    
    mock_session_doc = MagicMock()
    mock_session_doc.exists = True
    mock_session_doc.to_dict.return_value = session_data
    
    # User u2 is linked to Account a2
    mock_link_doc = MagicMock()
    mock_link_doc.exists = True
    mock_link_doc.to_dict.return_value = {"accountId": "a2"}
    
    def side_effect(col_name):
        m = MagicMock()
        if col_name == "sessions":
            m.document.return_value.get.return_value = mock_session_doc
        elif col_name == "uid_links":
            m.document.return_value.get.return_value = mock_link_doc
        return m
    
    mock_sessions_db.collection.side_effect = side_effect

    with patch("app.routes.sessions.db", mock_sessions_db):
        response = client.get("/sessions/s1", headers={"Authorization": "Bearer tok"})
    
    assert response.status_code == 403


