"""Context builder — prepares session data for Gemini prompt."""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("app.services.context_builder")

MAX_FULL_TRANSCRIPT_CHARS = 10000


def build_session_context(session_data: dict, query: str) -> dict:
    """Build context dict from a Firestore session document.

    Returns dict with: session_id, title, summary, transcript_excerpt, started_at
    """
    session_id = session_data.get("id", "")
    title = session_data.get("title", "無題")
    summary = session_data.get("summaryMarkdown") or session_data.get("topicSummary") or ""
    transcript = session_data.get("transcriptText") or ""
    started_at = session_data.get("startedAt")
    mode = session_data.get("mode", "lecture")

    transcript_len = len(transcript)
    summary_len = len(summary)

    # Short transcript → use full text
    if transcript_len <= MAX_FULL_TRANSCRIPT_CHARS:
        excerpt = transcript
        logger.info(
            f"[ContextBuilder] session={session_id} title=\"{title[:40]}\" mode={mode} "
            f"summary={summary_len} transcript={transcript_len} (full)"
        )
    else:
        # Extract relevant portions via simple keyword matching
        excerpt = _extract_relevant_portions(transcript, query)
        logger.info(
            f"[ContextBuilder] session={session_id} title=\"{title[:40]}\" mode={mode} "
            f"summary={summary_len} transcript={transcript_len}→excerpt={len(excerpt)} (extracted)"
        )

    return {
        "session_id": session_id,
        "title": title,
        "mode": mode,
        "summary": summary,
        "transcript_excerpt": excerpt,
        "started_at": str(started_at) if started_at else None,
    }


def _extract_relevant_portions(transcript: str, query: str, max_chars: int = 8000) -> str:
    """Extract portions of transcript relevant to the query."""
    # Split into paragraphs/sentences
    paragraphs = [p.strip() for p in transcript.split("\n") if p.strip()]

    if not paragraphs:
        return transcript[:max_chars]

    # Simple keyword scoring
    query_tokens = set(query.lower().replace("?", "").replace("？", "").split())
    scored = []
    for i, para in enumerate(paragraphs):
        score = sum(1 for t in query_tokens if t in para.lower())
        scored.append((score, i, para))

    # Sort by score descending, take top portions
    scored.sort(key=lambda x: (-x[0], x[1]))

    selected = []
    total_chars = 0
    # Always include first few paragraphs for context
    for i, para in enumerate(paragraphs[:3]):
        selected.append((i, para))
        total_chars += len(para)

    # Add high-scoring paragraphs
    for score, idx, para in scored:
        if total_chars >= max_chars:
            break
        if idx < 3:  # Already included
            continue
        selected.append((idx, para))
        total_chars += len(para)

    # Sort by original order
    selected.sort(key=lambda x: x[0])
    return "\n".join(p for _, p in selected)


def build_todo_context(todos: List[dict]) -> str:
    """Format user's TODO list for prompt injection."""
    if not todos:
        return "(TODOリストは空です)"

    lines = []
    for t in todos:
        title = t.get("title", "無題")
        status = t.get("status", "open")
        priority = t.get("priority", "mid")
        due = t.get("dueDate", "期限なし")
        source_title = ""
        source = t.get("source")
        if source and isinstance(source, dict):
            source_title = source.get("sessionTitle", "")
        notes = t.get("notes", "")

        parts = [f"- [{priority}] {title}"]
        if due and due != "期限なし":
            parts.append(f"期限: {due}")
        if status == "done":
            parts.append("完了済み")
        elif status == "overdue":
            parts.append("期限切れ")
        if source_title:
            parts.append(f"出典: {source_title}")
        if notes:
            parts.append(f"メモ: {notes[:100]}")
        lines.append(" / ".join(parts))

    return "\n".join(lines)


def build_turn_prompt(
    message: str,
    mode: str,
    contexts: List[dict],
    history: List[dict],
    conversation_summary: Optional[str] = None,
    todo_context: Optional[str] = None,
) -> str:
    """Build the full prompt for a single chat turn."""
    context_session_ids = [c.get("session_id", "?") for c in contexts]
    logger.info(
        f"[ContextBuilder] build_turn_prompt: mode={mode} contexts={context_session_ids} "
        f"history_turns={len(history)} has_summary={bool(conversation_summary)}"
    )

    # Conversation summary (persisted across turns)
    summary_text = conversation_summary if conversation_summary else "(初回の質問)"

    # Chat history (last 6 turns — shorter since we have summary)
    history_lines = []
    for m in history[-6:]:
        role = m.get("role", "user")
        text = m.get("text", "")
        history_lines.append(f"{role}: {text}")
    history_text = "\n".join(history_lines) if history_lines else "(なし)"

    # Session context
    if contexts:
        context_parts = []
        for c in contexts:
            part = f"[session_id={c['session_id']}]\n"
            part += f"title: {c['title']}\n"
            if c.get('summary'):
                part += f"summary:\n{c['summary']}\n"
            if c.get('transcript_excerpt'):
                part += f"transcript_excerpt:\n{c['transcript_excerpt']}"
            context_parts.append(part)
        context_text = "\n\n".join(context_parts)
    else:
        context_text = "(セッション文脈なし)"

    # Optional TODO context
    todo_section = ""
    if todo_context:
        todo_section = f"""
[user_todos]
{todo_context}
"""

    return f"""[conversation_summary]
{summary_text}

[chat_mode]
{mode}
{todo_section}
[chat_history]
{history_text}

[session_context]
{context_text}

[user_question]
{message}

[output_rule]
JSONのみを返してください。markdown記法は answer フィールド内で使用可能です。
follow_up_suggestion には次に聞けそうな提案を1つ必ず入れてください。
conversation_summary_next にはこの会話の要約を1〜2文で入れてください。"""
