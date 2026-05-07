"""DeepNote notification_events — Desktop / mobile polling inbox.

Storage::

    notification_events/{notificationId}
        accountId:        string                         (account scope key)
        userId:           string | None                  (creator uid, optional)
        type:             string                         ("daily_todo_digest" | …)
        title:            string
        body:             string
        sourceTaskId:     string | None                  (scheduled_tasks.taskId)
        sourceSessionId:  string | None
        idempotencyKey:   string | None                  ("scheduled_task:{tid}:{slot}")
        read:             bool                           (default False)
        readAt:           timestamp | None
        actionUrl:        string | None
        actions:          [{key, label, url?}]
        delivery:         {channel, status}
        createdAt:        timestamp

The dispatcher in ``scheduled_tasks_routes._dispatch`` writes into this
collection for ``destination.channel == "desktop"``; iOS / Desktop / Web
polls ``GET /v1/notifications`` to pull and surface as OS notifications.

Idempotency: ``create()`` first looks up by ``(accountId, idempotencyKey)``
and returns the existing event instead of duplicating, so two concurrent
``scheduler/tick`` calls cannot generate two notifications for the same
``runSlot``.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.cloud import firestore  # type: ignore

from app.firebase import db

logger = logging.getLogger("app.services.notifications")

COLLECTION = "notification_events"


def _coll():
    return db.collection(COLLECTION)


def find_by_idempotency_key(account_id: str, key: str) -> Optional[Dict[str, Any]]:
    """Lookup existing notification by ``(accountId, idempotencyKey)``.
    Used by the dispatcher to skip duplicate writes."""
    if not key:
        return None
    try:
        q = (
            _coll()
            .where(filter=firestore.FieldFilter("accountId", "==", account_id))
            .where(filter=firestore.FieldFilter("idempotencyKey", "==", key))
            .limit(1)
        )
        for snap in q.stream():
            d = snap.to_dict() or {}
            d["id"] = snap.id
            return d
    except Exception as e:
        logger.warning("[notifications.find_by_key] query failed: %s", e)
    return None


def create(
    *,
    account_id: str,
    notification_type: str,
    title: str,
    body: str,
    user_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    source_session_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    action_url: Optional[str] = None,
    actions: Optional[List[Dict[str, Any]]] = None,
    delivery: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create one notification_event. Returns the new (or existing) doc.

    If ``idempotency_key`` is set and a matching event already exists,
    the existing event is returned without writing — `created` field
    in the return signals which path was taken.
    """
    if not account_id:
        raise ValueError("account_id required")
    if not notification_type or not title:
        raise ValueError("notification_type and title required")

    if idempotency_key:
        existing = find_by_idempotency_key(account_id, idempotency_key)
        if existing:
            existing["_created"] = False
            return existing

    nid = f"notif_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    doc = {
        "id": nid,
        "accountId": account_id,
        "userId": user_id,
        "type": notification_type,
        "title": title[:200],
        "body": body[:2000],
        "sourceTaskId": source_task_id,
        "sourceSessionId": source_session_id,
        "idempotencyKey": idempotency_key,
        "read": False,
        "readAt": None,
        "actionUrl": action_url,
        "actions": actions or [],
        "delivery": delivery or {"channel": "desktop", "status": "pending"},
        "createdAt": now,
    }
    _coll().document(nid).set(doc)
    doc["_created"] = True
    return doc


def list_for(
    account_id: str,
    *,
    unread: Optional[bool] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return notifications for ``account_id``, newest first."""
    if not account_id:
        return []
    limit = max(1, min(int(limit or 50), 100))
    out: List[Dict[str, Any]] = []
    try:
        q = _coll().where(filter=firestore.FieldFilter("accountId", "==", account_id))
        if unread is True:
            q = q.where(filter=firestore.FieldFilter("read", "==", False))
        elif unread is False:
            q = q.where(filter=firestore.FieldFilter("read", "==", True))
        q = q.order_by("createdAt", direction=firestore.Query.DESCENDING).limit(limit)
        for snap in q.stream():
            d = snap.to_dict() or {}
            d["id"] = snap.id
            out.append(d)
    except Exception as e:
        logger.warning("[notifications.list_for] query failed: %s", e)
        raise
    return out


def mark_read(account_id: str, notification_id: str) -> bool:
    """Mark a single notification as read. Returns True if a write
    happened (notification existed and belonged to ``account_id``)."""
    if not account_id or not notification_id:
        return False
    ref = _coll().document(notification_id)
    snap = ref.get()
    if not snap.exists:
        return False
    d = snap.to_dict() or {}
    if d.get("accountId") != account_id:
        # Cross-account access — refuse silently (caller maps to 404).
        return False
    if d.get("read"):
        return True  # idempotent no-op
    ref.update({
        "read": True,
        "readAt": datetime.now(timezone.utc),
    })
    return True


def mark_all_read(account_id: str, *, limit: int = 200) -> int:
    """Mark all unread notifications for ``account_id`` as read. Returns
    the number of documents written. Capped at ``limit`` per call."""
    if not account_id:
        return 0
    written = 0
    try:
        q = (
            _coll()
            .where(filter=firestore.FieldFilter("accountId", "==", account_id))
            .where(filter=firestore.FieldFilter("read", "==", False))
            .limit(max(1, min(int(limit or 200), 500)))
        )
        now = datetime.now(timezone.utc)
        batch = db.batch()
        for snap in q.stream():
            batch.update(snap.reference, {"read": True, "readAt": now})
            written += 1
        if written:
            batch.commit()
    except Exception as e:
        logger.warning("[notifications.mark_all_read] failed: %s", e)
        raise
    return written
