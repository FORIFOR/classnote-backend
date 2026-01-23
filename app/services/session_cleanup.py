
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
        subcollections = ["transcript_chunks", "derived", "jobs", "calendar_sync", "vectors", "artifacts", "qa_results"]
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
