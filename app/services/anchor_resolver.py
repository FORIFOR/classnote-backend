"""
Anchor resolver: attach anchorMs / segmentIds to summary bullets by
text-matching against transcript chunks.

The LLM is not trusted to emit timestamps directly — instead, for each
generated bullet we find the transcript segment whose text has the highest
lexical overlap and use that segment's startMs as the anchor. This keeps
bullet-to-transcript links grounded in real data and avoids hallucinated
timecodes.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING

logger = logging.getLogger("app.anchor_resolver")

if TYPE_CHECKING:  # avoid runtime import cycle
    from app.util_models import (
        EvidenceRef,
        SummaryV2Item,
        SummaryV2Quality,
    )


# Minimum Jaccard-ish score required to claim a match. Below this threshold
# we leave the bullet anchor-less rather than attach a misleading timestamp.
MIN_MATCH_SCORE = 0.12


def _coerce_int_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_segments(raw: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Normalize diarized segments or transcript chunks into a uniform shape:
      { "id": str, "startMs": int, "endMs": int, "text": str }

    Accepts dicts with either ms or sec timing keys.
    """
    if not raw:
        return []

    segments: List[Dict[str, Any]] = []
    for idx, seg in enumerate(raw):
        if not isinstance(seg, dict):
            continue

        start_ms = _coerce_int_ms(seg.get("startMs"))
        if start_ms is None and seg.get("startSec") is not None:
            try:
                start_ms = int(float(seg.get("startSec")) * 1000)
            except (TypeError, ValueError):
                start_ms = None
        end_ms = _coerce_int_ms(seg.get("endMs"))
        if end_ms is None and seg.get("endSec") is not None:
            try:
                end_ms = int(float(seg.get("endSec")) * 1000)
            except (TypeError, ValueError):
                end_ms = None

        if start_ms is None:
            continue
        if end_ms is None or end_ms < start_ms:
            end_ms = start_ms

        text = seg.get("text") or seg.get("transcript") or ""
        if not isinstance(text, str):
            text = str(text)
        text = text.strip()
        if not text:
            continue

        seg_id = seg.get("id") or f"seg_{idx}"
        segments.append({
            "id": str(seg_id),
            "startMs": int(start_ms),
            "endMs": int(end_ms),
            "text": text,
        })

    segments.sort(key=lambda s: s["startMs"])
    return segments


# ── Tokenization & scoring ────────────────────────────────────────────────

_TOKEN_SPLIT_RE = re.compile(r"[\s、。,.!?！？「」『』（）()\[\]【】・:：;；/／\\]+")
_BIGRAM_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _tokenize(text: str) -> List[str]:
    """Crude tokenizer: split by whitespace/punctuation, keep tokens >= 2 chars."""
    if not text:
        return []
    parts = _TOKEN_SPLIT_RE.split(text.lower())
    return [p for p in parts if len(p) >= 2]


def _char_bigrams(text: str) -> List[str]:
    """Character bigrams — robust fallback for Japanese where whitespace-based
    tokenization yields little signal."""
    stripped = _BIGRAM_STRIP_RE.sub("", text.lower())
    if len(stripped) < 2:
        return []
    return [stripped[i : i + 2] for i in range(len(stripped) - 1)]


def _score(bullet_text: str, segment_text: str) -> float:
    """Return a similarity score in [0, 1] combining token overlap and
    character-bigram Jaccard."""
    if not bullet_text or not segment_text:
        return 0.0

    bullet_tokens = set(_tokenize(bullet_text))
    seg_tokens = set(_tokenize(segment_text))
    token_score = 0.0
    if bullet_tokens:
        inter = bullet_tokens & seg_tokens
        token_score = len(inter) / len(bullet_tokens)

    bullet_bigrams = set(_char_bigrams(bullet_text))
    seg_bigrams = set(_char_bigrams(segment_text))
    bigram_score = 0.0
    if bullet_bigrams and seg_bigrams:
        inter = bullet_bigrams & seg_bigrams
        union = bullet_bigrams | seg_bigrams
        bigram_score = len(inter) / len(union) if union else 0.0

    return max(token_score, bigram_score)


