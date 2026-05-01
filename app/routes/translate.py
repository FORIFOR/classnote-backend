"""
Translation API — Uses Gemini LLM for multilingual-to-Japanese translation.
Falls back to Google Cloud Translation v2 for non-Japanese targets.
Keeps API credentials server-side and provides in-memory caching.
"""

import re
import time
import logging
from collections import OrderedDict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies import get_current_user, CurrentUser
from app.middleware.rate_limit import limiter

logger = logging.getLogger("app.translate")

router = APIRouter(prefix="/translate", tags=["Translation"])


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

class TranslateRequest(BaseModel):
    text: str = Field(..., max_length=5000, description="Text to translate")
    source: str = Field(default="en", description="Source language code")
    target: str = Field(default="ja", description="Target language code")


class TranslateResponse(BaseModel):
    translated: str
    source: str
    target: str
    cached: bool = False


# ---------------------------------------------------------------------------
# In-memory LRU cache (best-effort on Cloud Run — instance-scoped)
# ---------------------------------------------------------------------------

_cache: OrderedDict = OrderedDict()
_CACHE_MAX = 1000
_CACHE_TTL = 3600  # 1 hour


def _cache_get(text: str, source: str, target: str) -> Optional[str]:
    key = (text.strip().lower(), source, target)
    entry = _cache.get(key)
    if entry is None:
        return None
    translated, ts = entry
    if time.monotonic() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return translated


def _cache_set(text: str, source: str, target: str, translated: str):
    key = (text.strip().lower(), source, target)
    _cache[key] = (translated, time.monotonic())
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Lazy-init Cloud Translation v2 client (uses ADC on Cloud Run)
# ---------------------------------------------------------------------------

_translate_client = None


def _get_translate_client():
    global _translate_client
    if _translate_client is None:
        from google.cloud import translate_v2 as translate
        _translate_client = translate.Client()
        logger.info("Cloud Translation v2 client initialized")
    return _translate_client


# ---------------------------------------------------------------------------
# Multilingual detection
# ---------------------------------------------------------------------------

_RE_HANGUL = re.compile(r'[\uAC00-\uD7AF]')
_RE_CJK = re.compile(r'[\u4E00-\u9FFF\u3400-\u4DBF]')
_RE_LATIN = re.compile(r'[A-Za-z]')


def _is_multilingual(text: str) -> bool:
    """Detect if text contains 2+ distinct scripts (CJK, Hangul, Latin)."""
    scripts_found = 0
    if _RE_CJK.search(text):
        scripts_found += 1
    if _RE_HANGUL.search(text):
        scripts_found += 1
    if len(_RE_LATIN.findall(text)) > 5:
        scripts_found += 1
    return scripts_found >= 2


# ---------------------------------------------------------------------------
# LLM translation (Gemini) for multilingual → Japanese
# ---------------------------------------------------------------------------

async def _translate_with_llm(text: str, target_lang: str = "日本語") -> str:
    """Use Gemini LLM for reliable multilingual translation."""
    from app.services.llm import translate_text as llm_translate
    return await llm_translate(text, target_lang)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("", response_model=TranslateResponse)
@limiter.limit("60/minute")
async def translate_text(
    request: Request,
    body: TranslateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Translate text. Uses Gemini LLM for multilingual input targeting Japanese."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    # Cache check
    cached = _cache_get(text, body.source, body.target)
    if cached is not None:
        return TranslateResponse(
            translated=cached,
            source=body.source,
            target=body.target,
            cached=True,
        )

    try:
        # For Japanese target: always use LLM to ensure complete translation
        # Google Translate v2 can only handle one source language per request,
        # so multilingual text (Chinese+Korean+English) gets partially translated.
        if body.target == "ja":
            translated = await _translate_with_llm(text, "日本語")
        else:
            # Non-Japanese targets: use Google Translate v2
            source_lang = None if body.source == "auto" else body.source
            client = _get_translate_client()
            result = client.translate(
                text,
                source_language=source_lang,
                target_language=body.target,
            )
            translated = result["translatedText"]
    except Exception as e:
        logger.error(f"Translation failed for uid={current_user.uid}: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Translation service error")

    _cache_set(text, body.source, body.target, translated)

    return TranslateResponse(
        translated=translated,
        source=body.source,
        target=body.target,
        cached=False,
    )
