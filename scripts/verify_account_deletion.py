
import asyncio
import os
import sys
import logging
import uuid
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore, storage
from firebase_admin import auth

# Initialize Firebase
from app.firebase import db, storage_client, AUDIO_BUCKET_NAME, MEDIA_BUCKET_NAME
from app.services.account_deletion import REQUESTS_COLLECTION, LOCKS_COLLECTION

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_deletion")

TEST_UID = f"verify_del_{uuid.uuid4().hex[:8]}"
TEST_EMAIL = f"{TEST_UID}@example.com"
TEST_SESSION_ID = f"sess_{uuid.uuid4().hex[:8]}"

async def setup_test_data():
    logger.info(f"--- Setting up test user {TEST_UID} ---")
    
    # 1. Create User Doc
    user_ref = db.collection("users").document(TEST_UID)
    user_ref.set({
        "uid": TEST_UID,
        "email": TEST_EMAIL,
        "emailLower": TEST_EMAIL,
        "displayName": "Deletion Tester",
        "createdAt": datetime.now(timezone.utc),
        "plan": "free"
    })
    
    # 2. Create Session Doc
    session_ref = db.collection("sessions").document(TEST_SESSION_ID)
    session_ref.set({
        "ownerUserId": TEST_UID,
        "title": "Deletion Test Session",
        "createdAt": datetime.now(timezone.utc)
    })
    
    # 3. Create dummy file in GCS
    try:
        bucket = storage_client.bucket(AUDIO_BUCKET_NAME)
        blob = bucket.blob(f"sessions/{TEST_SESSION_ID}/test_audio.txt")
        blob.upload_from_string("dummy audio content")
        logger.info("Created dummy GCS object")
    except Exception as e:
        logger.warning(f"Could not create GCS object (auth issue?): {e}")

    return TEST_UID

async def simulate_deletion_request(uid):
    logger.info(f"--- Simulating Deletion Request for {uid} ---")
    # Simulate logic from users.py delete_me
    from app.services.account_deletion import deletion_schedule_at, deletion_lock_id
    
    now = datetime.now(timezone.utc)
    delete_after = deletion_schedule_at(now)
    
    # Create Request
    req_ref = db.collection(REQUESTS_COLLECTION).document(uid)
    req_ref.set({
        "uid": uid,
        "email": TEST_EMAIL,
        "status": "requested",
        "requestedAt": now,
        "deleteAfterAt": delete_after
    })
    
    # Create Lock
    lock_id = deletion_lock_id(TEST_EMAIL, "email")
    db.collection(LOCKS_COLLECTION).document(lock_id).set({
        "uid": uid,
        "status": "requested"
    })
    
    # Update User
    db.collection("users").document(uid).update({
        "deletionStatus": "requested",
        "deletionScheduledAt": delete_after
    })
    logger.info("Deletion requested recorded.")

async def verify_nuke(uid):
    logger.info(f"--- Verifying Nuke for {uid} ---")
    
    # 1. Check User Doc
    u_snap = db.collection("users").document(uid).get()
    if u_snap.exists:
        logger.error("FAIL: User document still exists!")
    else:
        logger.info("PASS: User document deleted.")
        
    # 2. Check Session Doc
    s_snap = db.collection("sessions").document(TEST_SESSION_ID).get()
    if s_snap.exists:
        logger.error("FAIL: Session document still exists!")
    else:
        logger.info("PASS: Session document deleted.")

    # 3. Check GCS
    try:
        bucket = storage_client.bucket(AUDIO_BUCKET_NAME)
        blob = bucket.blob(f"sessions/{TEST_SESSION_ID}/test_audio.txt")
        if blob.exists():
             logger.error("FAIL: GCS object still exists!")
        else:
             logger.info("PASS: GCS object deleted.")
    except Exception:
        pass

async def main():
    try:
        # 1. Setup
        uid = await setup_test_data()
        
        # 2. Request
        await simulate_deletion_request(uid)
        
        # 3. Fast Forward (Force expiry)
        logger.info("--- Fast Forwarding Time ---")
        past = datetime.now(timezone.utc) - timedelta(days=1)
        db.collection(REQUESTS_COLLECTION).document(uid).update({
            "deleteAfterAt": past
        })
        
        # 4. Run Sweeper
        logger.info("--- Running Sweeper ---")
        from app.routes.tasks import handle_account_deletion_sweep
        
        # Monkey patch enqueue_nuke_user_task in app.task_queue
        import app.task_queue as task_queue_module
        
        original_enqueue = task_queue_module.enqueue_nuke_user_task
        
        from app.routes.tasks import handle_nuke_user_task
        # Need to patch Client in google.cloud.firestore/storage or mock them globally
        import google.cloud.firestore
        import google.cloud.storage
        
        original_firestore_client = google.cloud.firestore.Client
        original_storage_client = google.cloud.storage.Client
        
        # Mock Clients to return our initialized (or mock) db
        # Note: handle_nuke_user_task creates NEW client instances.
        # We want it to use our 'db' and 'storage_client' from app.firebase
        
        def mock_fs_client(*args, **kwargs):
            return db
            
        def mock_storage_client(*args, **kwargs):
            return storage_client

        def mock_enqueue(target_uid):
            logger.info(f"Mock Enqueue Nuke for {target_uid}")
            # Run the nuke handler directly
            async def run_nuke():
                class MockRequest:
                    async def json(self):
                        return {"userId": target_uid}
                try:
                    await handle_nuke_user_task(MockRequest())
                except Exception as e:
                    logger.error(f"Nuke Task Failed: {e}")
            
            asyncio.create_task(run_nuke())

        try:
            task_queue_module.enqueue_nuke_user_task = mock_enqueue
            google.cloud.firestore.Client = mock_fs_client
            google.cloud.storage.Client = mock_storage_client
            
            # Run Sweep
            res = await handle_account_deletion_sweep()
            logger.info(f"Sweep Result: {res}")
            
            # Wait a bit for async nuke task
            await asyncio.sleep(5)
            
        finally:
            task_queue_module.enqueue_nuke_user_task = original_enqueue
            google.cloud.firestore.Client = original_firestore_client
            google.cloud.storage.Client = original_storage_client

        # 5. Verify
        await verify_nuke(uid)
        
    except Exception as e:
        logger.exception(f"Test Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
