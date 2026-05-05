"""
entity_review_store.py — PR2 Entity Review Firestore access layer.

Collapses the spec's 5 separate repositories (session / transcript /
entity_review / patch / term_memory) into one module to mirror the
existing project style (`firestore_summary_v2.py`). Plain sync Firestore
client — no DI container.

Paths (spec §2):
    sessions/{sid}                                     — session doc mirror
    sessions/{sid}/derived/canonical_transcript        — canonical text
    sessions/{sid}/derived/term_hints                  — next-session hints
    sessions/{sid}/entity_reviews/{rid}                — review parent
    sessions/{sid}/entity_reviews/{rid}/candidates/{cid}
    sessions/{sid}/transcript_patches/{pid}
    users/{uid}/custom_terms/{tid}
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.firebase import db

logger = logging.getLogger("app.entity_review_store")


_CANONICAL_DOC = "canonical_transcript"
_TERM_HINTS_DOC = "term_hints"
_ENTITY_REVIEWS_SUB = "entity_reviews"
_CANDIDATES_SUB = "candidates"
_TRANSCRIPT_PATCHES_SUB = "transcript_patches"
_CUSTOM_TERMS_SUB = "custom_terms"


# ---------------------------------------------------------------------------
# Session-doc mirror
# ---------------------------------------------------------------------------

def _session_ref(session_id: str):
    return db.collection("sessions").document(session_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def update_entity_review_status(
    session_id: str, status: str, review_id: Optional[str]
) -> None:
    _session_ref(session_id).set({
        "entityReviewStatus": status,
        "entityReviewId": review_id,
        "updatedAt": _now(),
    }, merge=True)


def update_canonical_version(session_id: str, version: int) -> None:
    _session_ref(session_id).set({
        "canonicalTranscriptVersion": version,
        "updatedAt": _now(),
    }, merge=True)


# ---------------------------------------------------------------------------
# Canonical transcript
# ---------------------------------------------------------------------------

def _canonical_ref(session_id: str):
    return _session_ref(session_id).collection("derived").document(_CANONICAL_DOC)


def get_canonical_transcript(session_id: str) -> Optional[Dict[str, Any]]:
    snap = _canonical_ref(session_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


def save_canonical_transcript(
    session_id: str,
    *,
    version: int,
    text: str,
    base_version: int,
    patch_count: int,
    source: str,
    language: str = "ja",
) -> None:
    _canonical_ref(session_id).set({
        "version": version,
        "baseTranscriptVersion": base_version,
        "text": text,
        "language": language,
        "patchCount": patch_count,
        "source": source,
        "updatedAt": _now(),
    })


# ---------------------------------------------------------------------------
# Term hints (per-session cache; derived from user's custom_terms)
# ---------------------------------------------------------------------------

def _term_hints_ref(session_id: str):
    return _session_ref(session_id).collection("derived").document(_TERM_HINTS_DOC)


def save_term_hints(session_id: str, payload: Dict[str, Any]) -> None:
    _term_hints_ref(session_id).set({**payload, "createdAt": _now()})


def get_term_hints(session_id: str) -> Optional[Dict[str, Any]]:
    snap = _term_hints_ref(session_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


# ---------------------------------------------------------------------------
# Entity reviews (parent doc + candidates subcollection)
# ---------------------------------------------------------------------------

def _reviews_col(session_id: str):
    return _session_ref(session_id).collection(_ENTITY_REVIEWS_SUB)


def _review_ref(session_id: str, review_id: str):
    return _reviews_col(session_id).document(review_id)


def _candidates_col(session_id: str, review_id: str):
    return _review_ref(session_id, review_id).collection(_CANDIDATES_SUB)


def create_review(
    session_id: str,
    *,
    source_transcript_version: int,
    candidate_count: int,
    language: str = "ja",
) -> Dict[str, Any]:
    review_id = f"review_{uuid.uuid4().hex[:12]}"
    now = _now()
    data = {
        "reviewId": review_id,
        "status": "pending",
        "sourceTranscriptVersion": source_transcript_version,
        "candidateCount": candidate_count,
        "appliedCount": 0,
        "skippedCount": 0,
        "language": language,
        "createdAt": now,
        "updatedAt": now,
    }
    _review_ref(session_id, review_id).set(data)
    return data


def save_candidates(
    session_id: str, review_id: str, candidates: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Bulk-write candidates. Assigns candidateId if missing."""
    col = _candidates_col(session_id, review_id)
    batch = db.batch()
    assigned: List[Dict[str, Any]] = []
    for c in candidates:
        cid = c.get("candidateId") or f"cand_{uuid.uuid4().hex[:12]}"
        doc = {
            **c,
            "candidateId": cid,
            "decision": c.get("decision", "unreviewed"),
            "replacement": c.get("replacement"),
        }
        batch.set(col.document(cid), doc)
        assigned.append(doc)
    batch.commit()
    return assigned


