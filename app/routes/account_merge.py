from fastapi import APIRouter, Depends, HTTPException, Body
from google.cloud import firestore
from datetime import datetime, timezone
import logging
import uuid
from typing import Optional, List
from pydantic import BaseModel

from app.dependencies import get_current_user, CurrentUser
from app.firebase import db
# Lazy import: from app.task_queue import enqueue_merge_migration_task

router = APIRouter()
logger = logging.getLogger("app.account_merge")

class MergeStartRequest(BaseModel):
    targetUid: str # The UID to merge *into* the current session (or vice versa)
    strategy: str = "keep_target" # "keep_target" (phone) or "keep_current" (sns)

class MergeStartResponse(BaseModel):
    mergeId: str
    plan: dict # e.g. {"sessionsToMove": 10}

class MergeCommitRequest(BaseModel):
    mergeId: str

# ------------------------------------------------------------------
# 1. Merge Start
# ------------------------------------------------------------------
@router.post("/accounts/merge:start", response_model=MergeStartResponse)
def start_merge(
    req: MergeStartRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    Analyzes the merge between current_user and targetUid.
    Creates a Merge Job in 'mergeJobs' collection.
    """
    current_uid = current_user.uid
    target_uid = req.targetUid
    
    if current_uid == target_uid:
        raise HTTPException(400, "Cannot merge with self")

    # Verify target exists
    target_user_ref = db.collection("users").document(target_uid)
    target_doc = target_user_ref.get()
    if not target_doc.exists:
        raise HTTPException(404, "Target user not found")

    # Resolve Accounts
    # (Helper to get accountId from link)
    def _get_account_id(uid):
        link_doc = db.collection("uid_links").document(uid).get()
        if link_doc.exists:
            return link_doc.to_dict().get("accountId")
        return None

    current_acc_id = _get_account_id(current_uid)
    target_acc_id = _get_account_id(target_uid)

    if not current_acc_id and not target_acc_id:
        raise HTTPException(400, "Neither user has an account linked.")

    # Determine Primary/Secondary
    if req.strategy == "keep_current":
        primary_acc_id = current_acc_id
        secondary_acc_id = target_acc_id
        primary_uid = current_uid
        secondary_uid = target_uid
    else: # keep_target (default/recommended for Phone)
        primary_acc_id = target_acc_id
        secondary_acc_id = current_acc_id
        primary_uid = target_uid
        secondary_uid = current_uid

    if not primary_acc_id:
         # Fallback: if preferred side has no account, use the other
         primary_acc_id = secondary_acc_id
         secondary_acc_id = None
    
    if primary_acc_id == secondary_acc_id:
         raise HTTPException(400, "Users are already linked to the same account.")

    # Count resources to migrate (Approximation)
    sessions_count = 0
    # Query sessions where ownerUserId == secondary_uid
    # Note: large query cost? limit check
    sessions_ref = db.collection("sessions")
    # Using aggregation query if available or just limits
    # cost_guard tracks usage, maybe use that?
    # For now, just estimate or returning 0 is fine, UI just needs to know "it happens"
    
    # Create Merge Job
    merge_id = str(uuid.uuid4())
    job_ref = db.collection("mergeJobs").document(merge_id)
    job_data = {
        "status": "pending",
        "createdAt": datetime.now(timezone.utc),
        "createdByUid": current_uid,
        "primaryAccountId": primary_acc_id,
        "secondaryAccountId": secondary_acc_id, # Can be None if secondary was pure UID
        "primaryUid": primary_uid,
        "secondaryUid": secondary_uid,
        "targetUidInput": target_uid,
        "strategy": req.strategy,
        "migrationStatus": "pending",
        "migratedSessionCount": 0
    }
    job_ref.set(job_data)

    return MergeStartResponse(
        mergeId=merge_id,
        plan={"primaryAccountId": primary_acc_id, "secondaryUid": secondary_uid}
    )


# ------------------------------------------------------------------
# 2. Merge Commit
# ------------------------------------------------------------------
@router.post("/accounts/merge:commit")
def commit_merge(
    req: MergeCommitRequest,
    current_user: CurrentUser = Depends(get_current_user)
):
    merge_id = req.mergeId
    job_ref = db.collection("mergeJobs").document(merge_id)
    
    @firestore.transactional
    def txn_commit(transaction):
        job_doc = job_ref.get(transaction=transaction)
        if not job_doc.exists:
             raise HTTPException(404, "Job not found")
        
        job = job_doc.to_dict()
        if job.get("status") != "pending":
             raise HTTPException(409, f"Job is {job.get('status')}")
        
        # Verify ownership (creator only)
        if job.get("createdByUid") != current_user.uid:
             raise HTTPException(403, "Not your job")

        primary_acc_id = job["primaryAccountId"]
        secondary_acc_id = job.get("secondaryAccountId")
        primary_uid = job["primaryUid"]
        secondary_uid = job["secondaryUid"] # The one losing independence

        # 1. Update Links
        # secondary_uid -> primary_acc_id
        sec_link_ref = db.collection("uid_links").document(secondary_uid)
        transaction.set(sec_link_ref, {
             "accountId": primary_acc_id,
             "linkedAt": datetime.now(timezone.utc),
             "mergedFromAccountId": secondary_acc_id
        }, merge=True)

        # 2. Update User Profile (optional but good for consistency)
        # We also set accountId on user doc as requested
        transaction.set(db.collection("users").document(secondary_uid), {
             "accountId": primary_acc_id,
             "mergedAt": datetime.now(timezone.utc)
        }, merge=True)
         
        transaction.set(db.collection("users").document(primary_uid), {
             "accountId": primary_acc_id
        }, merge=True)

        # 3. Mark Job Committed
        transaction.update(job_ref, {
             "status": "committed",
             "committedAt": datetime.now(timezone.utc)
        })

        return job

    transaction = db.transaction()
    job = txn_commit(transaction)

    # 4. Trigger Async Migration (Cloud Tasks)
    # We pass the mergeId to the worker
    try:
        from app.task_queue import enqueue_merge_migration_task
        enqueue_merge_migration_task(merge_id)
    except Exception as e:
        logger.error(f"Failed to enqueue migration task for {merge_id}: {e}")
        # Job is committed, so UI will succeed. Migration will need manual retry or cron sweep if failed here.
    
    return {"ok": True, "status": "committed"}


# ------------------------------------------------------------------
# 3. Migration Logic (Called by Worker)
# ------------------------------------------------------------------
def execute_migration_batch(merge_id: str, batch_size=200):
    """
    Worker moves sessions (and other assets) from secondaryUid to primaryAccountId.
    Ideally, we change 'ownerAccountId' to primary.
    Legacy 'ownerUserId' might remain as 'originalOwnerUserId' or be updated if we want to fully hide the merge.
    Decision: 
     - Update ownerAccountId to primary_acc_id.
     - Keep ownerUserId as is (so we know who recorded it), OR update it if we want "single user" view.
       -> If we keep ownerUserId as secondary_uid, query by primary_uid won't find it unless we query by ownerAccountId.
       -> CURRENT APP: Queries by ownerUserId usually. 
       -> We must update ownerUserId to primary_uid OR update App to query by ownerAccountId.
       -> Updating App is cleaner but hard to deploy synchronously.
       -> Updating ownerUserId is destructive but makes it "just work".
       -> PLAN: Update ownerAccountId AND ownerUserId = primary_uid. (Full Merger)
    """
    job_ref = db.collection("mergeJobs").document(merge_id)
    job = job_ref.get().to_dict()
    
    if not job or job.get("status") != "committed":
        return "skipped_or_invalid"

    primary_acc_id = job["primaryAccountId"]
    primary_uid = job["primaryUid"]
    secondary_uid = job["secondaryUid"] # Data source
    
    # Query target: Sessions owned by secondary_uid
    sessions_ref = db.collection("sessions")
    # Finding un-migrated sessions
    query = sessions_ref.where("ownerUserId", "==", secondary_uid).limit(batch_size)
    docs = list(query.stream())
    
    if not docs:
        job_ref.update({"migrationStatus": "completed"})
        return "completed"

    batch = db.batch()
    count = 0
    for doc in docs:
        # Move to primary
        batch.update(doc.reference, {
            "ownerUserId": primary_uid, # Rewrite ownership
            "ownerUid": primary_uid,
            "ownerAccountId": primary_acc_id,
            "originalOwnerUserId": secondary_uid, # Audit trail
            "migratedInMergeId": merge_id,
            "updatedAt": datetime.now(timezone.utc)
        })
        count += 1
    
    batch.commit()
    
    # Update Job Stats
    job_ref.update({
        "migratedSessionCount": firestore.Increment(count),
        "lastBatchAt": datetime.now(timezone.utc)
    })

    # Reschedule if we hit limit (Chain)
    if len(docs) == batch_size:
        from app.task_queue import enqueue_merge_migration_task
        enqueue_merge_migration_task(merge_id)
        return "continued"
    else:
        job_ref.update({"migrationStatus": "completed"})
        return "completed"


