from unittest.mock import MagicMock, patch, ANY
import pytest
from datetime import datetime, timezone, timedelta
import sys
from fastapi import BackgroundTasks

# Mock google.cloud.firestore before importing app
sys.modules["google.cloud.firestore"] = MagicMock()
from google.cloud import firestore
from app.dependencies import CurrentUser

@pytest.fixture
def mock_user():
    return CurrentUser(
        uid="uid_test",
        email="test@example.com",
        phone_number="+819000000000",
        provider="apple"
    )

@patch("app.routes.users.db")
def test_jit_expiration_background_trigger(mock_db, mock_user):
    from app.routes.users import get_me, downgrade_account_if_expired
    import asyncio
    
    # Setup Mocks
    mock_link_ref = MagicMock()
    mock_link_doc = MagicMock()
    mock_link_doc.exists = True
    mock_link_doc.to_dict.return_value = {"accountId": "acc_001"}
    mock_link_ref.get.return_value = mock_link_doc
    
    # Account Doc (Expired)
    now = datetime.now(timezone.utc)
    expired_at = now - timedelta(hours=1)
    mock_acc_ref = MagicMock()
    mock_acc_doc = MagicMock()
    mock_acc_doc.exists = True
    mock_acc_doc.to_dict.return_value = {
        "phoneE164": "+819000000000",
        "plan": "standard",
        "planExpiresAt": expired_at,
        "credits": {}
    }
    mock_acc_ref.get.return_value = mock_acc_doc
    
    mock_phone_ref = MagicMock()
    mock_phone_doc = MagicMock()
    mock_phone_doc.exists = True
    mock_phone_doc.to_dict.return_value = {"standardOwnerUid": "uid_test"}
    mock_phone_ref.get.return_value = mock_phone_doc
    
    mock_user_ref = MagicMock()
    mock_user_doc = MagicMock()
    mock_user_doc.exists = False
    mock_user_ref.get.return_value = mock_user_doc
    
    def collection_side_effect(name):
        col = MagicMock()
        if name == "uid_links":
            col.document.return_value = mock_link_ref
        elif name == "accounts":
            col.document.return_value = mock_acc_ref
        elif name == "phone_numbers":
            col.document.return_value = mock_phone_ref
        elif name == "users":
            col.document.return_value = mock_user_ref
        return col
    
    mock_db.collection.side_effect = collection_side_effect
    
    # TEST Execution
    bg_tasks = BackgroundTasks()
    response = asyncio.run(get_me(bg_tasks, mock_user))
    
    # 1. Verify Response is FREE immediately
    assert response.plan == "free"
    
    # 2. Verify Background Task Added
    assert len(bg_tasks.tasks) == 1
    task = bg_tasks.tasks[0]
    assert task.func == downgrade_account_if_expired
    assert task.args == ("acc_001", "uid_test")

@patch("app.routes.users.db")
@patch("app.routes.users.firestore.transactional")
def test_downgrade_logic_execution(mock_transactional, mock_db):
    from app.routes.users import downgrade_account_if_expired
    
    # Passthrough decorator
    mock_transactional.side_effect = lambda x: x
    
    account_id = "acc_real"
    uid = "uid_real"
    
    # Mock Transaction (returned by db.transaction())
    txn = MagicMock()
    mock_db.transaction.return_value = txn
    
    # Mock Account Doc (Expired)
    now = datetime.now(timezone.utc)
    expired_at = now - timedelta(hours=1)
    
    mock_acc_doc = MagicMock()
    mock_acc_doc.exists = True
    mock_acc_doc.to_dict.return_value = {
        "plan": "standard",
        "planExpiresAt": expired_at
    }
    
    # Mock Ref
    mock_acc_ref = MagicMock()
    mock_acc_ref.get.return_value = mock_acc_doc
    
    # Wire up db.collection("accounts").document(account_id)
    mock_db.collection.return_value.document.return_value = mock_acc_ref
    
    # Invoke function
    downgrade_account_if_expired(account_id, uid)
    
    # Verify transaction.update called with 'free'
    args, _ = txn.update.call_args
    # args[0] is ref, args[1] is dict
    assert args[0] == mock_acc_ref
    assert args[1]["plan"] == "free"
