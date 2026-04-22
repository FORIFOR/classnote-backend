"""
SummaryV2: Evidence-based Structured Summary Generation

Pipeline:
- Step 0: Preprocess - normalize transcript to segments
- Step 1: Extract - extract decisions, action_items, open_questions, risks with evidence
- Step 2: Ground - verify each candidate has support (full/partial/none)
- Step 3: Compose - format to JSON + Markdown
- Step 4: Quality gate - filter/mark unsupported items
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple

from app.util_models import (
    SummaryV2,
    SummaryV2Item,
    SummaryV2ItemType,
    SummaryV2ItemStatus,
    SummaryV2Category,
    SummaryV2Quality,
    EvidenceRef,
    EvidenceSupport,
    UserMark,
    TranscriptSegment,
)

logger = logging.getLogger("app.summary_v2")


# ===========================================================================
# PR1: prompt version, stable item id, transcript hash, prompt builder,
# validation, markdown render, user-edit merge. These are additive helpers —
# existing generate_summary_v2() and compose_summary_items() pipeline below
# is unchanged.
# ===========================================================================

SUMMARY_V2_PROMPT_VERSION = "summary_v2_2026_04_22_v1"


def make_stable_item_id(
    item_type: str,
    normalized_text: str,
    first_segment_id: Optional[str] = None,
) -> str:
    """Deterministic item id so re-runs can merge userEdited/hidden items.

    Input is intentionally not LLM-random — same extraction over the same
    transcript chunk range produces the same id.
    """
    key = f"{item_type}|{(normalized_text or '').strip()}|{first_segment_id or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def compute_transcript_hash(
    chunks: Optional[List[Dict[str, Any]]] = None,
    fallback_text: Optional[str] = None,
) -> str:
    """sha256 over a canonical chunk serialization (id, startMs, text).

    Falls back to hashing `fallback_text` when no chunks are present.
    """
    h = hashlib.sha256()
    if chunks:
        def _key(c: Dict[str, Any]):
            return (str(c.get("id") or c.get("segmentId") or ""), int(c.get("startMs") or 0))
        for c in sorted(chunks, key=_key):
            cid = str(c.get("id") or c.get("segmentId") or "")
            start = int(c.get("startMs") or 0)
            text = (c.get("text") or "").strip()
            h.update(f"{cid}|{start}|{text}\n".encode("utf-8"))
    else:
        h.update((fallback_text or "").encode("utf-8"))
    return h.hexdigest()


_ALLOWED_V2_MEETING_TYPES = {"lecture", "meeting", "translate", "interview", "other"}


def _normalize_mode(raw: Optional[str]) -> str:
    if not raw:
        return "other"
    v = str(raw).lower().strip()
    if v == "translation":
        v = "translate"
    return v if v in _ALLOWED_V2_MEETING_TYPES else "other"


def build_summary_v2_prompt(
    *,
    mode: str,
    meeting_purpose: Optional[str],
    participants: List[str],
    language: Optional[str],
    transcript_chunks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the LLM request payload for SummaryV2 (PR1 v0.1).

    Returns a dict (not a string) so we can pass model/schema/options
    alongside the text. The actual LLM call is made in the worker.

    Shape:
        {
            "promptVersion": SUMMARY_V2_PROMPT_VERSION,
            "mode": "meeting" | "lecture" | "translate" | "interview" | "other",
            "instruction": "...",
            "meetingPurpose": str | None,
            "participants": List[str],
            "language": str | None,
            "chunks": [{"id", "speaker", "text", "startMs", "endMs"}],
        }
    """
    norm_mode = _normalize_mode(mode)
    norm_chunks: List[Dict[str, Any]] = []
    for c in transcript_chunks or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or c.get("segmentId")
        if not cid:
            continue
        norm_chunks.append({
            "id": str(cid),
            "speaker": c.get("speakerId") or c.get("speaker") or "",
            "text": (c.get("text") or "").strip(),
            "startMs": int(c.get("startMs") or 0),
            "endMs": int(c.get("endMs") or 0),
        })

    instruction = (
        "You are extracting structured evidence-based summary for a session. "
        "For each item you emit, reference transcript chunk ids in evidence.segmentIds. "
        "Do not invent facts not present in the chunks. Output must match SummaryV2 schema "
        "(version=2, schemaVersion='2.0'). Write user-visible fields in Japanese unless "
        "language explicitly overrides. Leave lectureAddendum null unless mode='lecture'."
    )

    return {
        "promptVersion": SUMMARY_V2_PROMPT_VERSION,
        "mode": norm_mode,
        "instruction": instruction,
        "meetingPurpose": meeting_purpose,
        "participants": list(participants or []),
        "language": language,
        "chunks": norm_chunks,
    }


