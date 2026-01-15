import logging
import os
from datetime import datetime, timezone

from google.cloud import firestore

from app.firebase import db, storage_client, AUDIO_BUCKET_NAME


logger = logging.getLogger("app.cleanup_audio")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_firestore_ts(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None
    return None


def cleanup_expired_audio() -> int:
    """
    Delete expired audio objects and update Firestore metadata.
    """
    limit = int(os.environ.get("AUDIO_CLEANUP_LIMIT", "200"))
    deleted_count = 0

    while True:
        now = _now()
        query = (
            db.collection("sessions")
            .where("audio.deleteAfterAt", "<", now)
            .limit(limit)
        )
        docs = list(query.stream())
        if not docs:
            break

        for doc in docs:
            data = doc.to_dict() or {}
            audio = data.get("audio") or {}
            delete_after = _parse_firestore_ts(audio.get("deleteAfterAt"))
            if not delete_after or delete_after > now:
                continue

            gcs_path = audio.get("gcsPath") or data.get("audioPath")
            if gcs_path:
                blob_name = gcs_path.replace(f"gs://{AUDIO_BUCKET_NAME}/", "")
                blob = storage_client.bucket(AUDIO_BUCKET_NAME).blob(blob_name)
                try:
                    if blob.exists():
                        blob.delete()
                except Exception as exc:
                    logger.error("Failed to delete audio blob %s: %s", gcs_path, exc)
                    continue

            doc.reference.update({
                "audio": {
                    "hasAudio": False,
                    "gcsPath": None,
                    "sizeBytes": None,
                    "uploadedAt": None,
                    "deletedAt": now,
                    "deleteAfterAt": None,
                    "contentType": None,
                },
                "audioPath": None,
                "audioStatus": "expired",
                "signedGetUrl": firestore.DELETE_FIELD,
                "signedGetUrlExpiresAt": firestore.DELETE_FIELD,
                "updatedAt": now,
            })
            deleted_count += 1

        if len(docs) < limit:
            break

    return deleted_count


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    deleted = cleanup_expired_audio()
    logger.info("Expired audio cleanup completed. deleted=%s", deleted)


if __name__ == "__main__":
    main()
