"""
SummaryV2: Evidence-based Structured Summary Generation

Pipeline:
- Step 0: Preprocess - normalize transcript to segments
- Step 1: Extract - extract decisions, action_items, open_questions, risks with evidence
- Step 2: Ground - verify each candidate has support (full/partial/none)
- Step 3: Compose - format to JSON + Markdown
- Step 4: Quality gate - filter/mark unsupported items
"""

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
    SummaryV2Quality,
    EvidenceRef,
    EvidenceSupport,
    UserMark,
    TranscriptSegment,
)

logger = logging.getLogger("app.summary_v2")


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

EXTRACTION_PROMPT = """以下の会議の文字起こしから、構造化された情報を抽出してください。

会議の目的: {meeting_purpose}
会議タイプ: {meeting_type}
参加者: {participants}

ユーザーがマークした重要ポイント:
{user_marks_text}

文字起こし:
{transcript_text}

以下のJSON形式で、決定事項、アクションアイテム、未決事項、リスクを抽出してください。
各項目には必ず「根拠となる発話の時間範囲（startMs, endMs）」を含めてください。

{{
  "decisions": [
    {{"text": "決定内容", "startMs": 12000, "endMs": 18000, "confidence": 0.9}}
  ],
  "actions": [
    {{"text": "アクション内容", "owner": "担当者", "dueDate": "2026-02-15", "startMs": 30000, "endMs": 45000, "confidence": 0.85}}
  ],
  "open_questions": [
    {{"text": "未決事項", "startMs": 60000, "endMs": 70000, "confidence": 0.7}}
  ],
  "risks": [
    {{"text": "リスク・懸念事項", "startMs": 90000, "endMs": 100000, "confidence": 0.8}}
  ]
}}

注意:
- 文字起こしに明確に記載されていない内容は抽出しないでください
- confidence は 0.0〜1.0 で、発話の明確さを示します
- 時間範囲は該当する発話のタイムスタンプを参照してください
- 推測や補完は行わないでください
"""


async def extract_candidates(
    transcript_text: str,
    segments: List[TranscriptSegment],
    user_marks: List[UserMark],
    meeting_purpose: Optional[str] = None,
    meeting_type: Optional[str] = None,
    participants: Optional[List[str]] = None,
) -> Dict[str, List[dict]]:
    """
    Extract candidate items (decisions, actions, etc.) from transcript.
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

    # Build transcript with timestamps
    transcript_with_time = ""
    for seg in segments[:200]:  # Limit segments
        time_str = f"{seg.startMs // 60000}:{(seg.startMs // 1000) % 60:02d}"
        speaker = f"[{seg.speakerId}]" if seg.speakerId else ""
        transcript_with_time += f"[{time_str}]{speaker} {seg.text}\n"

    if not transcript_with_time:
        transcript_with_time = transcript_text[:10000]  # Fallback

    prompt = EXTRACTION_PROMPT.format(
        meeting_purpose=meeting_purpose or "(未指定)",
        meeting_type=meeting_type or "(未指定)",
        participants=", ".join(participants) if participants else "(未指定)",
        user_marks_text=marks_text,
        transcript_text=transcript_with_time,
    )

    _ensure_model()
    try:
        response = await _model.generate_content_async(
            prompt,
            generation_config=GenerationConfig(
                temperature=0.3,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(response.text or "{}")
        return result
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return {"decisions": [], "actions": [], "open_questions": [], "risks": []}


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

def compose_summary_items(
    candidates: Dict[str, List[dict]],
    segments: List[TranscriptSegment],
) -> List[SummaryV2Item]:
    """
    Convert candidates to SummaryV2Items with evidence verification.
    """
    items: List[SummaryV2Item] = []

    type_mapping = {
        "decisions": SummaryV2ItemType.DECISION,
        "actions": SummaryV2ItemType.ACTION,
        "open_questions": SummaryV2ItemType.OPEN_QUESTION,
        "risks": SummaryV2ItemType.RISK,
    }

    for category, item_type in type_mapping.items():
        for idx, candidate in enumerate(candidates.get(category, [])):
            support, evidence, confidence = find_evidence_support(candidate, segments)

            item = SummaryV2Item(
                id=f"{category[:3]}_{idx + 1}",
                type=item_type,
                text=candidate.get("text", ""),
                owner=candidate.get("owner"),
                dueDate=candidate.get("dueDate"),
                status=SummaryV2ItemStatus.TODO if item_type == SummaryV2ItemType.ACTION else SummaryV2ItemStatus.UNKNOWN,
                evidence=evidence,
                support=support,
                confidence=confidence,
            )
            items.append(item)

    return items


def render_markdown(summary: SummaryV2) -> str:
    """
    Render summary as Markdown.
    """
    lines = []

    if summary.meetingPurpose:
        lines.append(f"## 会議の目的\n{summary.meetingPurpose}\n")

    # Group items by type
    decisions = [i for i in summary.items if i.type == SummaryV2ItemType.DECISION]
    actions = [i for i in summary.items if i.type == SummaryV2ItemType.ACTION]
    questions = [i for i in summary.items if i.type == SummaryV2ItemType.OPEN_QUESTION]
    risks = [i for i in summary.items if i.type == SummaryV2ItemType.RISK]

    if decisions:
        lines.append("## 決定事項")
        for item in decisions:
            badge = "✅" if item.support == EvidenceSupport.FULL else "⚠️" if item.support == EvidenceSupport.PARTIAL else "❓"
            lines.append(f"- {badge} {item.text}")
        lines.append("")

    if actions:
        lines.append("## アクションアイテム")
        for item in actions:
            badge = "✅" if item.support == EvidenceSupport.FULL else "⚠️" if item.support == EvidenceSupport.PARTIAL else "❓"
            owner = f" (@{item.owner})" if item.owner else ""
            due = f" [期限: {item.dueDate}]" if item.dueDate else ""
            lines.append(f"- {badge} {item.text}{owner}{due}")
        lines.append("")

    if questions:
        lines.append("## 未決事項")
        for item in questions:
            badge = "⚠️" if item.support != EvidenceSupport.NONE else "❓"
            lines.append(f"- {badge} {item.text}")
        lines.append("")

    if risks:
        lines.append("## リスク・懸念事項")
        for item in risks:
            badge = "⚠️" if item.support != EvidenceSupport.NONE else "❓"
            lines.append(f"- {badge} {item.text}")
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
) -> SummaryV2:
    """
    Main pipeline: Generate structured summary with evidence.
    """
    logger.info(f"[SummaryV2] Starting generation for session {session_id}")

    # Step 0: Preprocess
    segments, marks = normalize_segments(transcript_text, diarized_segments, user_marks)
    logger.info(f"[SummaryV2] Preprocessed: {len(segments)} segments, {len(marks)} marks")

    # Step 1: Extract
    candidates = await extract_candidates(
        transcript_text,
        segments,
        marks,
        meeting_purpose,
        meeting_type,
        participants,
    )
    total_candidates = sum(len(v) for v in candidates.values())
    logger.info(f"[SummaryV2] Extracted {total_candidates} candidates")

    # Step 2 & 3: Ground and Compose
    items = compose_summary_items(candidates, segments)
    logger.info(f"[SummaryV2] Composed {len(items)} items with evidence")

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
    summary.renderedMarkdown = render_markdown(summary)

    return summary
