import asyncio
import uuid
import logging
import sys
import os
from datetime import datetime, timezone

# Add app directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.firebase import db
from app.services.cost_guard import cost_guard
from app.services.usage import usage_logger
from app.routes.sessions import create_session, start_cloud_stt, _session_doc_ref
from app.routes.usage import get_usage_timeline
from app.dependencies import User
from app.util_models import CreateSessionRequest, StartSTTGlobalRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEST_UID = f"test_user_{uuid.uuid4().hex[:8]}"

async def verify_flow():
    logger.info(f"Starting verification for user: {TEST_UID}")
    
    # [NEW] Provision User Doc
    user_ref = db.collection("users").document(TEST_UID)
    user_ref.set({
        "uid": TEST_UID,
        "email": "test@example.com",
        "displayName": "Test User",
        "plan": "free",
        "createdAt": datetime.now(timezone.utc)
    })
    
    mock_user = User(uid=TEST_UID, email="test@example.com", display_name="Test User")
    
    # 1. Initial Usage Check
    report = await cost_guard.get_usage_report(TEST_UID)
    initial_started = report.get("sessionsStarted", 0)
    logger.info(f"Initial sessionsStarted: {initial_started}")
    assert initial_started == 0
    
    # 2. Timeline Format Check
    # Need to mock Query or just call the function
    # Let's call the function. It should return an empty list or list of docs.
    timeline = await get_usage_timeline(current_user=mock_user)
    logger.info(f"Timeline type: {type(timeline)}")
    assert isinstance(timeline, list), f"Expected list, got {type(timeline)}"

    # 3. Create Cloud Session
    logger.info("--- Creating Cloud Session ---")
    req = CreateSessionRequest(
        title="Cloud Session",
        mode="lecture",
        transcriptionMode="cloud_google"
    )
    # create_session(req, background_tasks, current_user)
    # We'll skip background tasks for now
    res = await create_session(req, None, mock_user, x_idempotency_key=None, x_cloud_trace_context=None)
    logger.info(f"Session Created. ID: {res.id}, Ticket: {res.cloudTicket}")
    assert res.cloudTicket is not None
    
    report = await cost_guard.get_usage_report(TEST_UID)
    started_after_1 = report.get("sessionsStarted", 0)
    logger.info(f"sessionsStarted after creation: {started_after_1}")
    assert started_after_1 == 1

    # 4. Simulate WebSocket (Redundant Ticket logic)
    # We'll manually call the txn_issue_ticket logic if possible, or just check doc
    logger.info("--- Simulating Redundant Ticket Logic ---")
    doc_ref = _session_doc_ref(res.id)
    
    # We'll use the doc's ticket. If we simulate another "issue", it should skip increment.
    # In sessions.py, we don't have a direct way to call the inner txn_issue_ticket from here,
    # but we can verify that the session doc HAS the ticket.
    doc = doc_ref.get()
    data = doc.to_dict()
    assert data.get("cloudTicket") == res.cloudTicket
    
    # 5. Create Device Session (No increment)
    logger.info("--- Creating Device Session ---")
    req_device = CreateSessionRequest(
        title="Device Session",
        mode="lecture",
        transcriptionMode="device_sherpa"
    )
    res_device = await create_session(req_device, None, mock_user, x_idempotency_key=None, x_cloud_trace_context=None)
    logger.info(f"Device Session Created. ID: {res_device.id}")
    
    report = await cost_guard.get_usage_report(TEST_UID)
    started_after_2 = report.get("sessionsStarted", 0)
    logger.info(f"sessionsStarted after device creation: {started_after_2}")
    assert started_after_2 == 1 # Still 1
    
    # 6. Upgrade Session to Cloud (start_cloud_stt)
    logger.info("--- Upgrading Device Session to Cloud ---")
    upgrade_res = await start_cloud_stt(res_device.id, mock_user)
    logger.info(f"Upgrade Allowed: {upgrade_res.allowed}, Ticket: {upgrade_res.ticket}")
    assert upgrade_res.allowed is True
    assert upgrade_res.ticket is not None
    
    report = await cost_guard.get_usage_report(TEST_UID)
    started_after_3 = report.get("sessionsStarted", 0)
    logger.info(f"sessionsStarted after upgrade: {started_after_3}")
    assert started_after_3 == 2 # Should be 2 now
    
    # 7. Check Limit (Set to 10 in cost_guard.py)
    logger.info(f"Cloud Session Limit: {report.get('sessionLimit')}")
    assert report.get('sessionLimit') == 10

    logger.info("Verification Successful!")

if __name__ == "__main__":
    asyncio.run(verify_flow())