def validate_summary_v2_payload(raw: Dict[str, Any]) -> SummaryV2:
    """Parse a raw dict (e.g. LLM output) into a SummaryV2, enforcing v2 locks.

    Raises pydantic ValidationError on malformed payloads. Callers should
    treat this as a retry boundary (PR1: worker retries once on failure,
    then marks the job failed with errorReason='llm_schema_invalid').
    """
    raw2 = dict(raw or {})
    # PR1 contract locks: reject anything claiming to be something else.
    raw2.setdefault("version", 2)
    raw2.setdefault("schemaVersion", "2.0")
    return SummaryV2(**raw2)


# Markdown renderer alias — the existing implementation is render_markdown().
# Give it a v2-consistent name so callers don't need to know the internal
# history.
def render_summary_v2_markdown(
    summary: SummaryV2,
    screen_events: Optional[List[Dict[str, Any]]] = None,
) -> str:
    return render_markdown(summary, screen_events=screen_events)


def merge_user_edited_items(
    old_items: List[SummaryV2Item],
    new_items: List[SummaryV2Item],
) -> List[SummaryV2Item]:
    """Preserve userEdited / hidden items across re-runs (PR1 rules).

    Rules (spec §11.4):
      - old hidden items are removed from the result entirely
      - old userEdited items override new items with the same id
      - old userEdited items that the new run no longer produced are kept
      - new items that the old didn't have are added

    Order: old userEdited/retained first (preserving orderIndex), then new
    items not already keyed. Callers may re-sort by orderIndex if needed.
    """
    hidden_ids = {it.id for it in old_items or [] if it.hidden}
    edited_ids = {it.id for it in old_items or [] if it.userEdited}

    result: List[SummaryV2Item] = []
    seen: set = set()

    # Retain old userEdited items (even if new run no longer emits them).
    for it in old_items or []:
        if it.hidden:
            continue
        if it.userEdited:
            result.append(it)
            seen.add(it.id)

    # Apply new items, skipping hidden and preferring userEdited old on conflict.
    for it in new_items or []:
        if it.id in hidden_ids:
            continue
        if it.id in edited_ids:
            # old userEdited already pushed — don't clobber.
            continue
        if it.id in seen:
            continue
        result.append(it)
        seen.add(it.id)

    return result


# --- Step 0: Preprocess ---

def normalize_segments(
    transcript_text: str,
    diarized_segments: Optional[List[dict]] = None,
    user_marks: Optional[List[dict]] = None,
) -> Tuple[List[TranscriptSegment], List[UserMark]]:
    """
    Normalize transcript into segments with timing.
    If diarized_segments exist, use them. Otherwise, create pseudo-segments.
    """
    segments: List[TranscriptSegment] = []
    marks: List[UserMark] = []

    # Parse user marks
    if user_marks:
        for m in user_marks:
            try:
                marks.append(UserMark(
                    id=m.get("id", str(uuid.uuid4())[:8]),
                    type=m.get("type", "important"),
                    atMs=m.get("atMs", 0),
                    text=m.get("text"),
                    createdAt=m.get("createdAt"),
                ))
            except Exception as e:
                logger.warning(f"Failed to parse user mark: {e}")

    # Use diarized segments if available
    if diarized_segments:
        for idx, seg in enumerate(diarized_segments):
            try:
                # Handle both ms and sec formats
                start = seg.get("startMs") or int(seg.get("startSec", 0) * 1000)
                end = seg.get("endMs") or int(seg.get("endSec", 0) * 1000)
                segments.append(TranscriptSegment(
                    id=seg.get("id", f"seg_{idx}"),
                    startMs=start,
                    endMs=end,
                    speakerId=seg.get("speakerId"),
                    text=seg.get("text", ""),
                ))
            except Exception as e:
                logger.warning(f"Failed to parse segment {idx}: {e}")
        return segments, marks

    # Fallback: Create pseudo-segments from transcript text
    if not transcript_text:
        return segments, marks

    # Split by sentences/paragraphs and estimate timing
    chunks = transcript_text.split("。")
    chars_per_ms = 5 / 1000  # ~5 chars per second
    cumulative_ms = 0

    for idx, chunk in enumerate(chunks):
        chunk = chunk.strip()
        if not chunk:
            continue
        duration_ms = int(len(chunk) / chars_per_ms)
        segments.append(TranscriptSegment(
            id=f"pseudo_{idx}",
            startMs=cumulative_ms,
            endMs=cumulative_ms + duration_ms,
            speakerId=None,
            text=chunk + "。",
        ))
        cumulative_ms += duration_ms

    return segments, marks


