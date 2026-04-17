"""Conversation management endpoints for the AI Chat v2 contract (Phase 7.4).

Provides explicit conversation CRUD so clients can:
  - Create a conversation ahead of the first user message
    (useful for drafting / pre-loading UI state)
  - List all conversations for a user / session
  - Fetch a conversation's metadata plus the latest page of messages
    in a single call (replaces the two-step "metadata + messages" dance)

Coexists with POST /v1/chat (which auto-creates conversations when given
no conversationId) — both entry points produce identical Firestore docs.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from google.cloud import firestore

from app.dependencies import CurrentUser, get_current_user
from app.firebase import db
from app.services import session_chat
from app.services.session_chat import ChatContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/chat/conversations", tags=["AI Chat v1"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


ChatScopeType = Literal["session", "general", "multi_session", "overlay_live"]
ChatSurface = Literal[
    "desktop_session_detail",
    "ios_session_detail",
    "global_chat",
    "overlay",
]


class ChatScopeIn(BaseModel):
    type: ChatScopeType
    sessionId: Optional[str] = None
    sessionIds: Optional[List[str]] = None


class ConversationSummary(BaseModel):
    conversationId: str
    scope: Dict[str, Any]
    ownerAccountId: Optional[str] = None
    surface: Optional[str] = None
    title: Optional[str] = None
    lastMessagePreview: Optional[str] = None
    messageCount: int = 0
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None
    archived: bool = False


class CreateConversationRequest(BaseModel):
    scope: ChatScopeIn
    surface: Optional[ChatSurface] = None
    title: Optional[str] = Field(None, max_length=120)


class CreateConversationResponse(BaseModel):
    conversation: ConversationSummary


class ListConversationsResponse(BaseModel):
    conversations: List[ConversationSummary]
    nextCursor: Optional[str] = None


class ConversationMessage(BaseModel):
    messageId: str
    role: Literal["user", "assistant"]
    text: Optional[str] = None
    citations: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    mode: Optional[str] = None
    usedModel: Optional[str] = None
    createdAt: Optional[str] = None
    clientSortKey: Optional[int] = None


class ConversationDetailResponse(BaseModel):
    conversation: ConversationSummary
    messages: List[ConversationMessage]
    nextCursor: Optional[int] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isoformat(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    return None


def _conversation_to_summary(data: Dict[str, Any], doc_id: str) -> ConversationSummary:
    scope = data.get("scope") or {}
    return ConversationSummary(
        conversationId=data.get("conversationId") or doc_id,
        scope=scope,
        ownerAccountId=data.get("ownerAccountId"),
        surface=data.get("surface"),
        title=data.get("title"),
        lastMessagePreview=data.get("lastMessagePreview"),
        messageCount=int(data.get("messageCount") or 0),
        createdAt=_isoformat(data.get("createdAt")),
        updatedAt=_isoformat(data.get("updatedAt")),
        archived=bool(data.get("archived", False)),
    )


def _conversation_ref_for_scope(
    scope: ChatScopeIn, conversation_id: str, account_id: str
):
    if scope.type == "session":
        if not scope.sessionId:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "SCOPE_INVALID", "message": "scope.sessionId required"}},
            )
        return (
            db.collection("sessions")
            .document(scope.sessionId)
            .collection("conversations")
            .document(conversation_id)
        )
    # general / multi_session / overlay_live → account-level
    return (
        db.collection("accounts")
        .document(account_id)
        .collection("conversations")
        .document(conversation_id)
    )


def _ensure_can_view_session(current_user: CurrentUser, session_id: str) -> None:
    ctx = ChatContext(
        user=current_user,
        scope={"type": "session", "sessionId": session_id},
        message="",
    )
    try:
        session_chat._load_session(ctx)
    except session_chat.NotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "Session not found"}},
        )
    except session_chat.ForbiddenError:
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "PERMISSION_DENIED", "message": "No access"}},
        )


# ---------------------------------------------------------------------------
# POST /v1/chat/conversations
# ---------------------------------------------------------------------------


@router.post("", response_model=CreateConversationResponse)
async def create_conversation(
    body: CreateConversationRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create an empty conversation.

    If the scope is `session`, the caller must have at least viewer access.
    The returned `conversationId` should be passed as `conversationId` on the
    very first `POST /v1/chat` turn so the server won't mint a new one.
    """
    if body.scope.type == "session":
        _ensure_can_view_session(current_user, body.scope.sessionId or "")
    if body.scope.type == "multi_session" and not body.scope.sessionIds:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "SCOPE_INVALID", "message": "scope.sessionIds required for multi_session"}},
        )

    conversation_id = f"conv_{uuid.uuid4().hex[:16]}"
    conv_ref = _conversation_ref_for_scope(body.scope, conversation_id, current_user.account_id)

    payload: Dict[str, Any] = {
        "conversationId": conversation_id,
        "scope": body.scope.model_dump(),
        "ownerAccountId": current_user.account_id,
        "surface": body.surface,
        "title": body.title,
        "schemaVersion": 2,
        "messageCount": 0,
        "archived": False,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }

    def _write():
        conv_ref.set(payload, merge=False)

    await asyncio.to_thread(_write)

    summary = ConversationSummary(
        conversationId=conversation_id,
        scope=body.scope.model_dump(),
        ownerAccountId=current_user.account_id,
        surface=body.surface,
        title=body.title,
        messageCount=0,
        archived=False,
    )
    return CreateConversationResponse(conversation=summary)


