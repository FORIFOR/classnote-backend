import pytest
from unittest.mock import MagicMock, patch, ANY
import sys

# Mock google.cloud.firestore before importing app
sys.modules["google.cloud.firestore"] = MagicMock()
from google.cloud import firestore
from app.dependencies import CurrentUser

@pytest.fixture
def mock_user():
    return CurrentUser(
        uid="uid_b",
        email="test@example.com",
        phone_number="+819000000000",
        provider="line"
    )

@patch("app.routes.users.db")
@patch("app.routes.users.firestore.transactional")
def test_claim_username_updates_profile(mock_transactional, mock_db, mock_user):
    # Make transactional decorator a passthrough
    mock_transactional.side_effect = lambda x: x
    
    from app.routes.users import claim_username, ClaimUsernameRequest
    
    # 1. Setup Data
    mock_user_doc = MagicMock()
    mock_user_doc.exists = True
    mock_user_doc.to_dict.return_value = {"uid": "uid_b", "displayName": "User B"}
    
    mock_claim_doc = MagicMock()
    mock_claim_doc.exists = False
    
    # 3. Setup Transaction Object
    transaction = MagicMock()
    # Note: user_ref.get(transaction=txn) calls return Mocks by default.
    # We must configure the refs returned by db.collection...
    
    # Create specific mocks for refs
    mock_user_ref = MagicMock()
    mock_user_ref.get.return_value = mock_user_doc
    
    mock_claim_ref = MagicMock()
    mock_claim_ref.get.return_value = mock_claim_doc
    
    # Configure db.collection(...).document(...) dispatch
    def doc_side_effect(arg):
        # This is called on .document(arg)
        # But wait, db.collection(col).document(doc)
        # We need to mock the chain.
        return MagicMock() 

    # Simpler: mock db.collection side effect
    def collection_side_effect(name):
        col_mock = MagicMock()
        if name == "users":
            col_mock.document.return_value = mock_user_ref
        elif name == "username_claims":
            col_mock.document.return_value = mock_claim_ref
        return col_mock
    
    mock_db.collection.side_effect = collection_side_effect
    
    # Also ensure transaction.set captures calls from these refs
    # When code calls transaction.set(claim_ref, ...), claim_ref is our mock_claim_ref
    # Since we mocked the decorator to pass through, `txn` is the raw function.
    # The code does:
    # transaction = db.transaction()
    # txn(transaction)
    
    mock_db.transaction.return_value = transaction
    
    # 4. Call Endpoint
    req = ClaimUsernameRequest(username="TestUser123")
    
    import asyncio
    asyncio.run(claim_username(req, mock_user))
    
    # 5. Verify Updates
    # We expect 2 sets. One for claim, one for user.
    # We specifically want to check the User update for "hasUsername": True
    
    # Find the call to user_ref
    user_calls = [call for call in transaction.set.call_args_list if call[0][0] is mock_user_ref]
    assert len(user_calls) > 0, "User document was not updated"
    
    args, kwargs = user_calls[0]
    update_data = args[1]
    
    assert update_data["username"] == "testuser123"
    assert update_data["hasUsername"] is True, "hasUsername field is missing from update!"