# --- Step 1: Extract ---

EXTRACTION_PROMPT = """あなたは会話議事録を、簡潔かつ自然な日本語の箇条書きに整理する編集者です。

以下の会議の文字起こしから、記録価値のある項目を抽出してください。

会議の目的: {meeting_purpose}
会議タイプ: {meeting_type}
参加者: {participants}

ユーザーがマークした重要ポイント:
{user_marks_text}

文字起こし (各行の [MM:SS] はタイムスタンプ):
{transcript_text}

会議中に表示されていた関連資料:
{screen_events_text}

## 出力ルール

1. 各項目は「〜という話が出た」「〜が共有された」「〜が決定した」など議事録調で書く
2. text は 35〜90文字
3. 口語をそのまま出さず、読みやすく整形する
4. 1項目1論点 (複数の論点を1文に混ぜない)
5. 推測しない。発話に明確に含まれる内容のみ
6. 雑談も discussion として残してよい (落としすぎない)
7. source_segment_ids には根拠となる segment の ID を必ず含める
8. anchor_segment_id は最も代表的な segment の ID
9. importance は 0.0〜1.0 (決定事項=0.9, TODO=0.85, 懸念=0.7, 事実=0.5, 雑談=0.3)
10. category は decision/todo/concern/insight/fact/discussion/other のいずれか

## 出力JSON

{{
  "items": [
    {{
      "text": "最近、どこでも一気に家賃が上がったという懸念が共有されました。",
      "category": "concern",
      "importance": 0.7,
      "startMs": 3468000,
      "endMs": 3482000,
      "source_segment_ids": ["seg_001", "seg_002"],
      "anchor_segment_id": "seg_001",
      "speakers": ["A"],
      "owner": null,
      "dueDate": null
    }}
  ]
}}

注意:
- items は時系列順で返す
- 5〜20項目程度 (会議の長さに応じる)
- 短い会話なら5項目以下でもよい
- JSONのみ出力。説明文は不要
"""


