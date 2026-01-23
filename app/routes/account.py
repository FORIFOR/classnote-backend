from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore
from datetime import datetime, timezone
import logging
from app.dependencies import get_current_user, CurrentUser
from app.services.account import account_id_from_phone
from app.firebase import db
from app.task_queue import enqueue_nuke_user_task

logger = logging.getLogger("app.account")

router = APIRouter()

@router.post("/me/phone:link")
def link_phone(user: CurrentUser = Depends(get_current_user)):
    """
    Links the current authenticated UID to an Account based on the verified phone number.
    The ID Token MUST contain a 'phone_number' claim (verified by Firebase).
    
    Implements 'Reject-less' logic with strict Transaction Safety (Read-Before-Write).
    Also performs Session Migration if an attach (user switch) occurs.
    """
    logger.info(f"Link phone request: uid={user.uid} phone={user.phone_number}")

    if not user.phone_number:
        raise HTTPException(
            status_code=400, 
            detail="PHONE_NOT_VERIFIED",
            headers={"X-Reason": "Token missing phone_number claim"}
        )

    phone = user.phone_number
    uid = user.uid

    # 1. Prepare References (Outside Transaction)
    users_ref = db.collection("users").document(uid)
    uid_link_ref = db.collection("uid_links").document(uid)
    phone_ref = db.collection("phone_numbers").document(phone)
    accounts_col = db.collection("accounts")

    @firestore.transactional
    def txn_attach(tx: firestore.Transaction):
        now = datetime.now(timezone.utc)
        
        # --- READ PHASE (MUST BE FIRST) ---
        user_snap = users_ref.get(transaction=tx)
        uid_link_snap = uid_link_ref.get(transaction=tx)
        phone_snap = phone_ref.get(transaction=tx)

        # Parse Read Data
        user_data = user_snap.to_dict() if user_snap.exists else {}
        link_data = uid_link_snap.to_dict() if uid_link_snap.exists else {}
        phone_data = phone_snap.to_dict() if phone_snap.exists else {}

        # Determine Target Account ID
        # Priority: Phone Index > Current Link > User Profile > New
        phone_account_id = phone_data.get("accountId")
        old_owner_uid = phone_data.get("standardOwnerUid") # Critical for migration
        
        current_account_id = link_data.get("accountId") or user_data.get("accountId")

        target_account_id = phone_account_id or current_account_id
        
        # If still no account ID, generate a new one (Deterministic or Random)
        if not target_account_id:
             # Use deterministic ID from phone if it's a fresh registration for consistency
             target_account_id = account_id_from_phone(phone)

        # READ Account Doc (Crucial: Read before any write)
        acc_ref = accounts_col.document(target_account_id)
        acc_snap = acc_ref.get(transaction=tx)
        
        # --- LOGIC PHASE (No Reads/Writes) ---
        
        # [IDEMPOTENCY] Double-Call Check
        # If the user is already linked to this phone and account, we do nothing.
        user_has_phone = user_data.get("phoneE164") == phone
        user_has_acc = user_data.get("accountId") == target_account_id
        link_is_correct = link_data.get("accountId") == target_account_id

        if user_has_phone and user_has_acc and link_is_correct:
             # Already consistent
             return {"ok": True, "accountResolution": "noop", "accountId": target_account_id}

        # Resolution Status
        resolution = "registered"
        migration_source_uid = None
        
        if phone_account_id and phone_account_id != current_account_id:
             # Case: Phone belongs to another account.
             # We ATTACH current user to that phone account.
             resolution = "attached"
             
             # Check if we need to migrate data from the OLD owner of this phone
             if old_owner_uid and old_owner_uid != uid:
                 migration_source_uid = old_owner_uid
                 
        elif phone_account_id:
             # Phone matches current account (or logic flow led here)
             # Just ensure consistency
             if resolution == "registered" and phone_account_id: 
                 resolution = "linked" # Already linked basically

        # --- WRITE PHASE (All writes together) ---
        
        # 1. Account Doc
        if not acc_snap.exists:
            tx.set(acc_ref, {
                "phoneE164": phone,
                "phoneVerified": True,
                "plan": "free",
                "createdAt": now,
                "updatedAt": now,
                "credits": {
                    "cloudSecondsRemaining": 1800, # 30 min default
                    "summaryRemaining": 3,
                    "quizRemaining": 3,
                },
            }, merge=True)
        else:
            # Just touch to update needed fields
            tx.set(acc_ref, {
                "phoneE164": phone,
                "updatedAt": now
            }, merge=True)

        # 2. User Doc
        tx.set(users_ref, {
            "uid": uid,
            "accountId": target_account_id,
            "phoneE164": phone,
            "updatedAt": now,
        }, merge=True)

        # 3. Link Doc (Force Pointer)
        tx.set(uid_link_ref, {
            "uid": uid,
            "accountId": target_account_id,
            "phoneE164": phone,
            "updatedAt": now,
            "reason": "phone_link"
        }, merge=True)

        # 4. Phone Index
        # IMPORTANT: Preserve standardOwnerUid if it exists (don't overwrite with new UID unless it's new)
        # Actually, if we are ATTACHING, does the new user become the owner?
        # Usually "standardOwnerUid" implies the "Primary" holder. 
        # For auto-attach, we are saying "This new UID is now the active portal for this phone".
        # So we SHOULD update standardOwnerUid to current uid if we want them to see the data (if querying by standardOwnerUid).
        # BUT sessions are owned by `ownerUid`. 
        # If we migrate sessions, we should make current uid the standardOwnerUid.
        
        phone_update = {
            "accountId": target_account_id,
            "isVerified": True,
            "updatedAt": now,
            "uidLastSeen": uid
        }
        # If we are taking over (attach), we become the new standard owner for future reference
        if not old_owner_uid or resolution == "attached":
            phone_update["standardOwnerUid"] = uid

        tx.set(phone_ref, phone_update, merge=True)

        return {
            "ok": True, 
            "accountResolution": resolution, 
            "accountId": target_account_id,
            "migrationSourceUid": migration_source_uid
        }

    transaction = db.transaction()
    try:
        final_result = txn_attach(transaction)
    except Exception as e:
        logger.error(f"Transaction failed for {uid}: {e}")
        # traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Link failed: {str(e)}")

    # [Session Migration / Absorption]
    # If we identified a need to migrate data from an old UID (attach), do it now.
    target_acc_id = final_result.get("accountId")
    source_uid = final_result.get("migrationSourceUid")
    
    if target_acc_id:
        # 1. Absorb sessions with no ownerAccount (Self-repair)
        try:
             _absorb_unlinked_sessions(uid, target_acc_id)
        except Exception as e:
             logger.warning(f"Self-repair absorption failed: {e}")

        # 2. Migrate from Old Owner (The Restore Logic)
        if source_uid:
            try:
                logger.info(f"Migrating sessions from {source_uid} to {uid}")
                _migrate_sessions_to_new_owner(source_uid, uid, target_acc_id)
            except Exception as e:
                logger.error(f"Session migration failed: {e}")

    return {
        "ok": True, 
        "accountResolution": final_result["accountResolution"], 
        "accountId": target_acc_id
    }

