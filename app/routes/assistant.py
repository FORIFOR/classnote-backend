"""DeepNote Assistant Hub — REST surface (Phase A).

Endpoints:
    POST /v1/assistant/messages
        Submit a question. Routes through assistant_hub → assistant_qna.

    GET /v1/assistant/conversations/{conversationId}
        Stub for Phase A. Returns 200 + empty list so iOS callers
        don't hit 404. Conversation tree lands in Phase B.

    POST /v1/assistant/actions
        Phase B placeholder. Returns 501 in Phase A.

Auth: every route requires a Firebase ID token; the request runs in the
caller's session-owner scope. Cross-account access is impossible
because ``assistant_qna.resolve_session_id`` filters on
``ownerUserId == current_user.uid``.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser
from app.services import assistant_hub

logger = logging.getLogger("app.routes.assistant")

router = APIRouter(prefix="/v1/assistant", tags=["Assistant"])


class AssistantMessageRequest(BaseModel):
    sessionId: Optional[str] = Field(None, description="Target session id; defaults to caller's latest session")
    question: str = Field(..., description="Free-form question, e.g. 「決定事項は？」")
    mode: Optional[str] = Field("session", description="'session' (default) or 'general'")
    channel: Optional[str] = Field("ios", description="ios / desktop / slack / line — for audit")
    idempotencyKey: Optional[str] = Field(None, description="Dedupe key for retries")


class AssistantCitation(BaseModel):
    type: str
    id: str
    snippet: str


class AssistantTokenUsage(BaseModel):
    prompt: int = 0
    completion: int = 0


class AssistantMessageResponse(BaseModel):
    messageId: Optional[str]
    intent: Optional[str]
    answer: str
    citations: List[AssistantCitation] = []
    sessionId: Optional[str]
    tokenUsage: AssistantTokenUsage = AssistantTokenUsage()
    createdAt: Optional[str]
    cached: bool = False


@router.post("/messages", response_model=AssistantMessageResponse)
async def post_message(
    req: AssistantMessageRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    if not (req.question or "").strip():
        raise HTTPException(status_code=422, detail="question is required")

    account_id = getattr(current_user, "account_id", None) or current_user.uid
    result = await assistant_hub.handle_message(
        account_id=account_id,
        owner_uid=current_user.uid,
        question=req.question,
        session_id=req.sessionId,
        mode=(req.mode or "session"),
        channel=(req.channel or "ios"),
        idempotency_key=req.idempotencyKey,
    )
    # Pydantic will coerce the dict; citations field tolerates missing
    # keys via the AssistantCitation model.
    return AssistantMessageResponse(
        messageId=result.get("messageId"),
        intent=result.get("intent"),
        answer=result.get("answer") or "",
        citations=[AssistantCitation(**c) for c in (result.get("citations") or []) if isinstance(c, dict)],
        sessionId=result.get("sessionId"),
        tokenUsage=AssistantTokenUsage(**(result.get("tokenUsage") or {})),
        createdAt=result.get("createdAt"),
        cached=bool(result.get("cached")),
    )


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Phase A stub: return an empty list so iOS doesn't 404. Phase B
    will return the actual conversation tree."""
    return {"conversationId": conversation_id, "messages": []}


@router.post("/actions")
async def post_action(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Phase B endpoint placeholder."""
    raise HTTPException(status_code=501, detail="Assistant actions ship in Phase B")
