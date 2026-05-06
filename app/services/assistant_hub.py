"""DeepNote Assistant Hub — central tool dispatch for chat-driven ops.

Phase A scope is intentionally tiny: route ``message`` requests to the
Q&A engine, persist a flat audit row in ``assistant_messages``, and
return a structured response. Phase B will add ``actions`` (export PDF,
share with confirmation, schedule task) and conversation threading.

Idempotency:
    Caller may pass an ``idempotencyKey`` in the request. If a previous
    message with the same key exists for the same account, we return
    the cached response — important for Slack / LINE retry storms and
    for iOS rapid double-taps.

Audit:
    Every call writes to ``assistant_messages/{messageId}`` regardless
    of intent. This gives us a per-account ledger of what users asked
    and what we answered, which is essential for support and for any
    future "show me what the bot has been doing" admin view.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.firebase import db

logger = logging.getLogger("app.services.assistant_hub")

MESSAGES_COLLECTION = "assistant_messages"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _find_idempotent_message(account_id: str, idem: str) -> Optional[Dict[str, Any]]:
    if not account_id or not idem:
        return None
    try:
        q = (
            db.collection(MESSAGES_COLLECTION)
            .where("accountId", "==", account_id)
            .where("idempotencyKey", "==", idem)
            .limit(1)
        )
        for snap in q.stream():
            d = snap.to_dict() or {}
            d["messageId"] = snap.id
            return d
    except Exception as e:
        logger.warning("[hub] idempotency lookup failed: %s", e)
    return None


async def handle_message(
    *,
    account_id: str,
    owner_uid: str,
    question: str,
    session_id: Optional[str],
    mode: str = "session",
    channel: str = "ios",
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Top-level entry. Returns a dict with the assistant_messages row
    shape plus the resolved answer. Routes are responsible for HTTP
    error handling; this fn raises only on programmer error.
    """
    if not question or not question.strip():
        return {
            "messageId": None,
            "intent": "unknown",
            "answer": "質問を入力してください。",
            "citations": [],
            "sessionId": session_id,
            "tokenUsage": {"prompt": 0, "completion": 0},
            "createdAt": _now().isoformat(),
        }

    if idempotency_key:
        cached = _find_idempotent_message(account_id, idempotency_key)
        if cached:
            return {
                "messageId": cached.get("messageId"),
                "intent": cached.get("intent"),
                "answer": cached.get("answer"),
                "citations": cached.get("citations") or [],
                "sessionId": cached.get("sessionId"),
                "tokenUsage": cached.get("tokenUsage") or {"prompt": 0, "completion": 0},
                "createdAt": cached.get("createdAt"),
                "cached": True,
            }

    from app.services import assistant_qna

    resolved_session_id = assistant_qna.resolve_session_id(session_id, owner_uid, account_id)
    if mode != "session" or not resolved_session_id:
        # Phase A: only session mode. General mode reserved for Phase D.
        if mode == "session" and not resolved_session_id:
            return {
                "messageId": None,
                "intent": "unknown",
                "answer": "対象の会議が見つかりません。会議を録音・要約してから再度お試しください。",
                "citations": [],
                "sessionId": None,
                "tokenUsage": {"prompt": 0, "completion": 0},
                "createdAt": _now().isoformat(),
            }
        # Fall back to session mode if iOS sent a different mode in
        # Phase A; Phase D will introduce general mode handling.
        pass

    qa = await assistant_qna.answer(
        question=question,
        session_id=resolved_session_id or "",
        owner_account_id=account_id,
    )

    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    record = {
        "accountId": account_id,
        "ownerUid": owner_uid,
        "channel": channel,
        "question": question[:1000],
        "answer": (qa.get("answer") or "")[:4000],
        "intent": qa.get("intent"),
        "sessionId": resolved_session_id,
        "citations": qa.get("citations") or [],
        "tokenUsage": qa.get("tokenUsage") or {"prompt": 0, "completion": 0},
        "idempotencyKey": idempotency_key,
        "createdAt": _now(),
    }
    try:
        db.collection(MESSAGES_COLLECTION).document(msg_id).set(record)
    except Exception as e:
        logger.warning("[hub] persist message failed: %s", e)

    return {
        "messageId": msg_id,
        "intent": qa.get("intent"),
        "answer": qa.get("answer"),
        "citations": qa.get("citations") or [],
        "sessionId": resolved_session_id,
        "tokenUsage": qa.get("tokenUsage") or {"prompt": 0, "completion": 0},
        "createdAt": record["createdAt"].isoformat(),
    }