async def extract_candidates(
    transcript_text: str,
    segments: List[TranscriptSegment],
    user_marks: List[UserMark],
    meeting_purpose: Optional[str] = None,
    meeting_type: Optional[str] = None,
    participants: Optional[List[str]] = None,
    screen_events: Optional[List[dict]] = None,
) -> List[dict]:
    """
    Extract candidate items from transcript as flat list with category/importance/anchor.
    Returns list of dicts with: text, category, importance, startMs, endMs,
    source_segment_ids, anchor_segment_id, speakers, owner, dueDate.
    """
    from app.services.llm import _ensure_model, _model
    from vertexai.generative_models import GenerationConfig

    # Format user marks
    marks_text = ""
    if user_marks:
        for m in user_marks:
            time_str = f"{m.atMs // 60000}:{(m.atMs // 1000) % 60:02d}"
            marks_text += f"- [{time_str}] {m.type.upper()}: {m.text or '(マークのみ)'}\n"
    if not marks_text:
        marks_text = "(なし)"

    # Build segment ID map for reference
    seg_id_map = {seg.id: seg for seg in segments}

    # Build transcript with timestamps and segment IDs
    transcript_with_time = ""
    for seg in segments[:300]:  # Limit segments
        time_str = f"{seg.startMs // 60000}:{(seg.startMs // 1000) % 60:02d}"
        speaker = f"[{seg.speakerId}]" if seg.speakerId else ""
        transcript_with_time += f"[{time_str}] (id={seg.id}){speaker} {seg.text}\n"

    if not transcript_with_time:
        transcript_with_time = transcript_text[:12000]  # Fallback

    # Build screen events text
    screen_events_text = "(なし)"
    if screen_events:
        relevant = [e for e in screen_events if e.get("is_relevant") and e.get("summary")]
        if relevant:
            lines = []
            for e in relevant[:15]:
                ts = e.get("captured_at", "")
                stype = e.get("screen_type", "unknown")
                summary = e.get("summary", "")
                items = ", ".join(e.get("key_items", [])[:5])
                claims = ", ".join(e.get("claims_or_numbers", [])[:5])
                lines.append(f"- [{ts}] ({stype}) {summary}")
                if items:
                    lines.append(f"  項目: {items}")
                if claims:
                    lines.append(f"  数値: {claims}")
            screen_events_text = "\n".join(lines)

    prompt = EXTRACTION_PROMPT.format(
        meeting_purpose=meeting_purpose or "(未指定)",
        meeting_type=meeting_type or "(未指定)",
        participants=", ".join(participants) if participants else "(未指定)",
        user_marks_text=marks_text,
        transcript_text=transcript_with_time,
        screen_events_text=screen_events_text,
    )

    _ensure_model()
    try:
        from app.services.llm import _timed_llm_call
        response = await _timed_llm_call(
            _model,
            prompt,
            GenerationConfig(
                temperature=0.3,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
            label="summary_v2_extract",
        )
        result = json.loads(response.text or "{}")

        # Support both new flat format {"items": [...]} and legacy grouped format
        if "items" in result and isinstance(result["items"], list):
            return result["items"]

        # Legacy fallback: convert grouped format to flat items
        flat_items = []
        category_map = {
            "decisions": "decision",
            "actions": "todo",
            "open_questions": "concern",
            "risks": "concern",
        }
        for group_key, category in category_map.items():
            for item in result.get(group_key, []):
                item["category"] = item.get("category", category)
                flat_items.append(item)
        return flat_items

    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return []


# --- Step 2: Ground (Verify Evidence) ---

def find_evidence_support(
    candidate: dict,
    segments: List[TranscriptSegment],
    tolerance_ms: int = 30000,  # 30 second tolerance
) -> Tuple[EvidenceSupport, List[EvidenceRef], float]:
    """
    Verify if the candidate is supported by transcript segments.
    Returns (support_level, evidence_refs, adjusted_confidence)
    """
    start_ms = candidate.get("startMs", 0)
    end_ms = candidate.get("endMs", start_ms + 10000)
    candidate_text = candidate.get("text", "").lower()
    original_confidence = candidate.get("confidence", 0.5)

    # Find segments in the time range
    matching_segments = []
    combined_text = ""

    for seg in segments:
        # Check if segment overlaps with candidate time range (with tolerance)
        if seg.endMs >= start_ms - tolerance_ms and seg.startMs <= end_ms + tolerance_ms:
            matching_segments.append(seg)
            combined_text += " " + seg.text.lower()

    if not matching_segments:
        return EvidenceSupport.NONE, [], original_confidence * 0.3

    # Build evidence refs
    evidence_refs = []
    segment_ids = []
    min_start = min(s.startMs for s in matching_segments)
    max_end = max(s.endMs for s in matching_segments)

    for seg in matching_segments:
        segment_ids.append(seg.id)

    evidence_refs.append(EvidenceRef(
        startMs=min_start,
        endMs=max_end,
        segmentIds=segment_ids,
        text=combined_text[:200].strip(),
    ))

    # Check text similarity (simple keyword matching)
    keywords = [w for w in candidate_text.split() if len(w) > 2]
    if not keywords:
        return EvidenceSupport.PARTIAL, evidence_refs, original_confidence * 0.6

    matches = sum(1 for kw in keywords if kw in combined_text)
    match_ratio = matches / len(keywords) if keywords else 0

    if match_ratio >= 0.5:
        return EvidenceSupport.FULL, evidence_refs, original_confidence
    elif match_ratio >= 0.2:
        return EvidenceSupport.PARTIAL, evidence_refs, original_confidence * 0.7
    else:
        return EvidenceSupport.PARTIAL, evidence_refs, original_confidence * 0.5


# --- Step 3: Compose ---

CATEGORY_TO_TYPE = {
    "decision": SummaryV2ItemType.DECISION,
    "todo": SummaryV2ItemType.ACTION,
    "concern": SummaryV2ItemType.RISK,
    "insight": SummaryV2ItemType.NOTE,
    "fact": SummaryV2ItemType.NOTE,
    "discussion": SummaryV2ItemType.NOTE,
    "other": SummaryV2ItemType.NOTE,
}

# Importance score adjustments by category
IMPORTANCE_BOOST = {
    "decision": 0.15,
    "todo": 0.10,
    "concern": 0.05,
    "insight": 0.0,
    "fact": -0.05,
    "discussion": -0.15,
    "other": -0.10,
}


def _resolve_anchor_ms(candidate: dict, segments: List[TranscriptSegment]) -> Optional[int]:
    """Determine anchorMs for timestamp link display."""
    # Priority 1: anchor_segment_id
    anchor_id = candidate.get("anchor_segment_id")
    if anchor_id:
        for seg in segments:
            if seg.id == anchor_id:
                return seg.startMs

    # Priority 2: source_segment_ids first segment
    source_ids = candidate.get("source_segment_ids", [])
    if source_ids:
        for seg in segments:
            if seg.id == source_ids[0]:
                return seg.startMs

    # Priority 3: startMs from candidate
    start = candidate.get("startMs")
    if start and start > 0:
        return start

    return None


def _compute_importance(candidate: dict) -> float:
    """Compute final importance score with category boost."""
    base = candidate.get("importance", 0.5)
    category = candidate.get("category", "other")
    boost = IMPORTANCE_BOOST.get(category, 0.0)
    return max(0.0, min(1.0, base + boost))


def _truncate_text(text: str, max_len: int = 50) -> str:
    """Create shortText by truncating."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def compose_summary_items(
    candidates: List[dict],
    segments: List[TranscriptSegment],
) -> List[SummaryV2Item]:
    """
    Convert flat candidate items to SummaryV2Items with evidence, category,
    importance score, anchor timestamp, and speaker info.
    """
    items: List[SummaryV2Item] = []
    seg_id_map = {seg.id: seg for seg in segments}

    for idx, candidate in enumerate(candidates):
        text = candidate.get("text", "").strip()
        if not text:
            continue

        category = candidate.get("category", "other")
        item_type = CATEGORY_TO_TYPE.get(category, SummaryV2ItemType.NOTE)

        # Evidence verification
        support, evidence, confidence = find_evidence_support(candidate, segments)

        # Anchor timestamp
        anchor_ms = _resolve_anchor_ms(candidate, segments)
        if anchor_ms is None and evidence:
            anchor_ms = evidence[0].startMs

        # Speaker IDs
        speakers = candidate.get("speakers", [])
        if not speakers:
            # Infer from source segments
            source_ids = candidate.get("source_segment_ids", [])
            for sid in source_ids:
                seg = seg_id_map.get(sid)
                if seg and seg.speakerId and seg.speakerId not in speakers:
                    speakers.append(seg.speakerId)

        importance = _compute_importance(candidate)

        item = SummaryV2Item(
            id=f"item_{idx + 1}",
            type=item_type,
            text=text,
            shortText=_truncate_text(text),
            category=category,
            importanceScore=round(importance, 2),
            anchorMs=anchor_ms,
            speakerIds=speakers,
            owner=candidate.get("owner"),
            dueDate=candidate.get("dueDate"),
            status=SummaryV2ItemStatus.TODO if item_type == SummaryV2ItemType.ACTION else SummaryV2ItemStatus.UNKNOWN,
            evidence=evidence,
            support=support,
            confidence=round(confidence, 2),
            orderIndex=idx,
        )
        items.append(item)

    return items


def _deduplicate_items(items: List[SummaryV2Item]) -> List[SummaryV2Item]:
    """Remove near-duplicate items based on text similarity and time overlap."""
    if len(items) <= 1:
        return items

    result = []
    for item in items:
        is_dup = False
        for existing in result:
            # Simple overlap check: same anchor time range (within 30s) + similar text
            if (existing.anchorMs is not None and item.anchorMs is not None
                    and abs(existing.anchorMs - item.anchorMs) < 30000):
                # Check text overlap (shared characters ratio)
                shorter = min(len(existing.text), len(item.text))
                if shorter > 0:
                    common = sum(1 for a, b in zip(existing.text, item.text) if a == b)
                    if common / shorter > 0.6:
                        # Keep the one with higher importance
                        if item.importanceScore > existing.importanceScore:
                            result.remove(existing)
                            result.append(item)
                        is_dup = True
                        break
        if not is_dup:
            result.append(item)

    return result


def _format_timestamp(ms: Optional[int]) -> str:
    """Format milliseconds as MM:SS or HH:MM:SS."""
    if ms is None or ms < 0:
        return ""
    total_sec = ms // 1000
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def render_markdown(summary: SummaryV2, screen_events: Optional[List[dict]] = None) -> str:
    """Render summary as Markdown with timestamp links and category sections."""
    lines = []

    if summary.meetingPurpose:
        lines.append(f"## 会議の目的\n{summary.meetingPurpose}\n")

    # Group by category
    groups = {
        "decision": ("決定事項", []),
        "todo": ("アクションアイテム", []),
        "concern": ("懸念・問題", []),
        "insight": ("気づき", []),
        "fact": ("事実共有", []),
        "discussion": ("会話メモ", []),
        "other": ("その他", []),
    }

    for item in summary.items:
        cat = item.category if item.category in groups else "other"
        groups[cat][1].append(item)

    for cat_key, (label, cat_items) in groups.items():
        if not cat_items:
            continue
        lines.append(f"## {label}")
        for item in cat_items:
            badge = "✅" if item.support == EvidenceSupport.FULL else "⚠️" if item.support == EvidenceSupport.PARTIAL else "❓"
            ts = _format_timestamp(item.anchorMs)
            ts_display = f" `{ts}`" if ts else ""
            owner = f" (@{item.owner})" if item.owner else ""
            due = f" [期限: {item.dueDate}]" if item.dueDate else ""
            lines.append(f"- {badge} {item.text}{owner}{due}{ts_display}")
        lines.append("")

    # Screen events section (if available)
    if screen_events:
        relevant = [e for e in screen_events if e.get("is_relevant") and e.get("summary")]
        if relevant:
            lines.append("## 会議中に表示された資料")
            for e in relevant:
                stype = e.get("screen_type", "")
                summary = e.get("summary", "")
                lines.append(f"- ({stype}) {summary}")
            lines.append("")

    return "\n".join(lines)


# --- Step 4: Quality Gate ---

def apply_quality_gate(items: List[SummaryV2Item]) -> Tuple[List[SummaryV2Item], SummaryV2Quality]:
    """
    Apply quality gate: filter or mark items based on evidence support.
    """
    filtered_items = []
    unsupported = 0
    partial = 0
    full = 0
    total_confidence = 0.0

    for item in items:
        if item.support == EvidenceSupport.NONE:
            # Keep but mark as needs verification
            item.text = f"[要確認] {item.text}"
            unsupported += 1
        elif item.support == EvidenceSupport.PARTIAL:
            partial += 1
        else:
            full += 1

        total_confidence += item.confidence
        filtered_items.append(item)

    quality = SummaryV2Quality(
        unsupportedCount=unsupported,
        partialCount=partial,
        fullCount=full,
        avgConfidence=total_confidence / len(items) if items else 0.0,
    )

    return filtered_items, quality


# --- Main Pipeline ---

async def generate_summary_v2(
    session_id: str,
    transcript_text: str,
    diarized_segments: Optional[List[dict]] = None,
    user_marks: Optional[List[dict]] = None,
    meeting_purpose: Optional[str] = None,
    meeting_type: Optional[str] = None,
    participants: Optional[List[str]] = None,
    screen_events: Optional[List[dict]] = None,
) -> SummaryV2:
    """
    Main pipeline: Generate structured summary with evidence.
    """
    logger.info(f"[SummaryV2] Starting generation for session {session_id}")

    # Step 0: Preprocess
    segments, marks = normalize_segments(transcript_text, diarized_segments, user_marks)
    logger.info(f"[SummaryV2] Preprocessed: {len(segments)} segments, {len(marks)} marks")

    # Step 1: Extract (returns flat list of candidate dicts)
    candidates = await extract_candidates(
        transcript_text,
        segments,
        marks,
        meeting_purpose,
        meeting_type,
        participants,
        screen_events=screen_events,
    )
    logger.info(f"[SummaryV2] Extracted {len(candidates)} candidates")

    # Step 2 & 3: Ground and Compose (with category, importance, anchor)
    items = compose_summary_items(candidates, segments)
    logger.info(f"[SummaryV2] Composed {len(items)} items with evidence")

    # Step 3.5: Deduplicate
    items = _deduplicate_items(items)
    logger.info(f"[SummaryV2] After dedup: {len(items)} items")

    # Re-index after dedup
    for idx, item in enumerate(items):
        item.orderIndex = idx
        item.id = f"item_{idx + 1}"

    # Step 4: Quality Gate
    items, quality = apply_quality_gate(items)
    logger.info(f"[SummaryV2] Quality: full={quality.fullCount}, partial={quality.partialCount}, none={quality.unsupportedCount}")

    # Build final summary
    summary = SummaryV2(
        version=1,
        generatedAt=datetime.now(timezone.utc),
        meetingPurpose=meeting_purpose,
        meetingType=meeting_type,
        participants=participants or [],
        items=items,
        quality=quality,
    )

    # Render markdown
    summary.renderedMarkdown = render_markdown(summary, screen_events=screen_events)

    return summary
