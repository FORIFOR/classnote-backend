import logging
import os
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

from app.firebase import db


logger = logging.getLogger("app.backfill_audio_delete_after")


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


def _resolve_base_time(data: dict) -> datetime:
    audio = data.get("audio") or {}
    candidates = [
        audio.get("uploadedAt"),
        audio.get("uploaded_at"),
        data.get("audioMeta", {}).get("updated"),
        data.get("updatedAt"),
        data.get("endedAt"),
        data.get("createdAt"),
    ]
    for value in candidates:
        ts = _parse_firestore_ts(value)
        if ts:
            return ts
    return _now()


def backfill_delete_after() -> dict:
    limit = int(os.environ.get("AUDIO_BACKFILL_LIMIT", "200"))
    ttl_days = int(os.environ.get("AUDIO_DELETE_TTL_DAYS", "30"))

    scanned = 0
    updated = 0
    skipped = 0

    last_doc = None
    while True:
        query = (
            db.collection("sessions")
            .where("audioPath", "!=", None)
            .order_by("audioPath")
            .limit(limit)
        )
        if last_doc:
            query = query.start_after(last_doc)
        docs = list(query.stream())
        if not docs:
            break

        for doc in docs:
            scanned += 1
            data = doc.to_dict() or {}
            audio = data.get("audio") or {}
            delete_after = audio.get("deleteAfterAt")
            if delete_after:
                skipped += 1
                continue

            gcs_path = audio.get("gcsPath") or data.get("audioPath")
            if not gcs_path:
                skipped += 1
                continue

            base_time = _resolve_base_time(data)
            delete_after_at = base_time + timedelta(days=ttl_days)

            update_audio = {
                "gcsPath": gcs_path,
                "deleteAfterAt": delete_after_at,
                "hasAudio": audio.get("hasAudio", True),
            }
            for key in ("sizeBytes", "uploadedAt", "contentType"):
                if audio.get(key) is not None:
                    update_audio[key] = audio.get(key)

            doc.reference.set({
                "audio": update_audio,
                "audioPath": data.get("audioPath") or gcs_path,
                "updatedAt": _now(),
            }, merge=True)
            updated += 1

        last_doc = docs[-1]
        if len(docs) < limit:
            break

    return {"scanned": scanned, "updated": updated, "skipped": skipped}


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    result = backfill_delete_after()
    logger.info("Backfill completed. scanned=%s updated=%s skipped=%s", result["scanned"], result["updated"], result["skipped"])


if __name__ == "__main__":
    main()
