"""summary_postprocess.py — summaryJson post-processing to patch UX gaps that LLM output misses.

Runs after the LLM returns summaryJson inside generate_summary_and_tags. Pure mutation
of the summary dict; no LLM calls, no Firestore I/O. Failures are logged and swallowed
so a postprocess defect never fails a successful summary.

Responsibilities:
  #1 ensure_overview      — synthesize overview from bottomLine / whyItMatters / highlights when empty
  #2 fallback_timeline    — derive rule-based timeline from transcript chunks when LLM returned []
  #4 decorate_ui_fields   — attach isInferred / missingFields / uiSeverity / uiLabel to decisions / todos

Entry point: finalize_summary_json(summary_json, segments=...)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple

logger = logging.getLogger("app.summary_postprocess")

_OVERVIEW_MIN_LEN = 200
_TIMELINE_CAP = 8
_TIMELINE_TITLE_CAP = 20
_TIMELINE_SUMMARY_CAP = 100
_TIMELINE_MIN_SENTENCE_LEN = 5
_TIMELINE_QUALITY_MIN_SUMMARY = 10
_FALLBACK_OVERVIEW_PLACEHOLDER = "(要確認: 本文が十分に生成されませんでした)"
_UNKNOWN_OWNER_TOKENS = {"", "不明", "未定", "unknown", "unassigned"}
_UNKNOWN_DUE_TOKENS = {"", "期限不明", "未定", "unknown"}

_FILLER_PREFIX = re.compile(r"^(?:えー(?:っと|と)?|あー|あのー?|あの、|まあ|はい(?:はい)?[、,]?|ええと|まず)+")
_LEADING_PUNCT = re.compile(r"^[、。，,.\s　]+")
_SENTENCE_SPLIT = re.compile(r"[。！？\n]+")


# --- #1 overview -----------------------------------------------------------


def ensure_overview(summary_json: MutableMapping[str, Any]) -> None:
    existing = (summary_json.get("overview") or "").strip()
    if len(existing) >= _OVERVIEW_MIN_LEN:
        return

    parts: List[str] = []
    bottom = (summary_json.get("bottomLine") or "").strip()
    if bottom:
        parts.append(bottom)
    why = (summary_json.get("whyItMatters") or "").strip()
    if why:
        parts.append(why)

    highlight_texts: List[str] = []
    for h in _coerce_list(summary_json.get("highlights"))[:5]:
        if isinstance(h, Mapping):
            t = (h.get("text") or "").strip()
        elif isinstance(h, str):
            t = h.strip()
        else:
            t = ""
        if t:
            highlight_texts.append(f"・{t}")
    if highlight_texts:
        parts.append("\n".join(highlight_texts))

    synthesized = "\n\n".join(parts).strip()
    summary_json["overview"] = synthesized or _FALLBACK_OVERVIEW_PLACEHOLDER


# --- #2 timeline fallback --------------------------------------------------


def fallback_timeline(
    summary_json: MutableMapping[str, Any],
    segments: Optional[Iterable[Any]],
) -> None:
    existing = _coerce_list(summary_json.get("timeline"))
    if _timeline_quality_ok(existing):
        return
    if not segments:
        return

    items: List[dict] = []
    for seg in segments:
        start_ms = _seg_int(seg, "startMs")
        end_ms = _seg_int(seg, "endMs")
        text = _seg_str(seg, "text")
        if not text:
            continue
        title, summary_text = _summarize_chunk_for_timeline(text)
        if not summary_text:
            continue
        items.append({
            "startSec": int((start_ms or 0) / 1000),
            "endSec": int((end_ms or 0) / 1000),
            "title": title,
            "summary": summary_text,
            "topicChange": False,
            "fallback": True,
        })
        if len(items) >= _TIMELINE_CAP:
            break

    if items:
        summary_json["timeline"] = items


def _timeline_quality_ok(items: List[Any]) -> bool:
    """True if the existing LLM-provided timeline is trustworthy enough to keep."""
    if not items:
        return False
    meaningful = 0
    for it in items:
        if not isinstance(it, Mapping):
            continue
        summary = (it.get("summary") or "").strip()
        if len(summary) >= _TIMELINE_QUALITY_MIN_SUMMARY:
            meaningful += 1
    # Require majority of items to have meaningful summary text.
    return meaningful >= max(1, len(items) // 2 + (1 if len(items) % 2 else 0))


def _summarize_chunk_for_timeline(text: str) -> Tuple[str, str]:
    """Derive (title, summary) from a raw transcript chunk.

    Drops filler prefixes, splits into sentences, keeps only sentences above
    _TIMELINE_MIN_SENTENCE_LEN, and uses the first 1-2 as summary. Title is the
    first meaningful sentence truncated to _TIMELINE_TITLE_CAP.
    """
    cleaned = _FILLER_PREFIX.sub("", text.strip()).strip()
    cleaned = _LEADING_PUNCT.sub("", cleaned)
    if not cleaned:
        trimmed = text.strip()
        return (trimmed[:_TIMELINE_TITLE_CAP] or "(断片)", trimmed[:_TIMELINE_SUMMARY_CAP])

    raw_sentences = [s.strip() for s in _SENTENCE_SPLIT.split(cleaned) if s.strip()]
    sentences = [s for s in raw_sentences if len(s) >= _TIMELINE_MIN_SENTENCE_LEN]
    if not sentences and raw_sentences:
        sentences = raw_sentences  # fallback: accept even short sentences

    if not sentences:
        return (cleaned[:_TIMELINE_TITLE_CAP], cleaned[:_TIMELINE_SUMMARY_CAP])

    first = sentences[0]
    if len(first) < _TIMELINE_TITLE_CAP // 2 and len(sentences) > 1:
        title_src = first + "、" + sentences[1]
    else:
        title_src = first
    title = title_src[:_TIMELINE_TITLE_CAP]
    if len(title_src) > _TIMELINE_TITLE_CAP:
        title = title.rstrip("、, ") + "…"

    joined = "。".join(sentences[:2])
    if not joined.endswith("。"):
        joined += "。"
    if len(joined) > _TIMELINE_SUMMARY_CAP:
        joined = joined[: _TIMELINE_SUMMARY_CAP - 1].rstrip("、, ") + "…"

    return (title, joined)


# --- #4 UI decoration ------------------------------------------------------


def decorate_ui_fields(summary_json: MutableMapping[str, Any]) -> None:
    for key in ("decisions", "todos"):
        raw = _coerce_list(summary_json.get(key))
        if not raw:
            continue
        decorated: List[dict] = []
        for item in raw:
            if not isinstance(item, MutableMapping):
                decorated.append(item)  # type: ignore[arg-type]
                continue
            _decorate_item(item)
            decorated.append(dict(item))
        summary_json[key] = decorated


def _decorate_item(item: MutableMapping[str, Any]) -> None:
    status = (item.get("status") or "").lower()
    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    is_inferred = status == "inferred" or confidence < 0.6
    item.setdefault("isInferred", is_inferred)

    missing: List[str] = []
    owner = (item.get("owner") or "").strip().lower()
    if owner in _UNKNOWN_OWNER_TOKENS:
        missing.append("owner")
    due = (item.get("due") or "").strip()
    if due.lower() in _UNKNOWN_DUE_TOKENS or due == "期限不明":
        missing.append("due")
    item.setdefault("missingFields", missing)

    priority = (item.get("priority") or "").lower()
    blocking_raw = item.get("blocking")
    blocking = str(blocking_raw).lower() in {"true", "1", "yes"}
    if priority == "high" or blocking:
        severity = "high"
    elif priority == "low" or (is_inferred and confidence < 0.4):
        severity = "low"
    else:
        severity = "mid"
    item.setdefault("uiSeverity", severity)

    if is_inferred:
        label = "要確認"
    elif "owner" in missing:
        label = "担当未定"
    elif "due" in missing:
        label = "期限未定"
    else:
        label = ""
    item.setdefault("uiLabel", label)


# --- entry point -----------------------------------------------------------


def finalize_summary_json(
    summary_json: Any,
    *,
    segments: Optional[Iterable[Any]] = None,
) -> Any:
    if not isinstance(summary_json, MutableMapping):
        return summary_json
    try:
        ensure_overview(summary_json)
    except Exception as exc:
        logger.warning(f"[summary_postprocess] ensure_overview failed (non-fatal): {exc}")
    try:
        fallback_timeline(summary_json, segments)
    except Exception as exc:
        logger.warning(f"[summary_postprocess] fallback_timeline failed (non-fatal): {exc}")
    try:
        decorate_ui_fields(summary_json)
    except Exception as exc:
        logger.warning(f"[summary_postprocess] decorate_ui_fields failed (non-fatal): {exc}")
    return summary_json


# --- helpers ---------------------------------------------------------------


def _coerce_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _seg_int(seg: Any, key: str) -> Optional[int]:
    v = _seg_get(seg, key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _seg_str(seg: Any, key: str) -> str:
    v = _seg_get(seg, key)
    if v is None:
        return ""
    return str(v)


def _seg_get(seg: Any, key: str) -> Any:
    if isinstance(seg, Mapping):
        return seg.get(key)
    return getattr(seg, key, None)
