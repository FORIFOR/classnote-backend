
import asyncio
import os
import sys
import logging
import uuid
from datetime import datetime, timezone

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore
from app.firebase import db
from app.util_models import ChatCreateRequest, SetReactionRequest

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_chat_reaction")

TEST_UID = f"chat_user_{uuid.uuid4().hex[:8]}"
TEST_SESSION_ID = f"sess_chat_{uuid.uuid4().hex[:8]}"

async def setup_test_data():
    logger.info(f"--- Setting up test user {TEST_UID} ---")
    
    # 1. Create User Doc with Photo
    user_ref = db.collection("users").document(TEST_UID)
    user_ref.set({
        "uid": TEST_UID,
        "displayName": "Chat Tester",
        "photoUrl": "https://example.com/photo.jpg",
        "createdAt": datetime.now(timezone.utc),
        "plan": "premium"
    })
    
    # 2. Create Session Doc
    session_ref = db.collection("sessions").document(TEST_SESSION_ID)
    session_ref.set({
        "ownerUid": TEST_UID,
        "title": "Chat Test Session",
        "createdAt": datetime.now(timezone.utc)
    })
    
    return TEST_UID

async def test_chat_flow(uid):
    logger.info("--- Testing Chat Flow ---")
    from app.routes.sessions import create_chat_message, get_chat_messages
    from app.dependencies import User
    
    mock_user = User(uid=uid, display_name="Chat Tester", photo_url="https://example.com/photo.jpg")
    
    # 1. Post Message
    req = ChatCreateRequest(text="Hello world!")
    msg = await create_chat_message(TEST_SESSION_ID, req, mock_user)
    logger.info(f"Chat Message Created: {msg}")
    assert msg.text == "Hello world!"
    assert msg.userName == "Chat Tester"
    assert msg.userPhotoUrl == "https://example.com/photo.jpg"
    
    # 2. Get Messages
    res = await get_chat_messages(TEST_SESSION_ID, mock_user)
    logger.info(f"Chat Messages Fetched: {len(res.messages)} messages")
    assert len(res.messages) >= 1
    assert res.messages[0].text == "Hello world!"

async def test_reaction_flow(uid):
    logger.info("--- Testing Reaction Flow ---")
    from app.routes.reactions import set_reaction, get_reaction_state
    from app.dependencies import User
    
    mock_user = User(uid=uid)
    
    # 1. Set Reaction
    req = SetReactionRequest(emoji="🔥")
    await set_reaction(TEST_SESSION_ID, req, mock_user)
    logger.info("Reaction set to 🔥")
    
    # 2. Get Reaction State
    state = await get_reaction_state(TEST_SESSION_ID, mock_user)
    logger.info(f"Reaction State: {state}")
    assert state.myEmoji == "🔥"
    assert state.users is not None
    assert state.users.get(uid) == "🔥"

async def main():
    try:
        uid = await setup_test_data()
        await test_chat_flow(uid)
        await test_reaction_flow(uid)
        logger.info("--- ALL TESTS PASSED ---")
    except Exception as e:
        logger.exception(f"Test Failed: {e}")
    finally:
        # Cleanup
        logger.info("Cleaning up...")
        db.collection("users").document(TEST_UID).delete()
        # Session subcollections cleanup simplified for test
        sess_ref = db.collection("sessions").document(TEST_SESSION_ID)
        for sub in ["chat_messages", "reactions"]:
            docs = sess_ref.collection(sub).stream()
            for d in docs: d.reference.delete()
        sess_ref.delete()

if __name__ == "__main__":
    asyncio.run(main())
