"""
YouTube Transcript Fetcher using youtube-transcript-api.
Lightweight alternative to yt-dlp + STT approach.
"""

import logging
import re
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )
    YOUTUBE_TRANSCRIPT_AVAILABLE = True
except ImportError:
    YouTubeTranscriptApi = None
    NoTranscriptFound = TranscriptsDisabled = VideoUnavailable = None
    YOUTUBE_TRANSCRIPT_AVAILABLE = False

logger = logging.getLogger("app.services.youtube")


def extract_video_id(url: str) -> str:
    """
    Extract YouTube video ID from various URL formats.
    Supports: youtube.com/watch, youtu.be, shorts, embed
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("URL is required")

    # Direct video ID (e.g., "dQw4w9WgXcQ")
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", cleaned):
        return cleaned

    parsed = urlparse(cleaned)
    if not parsed.netloc and not cleaned.startswith("http"):
        parsed = urlparse(f"https://{cleaned}")
    
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    # youtu.be/<id>
    if host in ("youtu.be", "www.youtu.be"):
        video_id = path.lstrip("/").split("/")[0].split("?")[0]
        if video_id:
            return video_id

    # youtube.com variants
    if "youtube.com" in host or "youtube-nocookie.com" in host:
        # /watch?v=<id>
        if path.startswith("/watch"):
            query = parse_qs(parsed.query)
            video_id = (query.get("v") or [None])[0]
            if video_id:
                return video_id
        
        # /shorts/<id>, /embed/<id>, /v/<id>
        for prefix in ("/shorts/", "/embed/", "/v/"):
            if path.startswith(prefix):
                video_id = path[len(prefix):].split("/")[0].split("?")[0]
                if video_id:
                    return video_id

    # Fallback regex
    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", cleaned)
    if match:
        return match.group(1)

    raise ValueError("Could not extract videoId from URL")


def build_language_priority(language: Optional[str]) -> List[str]:
    """Build language priority list with fallbacks."""
    langs: List[str] = []
    for value in (language, "ja", "en"):
        if value and value not in langs:
            langs.append(value)
    return langs


def format_transcript_text(items: List[dict]) -> str:
    """Convert transcript items to plain text."""
    return "\n".join(
        (item.get("text") or "").strip()
        for item in items
        if (item.get("text") or "").strip()
    )


def format_transcript_srt(items: List[dict]) -> str:
    """Convert transcript items to SRT format."""
    def fmt(t: float) -> str:
        ms = int(round(t * 1000))
        h = ms // 3600000; ms %= 3600000
        m = ms // 60000; ms %= 60000
        s = ms // 1000; ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    out = []
    for i, item in enumerate(items, start=1):
        start = float(item.get("start", 0))
        duration = float(item.get("duration", 0))
        end = start + duration
        text = (item.get("text") or "").strip()
        if text:
            out.append(str(i))
            out.append(f"{fmt(start)} --> {fmt(end)}")
            out.append(text)
            out.append("")
    return "\n".join(out)


def format_transcript_vtt(items: List[dict]) -> str:
    """Convert transcript items to WebVTT format."""
    def fmt(t: float) -> str:
        ms = int(round(t * 1000))
        h = ms // 3600000; ms %= 3600000
        m = ms // 60000; ms %= 60000
        s = ms // 1000; ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    out = ["WEBVTT", ""]
    for item in items:
        start = float(item.get("start", 0))
        duration = float(item.get("duration", 0))
        end = start + duration
        text = (item.get("text") or "").strip()
        if text:
            out.append(f"{fmt(start)} --> {fmt(end)}")
            out.append(text)
            out.append("")
    return "\n".join(out)


def infer_duration_sec(items: List[dict]) -> Optional[float]:
    """Infer video duration from last transcript item."""
    if not items:
        return None
    last = items[-1]
    start = last.get("start")
    duration = last.get("duration", 0)
    if start is None:
        return None
    try:
        return float(start) + float(duration)
    except (TypeError, ValueError):
        return None


def fetch_youtube_transcript(
    video_id: str,
    languages: Optional[List[str]] = None,
    format: str = "text"
) -> dict:
    """
    Fetch YouTube transcript using youtube-transcript-api.
    
    Args:
        video_id: YouTube video ID
        languages: List of language codes in priority order (default: ["ja", "en"])
        format: Output format - "json", "text", "srt", "vtt"
    
    Returns:
        dict with keys: videoId, items (raw), text/srt/vtt (formatted), durationSec, language
    
    Raises:
        ValueError: On fetch failure with descriptive message
    """
    if languages is None:
        languages = ["ja", "en"]

    if not YOUTUBE_TRANSCRIPT_AVAILABLE:
        raise ValueError("youtube-transcript-api is not installed")
    
    logger.info(f"Fetching transcript for video {video_id} with languages {languages}")
    
    try:
        items = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
    except TranscriptsDisabled:
        raise ValueError("この動画では字幕が無効化されています")
    except NoTranscriptFound:
        raise ValueError(f"指定された言語 ({', '.join(languages)}) の字幕が見つかりませんでした")
    except VideoUnavailable:
        raise ValueError("動画が利用できません（非公開または削除済み）")
    except Exception as e:
        logger.exception(f"Transcript fetch failed for {video_id}")
        raise ValueError(f"字幕の取得に失敗しました: {str(e)}")
    
    # Detect which language was actually returned
    # youtube-transcript-api returns in priority order, so we got the first available
    detected_lang = languages[0] if languages else "unknown"
    
    result = {
        "videoId": video_id,
        "items": items,
        "durationSec": infer_duration_sec(items),
        "language": detected_lang,
    }
    
    # Format output
    fmt = format.lower()
    if fmt == "json":
        pass  # items already included
    elif fmt == "text":
        result["text"] = format_transcript_text(items)
    elif fmt == "srt":
        result["srt"] = format_transcript_srt(items)
    elif fmt == "vtt":
        result["vtt"] = format_transcript_vtt(items)
    else:
        result["text"] = format_transcript_text(items)
    
    logger.info(f"Transcript fetched: {len(items)} segments, ~{result['durationSec']:.0f}s" if result['durationSec'] else f"Transcript fetched: {len(items)} segments")
    
    return result


def process_youtube_import(session_id: str, url: str, language: str = "ja") -> str:
    """
    Process YouTube import for a session.
    This is the main entry point called by the task worker.
    
    Returns:
        Full transcript text
    """
    # Extract video ID
    video_id = extract_video_id(url)
    
    # Build language priority
    languages = build_language_priority(language)
    
    # Fetch transcript
    result = fetch_youtube_transcript(video_id, languages=languages, format="text")
    
    return result.get("text", "")