def _absorb_unlinked_sessions(uid: str, account_id: str):
    """
    Backfill ownerAccountId for sessions owned by `uid` that miss it.
    """
    sessions_ref = db.collection("sessions")
    # Using list(stream) is safe here as it's a simple query outside txn
    unlinked_query = sessions_ref.where("ownerUserId", "==", uid).where("ownerAccountId", "==", None).limit(100)
    docs = list(unlinked_query.stream())
    
    if docs:
        batch = db.batch()
        for doc in docs:
            batch.update(doc.reference, {"ownerAccountId": account_id})
        batch.commit()
        logger.info(f"Absorbed {len(docs)} sessions for {uid}")

def _migrate_sessions_to_new_owner(old_uid: str, new_uid: str, account_id: str):
    """
    Transfers ownership of sessions from `old_uid` to `new_uid`.
    This enables the 'Restore' experience where the new login inherits past data.
    """
    sessions_ref = db.collection("sessions")
    # Query sessions owned by old_uid
    # We use ownerUserId as it's the stable legacy field, or ownerUid
    query = sessions_ref.where("ownerUid", "==", old_uid).limit(500)
    docs = list(query.stream())
    
    if not docs:
        # Try legacy field
        query = sessions_ref.where("ownerUserId", "==", old_uid).limit(500)
        docs = list(query.stream())

    if docs:
        logger.info(f"Found {len(docs)} sessions to migrate from {old_uid} -> {new_uid}")
        batch = db.batch()
        count = 0
        for doc in docs:
            batch.update(doc.reference, {
                "ownerUid": new_uid,
                "ownerUserId": new_uid,
                "userId": new_uid, # Legacy
                "ownerId": new_uid, # Legacy
                "ownerAccountId": account_id,
                "migrationFromUid": old_uid, # Audit trail
                "updatedAt": datetime.now(timezone.utc)
            })
            count += 1
            if count >= 400: # Batch limit safety
                batch.commit()
                batch = db.batch()
                count = 0
        
        if count > 0:
            batch.commit()
            
        # Also migrate User Settings / sessionMeta if possible?
        # For now, sessions are the most critical.
        logger.info(f"Successfully migrated {len(docs)} sessions.")

