
import logging
from google.cloud import firestore
from app.firebase import db, storage_client, AUDIO_BUCKET_NAME, MEDIA_BUCKET_NAME

logger = logging.getLogger("app.services.session_cleanup")

def cascade_delete_session(session_id: str, session_data: dict, owner_uid: str, db=db) -> bool:
    """
    Cascade delete all data associated with a session:
    - GCS audio files
    - GCS image files
    - Firestore subcollections (transcript_chunks, derived, jobs, calendar_sync, vectors, artifacts)
    - session_members documents
    - sessionMeta documents for all participants
    """
    try:
        doc_ref = db.collection("sessions").document(session_id)

        # 1. Delete GCS Audio
        audio_info = session_data.get("audio") or {}
        gcs_path = audio_info.get("gcsPath") or session_data.get("audioPath")
        if gcs_path:
            try:
                blob_name = gcs_path.replace(f"gs://{AUDIO_BUCKET_NAME}/", "")
                blob = storage_client.bucket(AUDIO_BUCKET_NAME).blob(blob_name)
                if blob.exists():
                    blob.delete()
                    logger.info(f"[CASCADE DELETE] Deleted audio: {blob_name}")
            except Exception as e:
                logger.warning(f"[CASCADE DELETE] Failed to delete audio for session {session_id}: {e}")

        # 2. Delete GCS Images (prefix-based for safety)
        try:
            media_bucket = storage_client.bucket(MEDIA_BUCKET_NAME)
            blobs = list(media_bucket.list_blobs(prefix=f"sessions/{session_id}/"))
            for blob in blobs:
                blob.delete()
            if blobs:
                logger.info(f"[CASCADE DELETE] Deleted {len(blobs)} media files for session {session_id}")
        except Exception as e:
            logger.warning(f"[CASCADE DELETE] Failed to delete media for session {session_id}: {e}")

        # 3. Delete imageNotes from GCS (legacy paths)
        image_notes = session_data.get("imageNotes") or []
        for note in image_notes:
            storage_path = note.get("storagePath")
            if storage_path:
                try:
                    _, _, rest = storage_path.partition("://")
                    bucket_name, _, blob_name = rest.partition("/")
                    blob = storage_client.bucket(bucket_name).blob(blob_name)
                    if blob.exists():
                        blob.delete()
                except Exception as e:
                    logger.warning(f"[CASCADE DELETE] Failed to delete image {storage_path}: {e}")

        # 4. Delete Firestore subcollections
        subcollections = ["transcript_chunks", "derived", "jobs", "calendar_sync", "vectors", "artifacts", "qa_results", "chat_messages", "reactions"]
        for sub_name in subcollections:
            try:
                sub_ref = doc_ref.collection(sub_name)
                _delete_collection(sub_ref, db=db)
            except Exception as e:
                logger.warning(f"[CASCADE DELETE] Failed to delete subcollection {sub_name} for session {session_id}: {e}")

        # 5. Delete session_members documents
        try:
            members_query = db.collection("session_members").where("sessionId", "==", session_id)
            member_docs = list(members_query.stream())
            if member_docs:
                batch = db.batch()
                for mdoc in member_docs:
                    batch.delete(mdoc.reference)
                batch.commit()
                logger.info(f"[CASCADE DELETE] Deleted {len(member_docs)} session_members for session {session_id}")
        except Exception as e:
            logger.warning(f"[CASCADE DELETE] Failed to delete session_members for session {session_id}: {e}")

        # 6. Delete sessionMeta for owner (shared users' meta stays - they just won't see the session)
        try:
            owner_meta_ref = db.collection("users").document(owner_uid).collection("sessionMeta").document(session_id)
            owner_meta_ref.delete()
        except Exception as e:
            logger.warning(f"[CASCADE DELETE] Failed to delete owner sessionMeta for session {session_id}: {e}")

        # 7. Delete the session document itself
        doc_ref.delete()

        # [FIX] Decrement serverSessionCount for owner (Storage Limit Release)
        try:
             db.collection("users").document(owner_uid).update({
                 "serverSessionCount": firestore.Increment(-1)
             })
        except Exception as e:
             logger.warning(f"[CASCADE DELETE] Failed to decrement serverSessionCount: {e}")

        # [FIX] Recalculate serverSessionCount to prevent stale limits
        try:
            docs_stream = db.collection("sessions")\
                .where("ownerUid", "==", owner_uid)\
                .limit(100).stream()
            active_count = 0
            for d in docs_stream:
                if d.to_dict().get("deletedAt") is None:
                    active_count += 1
            db.collection("users").document(owner_uid).update({
                "serverSessionCount": active_count
            })
        except Exception as e:
            logger.warning(f"[CASCADE DELETE] Failed to recalc serverSessionCount: {e}")

        logger.info(f"[CASCADE DELETE] Successfully deleted session {session_id} and all associated data")
        return True


    except Exception as e:
        logger.error(f"[CASCADE DELETE] Failed to cascade delete session {session_id}: {e}")
        return False

