"""Build per-session transcript chunk index for Chat retrieval (Phase 7.6 prep).

Reads the session's transcript segments (from `sessions/{sid}/transcript_chunks/*`
or legacy `session.transcriptText`) and produces a **Chat-retrieval-ready**
index stored under `/transcript_chunks/{chunkId}`:

  - chunk_id, session_id, transcriptVersion
  - startMs / endMs
  - text, normalizedText (lowercase + whitespace normalized)
  - keywords (pre-extracted nouns / domain terms)
  - speakerHints (speakers that appear in the chunk)
  - segmentIds (original segment ids this chunk spans)
  - hasActionSignal / hasDecisionSignal (for rerank)

Chunk rules (target 15-45s / 300-800 chars, prefer speaker boundary):
  - A new chunk starts whenever total text ≥ 500 chars OR duration ≥ 30 s
  - Or when the speaker changes and current chunk has > 200 chars
  - Cap at 800 chars / 45 s to avoid runaway

This worker is designed to be idempotent: running it twice on the same
session replaces the existing chunks in-place (keyed by deterministic
chunkId = `{sessionId}_{firstSegmentIndex}_{lastSegmentIndex}`).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from google.cloud import firestore

from app.firebase import db


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

TARGET_MIN_CHARS = 300
TARGET_MAX_CHARS = 800
TARGET_MIN_DURATION_MS = 15_000
TARGET_MAX_DURATION_MS = 45_000
SPEAKER_BREAK_MIN_CHARS = 200

# Signal keyword seeds. Deliberately small — the retrieval layer can do
# heavy lifting via embeddings later. These just bias the reranker.
ACTION_SIGNALS = [
    "TODO", "todo", "タスク", "宿題", "やる", "やっておく", "までに",
    "来週", "今週", "本日中", "明日まで", "期限", "締切", "deadline",
    "担当",
]
DECISION_SIGNALS = [
    "決定", "決まった", "決めた", "合意", "結論", "これで確定",
    "承認", "採用", "go", "OK です",
]


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _extract_keywords(text: str) -> List[str]:
    """Very lightweight domain-term extraction.

    Long-term we should use embeddings + named entity extraction. For the
    retrieval-boosting-only use case, a frozen lexicon is enough to flag
    "this chunk mentions 'UI 案'" etc.
    """
    seeds = [
        "決定", "TODO", "タスク", "宿題", "来週", "火曜", "水曜", "木曜", "金曜",
        "UI", "UX", "API", "要件", "仕様", "見積もり", "予算", "スケジュール",
        "レビュー", "リリース", "本番", "テスト", "品質", "障害",
    ]
    out: List[str] = []
    for seed in seeds:
        if seed in text:
            out.append(seed)
    return out


def _scan_signals(text: str) -> tuple[bool, bool]:
    action = any(s in text for s in ACTION_SIGNALS)
    decision = any(s in text for s in DECISION_SIGNALS)
    return action, decision


# ---------------------------------------------------------------------------
# Segment loading — supports both the modern sub-collection and the legacy
# `session.transcriptText` (treated as one giant segment, indexed anyway).
# ---------------------------------------------------------------------------


def _load_segments(session_id: str) -> List[Dict[str, Any]]:
    """Yield segments in chronological order. Empty list if none available."""
    session_ref = db.collection("sessions").document(session_id)

    # Primary: sessions/{id}/transcript_chunks/{index} (server canonical)
    try:
        docs = list(
            session_ref.collection("transcript_chunks").order_by("index").stream()
        )
        if docs:
            segs: List[Dict[str, Any]] = []
            for doc in docs:
                d = doc.to_dict() or {}
                segs.append(
                    {
                        "index": int(d.get("index") or 0),
                        "startMs": int(d.get("startMs") or 0),
                        "endMs": int(d.get("endMs") or 0),
                        "text": (d.get("text") or "").strip(),
                        "speaker": d.get("speaker"),
                        "segmentIds": d.get("segmentIds") or [str(doc.id)],
                    }
                )
            return segs
    except Exception as e:
        logger.warning(f"[build_chunk_index] transcript_chunks read failed: {e}")

    # Fallback: derived/transcript/segments array (older schema)
    try:
        dsnap = session_ref.collection("derived").document("transcript").get()
        if dsnap.exists:
            d = dsnap.to_dict() or {}
            segs_raw = d.get("segments") or []
            if segs_raw:
                segs: List[Dict[str, Any]] = []
                for i, s in enumerate(segs_raw):
                    if not isinstance(s, dict):
                        continue
                    segs.append(
                        {
                            "index": i,
                            "startMs": int(s.get("startMs") or 0),
                            "endMs": int(s.get("endMs") or s.get("startMs") or 0),
                            "text": (s.get("text") or "").strip(),
                            "speaker": s.get("speaker"),
                            "segmentIds": [s.get("id") or f"seg_{i}"],
                        }
                    )
                return segs
    except Exception as e:
        logger.warning(f"[build_chunk_index] derived/transcript read failed: {e}")

    # Last resort: session.transcriptText (legacy, no timing info)
    try:
        snap = session_ref.get()
        if snap.exists:
            d = snap.to_dict() or {}
            full = (d.get("transcriptText") or "").strip()
            if full:
                return [
                    {
                        "index": 0,
                        "startMs": 0,
                        "endMs": int(d.get("durationSec", 0)) * 1000,
                        "text": full,
                        "speaker": None,
                        "segmentIds": ["seg_0"],
                    }
                ]
    except Exception as e:
        logger.warning(f"[build_chunk_index] session doc read failed: {e}")

    return []


# ---------------------------------------------------------------------------
# Chunk packing
# ---------------------------------------------------------------------------


def _should_close_chunk(
    *, total_chars: int, total_ms: int, speaker_changed: bool
) -> bool:
    if total_chars >= TARGET_MAX_CHARS:
        return True
    if total_ms >= TARGET_MAX_DURATION_MS:
        return True
    if speaker_changed and total_chars >= SPEAKER_BREAK_MIN_CHARS:
        return True
    if total_chars >= TARGET_MIN_CHARS and total_ms >= TARGET_MIN_DURATION_MS:
        return True
    return False


def _pack_chunks(
    session_id: str, segments: List[Dict[str, Any]], transcript_version: int
) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    buf: List[Dict[str, Any]] = []

    for seg in segments:
        prev_speaker = buf[-1].get("speaker") if buf else None
        speaker_changed = (
            seg.get("speaker") is not None
            and prev_speaker is not None
            and seg.get("speaker") != prev_speaker
        )

        if buf and speaker_changed:
            current_chars = sum(len(s.get("text") or "") for s in buf)
            current_ms = int(buf[-1]["endMs"]) - int(buf[0]["startMs"])
            if _should_close_chunk(
                total_chars=current_chars, total_ms=current_ms, speaker_changed=True
            ):
                chunks.append(_pack_one(session_id, buf, transcript_version))
                buf = []

        buf.append(seg)

        total_chars = sum(len(s.get("text") or "") for s in buf)
        total_ms = int(buf[-1]["endMs"]) - int(buf[0]["startMs"])
        if _should_close_chunk(
            total_chars=total_chars, total_ms=total_ms, speaker_changed=False
        ):
            chunks.append(_pack_one(session_id, buf, transcript_version))
            buf = []

    if buf:
        chunks.append(_pack_one(session_id, buf, transcript_version))

    return chunks


def _pack_one(
    session_id: str, buf: List[Dict[str, Any]], transcript_version: int
) -> Dict[str, Any]:
    text = " ".join((s.get("text") or "").strip() for s in buf if s.get("text")).strip()
    normalized = _normalize(text)
    has_action, has_decision = _scan_signals(text)
    speakers = list({s.get("speaker") for s in buf if s.get("speaker")})
    segment_ids: List[str] = []
    for s in buf:
        for sid in s.get("segmentIds") or []:
            if sid and sid not in segment_ids:
                segment_ids.append(sid)

    first_idx = int(buf[0].get("index") or 0)
    last_idx = int(buf[-1].get("index") or 0)
    chunk_id = f"{session_id}_{first_idx:04d}_{last_idx:04d}"

    return {
        "chunkId": chunk_id,
        "sessionId": session_id,
        "transcriptVersion": int(transcript_version),
        "startMs": int(buf[0]["startMs"]),
        "endMs": int(buf[-1]["endMs"]),
        "text": text,
        "normalizedText": normalized,
        "keywords": _extract_keywords(text),
        "speakerHints": speakers,
        "segmentIds": segment_ids,
        "hasActionSignal": has_action,
        "hasDecisionSignal": has_decision,
    }


# ---------------------------------------------------------------------------
# Entry point (idempotent: replaces the session's chunks in-place)
# ---------------------------------------------------------------------------


def build_transcript_chunk_index_for_session(
    session_id: str, transcript_version: Optional[int] = None
) -> Dict[str, Any]:
    """Build chunk index. Returns a summary of what was written.

    Usage:
        from app.jobs.build_transcript_chunk_index import build_transcript_chunk_index_for_session
        stats = build_transcript_chunk_index_for_session(session_id)
    """
    sess_ref = db.collection("sessions").document(session_id)
    sess_snap = sess_ref.get()
    if not sess_snap.exists:
        return {"status": "skipped", "reason": "session_not_found"}

    sess = sess_snap.to_dict() or {}
    version = int(transcript_version or sess.get("transcriptVersion") or 1)

    segments = _load_segments(session_id)
    if not segments:
        return {"status": "skipped", "reason": "no_segments"}

    chunks = _pack_chunks(session_id, segments, transcript_version=version)

    # Delete previous chunks for this session first (idempotency)
    try:
        prev = list(
            db.collection("transcript_chunks")
            .where("sessionId", "==", session_id)
            .stream()
        )
        if prev:
            batch = db.batch()
            for i, doc in enumerate(prev):
                batch.delete(doc.reference)
                if (i + 1) % 400 == 0:
                    batch.commit()
                    batch = db.batch()
            batch.commit()
    except Exception as e:
        logger.warning(f"[build_chunk_index] previous chunk cleanup failed: {e}")

    # Write new chunks in batches of 400
    written = 0
    batch = db.batch()
    for i, chunk in enumerate(chunks):
        ref = db.collection("transcript_chunks").document(chunk["chunkId"])
        batch.set(
            ref,
            {**chunk, "createdAt": firestore.SERVER_TIMESTAMP},
        )
        if (i + 1) % 400 == 0:
            batch.commit()
            batch = db.batch()
            written = i + 1
    batch.commit()
    written = len(chunks)

    logger.info(
        f"[build_chunk_index] session={session_id} chunks={written} "
        f"segments={len(segments)} version={version}"
    )
    return {
        "status": "completed",
        "sessionId": session_id,
        "transcriptVersion": version,
        "segmentCount": len(segments),
        "chunkCount": written,
    }