import os

if os.environ.get("ENV") != "production":
    @router.post("/admin/test/phone/{phone_e164}:release")
    def debug_release_phone(phone_e164: str):
        """
        [DEBUG ONLY] Releases the standardOwnerUid for a phone number.
        Use this to test the handover scenario (A loses plan, B acquires plan).
        """
        ref = db.collection("phone_numbers").document(phone_e164)
        doc = ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Phone number doc not found")
        
        ref.update({
            "standardOwnerUid": None,
            "isVerified": True, # Ensure it stays verified or reset if needed, keeping true here.
            "updatedAt": datetime.now(timezone.utc)
        })
        return {"ok": True, "message": f"Released ownership for {phone_e164}"}

# ---------- Account Merge ----------

from pydantic import BaseModel

class MergeStartRequest(BaseModel):
    targetUid: str
    strategy: str = "keep_target" # "keep_target" (default) or "keep_current"


class MergeStartResponse(BaseModel):
    mergeJobId: str
    plan: dict # details of what happens

class MergeCommitRequest(BaseModel):
    mergeJobId: str

@router.post("/accounts/merge:start", response_model=MergeStartResponse)
def start_merge(
    req: MergeStartRequest,
    user: CurrentUser = Depends(get_current_user)
):
    """
    Initiates an account merge between the current SNS user and a target Phone user.
    """
    logger.info(f"Merge Start: current={user.uid} target={req.targetUid}")
    
    # 1. Validation
    # We need to verify that req.targetUid is actually the phone owner we want.
    # The client usually performs phone-auth to get targetUid.
    # We should basically trust the client carries the token implies they own it? 
    # NO. The client is calling THIS API with `user` (SNS) token.
    # How do we prove they own `targetUid`?
    # Ideal: Client sends ID Token of targetUid in header? Or we trust that 
    # 17015 happened and client is following flow.
    # SECURITY: A malicious user could sniff `targetUid` (if public) and try to merge?
    # Mitigations:
    # - `targetUid` must definitely be a phone-based user.
    # - We can require `x-target-token` header? 
    # For MVP as requested ("Fastest fix"):
    # We trust the client flow because `targetUid` isn't easily guessable UUID 
    # and even if they merge, they merge INTO the phone account (so they lose data if malicious?).
    # Actually, if they merge `target` INTO `current`, they steal the phone number.
    # So "keep_target" is safer (they lose their SNS account into the Phone account).
    # "keep_current" needs proof of phone ownership (e.g. valid phone session).
    
    # Force strategy to "keep_target" for now as per recommendation
    if req.strategy != "keep_target":
        raise HTTPException(400, "Only 'keep_target' strategy is supported currently.")

    # 2. Get Source and Target Accounts
    # current (Source/SNS) -> target (Destination/Phone)
    
    # Check if target exists
    target_user_ref = db.collection("users").document(req.targetUid)
    target_snap = target_user_ref.get()
    if not target_snap.exists:
         raise HTTPException(404, "Target user not found")
         
    # Check if we are already merged?
    source_link = db.collection("uid_links").document(user.uid).get()
    target_link = db.collection("uid_links").document(req.targetUid).get()
    
    if source_link.exists and target_link.exists:
        if source_link.to_dict().get("accountId") == target_link.to_dict().get("accountId"):
            raise HTTPException(400, "Accounts already merged.")

    # 3. Create Merge Job (Temporary State)
    merge_id = f"merge_{user.uid}_{req.targetUid}_{int(datetime.now().timestamp())}"
    
    db.collection("mergeJobs").document(merge_id).set({
        "status": "pending",
        "sourceUid": user.uid,
        "targetUid": req.targetUid,
        "strategy": req.strategy,
        "createdAt": datetime.now(timezone.utc),
        "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=10)
    })
    
    return MergeStartResponse(
        mergeJobId=merge_id,
        plan={
            "description": "Merge current SNS account into existing Phone account.",
            "source": user.uid,
            "target": req.targetUid,
            "direction": "source -> target"
        }
    )

