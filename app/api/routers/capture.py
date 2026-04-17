"""
Capture domain — audio, transcript, STT control, imports, translate.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request, Query
from app.dependencies import get_current_user, CurrentUser

logger = logging.getLogger("app.api.capture")
router = APIRouter(tags=["Capture"])


# ── Audio ───────────────────────────────────────────

@router.get("/sessions/{session_id}/audio")
async def get_audio(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import get_audio_url as _get
    return await _get(session_id=session_id, current_user=current_user)


@router.post("/sessions/{session_id}/audio/uploads")
async def prepare_upload(session_id: str, request: Request, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import prepare_audio_upload as _prepare, AudioPrepareRequest
    body = await request.json()
    req = AudioPrepareRequest(**body)
    return await _prepare(request=request, session_id=session_id, body=req, current_user=current_user)


@router.post("/sessions/{session_id}/audio/uploads/{upload_id}:commit")
async def commit_upload(session_id: str, upload_id: str, request: Request, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import commit_audio_upload as _commit, AudioCommitRequest
    body = await request.json()
    req = AudioCommitRequest(**body)
    return await _commit(session_id=session_id, body=req, current_user=current_user)


@router.delete("/sessions/{session_id}/audio")
async def delete_audio(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import delete_audio as _delete
    return await _delete(session_id=session_id, current_user=current_user)


# ── Transcript ──────────────────────────────────────

@router.get("/sessions/{session_id}/transcript")
async def get_transcript(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import get_artifact_transcript as _get
    return await _get(session_id=session_id, current_user=current_user)


@router.put("/sessions/{session_id}/transcript")
async def update_transcript(session_id: str, request: Request, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import update_transcript as _update, TranscriptUpdateRequest
    body = await request.json()
    req = TranscriptUpdateRequest(**body)
    return await _update(session_id=session_id, body=req, current_user=current_user)


# ── Transcript chunks/segments — DEPRECATED aliases ─────
# canonical:
#   GET  /sessions/{id}/transcript_chunks
#   POST /sessions/{id}/transcript_chunks:append
#   POST /sessions/{id}/transcript_chunks:replace
#   GET  /sessions/{id}/transcript_segments
# これらは既に app/routes/sessions.py に定義されている。ここでは deprecated alias のみ。

@router.get(
    "/sessions/{session_id}/transcript/chunks",
    deprecated=True,
    include_in_schema=False,
)
async def legacy_get_chunks(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.deprecation import log_deprecated_path
    log_deprecated_path(
        request,
        user_id=current_user.uid,
        replacement="/sessions/{session_id}/transcript_chunks",
    )
    from app.routes.sessions import get_session_transcript_chunks as _get
    return await _get(session_id=session_id, current_user=current_user)


@router.post(
    "/sessions/{session_id}/transcript/chunks",
    deprecated=True,
    include_in_schema=False,
)
async def legacy_append_chunks(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.deprecation import log_deprecated_path
    log_deprecated_path(
        request,
        user_id=current_user.uid,
        replacement="/sessions/{session_id}/transcript_chunks:append",
    )
    from app.routes.sessions import append_transcript_chunks as _append, TranscriptChunkAppendRequest
    body = await request.json()
    req = TranscriptChunkAppendRequest(**body)
    return await _append(session_id=session_id, body=req, current_user=current_user)


@router.put(
    "/sessions/{session_id}/transcript/chunks",
    deprecated=True,
    include_in_schema=False,
)
async def legacy_replace_chunks(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.deprecation import log_deprecated_path
    log_deprecated_path(
        request,
        user_id=current_user.uid,
        replacement="/sessions/{session_id}/transcript_chunks:replace",
    )
    from app.routes.sessions import replace_transcript_chunks as _replace, TranscriptChunkReplaceRequest
    body = await request.json()
    req = TranscriptChunkReplaceRequest(**body)
    return await _replace(session_id=session_id, body=req, current_user=current_user)


@router.get(
    "/sessions/{session_id}/transcript/segments",
    deprecated=True,
    include_in_schema=False,
)
async def legacy_get_segments(
    session_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    limit: int = 200,
):
    from app.services.deprecation import log_deprecated_path
    log_deprecated_path(
        request,
        user_id=current_user.uid,
        replacement="/sessions/{session_id}/transcript_segments",
    )
    from app.routes.sessions import get_transcript_segments as _get
    return await _get(
        session_id=session_id,
        current_user=current_user,
        from_ms=from_ms,
        to_ms=to_ms,
        limit=limit,
    )


# ── STT Control ─────────────────────────────────────

@router.post("/sessions/{session_id}/capture/cloud-stt:start")
async def cloud_stt_start(session_id: str, request: Request, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import start_cloud_stt as _start
    body = await request.json() if (await request.body()) else {}
    return await _start(session_id=session_id, body=body, current_user=current_user)


@router.post("/sessions/{session_id}/capture/transcription:retry")
async def transcription_retry(session_id: str, request: Request, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.sessions import retry_transcription as _retry
    body = await request.json() if (await request.body()) else {}
    return await _retry(session_id=session_id, current_user=current_user)


# ── Imports ─────────────────────────────────────────

@router.post("/sessions/{session_id}/imports/transcript")
async def import_transcript(session_id: str, request: Request, current_user: CurrentUser = Depends(get_current_user)):
    """Thin wrapper over /sessions/{id}/import:transcript.

    Fix (2026-04-18): Previously passed the raw dict as `body=body`, but the
    downstream handler declares `body: TranscriptUploadRequest` (Pydantic
    model), causing `TypeError: import_session_transcript() got an
    unexpected keyword argument 'body'` when called directly (routes
    can't re-use FastAPI's dependency injection). We now parse the dict
    into the Pydantic model explicitly before forwarding.
    """
    from app.routes.sessions import import_session_transcript as _import
    from app.util_models import TranscriptUploadRequest
    body_json = await request.json()
    body_model = TranscriptUploadRequest(**body_json)
    return await _import(session_id=session_id, body=body_model, current_user=current_user)


@router.post("/sessions/{session_id}/imports/audio")
async def import_audio(session_id: str, request: Request, current_user: CurrentUser = Depends(get_current_user)):
    """Thin wrapper over /sessions/{id}/import:audio.

    Fix (2026-04-18): The downstream handler takes `durationSec: float`
    via `Body(..., embed=True)`, NOT a `body=` kwarg. The previous
    implementation passed `body=body` which raised
    `TypeError: import_session_audio() got an unexpected keyword argument 'body'`
    — visible to users as "文字起こしのサーバー保存に失敗しました（500）".
    We now extract `durationSec` from the JSON payload and pass it
    positionally-correctly.
    """
    from app.routes.sessions import import_session_audio as _import
    body_json = await request.json()
    duration_sec = float(body_json.get("durationSec") or 0)
    return await _import(
        session_id=session_id,
        durationSec=duration_sec,
        current_user=current_user,
    )


@router.post("/imports/youtube/check")
async def youtube_check(request: Request):
    from app.routes.imports import check_youtube_transcript as _check, YouTubeCheckRequest
    body = await request.json()
    req = YouTubeCheckRequest(**body)
    return await _check(req=req)


@router.post("/imports/youtube")
async def youtube_import(request: Request, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.imports import import_youtube as _import
    body = await request.json()
    return await _import(body=body, current_user=current_user)


# ── Translate ───────────────────────────────────────

@router.post("/translate")
async def translate(request: Request, current_user: CurrentUser = Depends(get_current_user)):
    from app.routes.translate import translate_text as _translate
    body = await request.json()
    return await _translate(body=body, current_user=current_user)
