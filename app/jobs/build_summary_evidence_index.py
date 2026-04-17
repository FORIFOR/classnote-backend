"""Build a root-level summary_evidence_index for cross-session retrieval.

For every bullet in a session's SummaryV2 (keyPoints / decisions / todos /
openQuestions / discussionPoints / sections[*].bullets / terms / formulas /
contextNotes / decisionLog), emit one document to `/summary_evidence_index/{id}`
with evidence timestamps + text + chunkId if known.

Purpose:
  - Chat retrieval can seed with these before touching transcript chunks
    ("evidence-first strategy" from the design doc).
  - Admin dashboards can query evidence across many sessions.
  - Future cross-session chat / Q&A (Phase 7.6) can look up semantic hits
    by keyword or embedding over this flattened collection.

Permissions (rules): admin-only read for now; clients should continue
reading the per-session derived/summary sub-collection.

Idempotent: documents are keyed by
  evidence_id = `{sessionId}:{kind}:{itemId}:{index}`
so re-running replaces existing rows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from google.cloud import firestore

from app.firebase import db


logger = logging.getLogger(__name__)


# kind → SummaryV2 field name mapping we flatten into the index
_BULLET_FIELDS: List[tuple[str, str]] = [
    ("keyPoint", "keyPoints"),
    ("decision", "decisions"),
    ("todo", "todos"),
    ("openQuestion", "openQuestions"),
    ("discussionPoint", "discussionPoints"),
    ("contextNote", "contextNotes"),
    ("decisionLog", "decisionLog"),
    ("term", "terms"),
    ("formula", "formulas"),
]


def _keywords_from_text(text: str) -> List[str]:
    seeds = [
        "決定", "TODO", "タスク", "宿題", "来週", "火曜", "水曜", "木曜", "金曜",
        "UI", "UX", "API", "要件", "仕様", "見積もり", "予算", "スケジュール",
        "リスク", "課題", "締切", "担当",
    ]
    out: List[str] = []
    for seed in seeds:
        if seed in text:
            out.append(seed)
    return out


def _iter_bullets(payload: Dict[str, Any]) -> Iterable[tuple[str, int, Dict[str, Any]]]:
    """Yield (kind, index_within_list, item_dict) across every SummaryV2 field."""
    for kind, field in _BULLET_FIELDS:
        items = payload.get(field) or []
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if isinstance(item, dict):
                yield kind, i, item

    # sections[*].bullets
    sections = payload.get("sections") or []
    if isinstance(sections, list):
        for si, sec in enumerate(sections):
            if not isinstance(sec, dict):
                continue
            for bi, bullet in enumerate(sec.get("bullets") or []):
                if isinstance(bullet, dict):
                    yield "section_bullet", si * 1000 + bi, {
                        **bullet,
                        "_sectionHeading": sec.get("heading"),
                    }


def _evidence_doc_id(session_id: str, kind: str, item_id: str, idx: int) -> str:
    safe_id = (item_id or f"idx{idx}").replace("/", "_").replace(":", "_")
    return f"{session_id}:{kind}:{safe_id}"


def _evidence_payload(
    *,
    session_id: str,
    summary_version: int,
    kind: str,
    item: Dict[str, Any],
    evidence_refs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    text = (item.get("text") or item.get("task") or item.get("term") or "").strip()
    first_ref = evidence_refs[0] if evidence_refs else {}

    return {
        "evidenceId": item.get("id") or item.get("evidenceId"),
        "sessionId": session_id,
        "summaryVersion": summary_version,
        "kind": kind,
        "text": text[:500],
        "segmentId": first_ref.get("segmentId"),
        "startMs": first_ref.get("startMs"),
        "endMs": first_ref.get("endMs"),
        "quotePreview": first_ref.get("quotePreview"),
        "chunkId": first_ref.get("chunkId"),
        "keywords": _keywords_from_text(text),
        "confidence": item.get("confidence"),
        "needConfirm": item.get("needConfirm"),
        "owner": item.get("owner"),
        "due": item.get("due") or item.get("dueDate"),
        "sectionHeading": item.get("_sectionHeading"),
        "allEvidenceRefs": evidence_refs,
        "createdAt": firestore.SERVER_TIMESTAMP,
    }


def backfill_summary_evidence_for_session(session_id: str) -> Dict[str, Any]:
    """Backfill / rebuild summary_evidence_index for a single session.

    Reads `sessions/{id}/derived/summary.result.json` (current schema) or
    `sessions/{id}/derived/summary_v2` (legacy alt path) and writes one
    doc per bullet into `/summary_evidence_index`.
    """
    sess_ref = db.collection("sessions").document(session_id)

    # Try the canonical path first (written by app/routes/tasks.py summary worker)
    snap = sess_ref.collection("derived").document("summary").get()
    payload: Optional[Dict[str, Any]] = None
    summary_version = 2

    if snap.exists:
        data = snap.to_dict() or {}
        result = data.get("result") or {}
        payload_raw = result.get("json")
        if isinstance(payload_raw, dict):
            payload = payload_raw
            summary_version = int(
                (data.get("meta") or {}).get("schemaVersion") or payload.get("schemaVersion") or 2
            )

    # Fallback: legacy summary_v2 doc
    if payload is None:
        legacy_snap = sess_ref.collection("derived").document("summary_v2").get()
        if legacy_snap.exists:
            payload = legacy_snap.to_dict() or {}

    if not payload:
        return {"status": "skipped", "reason": "no_summary", "sessionId": session_id}

    # Delete previous rows for idempotency
    try:
        prev = list(
            db.collection("summary_evidence_index")
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
        logger.warning(f"[build_evidence_index] cleanup failed: {e}")

    written = 0
    batch = db.batch()

    for kind, idx, item in _iter_bullets(payload):
        evidence_refs = item.get("evidence") or []
        if not isinstance(evidence_refs, list):
            evidence_refs = []
        # Normalize evidence refs (copy of session_projection._normalize_evidence)
        normalized_refs: List[Dict[str, Any]] = []
        for ref in evidence_refs:
            if not isinstance(ref, dict):
                continue
            r: Dict[str, Any] = {}
            for key in ("segmentId", "startMs", "endMs", "quotePreview", "chunkId"):
                if key in ref and ref[key] is not None:
                    r[key] = ref[key]
            if r:
                normalized_refs.append(r)

        item_id = item.get("id") or item.get("evidenceId") or f"idx_{idx}"
        doc_id = _evidence_doc_id(session_id, kind, str(item_id), idx)
        doc_payload = _evidence_payload(
            session_id=session_id,
            summary_version=summary_version,
            kind=kind,
            item=item,
            evidence_refs=normalized_refs,
        )
        ref = db.collection("summary_evidence_index").document(doc_id)
        batch.set(ref, doc_payload)
        written += 1
        if written % 400 == 0:
            batch.commit()
            batch = db.batch()

    if written % 400 != 0:
        batch.commit()

    logger.info(
        f"[build_evidence_index] session={session_id} rows={written} version={summary_version}"
    )
    return {
        "status": "completed",
        "sessionId": session_id,
        "summaryVersion": summary_version,
        "rows": written,
    }