def _delete_collection(coll_ref, batch_size=50, db=db):
    docs = list(coll_ref.limit(batch_size).stream())
    deleted = 0
    while len(docs) > 0:
        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        deleted += len(docs)
        docs = list(coll_ref.limit(batch_size).stream())
    return deleted


def _delete_subcollection(doc_ref, subcoll_name: str, batch_size: int = 100, db=db) -> int:
    """Delete all documents in a subcollection."""
    coll_ref = doc_ref.collection(subcoll_name)
    return _delete_collection(coll_ref, batch_size=batch_size, db=db)


# User subcollections to delete
USER_SUBCOLLECTIONS = [
    "sessionMeta",
    "subscriptions",
    "monthly_usage",
    "notifications",
    "usage_logs",
]


def nuke_user_complete(user_id: str, db_client=None) -> dict:
    """
    Completely wipe a user's account and all associated data.

    Deletes:
    - All owned sessions (cascading delete with storage)
    - Removes user from shared sessions
    - User document and all subcollections
    - uid_links
    - phone_numbers entry
    - Account (if sole owner)
    - Apple subscription data
    - Entitlements
    - Share links
    - Username claims
    - Firebase Auth user

    Args:
        user_id: The user ID to delete
        db_client: Optional Firestore client (uses default if not provided)

    Returns:
        Dict with deletion stats and status
    """
    from datetime import datetime, timezone

    # Use provided db or default
    _db = db_client if db_client is not None else db

    stats = {
        "sessions_deleted": 0,
        "memberships_removed": 0,
        "subcollections_cleaned": 0,
        "errors": [],
    }

    user_ref = _db.collection("users").document(user_id)
    user_doc = user_ref.get()
    user_data = user_doc.to_dict() if user_doc.exists else {}

    logger.info(f"[NukeUser] Starting complete wipe for user {user_id}")

    try:
        # Update status to running
        if user_doc.exists:
            user_ref.set({
                "deletion": {
                    "state": "running",
                    "startedAt": datetime.now(timezone.utc)
                }
            }, merge=True)

        # 1. Delete all OWNED sessions (cascade)
        owned_docs = []

        # Query by ownerUid
        try:
            owned_query = _db.collection("sessions").where("ownerUid", "==", user_id)
            owned_docs.extend(list(owned_query.stream()))
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to query by ownerUid: {e}")

        # Also query by ownerUserId
        try:
            owner_user_query = _db.collection("sessions").where("ownerUserId", "==", user_id)
            owned_docs.extend(list(owner_user_query.stream()))
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to query by ownerUserId: {e}")

        # Deduplicate
        seen_ids = set()
        unique_docs = []
        for doc in owned_docs:
            if doc.id not in seen_ids:
                seen_ids.add(doc.id)
                unique_docs.append(doc)

        logger.info(f"[NukeUser] Found {len(unique_docs)} owned sessions to delete")

        for doc in unique_docs:
            try:
                success = cascade_delete_session(
                    doc.id,
                    doc.to_dict(),
                    user_id,
                    db=_db
                )
                if success:
                    stats["sessions_deleted"] += 1
            except Exception as e:
                stats["errors"].append(f"session:{doc.id}:{str(e)}")
                logger.error(f"[NukeUser] Failed to delete session {doc.id}: {e}")

        # 2. Remove user from SHARED sessions
        try:
            member_query = _db.collection("session_members").where("userId", "==", user_id)
            member_docs = list(member_query.stream())
            logger.info(f"[NukeUser] Found {len(member_docs)} memberships to remove")

            for mdoc in member_docs:
                try:
                    mdata = mdoc.to_dict()
                    session_id = mdata.get("sessionId")
                    if session_id:
                        sess_ref = _db.collection("sessions").document(session_id)
                        sess_ref.update({
                            "participantUserIds": firestore.ArrayRemove([user_id]),
                            "sharedWithUserIds": firestore.ArrayRemove([user_id]),
                        })
                    mdoc.reference.delete()
                    stats["memberships_removed"] += 1
                except Exception as e:
                    logger.warning(f"[NukeUser] Failed to remove membership {mdoc.id}: {e}")
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to query memberships: {e}")

        # 3. Delete user subcollections
        for subcoll in USER_SUBCOLLECTIONS:
            try:
                count = _delete_subcollection(user_ref, subcoll, db=_db)
                stats["subcollections_cleaned"] += count
            except Exception as e:
                logger.warning(f"[NukeUser] Failed to delete user/{subcoll}: {e}")

        # 4. Delete uid_links and get account_id
        account_id = None
        try:
            link_doc = _db.collection("uid_links").document(user_id).get()
            if link_doc.exists:
                account_id = link_doc.to_dict().get("accountId")
            _db.collection("uid_links").document(user_id).delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete uid_links: {e}")

        # 5. Delete phone_numbers entry
        phone = user_data.get("phoneE164") or user_data.get("phoneNumber")
        if phone:
            try:
                _db.collection("phone_numbers").document(phone).delete()
            except Exception as e:
                logger.warning(f"[NukeUser] Failed to delete phone_numbers: {e}")

        # 6. Handle Account (if user is the primary owner)
        if account_id:
            try:
                acc_ref = _db.collection("accounts").document(account_id)
                acc_doc = acc_ref.get()
                if acc_doc.exists:
                    acc_data = acc_doc.to_dict()
                    member_uids = acc_data.get("memberUids", [])
                    primary_uid = acc_data.get("primaryUid")

                    if primary_uid == user_id:
                        if len(member_uids) <= 1:
                            # Sole owner - delete account and its subcollections
                            for subcoll in ["monthly_usage", "subscriptions", "billing_history"]:
                                _delete_subcollection(acc_ref, subcoll, db=_db)
                            acc_ref.delete()
                            logger.info(f"[NukeUser] Deleted account {account_id} (sole owner)")
                        else:
                            # Transfer ownership to another member
                            new_owner = [u for u in member_uids if u != user_id][0]
                            acc_ref.update({
                                "primaryUid": new_owner,
                                "memberUids": firestore.ArrayRemove([user_id]),
                                "updatedAt": datetime.now(timezone.utc)
                            })
                            logger.info(f"[NukeUser] Transferred account {account_id} to {new_owner}")
                    else:
                        # Just remove from members
                        acc_ref.update({
                            "memberUids": firestore.ArrayRemove([user_id]),
                            "updatedAt": datetime.now(timezone.utc)
                        })
            except Exception as e:
                logger.warning(f"[NukeUser] Failed to handle account: {e}")

        # 7. Delete Apple subscription data
        try:
            token_query = _db.collection("apple_app_account_tokens").where("uid", "==", user_id)
            for tdoc in token_query.stream():
                tdoc.reference.delete()

            txn_query = _db.collection("apple_transactions").where("uid", "==", user_id)
            for tdoc in txn_query.stream():
                tdoc.reference.delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete Apple data: {e}")

        # 8. Delete entitlements where ownerUserId == user_id
        try:
            ent_query = _db.collection("entitlements").where("ownerUserId", "==", user_id)
            for edoc in ent_query.stream():
                edoc.reference.delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete entitlements: {e}")

        # 9. Delete share links created by user
        try:
            share_query = _db.collection("shareLinks").where("creatorUid", "==", user_id)
            for sdoc in share_query.stream():
                sdoc.reference.delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete share links: {e}")

        # 9b. Delete ops_events containing user's uid (GDPR compliance)
        try:
            ops_query = _db.collection("ops_events").where("uid", "==", user_id)
            ops_docs = list(ops_query.stream())
            if ops_docs:
                batch = _db.batch()
                batch_count = 0
                for odoc in ops_docs:
                    batch.delete(odoc.reference)
                    batch_count += 1
                    if batch_count >= 400:
                        batch.commit()
                        batch = _db.batch()
                        batch_count = 0
                if batch_count > 0:
                    batch.commit()
                logger.info(f"[NukeUser] Deleted {len(ops_docs)} ops_events for user {user_id}")
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete ops_events: {e}")

        # 9c. Delete active_streams for user's sessions (cleanup)
        try:
            # Active streams are keyed by sessionId, so we delete those related to user's sessions
            for doc in unique_docs:
                _db.collection("active_streams").document(doc.id).delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete active_streams: {e}")

        # 9d. Delete account_deletion_requests and locks
        try:
            del_req_query = _db.collection("account_deletion_requests").where("uid", "==", user_id)
            for doc in del_req_query.stream():
                doc.reference.delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete account_deletion_requests: {e}")

        try:
            # Delete locks by email if we have it
            email = user_data.get("email")
            if email:
                lock_query = _db.collection("account_deletion_locks").where("email", "==", email.lower())
                for doc in lock_query.stream():
                    doc.reference.delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete account_deletion_locks: {e}")

        # 9e. Delete mergeJobs involving this user
        try:
            # Delete as source
            merge_source_query = _db.collection("mergeJobs").where("sourceUid", "==", user_id)
            for doc in merge_source_query.stream():
                doc.reference.delete()
            # Delete as target
            merge_target_query = _db.collection("mergeJobs").where("targetUid", "==", user_id)
            for doc in merge_target_query.stream():
                doc.reference.delete()
        except Exception as e:
            logger.warning(f"[NukeUser] Failed to delete mergeJobs: {e}")

        # 10. Delete username claim
        username = user_data.get("username")
        if username:
            try:
                _db.collection("username_claims").document(username.lower()).delete()
            except Exception:
                pass

        # 11. Delete Firebase Auth user
        try:
            from firebase_admin import auth
            auth.delete_user(user_id)
            logger.info(f"[NukeUser] Deleted Firebase Auth user {user_id}")
        except Exception as e:
            stats["errors"].append(f"auth:{str(e)}")
            logger.error(f"[NukeUser] Failed to delete Auth user: {e}")

        # 12. Finally, delete user document
        user_ref.delete()
        logger.info(f"[NukeUser] Successfully nuked user {user_id}")

        return {
            "status": "completed",
            **stats
        }

    except Exception as e:
        logger.exception(f"[NukeUser] Failed: {e}")
        try:
            user_ref.set({
                "deletion": {
                    "state": "failed",
                    "error": str(e),
                    "updatedAt": datetime.now(timezone.utc)
                }
            }, merge=True)
        except Exception:
            pass

        return {
            "status": "failed",
            "error": str(e),
            **stats
        }
