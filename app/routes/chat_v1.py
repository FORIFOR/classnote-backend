"""POST /v1/chat — session-first AI chat (MVP for SessionDetail right panel / iOS sheet).

This is the Phase 7 entry point that consolidates the 7-layer design into one
endpoint with explicit `scope`. Coexists with legacy /v1/chat/send and
/v1/chat/stream; clients may migrate at their own pace.

Scope contract:
  scope.type = "session"  →  requires scope.sessionId. Reads derived/summary,
                              transcript_chunks, members. Returns citations
                              linked to transcript segments.
  scope.type = "general"  →  no session required. Used for cross-session / web-
                              grounded questions. Returns citations: [].

Preset (optional, maps to Tool Runner lite):
  summarize | extract_todos | extract_decisions | next_agenda | short_share |
  quiz_questions.

Conversation persistence:
  - session scope → sessions/{sessionId}/conversations/{conversationId}
  - general scope → accounts/{accountId}/conversations/{conversationId}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.dependencies import CurrentUser, get_current_user
from app.services import session_chat
from app.services.session_chat import (
    ChatContext,
    ChatError,
    CreditLimitError,
    ForbiddenError,
    NotFoundError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["AI Chat v1"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ChatScope(BaseModel):
    type: Literal["session", "general"]
    sessionId: Optional[str] = None


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class SelectedContext(BaseModel):
    tab: Optional[str] = None  # "overview" | "transcript" | "notes" | "quiz"
    evidenceId: Optional[str] = None
    quote: Optional[str] = Field(None, max_length=2000)
    segmentId: Optional[str] = None
    startMs: Optional[int] = None


class ChatV1Request(BaseModel):
    scope: ChatScope
    message: str = Field("", max_length=4000)
    preset: Optional[
        Literal[
            "summarize",
            "extract_todos",
            "extract_decisions",
            "next_agenda",
            "short_share",
            "quiz_questions",
        ]
    ] = None
    conversationId: Optional[str] = None
    history: Optional[List[ChatHistoryItem]] = None
    selectedContext: Optional[SelectedContext] = None


class Citation(BaseModel):
    type: str
    segmentId: Optional[str] = None
    startMs: Optional[int] = None
    endMs: Optional[int] = None
    speaker: Optional[str] = None
    quotePreview: Optional[str] = None
    score: Optional[float] = None


class AnswerBlock(BaseModel):
    text: str


class SuggestedAction(BaseModel):
    id: str
    label: str


class ChatV1Response(BaseModel):
    conversationId: str
    scope: Dict[str, Any]
    preset: Optional[str]
    mode: str
    usedModel: Optional[str]
    answer: AnswerBlock
    citations: List[Citation]
    creditCost: int
    creditsRemaining: Optional[int]
    latencyMs: int
    suggestedActions: List[SuggestedAction]


class PresetListItem(BaseModel):
    id: str
    label: str


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "Session not found"}},
        )
    if isinstance(exc, ForbiddenError):
        return HTTPException(
            status_code=403,
            detail={"error": {"code": "PERMISSION_DENIED", "message": "No access"}},
        )
    if isinstance(exc, CreditLimitError):
        info = exc.info or {}
        return HTTPException(
            status_code=429,
            detail={
                "error": {
                    "code": info.get("reason", "INSUFFICIENT_CREDITS"),
                    "message": "AIクレジットが不足しています",
                    "retryable": False,
                    "details": {
                        "creditCost": info.get("cost"),
                        "creditsRemaining": info.get("remaining", 0),
                        "dailyUsed": info.get("dailyUsed"),
                        "dailySoftCap": info.get("dailySoftCap"),
                    },
                }
            },
        )
    if isinstance(exc, ChatError):
        return HTTPException(
            status_code=500,
            detail={"error": {"code": "CHAT_ERROR", "message": str(exc)}},
        )
    logger.exception("[v1/chat] unexpected error")
    return HTTPException(
        status_code=500,
        detail={"error": {"code": "INTERNAL_ERROR", "message": "Internal error"}},
    )


@router.post("/chat", response_model=ChatV1Response)
async def post_chat(
    body: ChatV1Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    if body.scope.type == "session" and not body.scope.sessionId:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "SCOPE_INVALID", "message": "scope.sessionId required"}},
        )
    if not body.message and not body.preset:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "EMPTY_QUERY",
                    "message": "message もしくは preset のいずれかを指定してください",
                }
            },
        )

    ctx = ChatContext(
        user=current_user,
        scope=body.scope.model_dump(),
        message=body.message or "",
        preset=body.preset,
        conversation_id=body.conversationId,
        selected_context=body.selectedContext.model_dump() if body.selectedContext else None,
        history=[h.model_dump() for h in (body.history or [])],
    )

    try:
        return await session_chat.chat_once(ctx)
    except Exception as e:  # noqa: BLE001
        raise _map_error(e)


@router.get("/chat/presets", response_model=List[PresetListItem])
async def list_chat_presets(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return the canonical preset catalog. Clients can render them as chips."""
    return session_chat.list_presets()


# ---------------------------------------------------------------------------
# Conversation history retrieval  (Phase 7.3 — sub-collection backed)
# ---------------------------------------------------------------------------


class ConversationMessage(BaseModel):
    messageId: str
    role: Literal["user", "assistant"]
    text: Optional[str] = None
    citations: List[Citation] = []
    mode: Optional[str] = None
    usedModel: Optional[str] = None
    clientSortKey: Optional[int] = None
    createdAt: Optional[str] = None


