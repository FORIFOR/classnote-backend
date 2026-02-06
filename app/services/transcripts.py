from typing import Optional, List, Dict, Any, Tuple

from app.firebase import db


def has_transcript_chunks(session_id: str) -> bool:
    ref = db.collection("sessions").document(session_id).collection("transcript_chunks")
    try:
        docs = ref.limit(1).stream()
    except Exception:
        return False
    return any(True for _ in docs)

def get_transcript_chunks(session_id: str) -> List[Dict[str, Any]]:
    ref = db.collection("sessions").document(session_id).collection("transcript_chunks")
    try:
        docs = ref.order_by("startMs").stream()
    except Exception:
        docs = ref.order_by("createdAt").stream()

    chunks: List[Dict[str, Any]] = []
    for doc in docs:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        chunks.append(data)
    return chunks


def get_transcript_chunks_paginated(
    session_id: str,
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    after_cursor: Optional[str] = None,
    limit: int = 50,
    skip_count: bool = False,
) -> Tuple[List[Dict[str, Any]], int, bool, Optional[str]]:
    """
    Paginated transcript chunk retrieval.

    Args:
        session_id: Session ID
        from_ms: Start time in milliseconds (inclusive)
        to_ms: End time in milliseconds (inclusive)
        after_cursor: Cursor for pagination (chunk ID)
        limit: Max chunks to return
        skip_count: If True, skip expensive total count query (returns -1)

    Returns: (chunks, totalCount, hasMore, nextCursor)
    """
    ref = db.collection("sessions").document(session_id).collection("transcript_chunks")

    # Count total (skip if not needed for performance)
    total_count = -1
    if not skip_count:
        try:
            # Use aggregation if available (Firestore SDK >= 2.11)
            count_query = ref.count()
            count_result = count_query.get()
            total_count = count_result[0][0].value if count_result else 0
        except Exception:
            # Fallback: count via select (more efficient than full stream)
            try:
                total_count = sum(1 for _ in ref.select([]).stream())
            except Exception:
                total_count = 0

    # Build query with time range filter
    query = ref.order_by("startMs")
    if from_ms is not None:
        query = query.where("startMs", ">=", from_ms)
    if to_ms is not None:
        query = query.where("startMs", "<=", to_ms)

    # Cursor-based pagination: start after the given document
    if after_cursor:
        cursor_doc = ref.document(after_cursor).get()
        if cursor_doc.exists:
            query = query.start_after(cursor_doc)

    # Fetch limit+1 to detect hasMore
    docs = list(query.limit(limit + 1).stream())
    has_more = len(docs) > limit
    if has_more:
        docs = docs[:limit]

    chunks = []
    next_cursor = None
    for doc in docs:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        chunks.append(data)
        next_cursor = doc.id

    if not has_more:
        next_cursor = None

    return chunks, total_count, has_more, next_cursor


def count_transcript_chunks(session_id: str) -> int:
    ref = db.collection("sessions").document(session_id).collection("transcript_chunks")
    count = 0
    try:
        for _ in ref.select([]).stream():
            count += 1
    except Exception:
        pass
    return count


def build_transcript_text_from_chunks(chunks: List[Dict[str, Any]]) -> Optional[str]:
    texts: List[str] = []
    for chunk in chunks:
        text = chunk.get("text")
        if text:
            texts.append(str(text).strip())
    if not texts:
        return None
    return "\n".join([t for t in texts if t])


def resolve_transcript_text(
    session_id: str,
    session_data: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    if session_data:
        transcript = session_data.get("transcriptText")
        if transcript:
            return transcript
    chunks = get_transcript_chunks(session_id)
    return build_transcript_text_from_chunks(chunks)
