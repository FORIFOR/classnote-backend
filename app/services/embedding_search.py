"""DeepNote — embedding-backed span retrieval (Phase D).

Replaces / augments the keyword-only ``_select_transcript_spans`` in
``assistant_qna`` so freeform Q&A grounds on transcript chunks chosen
by semantic similarity, not just literal keyword overlap.

Cost notes:
  - Vertex AI ``text-embedding-004`` is roughly two orders of magnitude
    cheaper per 1k tokens than Gemini Flash Lite, so a 30-chunk
    transcript embedding pass is ~free at chat-ops volumes.
  - We embed only when the freeform path is taken; keyword scoring
    still runs first as a cheap fallback when Vertex is unavailable.
  - Env flag ``ASSISTANT_EMBEDDING_SEARCH=on`` activates the path.

Public API:
  ``select_spans_by_embedding(transcript, question, max_spans=4) -> List[span]``
  Returns the same span shape as the keyword version
  (``{start_line, end_line, text}``).
"""
from __future__ import annotations

import logging
import math
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("app.services.embedding_search")

DEFAULT_MODEL = os.environ.get("EMBEDDING_MODEL_NAME") or "text-embedding-004"
CHUNK_LINES = 4   # ~ 4 transcript lines per chunk
CHUNK_STRIDE = 3  # overlap 1 line between adjacent chunks


def _chunkify(transcript: str) -> List[Dict[str, Any]]:
    if not transcript:
        return []
    lines = transcript.splitlines()
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        end = min(len(lines), i + CHUNK_LINES)
        text = "\n".join(lines[i:end]).strip()
        if text:
            out.append({"start_line": i, "end_line": end - 1, "text": text})
        if end >= len(lines):
            break
        i += CHUNK_STRIDE
    return out


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    num = 0.0
    da = 0.0
    db = 0.0
    for x, y in zip(a, b):
        num += x * y
        da += x * x
        db += y * y
    if da == 0 or db == 0:
        return 0.0
    return num / (math.sqrt(da) * math.sqrt(db))


def _embed(texts: List[str]) -> Optional[List[List[float]]]:
    """Return parallel list of embedding vectors. Returns None if Vertex
    is unavailable so the caller can fall back gracefully.
    """
    if not texts:
        return []
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel
        from app.services import llm as _llm
        project_id = _llm._get_project_id()
        location = (
            os.environ.get("VERTEX_REGION")
            or os.environ.get("VERTEX_LOCATION")
            or "us-central1"
        )
        if project_id:
            vertexai.init(project=project_id, location=location)
        model = TextEmbeddingModel.from_pretrained(DEFAULT_MODEL)
        # Vertex caps each request at ~250 inputs; we batch in 64s.
        out: List[List[float]] = []
        for i in range(0, len(texts), 64):
            batch = texts[i : i + 64]
            resp = model.get_embeddings(batch)
            for emb in resp:
                out.append(list(emb.values))
        return out
    except Exception as e:
        logger.warning("[embedding_search] Vertex embed failed: %s", e)
        return None


def select_spans_by_embedding(
    transcript: str, question: str, *, max_spans: int = 4
) -> List[Dict[str, Any]]:
    """Pick top-N transcript chunks by cosine similarity to the question.

    Returns spans in the same dict shape as the keyword version so it
    is a drop-in replacement.
    """
    if not transcript or not question:
        return []
    chunks = _chunkify(transcript)
    if not chunks:
        return []
    # Embed the question + every chunk in one request batch.
    inputs = [question] + [c["text"] for c in chunks]
    vecs = _embed(inputs)
    if not vecs or len(vecs) != len(inputs):
        return []
    qvec = vecs[0]
    cvecs = vecs[1:]
    scored = []
    for i, c in enumerate(chunks):
        s = _cosine(qvec, cvecs[i])
        scored.append((s, c))
    scored.sort(key=lambda kv: -kv[0])
    out: List[Dict[str, Any]] = []
    used: List = []
    for _, c in scored:
        if any(not (c["end_line"] < us or c["start_line"] > ue) for us, ue in used):
            continue
        used.append((c["start_line"], c["end_line"]))
        out.append({"start_line": c["start_line"], "end_line": c["end_line"],
                    "text": c["text"][:400]})
        if len(out) >= max_spans:
            break
    return out