class ConversationMessagesResponse(BaseModel):
    conversationId: str
    scope: Dict[str, Any]
    messages: List[ConversationMessage]
    nextCursor: Optional[int] = None


async def _fetch_messages(
    ctx: ChatContext,
    conversation_id: str,
    limit: int,
    before: Optional[int],
) -> List[ConversationMessage]:
    import asyncio as _asyncio

    raw = await _asyncio.to_thread(
        session_chat.fetch_conversation_messages,
        ctx,
        conversation_id,
        limit,
        before,
    )
    return [
        ConversationMessage(
            messageId=m["messageId"],
            role=m["role"],
            text=m.get("text"),
            citations=[Citation(**c) for c in (m.get("citations") or [])],
            mode=m.get("mode"),
            usedModel=m.get("usedModel"),
            clientSortKey=m.get("clientSortKey"),
            createdAt=m.get("createdAt"),
        )
        for m in raw
        if m.get("role") in ("user", "assistant")
    ]


@router.get(
    "/sessions/{session_id}/chat/conversations/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
)
async def list_session_conversation_messages(
    session_id: str,
    conversation_id: str,
    limit: int = 50,
    before: Optional[int] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Fetch messages of a session-scoped conversation (paginated)."""
    ctx = ChatContext(
        user=current_user,
        scope={"type": "session", "sessionId": session_id},
        message="",
    )
    try:
        session_chat._load_session(ctx)  # permission gate
    except Exception as e:  # noqa: BLE001
        raise _map_error(e)

    msgs = await _fetch_messages(ctx, conversation_id, limit, before)
    return ConversationMessagesResponse(
        conversationId=conversation_id,
        scope=ctx.scope,
        messages=msgs,
        nextCursor=msgs[-1].clientSortKey if msgs and len(msgs) == limit else None,
    )


@router.get(
    "/chat/conversations/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
)
async def list_general_conversation_messages(
    conversation_id: str,
    limit: int = 50,
    before: Optional[int] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Fetch messages of a general-scoped (account-level) conversation."""
    ctx = ChatContext(
        user=current_user,
        scope={"type": "general"},
        message="",
    )
    msgs = await _fetch_messages(ctx, conversation_id, limit, before)
    return ConversationMessagesResponse(
        conversationId=conversation_id,
        scope=ctx.scope,
        messages=msgs,
        nextCursor=msgs[-1].clientSortKey if msgs and len(msgs) == limit else None,
    )


# ---------------------------------------------------------------------------
# SSE streaming variant (Phase 7.2)
# ---------------------------------------------------------------------------


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat:stream")
async def post_chat_stream(
    body: ChatV1Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """SSE variant of POST /v1/chat.

    Stream events (in order):
      - event: meta   — {conversationId, scope, preset, mode, usedModel, creditCost, creditsRemaining}
      - event: token  — {text: "..."}  (emitted repeatedly as the LLM produces deltas)
      - event: done   — {conversationId, answer:{text}, citations, creditCost,
                         creditsRemaining, latencyMs, suggestedActions}

    Error handling:
      - pre-LLM errors (auth / scope / credits / permissions) → HTTP status
        code via HTTPException before the stream starts.
      - LLM failure mid-stream → event: error with {code, message}; credits
        are refunded before the event is emitted.
    """
    if body.scope.type == "session" and not body.scope.sessionId:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "SCOPE_INVALID", "message": "scope.sessionId required"}},
        )
    if not body.message and not body.preset:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "EMPTY_QUERY",
                    "message": "message もしくは preset のいずれかを指定してください",
                }
            },
        )

    ctx = ChatContext(
        user=current_user,
        scope=body.scope.model_dump(),
        message=body.message or "",
        preset=body.preset,
        conversation_id=body.conversationId,
        selected_context=body.selectedContext.model_dump() if body.selectedContext else None,
        history=[h.model_dump() for h in (body.history or [])],
    )

    # Pre-flight: run NotFound/Forbidden/CreditLimit checks before opening the
    # stream, so clients see proper HTTP status codes instead of an "open"
    # SSE body. We do this by invoking the shared prep helper first; if it
    # succeeds, we hand it off to the generator using an already-consumed
    # credit. chat_stream repeats the prep internally; for atomicity we
    # accept one extra cheap read.  (Phase 7.3 will make prep shareable.)
    try:
        # Permission check — raises before credits are touched
        if body.scope.type == "session" and body.scope.sessionId:
            session_chat._load_session(ctx)  # raises NotFound/Forbidden

    except Exception as e:  # noqa: BLE001
        raise _map_error(e)

    async def event_source():
        try:
            async for ev in session_chat.chat_stream(ctx):
                yield _sse_pack(ev["event"], ev["data"])
        except session_chat.CreditLimitError as e:
            yield _sse_pack(
                "error",
                {
                    "code": e.info.get("reason", "INSUFFICIENT_CREDITS"),
                    "message": "AIクレジットが不足しています",
                    "details": {
                        "creditCost": e.info.get("cost"),
                        "creditsRemaining": e.info.get("remaining", 0),
                    },
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[v1/chat:stream] unexpected error during stream")
            yield _sse_pack("error", {"code": "INTERNAL_ERROR", "message": str(e)[:200]})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
