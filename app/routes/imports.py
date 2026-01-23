import logging
import re
import uuid
from datetime import timedelta
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import CurrentUser, get_current_user
from app.routes.sessions import (
    _ensure_session_meta,
    _now_timestamp,
    _session_doc_ref,
    _upsert_session_member,
)
from app.task_queue import enqueue_quiz_task, enqueue_summarize_task
from app.util_models import ImportYouTubeRequest, ImportYouTubeResponse, YouTubeCheckRequest, YouTubeCheckResponse, YouTubeTrack

router = APIRouter()
logger = logging.getLogger("app.imports")

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
    )
    YT_TRANSCRIPT_AVAILABLE = True
except ImportError:
    YouTubeTranscriptApi = None
    TranscriptsDisabled = NoTranscriptFound = VideoUnavailable = None
    YT_TRANSCRIPT_AVAILABLE = False

@router.post("/imports/youtube/check", response_model=YouTubeCheckResponse)
async def check_youtube_transcript(req: YouTubeCheckRequest):
    """
    [vNext] Verifies if a YouTube video has available transcripts before import.
    """
    if not YT_TRANSCRIPT_AVAILABLE:
        raise HTTPException(status_code=503, detail="youtube-transcript-api is not installed")

    try:
        video_id = _parse_youtube_video_id(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        # Utilizing the library to list available transcripts
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        tracks = []
        for t in transcript_list:
            tracks.append(YouTubeTrack(
                language=t.language,
                language_code=t.language_code,
                is_generated=t.is_generated,
                is_translatable=t.is_translatable
            ))
            
        return YouTubeCheckResponse(
            videoId=video_id,
            available=len(tracks) > 0,
            tracks=tracks
        )

    except TranscriptsDisabled:
        return YouTubeCheckResponse(videoId=video_id, available=False, reason="transcripts_disabled")
    except NoTranscriptFound:
        return YouTubeCheckResponse(videoId=video_id, available=False, reason="no_transcript")
    except VideoUnavailable:
        return YouTubeCheckResponse(videoId=video_id, available=False, reason="video_unavailable")
    except Exception as e:
        logger.error(f"Unexpected error checking YouTube video {video_id}: {e}")
        return YouTubeCheckResponse(videoId=video_id, available=False, reason="internal_error")




def _parse_youtube_video_id(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("URL is required")

    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", cleaned):
        return cleaned

    parsed = urlparse(cleaned)
    if not parsed.netloc and not cleaned.startswith("http"):
        parsed = urlparse(f"https://{cleaned}")
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if host in ("youtu.be", "www.youtu.be"):
        video_id = path.lstrip("/").split("/")[0]
        if video_id:
            return video_id

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        if path.startswith("/watch"):
            query = parse_qs(parsed.query)
            video_id = (query.get("v") or [None])[0]
            if video_id:
                return video_id
        for prefix in ("/shorts/", "/embed/", "/v/"):
            if path.startswith(prefix):
                video_id = path[len(prefix):].split("/")[0]
                if video_id:
                    return video_id

    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{6,})", cleaned)
    if match:
        return match.group(1)

    raise ValueError("Invalid YouTube URL")


def _build_language_priority(language: Optional[str]) -> List[str]:
    langs: List[str] = []
    for value in (language, "ja", "en"):
        if value and value not in langs:
            langs.append(value)
    return langs


def _format_transcript(items: List[dict]) -> str:
    texts: List[str] = []
    for item in items:
        text = (item.get("text") or "").strip()
        if text:
            texts.append(text)
    return "\n".join(texts)


def _infer_duration_sec(items: List[dict]) -> Optional[float]:
    if not items:
        return None
    last = items[-1]
    start = last.get("start")
    duration = last.get("duration") or 0
    if start is None:
        return None
    try:
        return float(start) + float(duration)
    except (TypeError, ValueError):
        return None


@router.post("/imports/youtube", response_model=ImportYouTubeResponse)
async def import_youtube(req: ImportYouTubeRequest, current_user: CurrentUser = Depends(get_current_user)):
    try:
        # Simple Validation using regex or urllib
        # Use existing parser to ensure it's a youtube ID, but pass the full URL to worker
        # logic: if we can extract ID, it's likely valid.
        video_id = _parse_youtube_video_id(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = _now_timestamp()
    owner_uid = current_user.uid
    # ID format: mode-timestamp-uuid
    session_id = f"{req.mode}-{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:6]}"
    title = (req.title or "").strip() or "YouTube取り込み"

    # Determine status based on provided transcript
    has_transcript = bool(req.transcriptText and req.transcriptText.strip())
    if not has_transcript and not YT_TRANSCRIPT_AVAILABLE:
        raise HTTPException(status_code=503, detail="youtube-transcript-api is not installed")
    initial_status = "recording_finished" if has_transcript else "queued"
    
    # Initial Session Data
    data = {
        "title": title,
        "mode": req.mode,
        "userId": owner_uid,
        "ownerId": owner_uid,
        "ownerUserId": owner_uid,
        "ownerUid": owner_uid,
        "status": initial_status,
        "visibility": "private",
        "participantUserIds": [],
        "autoTags": [],
        "topicSummary": None,
        "createdAt": now,
        "startedAt": now,
        "startAt": now,
        "endAt": now,
        "endedAt": now,
        "durationSec": 0, # Unknown initially
        "audioPath": None, # Could be None for text-only import
        "transcriptText": req.transcriptText if has_transcript else "",
        "transcriptSource": req.source or "youtube",
        "transcriptLang": req.transcriptLang or req.language, # [NEW]
        "isAutoGenerated": req.isAutoGenerated, # [NEW]
        "summaryStatus": "pending",
        "summaryError": None,
        "quizStatus": "pending",
        "quizError": None,
        "playlistStatus": "pending",
        "playlistError": None,
        "sharedWith": {},
        "sourceType": "youtube",
        "sourceUrl": req.url,
        "sourceVideoId": video_id,
    }

    doc_ref = _session_doc_ref(session_id)
    doc_ref.set(data)

    _ensure_session_meta(owner_uid, session_id, "OWNER", last_opened_at=now)
    _upsert_session_member(
        session_id=session_id,
        user_id=owner_uid,
        role="owner",
        source="owner",
        display_name=current_user.display_name,
    )

    if has_transcript:
        # Transcript provided: Bypass download/STT and trigger Summary/Quiz directly
        try:
            enqueue_summarize_task(session_id, user_id=owner_uid)
            enqueue_quiz_task(session_id, user_id=owner_uid)
        except Exception as exc:
            logger.exception(f"Failed to enqueue summary/quiz for {session_id}: {exc}")
            # Non-blocking error?
    else:
        # No transcript: Enqueue Import Task (Server-side) - Likely to fail if IP blocked
        from app.task_queue import enqueue_youtube_import_task
        try:
            enqueue_youtube_import_task(session_id, req.url, language=req.language or "ja", user_id=owner_uid)
        except Exception as exc:
            # If enqueue fails, mark as failed
            logger.exception(f"Failed to enqueue youtube import for {session_id}: {exc}")
            doc_ref.update({"status": "failed", "errorMessage": "Failed to enqueue task"})
            raise HTTPException(status_code=500, detail="Failed to enqueue import task")

    return ImportYouTubeResponse(
        sessionId=session_id,
        transcriptStatus="processing", # UI might show spinner
        summaryStatus="pending",
        quizStatus="pending",
        sourceUrl=req.url,
    )
