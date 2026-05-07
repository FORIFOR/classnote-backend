"""DeepNote Notifications REST surface (V-037).

Routes:
    GET    /v1/notifications                                  list (account-scoped)
    POST   /v1/notifications/{notificationId}:markRead        single mark read
    POST   /v1/notifications:markAllRead                      bulk mark read

Auth: Firebase ID token required on all routes; account scope is enforced
in the service layer (`notifications.list_for`, `mark_read`, `mark_all_read`).

Storage / dispatcher: see ``app/services/notifications.py``. Writes happen
from the scheduler dispatcher (``destination.channel == "desktop"``);
clients only read here. The corresponding canonical OpenAPI definitions
live in ``deepnote-contracts/api/openapi.yaml`` under
``/v1/notifications`` paths.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.dependencies import get_current_user, CurrentUser
from app.services import notifications as _notif

logger = logging.getLogger("app.routes.notifications")

router = APIRouter(prefix="/v1/notifications", tags=["Notifications"])


# ──────────────────────────────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────────────────────────────

class NotificationActionItem(BaseModel):
    key: str
    label: str
    url: Optional[str] = None


class NotificationEvent(BaseModel):
    id: str
    accountId: Optional[str] = None
    userId: Optional[str] = None
    type: str
    title: str
    body: str
    sourceTaskId: Optional[str] = None
    sourceSessionId: Optional[str] = None
    read: bool = False
    actionUrl: Optional[str] = None
    actions: List[NotificationActionItem] = []
    delivery: Optional[Dict[str, Any]] = None
    createdAt: datetime
    readAt: Optional[datetime] = None


class NotificationListResponse(BaseModel):
    items: List[NotificationEvent]
    nextCursor: Optional[str] = None  # reserved for future pagination


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=NotificationListResponse)
def list_notifications(
    unread: Optional[bool] = Query(None, description="True → only unread, False → only read, omit → all"),
    limit: int = Query(50, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    items = _notif.list_for(account_id, unread=unread, limit=limit)
    return NotificationListResponse(
        items=[NotificationEvent(**i) for i in items],
        nextCursor=None,
    )


@router.post("/{notification_id}:markRead", status_code=204)
def mark_notification_read(
    notification_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    if not _notif.mark_read(account_id, notification_id):
        # Treat both "not found" and "wrong account" as 404 — never leak
        # whether a different account owns the id.
        raise HTTPException(status_code=404, detail="notification_not_found")
    return None


class MarkAllReadResponse(BaseModel):
    written: int


@router.post(":markAllRead", response_model=MarkAllReadResponse)
def mark_all_notifications_read(
    current_user: CurrentUser = Depends(get_current_user),
):
    account_id = getattr(current_user, "account_id", None) or current_user.uid
    n = _notif.mark_all_read(account_id)
    return MarkAllReadResponse(written=n)
