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

from fastapi import APIRouter, Depends, HTTPException
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
