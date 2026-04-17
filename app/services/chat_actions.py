"""Server-side execution for chat `actions[]` returned by /v1/chat (Phase 7.5).

The chat response emits structured actions (save_as_note, create_todo,
copy_answer, rewrite_answer, jump_to_transcript). Client-side actions
(copy_answer, jump_to_transcript) are handled entirely in the UI — no
server call is needed. This module implements the server-side execution
for the remaining two:

  - save_as_note  → append text to `sessions/{id}.notes`
  - create_todo   → insert a doc into `/todos/{todoId}` scoped by account

rewrite_answer is intentionally not a server action: clients re-invoke
`POST /v1/chat` with `responseMode=rewrite` + the desired preset. The
server does not need a parallel path.

All actions are permission-gated through `compute_permissions` (session
scope). Idempotency is provided by a content-hash on the action payload
so double-tap on the "save" button doesn't produce duplicates within a
short window.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

from app.firebase import db
from app.services.session_projection import compute_permissions


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChatActionError(Exception):
    pass


class NotFoundError(ChatActionError):
    pass


class ForbiddenError(ChatActionError):
    pass


class BadActionError(ChatActionError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_session_for_action(session_id: str, user) -> Dict[str, Any]:
    snap = db.collection("sessions").document(session_id).get()
    if not snap.exists:
        raise NotFoundError("session not found")
    data = snap.to_dict() or {}
    data["id"] = session_id
    perms = compute_permissions(data, user)
    if not perms["canView"]:
        raise ForbiddenError("permission denied")
    return data


def _idempotency_hash(user_uid: str, action_type: str, payload: Dict[str, Any], session_id: Optional[str]) -> str:
    payload_str = str(sorted((payload or {}).items()))
    raw = f"{user_uid}|{action_type}|{session_id or ''}|{payload_str}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _recent_idempotency_exists(hash_key: str, ttl_seconds: int = 30) -> bool:
    """Best-effort check: within ttl_seconds, has the same action fired?"""
    try:
        snap = db.collection("chat_action_idempotency").document(hash_key).get()
        if not snap.exists:
            return False
        created = (snap.to_dict() or {}).get("createdAt")
        if not created or not hasattr(created, "timestamp"):
            return False
        if (datetime.now(timezone.utc).timestamp() - created.timestamp()) > ttl_seconds:
            return False
        return True
    except Exception:
        return False


def _record_idempotency(hash_key: str, result: Dict[str, Any]) -> None:
    try:
        db.collection("chat_action_idempotency").document(hash_key).set(
            {
                "createdAt": firestore.SERVER_TIMESTAMP,
                "result": result,
            }
        )
    except Exception as e:
        logger.warning(f"[chat_actions] idempotency record failed: {e}")


# ---------------------------------------------------------------------------
# Individual action executors
# ---------------------------------------------------------------------------


def execute_save_as_note(
    user,
    session_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Append `payload.text` to `sessions/{id}.notes` (newline-separated).

    Permission: requires owner (canEdit=true). Shared viewers cannot edit notes.
    """
    text = (payload or {}).get("text")
    if not isinstance(text, str) or not text.strip():
        raise BadActionError("save_as_note requires payload.text")

    data = _load_session_for_action(session_id, user)
    perms = compute_permissions(data, user)
    if not perms["canEditNotes"]:
        raise ForbiddenError("cannot edit notes on this session")

    existing = data.get("notes") or ""
    separator = "\n\n" if existing and not existing.endswith("\n") else ""
    merged = f"{existing}{separator}{text.strip()}"

    now = datetime.now(timezone.utc)
    db.collection("sessions").document(session_id).set(
        {
            "notes": merged,
            "notesUpdatedAt": now,
            "updatedAt": now,
        },
        merge=True,
    )
    logger.info(
        f"[chat_actions] save_as_note session={session_id} user={user.uid} appended_chars={len(text)}"
    )
    return {
        "sessionId": session_id,
        "notesUpdatedAt": now.isoformat(),
        "appendedChars": len(text),
    }