def list_candidates(session_id: str, review_id: str) -> List[Dict[str, Any]]:
    return [doc.to_dict() for doc in _candidates_col(session_id, review_id).stream()]


def get_latest_review(session_id: str) -> Optional[Dict[str, Any]]:
    """Most-recent parent review doc (no candidates)."""
    from google.cloud import firestore as _fs
    qs = (
        _reviews_col(session_id)
        .order_by("createdAt", direction=_fs.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    docs = list(qs)
    return docs[0].to_dict() if docs else None


def mark_review_applied(
    session_id: str, review_id: str, *, applied_count: int
) -> None:
    _review_ref(session_id, review_id).set({
        "status": "applied",
        "appliedCount": applied_count,
        "updatedAt": _now(),
    }, merge=True)


def mark_review_skipped(
    session_id: str, review_id: str, *, skipped_count: Optional[int] = None
) -> None:
    payload: Dict[str, Any] = {
        "status": "skipped",
        "updatedAt": _now(),
    }
    if skipped_count is not None:
        payload["skippedCount"] = skipped_count
    _review_ref(session_id, review_id).set(payload, merge=True)


# ---------------------------------------------------------------------------
# Transcript patches (audit trail)
# ---------------------------------------------------------------------------

def _patches_col(session_id: str):
    return _session_ref(session_id).collection(_TRANSCRIPT_PATCHES_SUB)


def create_patch(
    session_id: str,
    *,
    review_id: str,
    candidate_id: str,
    action: str,
    find: str,
    replace: str,
    applied_to_occurrences: int,
) -> str:
    patch_id = f"patch_{uuid.uuid4().hex[:12]}"
    _patches_col(session_id).document(patch_id).set({
        "reviewId": review_id,
        "candidateId": candidate_id,
        "action": action,
        "find": find,
        "replace": replace,
        "appliedToOccurrences": applied_to_occurrences,
        "createdAt": _now(),
    })
    return patch_id


# ---------------------------------------------------------------------------
# Custom terms (user's personal glossary; seed for future term_hints)
# ---------------------------------------------------------------------------

def _custom_terms_col(user_id: str):
    return db.collection("users").document(user_id).collection(_CUSTOM_TERMS_SUB)


def list_terms_for_user(user_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
    from google.cloud import firestore as _fs
    qs = (
        _custom_terms_col(user_id)
        .order_by("weight", direction=_fs.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() for doc in qs]


def upsert_term(
    user_id: str,
    *,
    canonical: str,
    alias: str,
    entity_type: str = "unknown",
    created_from_session_id: Optional[str] = None,
    language: str = "ja",
) -> None:
    """Merge alias into an existing canonical entry, or create a new one."""
    col = _custom_terms_col(user_id)
    existing_qs = col.where("canonical", "==", canonical).limit(1).stream()
    existing = list(existing_qs)
    now = _now()

    if existing:
        snap = existing[0]
        data = snap.to_dict() or {}
        aliases = set(data.get("aliases") or [])
        aliases.add(alias)
        weight = float(data.get("weight", 0.8))
        snap.reference.set({
            "aliases": sorted(aliases),
            "weight": round(min(1.0, weight + 0.03), 4),
            "lastUsedAt": now,
            "updatedAt": now,
        }, merge=True)
        return

    col.document().set({
        "canonical": canonical,
        "aliases": [alias] if alias and alias != canonical else [],
        "entityType": entity_type,
        "language": language,
        "scope": "user",
        "weight": 0.95,
        "createdFromSessionId": created_from_session_id,
        "lastUsedAt": now,
        "updatedAt": now,
    })
