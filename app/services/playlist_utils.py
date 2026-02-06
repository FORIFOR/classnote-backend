from __future__ import annotations

from typing import List, Optional, Dict, Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _duration_from_segments(segments: Optional[List[dict]]) -> Optional[float]:
    if not segments:
        return None
    max_end = 0.0
    for seg in segments:
        end = _safe_float(seg.get("endSec") or seg.get("end") or 0.0, 0.0)
        if end > max_end:
            max_end = end
    return max_end or None


def _min_chapter_duration(duration_sec: Optional[float]) -> float:
    if not duration_sec:
        return 30.0
    if duration_sec <= 120:
        return 10.0
    if duration_sec <= 600:
        return 20.0
    return 30.0


def _detect_timebase_and_convert(items_raw: List[dict], duration_sec: Optional[float]) -> tuple[List[dict], str]:
    """
    [NEW] Detect if playlist items are in milliseconds and convert to seconds.

    Heuristic:
    - If max(startSec, endSec) > 10000 and duration_sec < 3600, likely milliseconds
    - Or if max value is > 10x duration_sec, likely milliseconds

    Returns (converted_items, detected_timebase)
    """
    if not items_raw:
        return items_raw, "audio_seconds"

    # Find max timing value in raw items
    max_value = 0.0
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        start = _safe_float(it.get("startSec") or it.get("start_sec") or it.get("start"))
        end = _safe_float(it.get("endSec") or it.get("end_sec") or it.get("end"))
        max_value = max(max_value, start, end)

    # Detect if values are likely in milliseconds
    is_milliseconds = False
    if max_value > 10000:
        # Large values: likely milliseconds if duration is reasonable
        if duration_sec and duration_sec < 7200:  # < 2 hours
            is_milliseconds = True
        elif not duration_sec and max_value > 100000:  # > 100k without duration hint
            is_milliseconds = True

    # Also check if max_value >> duration_sec (10x threshold)
    if duration_sec and max_value > duration_sec * 10:
        is_milliseconds = True

    if not is_milliseconds:
        return items_raw, "audio_seconds"

    # Convert milliseconds to seconds
    converted = []
    for it in items_raw:
        if not isinstance(it, dict):
            converted.append(it)
            continue
        it_copy = dict(it)
        for key in ("startSec", "start_sec", "start", "endSec", "end_sec", "end"):
            if key in it_copy and it_copy[key] is not None:
                it_copy[key] = _safe_float(it_copy[key]) / 1000.0
        converted.append(it_copy)

    return converted, "converted_from_ms"


def normalize_playlist_items(
    items_raw: Any,
    segments: Optional[List[dict]] = None,
    duration_sec: Optional[float] = None,
) -> List[Dict[str, Any]]:
    if not isinstance(items_raw, list):
        return []

    # [NEW] Auto-detect ms vs sec and convert if needed
    items_converted, _timebase = _detect_timebase_and_convert(items_raw, duration_sec)

    duration = duration_sec or _duration_from_segments(segments)
    items: List[Dict[str, Any]] = []
    for it in items_converted:
        if not isinstance(it, dict):
            continue
        start = _safe_float(it.get("startSec") or it.get("start_sec") or it.get("start"))
        end = _safe_float(it.get("endSec") or it.get("end_sec") or it.get("end"))
        title = it.get("title") or it.get("label") or ""
        summary = it.get("summary")
        label = it.get("label")
        confidence = it.get("confidence")
        items.append({
            "startSec": start,
            "endSec": end,
            "title": title,
            "summary": summary,
            "label": label,
            "confidence": confidence,
        })

    items.sort(key=lambda x: x.get("startSec", 0.0))

    # Fill missing or invalid endSec using next start or duration
    for index, item in enumerate(items):
        start = _safe_float(item.get("startSec"), 0.0)
        end = _safe_float(item.get("endSec"), 0.0)
        if end <= start:
            if index + 1 < len(items):
                end = _safe_float(items[index + 1].get("startSec"), start)
            elif duration is not None:
                end = duration
        item["startSec"] = max(0.0, start)
        if duration is not None:
            end = min(end, duration)
        item["endSec"] = max(item["startSec"], end)

    # Merge very short chapters
    min_dur = _min_chapter_duration(duration)
    merged: List[Dict[str, Any]] = []
    for item in items:
        duration_item = item["endSec"] - item["startSec"]
        if not merged:
            merged.append(item)
            continue
        last = merged[-1]
        last_dur = last["endSec"] - last["startSec"]
        if last_dur < min_dur or duration_item < min_dur:
            last["endSec"] = max(last["endSec"], item["endSec"])
            if not last.get("title") and item.get("title"):
                last["title"] = item["title"]
            if not last.get("summary") and item.get("summary"):
                last["summary"] = item["summary"]
            continue
        merged.append(item)

    # Normalize ids and attach segments if present
    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(merged):
        normalized.append({
            "id": f"c{idx + 1}",
            "startSec": item["startSec"],
            "endSec": item["endSec"],
            "title": item.get("title") or f"チャプター {idx + 1}",
            "summary": item.get("summary"),
            "label": item.get("label"),
            "confidence": item.get("confidence"),
            "segments": segments if segments else None,
        })
    return normalized