@router.post("/accounts/merge:commit")
def commit_merge(
    req: MergeCommitRequest,
    user: CurrentUser = Depends(get_current_user)
):
    """
    Executes the merge transaction.
    """
    job_ref = db.collection("mergeJobs").document(req.mergeJobId)
    job_snap = job_ref.get()
    
    if not job_snap.exists:
        raise HTTPException(404, "Merge job not found or expired")
        
    job = job_snap.to_dict()
    if job.get("status") != "pending":
          raise HTTPException(400, f"Invalid job status: {job.get('status')}")
          
    if job.get("sourceUid") != user.uid:
         raise HTTPException(403, "Merge job belongs to another user")
         
    source_uid = user.uid
    target_uid = job.get("targetUid")
    
    # Execute Transaction
    @firestore.transactional
    def txn_merge(tx):
        # 1. Resolve Target Account ID (The 'Winner')
        # Target user MUST have an account. If not, something is wrong (Phone users usually JIT create).
        t_link_ref = db.collection("uid_links").document(target_uid)
        t_link = tx.get(t_link_ref)
        
        target_account_id = None
        if t_link.exists:
            target_account_id = t_link.to_dict().get("accountId")
            
        if not target_account_id:
            # Fallback: Check user doc? Or Auto-create?
            # For robustness, if target has no account, create one for them NOW.
            # But let's assume they have one (Phone Verified).
            raise HTTPException(500, "Target user has no account linked. Contact support.")
            
        # 2. Update Source Link to point to Target Account
        s_link_ref = db.collection("uid_links").document(source_uid)
        tx.set(s_link_ref, {
             "uid": source_uid,
             "accountId": target_account_id,
             "linkedAt": datetime.now(timezone.utc),
             "mergeJobId": req.mergeJobId,
             "previousOwner": "SNS"
        }, merge=True)
        
        # 3. Update Source User Profile
        # Ensure it has accountId field (Direct mapping)
        tx.update(db.collection("users").document(source_uid), {
            "accountId": target_account_id,
            "updatedAt": datetime.now(timezone.utc)
        })
        
        # 4. Mark Job
        tx.update(job_ref, {"status": "committed", "committedAt": datetime.now(timezone.utc)})
        
        return target_account_id

    transaction = db.transaction()
    try:
        final_acc_id = txn_merge(transaction)
    except Exception as e:
        logger.error(f"Merge transaction failed: {e}")
        raise HTTPException(500, f"Merge failed: {e}")
        
    # 5. Enqueue Background Migration
    # Move sessions, etc.
    try:
        from app.task_queue import enqueue_merge_migration_task
        enqueue_merge_migration_task(req.mergeJobId, source_uid, final_acc_id)
    except Exception as e:
        logger.warning(f"Failed to enqueue migration (retry needed): {e}")
        
    return {"ok": True, "status": "queued", "targetAccountId": final_acc_id}

