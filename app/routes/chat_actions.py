"""POST /v1/chat/actions — execute a chat action returned by /v1/chat (Phase 7.5).

Chat responses include an `actions[]` array describing what the user can do
next with the answer (save it as a note, create a TODO, jump to transcript,
copy, rewrite). This endpoint executes the server-side part of those
actions. Purely client-side actions (copy_answer, jump_to_transcript) are
accepted and echoed so traces remain symmetrical, but produce no server
state change.

Contract:

POST /v1/chat/actions
  body: {
    action: ChatAction,
    sessionId?: string,            // required for save_as_note; optional for create_todo
    conversationId?: string,       // optional — pass for better traces
    messageId?: string             // optional
  }

  200 {
    action: {...},                 // echoed
    result: { ... action-specific },
    conversationId, messageId
  }

  422 SCOPE_INVALID        missing required fields
  403 PERMISSION_DENIED    caller cannot edit this session
  404 SESSION_NOT_FOUND    sessionId doesn't exist
  400 BAD_ACTION           unknown type or malformed payload
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import CurrentUser, get_current_user
from app.services import chat_actions as chat_actions_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/chat", tags=["AI Chat v1"])


# ---------------------------------------------------------------------------
# Action body — discriminated union (kept loose so MVP accepts unknown types
# for trace-only purposes; validated inside chat_actions_service.execute).
# ---------------------------------------------------------------------------


class JumpToTranscriptAction(BaseModel):
    type: Literal["jump_to_transcript"]
    targetMs: int
    segmentId: Optional[str] = None


class SaveAsNoteAction(BaseModel):
    type: Literal["save_as_note"]
    payload: Dict[str, Any] = Field(default_factory=dict)


class CreateTodoAction(BaseModel):
    type: Literal["create_todo"]
    payload: Dict[str, Any] = Field(default_factory=dict)


class CopyAnswerAction(BaseModel):
    type: Literal["copy_answer"]


class RewriteAnswerAction(BaseModel):
    type: Literal["rewrite_answer"]
    mode: Literal["slack", "email", "summary"]


ChatActionBody = Union[
    JumpToTranscriptAction,
    SaveAsNoteAction,
    CreateTodoAction,
    CopyAnswerAction,
    RewriteAnswerAction,
]


class ExecuteChatActionRequest(BaseModel):
    action: Dict[str, Any]  # discriminated by `type`; validated by the service
    sessionId: Optional[str] = None
    conversationId: Optional[str] = None
    messageId: Optional[str] = None


class ExecuteChatActionResponse(BaseModel):
    action: Dict[str, Any]
    result: Dict[str, Any]
    conversationId: Optional[str] = None
    messageId: Optional[str] = None


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, chat_actions_service.NotFoundError):
        return HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": str(exc) or "Session not found"}},
        )
    if isinstance(exc, chat_actions_service.ForbiddenError):
        return HTTPException(
            status_code=403,
            detail={"error": {"code": "PERMISSION_DENIED", "message": str(exc) or "No access"}},
        )
    if isinstance(exc, chat_actions_service.BadActionError):
        return HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_ACTION", "message": str(exc)}},
        )
    logger.exception("[chat/actions] unexpected error")
    return HTTPException(
        status_code=500,
        detail={"error": {"code": "INTERNAL_ERROR", "message": "Internal error"}},
    )


# ---------------------------------------------------------------------------
# POST /v1/chat/actions
# ---------------------------------------------------------------------------


@router.post("/actions", response_model=ExecuteChatActionResponse)
async def execute_chat_action(
    body: ExecuteChatActionRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Execute an action returned by /v1/chat.

    Client-side actions (copy_answer, jump_to_transcript) may be called for
    trace symmetry; the server acknowledges them without state change.

    Server-side actions:
      - save_as_note: appends payload.text to sessions/{sessionId}.notes
      - create_todo:  inserts a doc into /todos with source=chat_action

    rewrite_answer returns a `reissue_required` hint; the client re-invokes
    POST /v1/chat with responseMode=rewrite + the hinted preset.
    """
    action = body.action or {}
    if not isinstance(action.get("type"), str):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_ACTION", "message": "action.type required"}},
        )

    try:
        result = await asyncio.to_thread(
            chat_actions_service.execute,
            current_user,
            action,
            body.sessionId,
            body.conversationId,
            body.messageId,
        )
    except Exception as e:  # noqa: BLE001
        raise _map_error(e)

    return ExecuteChatActionResponse(
        action=result["action"],
        result=result["result"],
        conversationId=result.get("conversationId"),
        messageId=result.get("messageId"),
    )