def find_best_segments(
    bullet_text: str,
    segments: List[Dict[str, Any]],
    top_k: int = 3,
) -> List[Tuple[Dict[str, Any], float]]:
    """Return up to top_k (segment, score) pairs sorted by score descending."""
    if not bullet_text or not segments:
        return []

    scored: List[Tuple[Dict[str, Any], float]] = []
    for seg in segments:
        s = _score(bullet_text, seg["text"])
        if s > 0:
            scored.append((seg, s))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def resolve_anchor(
    bullet_text: str,
    segments: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Resolve an anchor for a bullet. Returns a dict with anchorMs / segmentIds
    / score, or None if no segment clears the minimum threshold.
    """
    matches = find_best_segments(bullet_text, segments, top_k=3)
    if not matches:
        return None

    best_seg, best_score = matches[0]
    if best_score < MIN_MATCH_SCORE:
        return None

    # Include additional near-matches (within 30s window of the best match)
    # as supporting segment ids for evidence.
    window_start = best_seg["startMs"] - 15000
    window_end = best_seg["endMs"] + 15000
    segment_ids: List[str] = [best_seg["id"]]
    for seg, score in matches[1:]:
        if score < MIN_MATCH_SCORE * 0.8:
            continue
        if window_start <= seg["startMs"] <= window_end:
            segment_ids.append(seg["id"])

    return {
        "anchorMs": int(best_seg["startMs"]),
        "endMs": int(best_seg["endMs"]),
        "segmentIds": segment_ids,
        "matchScore": round(best_score, 3),
    }


# ── Summary JSON enrichment ───────────────────────────────────────────────

# Keys that carry a bullet text and should be enriched in-place with
# anchorMs / segmentIds. Each entry is (list_key, text_key).
_DICT_BULLET_FIELDS: List[Tuple[str, str]] = [
    ("highlights", "text"),
    ("decisions", "text"),
    ("todos", "text"),
    ("openQuestions", "text"),
    ("timeline", "event"),
]


def _enrich_dict_item(item: Dict[str, Any], text_key: str, segments: List[Dict[str, Any]]) -> None:
    if not isinstance(item, dict):
        return
    # Do not overwrite anchors already set by upstream (e.g. SummaryV2).
    if item.get("anchorMs") is not None:
        return
    text = item.get(text_key)
    if not isinstance(text, str) or not text.strip():
        return
    anchor = resolve_anchor(text, segments)
    if not anchor:
        return
    item["anchorMs"] = anchor["anchorMs"]
    item["segmentIds"] = anchor["segmentIds"]
    item.setdefault("evidence", []).append({
        "startMs": anchor["anchorMs"],
        "endMs": anchor["endMs"],
        "segmentIds": anchor["segmentIds"],
        "matchScore": anchor["matchScore"],
    })


def _enrich_string_bullet(
    bullet: Any,
    segments: List[Dict[str, Any]],
) -> Any:
    """
    For list fields whose items are plain strings (e.g. section bullets,
    tldr lines), we cannot attach anchors without changing the shape.
    We upgrade each string to { text, anchorMs, segmentIds } when a match
    is found, otherwise leave it as a string for backward compatibility.
    """
    if not isinstance(bullet, str) or not bullet.strip():
        return bullet
    anchor = resolve_anchor(bullet, segments)
    if not anchor:
        return bullet
    return {
        "text": bullet,
        "anchorMs": anchor["anchorMs"],
        "segmentIds": anchor["segmentIds"],
    }


def enrich_summary_with_anchors(
    summary_json: Dict[str, Any],
    segments: Optional[Iterable[Dict[str, Any]]],
) -> Dict[str, Any]:
    """
    Walk a normalized summary JSON and attach anchorMs / segmentIds to each
    bullet by matching against transcript segments. Mutates and returns
    summary_json. Safe no-op when segments is empty.
    """
    if not isinstance(summary_json, dict):
        return summary_json
    normalized_segments = normalize_segments(segments)
    if not normalized_segments:
        return summary_json

    total_enriched = 0

    # 1) dict-shaped bullet lists — mutate items in place
    for list_key, text_key in _DICT_BULLET_FIELDS:
        items = summary_json.get(list_key)
        if not isinstance(items, list):
            continue
        for item in items:
            # Phase 7.10: forward-path citation already satisfied
            # (llm._hydrate_source_segment_ids populated sourceSegmentIds +
            # segmentId + startSec from LLM-provided ids). Skip text matching
            # to avoid overwriting the more reliable LLM result.
            if isinstance(item, dict) and item.get("sourceSegmentIds"):
                continue
            before = isinstance(item, dict) and item.get("anchorMs") is not None
            _enrich_dict_item(item, text_key, normalized_segments)
            if not before and isinstance(item, dict) and item.get("anchorMs") is not None:
                total_enriched += 1

    # 2) tldr: list of strings
    tldr = summary_json.get("tldr")
    if isinstance(tldr, list):
        new_tldr = []
        for bullet in tldr:
            upgraded = _enrich_string_bullet(bullet, normalized_segments)
            new_tldr.append(upgraded)
            if isinstance(upgraded, dict):
                total_enriched += 1
        summary_json["tldr"] = new_tldr

    # 3) sections[].bullets — lecture mode, list of strings
    sections = summary_json.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            bullets = section.get("bullets")
            if not isinstance(bullets, list):
                continue
            new_bullets = []
            for bullet in bullets:
                upgraded = _enrich_string_bullet(bullet, normalized_segments)
                new_bullets.append(upgraded)
                if isinstance(upgraded, dict):
                    total_enriched += 1
            section["bullets"] = new_bullets

    # 4) discussionPoints: dict with topic/conclusion — use topic as anchor source
    discussion_points = summary_json.get("discussionPoints")
    if isinstance(discussion_points, list):
        for item in discussion_points:
            if not isinstance(item, dict) or item.get("anchorMs") is not None:
                continue
            anchor_text = item.get("topic") or item.get("conclusion")
            if not isinstance(anchor_text, str) or not anchor_text.strip():
                continue
            anchor = resolve_anchor(anchor_text, normalized_segments)
            if not anchor:
                continue
            item["anchorMs"] = anchor["anchorMs"]
            item["segmentIds"] = anchor["segmentIds"]
            total_enriched += 1

    # 5) conversationHighlights (Phase 7.9) — natural-sentence cards with a
    # primaryTimestampMs. If the LLM already filled primaryTimestampSec the
    # normalizer converted it; here we repair missing / zero timestamps by
    # matching the card text against the transcript and attach segmentId +
    # an `evidence` array so it conforms to the Summary v2 evidence contract.
    conv_highlights = summary_json.get("conversationHighlights")
    if isinstance(conv_highlights, list):
        for item in conv_highlights:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            anchor = resolve_anchor(text, normalized_segments)
            if not anchor:
                # Keep existing primaryTimestampMs (from LLM); no evidence backfill
                item.setdefault("evidence", [])
                continue
            # Always prefer anchor-backed timestamp over LLM-claimed one —
            # LLM timestamps are unreliable for long sessions.
            item["primaryTimestampMs"] = int(anchor["anchorMs"])
            item["segmentIds"] = anchor["segmentIds"]
            primary_seg = anchor["segmentIds"][0] if anchor["segmentIds"] else None
            evidence_entry: Dict[str, Any] = {
                "startMs": int(anchor["anchorMs"]),
            }
            if primary_seg:
                evidence_entry["segmentId"] = primary_seg
            existing_evidence = item.get("evidence")
            if isinstance(existing_evidence, list) and existing_evidence:
                # Keep LLM-provided evidence but prepend the anchor result
                item["evidence"] = [evidence_entry, *existing_evidence]
            else:
                item["evidence"] = [evidence_entry]
            total_enriched += 1

    if total_enriched:
        logger.info(f"[anchor_resolver] enriched {total_enriched} bullets with anchors")

    return summary_json


# =============================================================================
# Summary v2 resolver (PR1 v0.1)
# =============================================================================
# The v1 API above (enrich_summary_with_anchors) is a text-matching heuristic
# used by the existing summary pipeline. Summary v2 is a different contract:
# the LLM emits segmentIds directly and we resolve them structurally from
# chunks_by_id. This section exposes that v2 surface without touching the v1
# code path above.
#
# Public names:
#   resolve_item_anchors_v2(item, chunks_by_id)   -> SummaryV2Item
#   resolve_all_anchors_v2(items, chunks_by_id)   -> list[SummaryV2Item]
#   build_chunks_by_id(chunks)                    -> dict[str, chunk]
#   apply_quality_gate_v2(items)
#       -> (kept, SummaryV2Quality, filtered)
#
# v0.1 semantics (deliberately narrow so future diff can assert on them):
#   - Evidence with unknown segmentIds is silently pruned.
#   - When ≥1 valid segment remains:
#         startMs  = min(chunk.startMs)
#         endMs    = max(chunk.endMs)
#         text     = " ".join(chunk.text).strip()[:200]
#         anchorMs = first_valid_chunk.startMs
#   - support:
#         NONE    if zero resolved evidence refs
#         PARTIAL if ≥1 resolved evidence ref
#         FULL    reserved for v0.2 (token-overlap heuristic)
#   - quality gate is pass-through; no item is filtered but
#     SummaryV2Quality is still aggregated so clients see support
#     distribution today.

_V2_MAX_EVIDENCE_TEXT = 200


def _v2_coerce_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _v2_resolve_single_evidence(ref, chunks_by_id: Dict[str, Dict[str, Any]]):
    """Return a new EvidenceRef with start/end/text populated from valid chunks.

    Invalid segmentIds are silently dropped. If zero valid ids remain, the
    result has segmentIds=[] and text="" (caller should discard it).
    """
    from app.util_models import EvidenceRef  # local import, avoid boot cycle

    valid_ids: List[str] = []
    valid_chunks: List[Dict[str, Any]] = []
    for sid in getattr(ref, "segmentIds", None) or []:
        chunk = chunks_by_id.get(sid)
        if chunk:
            valid_ids.append(sid)
            valid_chunks.append(chunk)

    if not valid_chunks:
        return EvidenceRef(segmentIds=[], startMs=None, endMs=None, text="")

    start_ms = min(_v2_coerce_int(c.get("startMs")) for c in valid_chunks)
    end_ms = max(_v2_coerce_int(c.get("endMs")) for c in valid_chunks)

    parts: List[str] = []
    for c in valid_chunks:
        t = (c.get("text") or "").strip()
        if t:
            parts.append(t)
    joined = " ".join(parts).strip()
    if len(joined) > _V2_MAX_EVIDENCE_TEXT:
        joined = joined[:_V2_MAX_EVIDENCE_TEXT]

    return EvidenceRef(
        segmentIds=valid_ids,
        startMs=start_ms,
        endMs=end_ms,
        text=joined,
    )


def _v2_infer_support(valid_evidence: List[Any]):
    """v0.1: PARTIAL iff ≥1 resolved evidence, otherwise NONE. No FULL."""
    from app.util_models import EvidenceSupport
    if not valid_evidence:
        return EvidenceSupport.NONE
    return EvidenceSupport.PARTIAL


def resolve_item_anchors_v2(item, chunks_by_id: Dict[str, Dict[str, Any]]):
    """Return a new SummaryV2Item with resolved evidence / support / anchorMs.

    Does not mutate the input item.
    """
    resolved = []
    for ev in getattr(item, "evidence", None) or []:
        new_ev = _v2_resolve_single_evidence(ev, chunks_by_id)
        if new_ev.segmentIds:
            resolved.append(new_ev)

    anchor_ms = resolved[0].startMs if resolved else None
    support = _v2_infer_support(resolved)

    return item.model_copy(update={
        "evidence": resolved,
        "support": support,
        "anchorMs": anchor_ms if anchor_ms is not None else item.anchorMs,
    })


def resolve_all_anchors_v2(items, chunks_by_id: Dict[str, Dict[str, Any]]):
    return [resolve_item_anchors_v2(it, chunks_by_id) for it in items]


def build_chunks_by_id(chunks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Normalize a flat chunks list into an id→chunk dict.

    Tolerates chunks keyed by "id" or "segmentId". Chunks without an id are
    silently skipped.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for c in chunks or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or c.get("segmentId")
        if not cid:
            continue
        out[str(cid)] = c
    return out


def _v2_aggregate_quality(items, filtered_count: int):
    """Aggregate SummaryV2Quality from a list of items (PR1 v0.1)."""
    from app.util_models import EvidenceSupport, SummaryV2Quality
    full = sum(1 for it in items if it.support == EvidenceSupport.FULL)
    partial = sum(1 for it in items if it.support == EvidenceSupport.PARTIAL)
    none = sum(1 for it in items if it.support == EvidenceSupport.NONE)
    avg_conf = (
        sum(float(it.confidence or 0.0) for it in items) / len(items)
        if items else 0.0
    )
    return SummaryV2Quality(
        fullCount=full,
        partialCount=partial,
        unsupportedCount=none,
        avgConfidence=round(avg_conf, 4),
        filteredCount=filtered_count,
    )


def apply_quality_gate_v2(items):
    """PR1 v0.1: pass-through quality gate with aggregation.

    Returns (kept, quality, filtered). filtered is always [] in v0.1.
    quality.filteredCount == 0 here; v0.2 will drop items where support==NONE
    AND confidence < 0.4 and bump filteredCount accordingly.
    """
    kept = list(items)
    filtered: List[Any] = []
    quality = _v2_aggregate_quality(kept, filtered_count=len(filtered))
    return kept, quality, filtered