# ---------------------------------------------------------------------------
# GET /v1/chat/conversations
# ---------------------------------------------------------------------------


@router.get("", response_model=ListConversationsResponse)
async def list_conversations(
    scope: Optional[ChatScopeType] = Query(None, description="filter by scope.type"),
    sessionId: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List conversations owned by the caller, filtered by scope."""
    if scope == "session" and not sessionId:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "SCOPE_INVALID", "message": "sessionId required when scope=session"}},
        )

    results: List[ConversationSummary] = []

    if scope in (None, "session") and sessionId:
        _ensure_can_view_session(current_user, sessionId)

        def _load_session_convs():
            q = (
                db.collection("sessions")
                .document(sessionId)
                .collection("conversations")
                .order_by("updatedAt", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            return list(q.stream())

        try:
            docs = await asyncio.to_thread(_load_session_convs)
            for doc in docs:
                data = doc.to_dict() or {}
                # Filter to own account
                if data.get("ownerAccountId") and data.get("ownerAccountId") != current_user.account_id:
                    continue
                results.append(_conversation_to_summary(data, doc.id))
        except Exception as e:
            logger.warning(f"[conversations] session conv list failed: {e}")

    if scope in (None, "general", "multi_session", "overlay_live") and not sessionId:
        def _load_account_convs():
            q = (
                db.collection("accounts")
                .document(current_user.account_id)
                .collection("conversations")
                .order_by("updatedAt", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            return list(q.stream())

        try:
            docs = await asyncio.to_thread(_load_account_convs)
            for doc in docs:
                data = doc.to_dict() or {}
                sc = data.get("scope") or {}
                if scope and sc.get("type") != scope:
                    continue
                results.append(_conversation_to_summary(data, doc.id))
        except Exception as e:
            logger.warning(f"[conversations] account conv list failed: {e}")

    return ListConversationsResponse(conversations=results)


# ---------------------------------------------------------------------------
# GET /v1/chat/conversations/{conversationId}
# ---------------------------------------------------------------------------


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: str,
    sessionId: Optional[str] = Query(None, description="required when scope=session"),
    scope: Optional[ChatScopeType] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    before: Optional[int] = Query(None, description="clientSortKey of oldest message in previous page"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Fetch conversation metadata + the latest page of messages."""
    # Permission gate + ref resolution
    if sessionId:
        _ensure_can_view_session(current_user, sessionId)
        conv_ref = (
            db.collection("sessions")
            .document(sessionId)
            .collection("conversations")
            .document(conversation_id)
        )
        ctx = ChatContext(
            user=current_user,
            scope={"type": "session", "sessionId": sessionId},
            message="",
            conversation_id=conversation_id,
        )
    else:
        conv_ref = (
            db.collection("accounts")
            .document(current_user.account_id)
            .collection("conversations")
            .document(conversation_id)
        )
        ctx = ChatContext(
            user=current_user,
            scope={"type": "general"},
            message="",
            conversation_id=conversation_id,
        )

    def _load():
        snap = conv_ref.get()
        if not snap.exists:
            return None
        return snap.to_dict() or {}

    data = await asyncio.to_thread(_load)
    if not data:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "CONVERSATION_NOT_FOUND", "message": "Conversation not found"}},
        )

    if data.get("ownerAccountId") and data.get("ownerAccountId") != current_user.account_id:
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "PERMISSION_DENIED", "message": "No access to this conversation"}},
        )

    summary = _conversation_to_summary(data, conversation_id)

    msgs_raw = await asyncio.to_thread(
        session_chat.fetch_conversation_messages,
        ctx,
        conversation_id,
        limit,
        before,
    )

    messages = [
        ConversationMessage(
            messageId=m["messageId"],
            role=m["role"],
            text=m.get("text"),
            citations=m.get("citations") or [],
            actions=m.get("actions") or [],
            mode=m.get("mode"),
            usedModel=m.get("usedModel"),
            createdAt=m.get("createdAt"),
            clientSortKey=m.get("clientSortKey"),
        )
        for m in msgs_raw
        if m.get("role") in ("user", "assistant")
    ]

    next_cursor = (
        messages[-1].clientSortKey
        if messages and len(messages) == limit
        else None
    )

    return ConversationDetailResponse(
        conversation=summary,
        messages=messages,
        nextCursor=next_cursor,
    )
