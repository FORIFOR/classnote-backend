"""/v1/session-details/* — read model endpoints for SessionDetailScreen.

Frontends (Desktop / iOS) should call:
  - GET /v1/session-details/{sessionId}                  # full projection (meta only)
  - GET /v1/session-details/{sessionId}/overview         # full summary payload
  - GET /v1/session-details/{sessionId}/quiz             # full quiz questions
  - GET /v1/session-details/{sessionId}/notes            # full notes markdown
  - GET /v1/session-details/{sessionId}/transcript       # paginated chunks

These coexist with legacy /sessions/{id} endpoints; no breaking change.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import CurrentUser, get_current_user
from app.services import session_projection
from app.services.session_projection import (
    ForbiddenError,
    NotFoundError,
    ProjectionContext,
    ProjectionError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/session-details", tags=["Session Details"])


def _handle_error(exc: Exception):
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail={"error": {"code": "SESSION_NOT_FOUND", "message": "Session not found"}})
    if isinstance(exc, ForbiddenError):
        raise HTTPException(status_code=403, detail={"error": {"code": "PERMISSION_DENIED", "message": "No access"}})
    if isinstance(exc, ProjectionError):
        logger.exception("[session-details] projection error")
        raise HTTPException(status_code=500, detail={"error": {"code": "PROJECTION_ERROR", "message": str(exc)}})
    raise exc


@router.get("/{session_id}")
async def get_session_detail(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Aggregate read model for session detail screen.

    Returns header / context / audio / overview / transcript / notes / quiz /
    playlist / permissions / jobs / share / uiHints, with `partials` flags
    for any section that failed to load.
    """
    ctx = ProjectionContext(user=current_user, session_id=session_id)
    try:
        return await session_projection.build_session_detail(ctx)
    except (NotFoundError, ForbiddenError, ProjectionError) as e:
        _handle_error(e)


@router.get("/{session_id}/overview")
async def get_session_overview(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Full structured Summary v2 payload + markdown.

    All bullets include `evidence: []` array (possibly empty for legacy data).
    """
    ctx = ProjectionContext(user=current_user, session_id=session_id)
    try:
        return await session_projection.fetch_overview_full(ctx)
    except (NotFoundError, ForbiddenError, ProjectionError) as e:
        _handle_error(e)


@router.get("/{session_id}/quiz")
async def get_session_quiz(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Full quiz payload with questions / choices / correctChoiceIds / explanation / evidence."""
    ctx = ProjectionContext(user=current_user, session_id=session_id)
    try:
        return await session_projection.fetch_quiz_full(ctx)
    except (NotFoundError, ForbiddenError, ProjectionError) as e:
        _handle_error(e)


@router.get("/{session_id}/notes")
async def get_session_notes(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Full notes markdown body."""
    ctx = ProjectionContext(user=current_user, session_id=session_id)
    try:
        return await session_projection.fetch_notes_full(ctx)
    except (NotFoundError, ForbiddenError, ProjectionError) as e:
        _handle_error(e)


@router.get("/{session_id}/transcript")
async def get_session_transcript(
    session_id: str,
    limit: int = Query(200, ge=1, le=500),
    cursor: Optional[int] = Query(None, description="Last transcript chunk index from previous page"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Paginated transcript chunks. Use `nextCursor` to fetch subsequent pages."""
    ctx = ProjectionContext(user=current_user, session_id=session_id)
    try:
        return await session_projection.fetch_transcript_slice(ctx, limit=limit, cursor=cursor)
    except (NotFoundError, ForbiddenError, ProjectionError) as e:
        _handle_error(e)
