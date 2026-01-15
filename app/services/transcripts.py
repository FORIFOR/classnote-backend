from typing import Optional, List, Dict, Any

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