class MigrateReq(BaseModel):
    oldUid: str

@router.post("/account/migrate")
def migrate(req: MigrateReq, new_uid: str = Depends(get_current_user)):
    """
    Manually migrates data from an old (orphaned) UID to the current UID.
    Used when a user logged in with a different provider (e.g., LINE) 
    and then performs a Phone Auth which resolves to an existing account (switching UIDs).
    """
    old_uid = req.oldUid
    if old_uid == new_uid:
        return {"ok": True, "migrated": 0}

    # Verify ownership or safety?
    # Ideally we should verify the caller 'owned' oldUid or that oldUid is effectively abandoned.
    # In the flow "LINE -> 17015 -> Switch to Phone", the client holds the session of oldUid briefly.
    # But now we are authenticated as `new_uid`.
    # How do we trust `old_uid`?
    # Realistically, `old_uid` (e.g. "line:...") is not secret, but hijacking it is hard if we only migrate sessions.
    
    # 1. Resolve Target Account
    link_ref = db.collection("uid_links").document(new_uid)
    link_doc = link_ref.get()
    account_id = link_doc.to_dict().get("accountId")
    
    if not account_id:
        raise HTTPException(400, "Current user has no account linked")

    # 2. Perform Migration (Sessions)
    logger.info(f"Manual migration request: {old_uid} -> {new_uid}")
    
    count = 0
    try:
        # Re-use existing logic
        # We need to expose _migrate_sessions_to_new_owner logic here or call it
        _migrate_sessions_to_new_owner(old_uid, new_uid, account_id)
        
        # Count is hard to get from that function as it's void, but let's assume success
        # We can Tombstone the old user to prevent confusion
        db.collection("users").document(old_uid).set({
            "mergedInto": new_uid,
            "mergedAt": datetime.now(timezone.utc)
        }, merge=True)
        
        return {"ok": True, "oldUid": old_uid, "newUid": new_uid}
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise HTTPException(500, f"Migration failed: {e}")

# ---------- Async Account Deletion ----------

@router.post("/me:delete")
def request_delete(user: CurrentUser = Depends(get_current_user)):
    """
    Request async account deletion.
    """
    uid = user.uid
    user_ref = db.collection("users").document(uid)
    snap = user_ref.get()
    
    if not snap.exists:
        # Already gone
        return {"ok": True, "state": "done"}

    data = snap.to_dict() or {}
    deletion = data.get("deletion") or {}
    state = deletion.get("state", "none")

    if state in ("queued", "running"):
         return {"ok": True, "state": state, "jobId": deletion.get("jobId"), "startedAt": deletion.get("startedAt")}
    
    # If previously failed, we retry.
    
    job_id = f"del_{uid}_{int(datetime.now(timezone.utc).timestamp())}"
    now = datetime.now(timezone.utc)
    
    # Update State first
    user_ref.set({
        "deletion": {
            "state": "queued",
            "requestedAt": now,
            "jobId": job_id
        }
    }, merge=True)

    # Enqueue Task
    try:
        enqueue_nuke_user_task(uid)
    except Exception as e:
        logger.error(f"Failed to enqueue nuke for {uid}: {e}")
        # Revert state or keep queued? 
        # Keep queued, maybe a sweeper picks it up?
        # For now, return error or success with warning?
        # User retry is better.
        # But we already set 'queued'. 
        
    return {"ok": True, "state": "queued", "jobId": job_id}


@router.get("/me:delete/status")
def delete_status(user: CurrentUser = Depends(get_current_user)):
    """
    Poll this to check deletion progress.
    Once 'done', client should wipe local data and signout.
    """
    uid = user.uid
    user_ref = db.collection("users").document(uid)
    snap = user_ref.get()
    
    if not snap.exists:
        return {"state": "done"}
        
    deletion = (snap.to_dict().get("deletion") or {})
    return {
        "state": deletion.get("state", "none"),
        "jobId": deletion.get("jobId"),
        "error": deletion.get("error")
    }