def execute_create_todo(
    user,
    session_id: Optional[str],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a TODO under `/todos` scoped by account.

    Follows the same shape as `app/routes/todos.py:create_todo` so the TODO
    list UI renders it natively. If `session_id` is provided, links the
    TODO back to the source session (`source.createdFrom = "chat_action"`).
    """
    raw_text = (payload or {}).get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise BadActionError("create_todo requires payload.text")

    text = raw_text.strip()
    title = text[:140]  # TodoCreateRequest.title length cap
    notes = text[140:].strip() if len(text) > 140 else ""

    owner = (payload or {}).get("owner")
    due = (payload or {}).get("due")

    # Permission check on session if given
    source: Optional[Dict[str, Any]] = None
    if session_id:
        data = _load_session_for_action(session_id, user)
        perms = compute_permissions(data, user)
        if not perms["canEditTags"]:  # "can interact with session-scoped TODO"
            raise ForbiddenError("cannot create TODO for this session")
        source = {
            "sessionId": session_id,
            "sessionTitle": data.get("title", "Untitled"),
            "createdFrom": "chat_action",
            "evidence": None,
        }

    now = datetime.now(timezone.utc)
    today_iso = date.today().isoformat()

    todo_ref = db.collection("todos").document()
    todo_data: Dict[str, Any] = {
        "accountId": user.account_id,
        "title": title,
        "notes": notes or None,
        "dueDate": due or today_iso,
        "status": "open",
        "priority": "mid",
        "source": source,
        "origin": {
            "extractorVersion": "chat_action",
            "confidence": 1.0,
            "autoCreated": True,
            "userEdited": False,
            "userMoved": False,
        },
        "dedupe": {
            "semanticKey": hashlib.sha256(f"{title}|{session_id or 'general'}".encode()).hexdigest()[:16],
            "rejectedByUser": False,
        },
        "assigneeOwner": owner,
        "createdAt": now,
        "updatedAt": now,
        "createdByUid": user.uid,
    }
    todo_ref.set(todo_data)

    logger.info(
        f"[chat_actions] create_todo id={todo_ref.id} account={user.account_id} session={session_id}"
    )
    return {
        "todoId": todo_ref.id,
        "title": title,
        "dueDate": todo_data["dueDate"],
        "sessionId": session_id,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def execute(
    user,
    action: Dict[str, Any],
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a chat action to the correct handler.

    Returns the execution result for the client to reflect in UI.
    """
    if not isinstance(action, dict):
        raise BadActionError("action must be an object")

    action_type = action.get("type")
    payload = action.get("payload") or {}

    # Idempotency: collapse double-taps within 30s
    hash_key = _idempotency_hash(user.uid, action_type, payload, session_id)
    if _recent_idempotency_exists(hash_key):
        logger.info(f"[chat_actions] idempotent hit for {action_type} user={user.uid}")
        return {
            "action": action,
            "result": {"status": "idempotent_hit"},
            "conversationId": conversation_id,
            "messageId": message_id,
        }

    if action_type == "save_as_note":
        if not session_id:
            raise BadActionError("save_as_note requires sessionId")
        result = execute_save_as_note(user, session_id, payload)
    elif action_type == "create_todo":
        result = execute_create_todo(user, session_id, payload)
    elif action_type == "copy_answer":
        # Client-side action; no server effect. Echo for trace symmetry.
        result = {"status": "noop_client_side"}
    elif action_type == "jump_to_transcript":
        # Client-side navigation; no server effect.
        result = {"status": "noop_client_side", "targetMs": action.get("targetMs")}
    elif action_type == "rewrite_answer":
        # Clients should re-invoke POST /v1/chat with responseMode=rewrite
        # + the appropriate preset. Return a hint.
        result = {
            "status": "reissue_required",
            "hint": {
                "responseMode": "rewrite",
                "presetHint": {
                    "slack": "short_share",
                    "email": "short_share",
                    "summary": "summarize",
                }.get(action.get("mode"), "summarize"),
            },
        }
    else:
        raise BadActionError(f"unknown action type: {action_type}")

    # Log for audit / evaluation
    _record_idempotency(
        hash_key,
        {
            "actionType": action_type,
            "sessionId": session_id,
            "result": result,
        },
    )
    try:
        db.collection("chat_action_log").add(
            {
                "actionType": action_type,
                "sessionId": session_id,
                "conversationId": conversation_id,
                "messageId": message_id,
                "uid": user.uid,
                "accountId": user.account_id,
                "payload": payload,
                "result": result,
                "createdAt": firestore.SERVER_TIMESTAMP,
            }
        )
    except Exception as e:
        logger.warning(f"[chat_actions] action log failed: {e}")

    return {
        "action": action,
        "result": result,
        "conversationId": conversation_id,
        "messageId": message_id,
    }
