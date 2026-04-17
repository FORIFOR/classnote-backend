"""Session-first AI chat service.

Consolidates the 7 layers proposed in the design doc into a single entry point:
  1. Intent Router        — reuses app.services.chat_router.classify_route
  2. Context Builder      — reuses app.services.context_builder.build_session_context
  3. Retrieval Layer      — keyword-based chunk selection (embedding retrieval
                            scheduled for a later phase)
  4. Tool Runner          — surfaced as presets (`summarize`, `extract_todos`,
                            `extract_decisions`, `next_agenda`, `short_share`,
                            `quiz_questions`). Generic tool-calling is future work.
  5. LLM Orchestrator     — routes to gemini_chat (session) or the grounded /
                            general model (web-grounded / general knowledge)
  6. Citation Builder     — maps LLM output back to transcript_chunks via
                            anchor_resolver; citations are always returned as
                            an array (possibly empty).
  7. Response Streamer    — /v1/chat returns non-stream JSON for MVP; streaming
                            variant reuses gemini_stream.

The new `POST /v1/chat` coexists with the legacy `/v1/chat/send` and
`/v1/chat/stream` endpoints; existing clients are not affected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from google.cloud import firestore

from app.firebase import db
from app.services import chat_router, context_builder
from app.services.ai_credits import ai_credits, estimate_cost
from app.services.anchor_resolver import find_best_segments, normalize_segments
from app.services.gemini_chat import (
    CHAT_MODEL_NAME,
    GENERAL_MODEL_NAME,
    call_gemini_chat,
    call_gemini_general_chat,
    call_gemini_general_with_search,
    call_gemini_search_hybrid,
)
from app.services.gemini_stream import (
    stream_gemini_chat,
    stream_gemini_search_hybrid,
    stream_gemini_with_search,
)
from app.services.session_projection import compute_permissions


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChatError(Exception):
    pass


class NotFoundError(ChatError):
    pass


class ForbiddenError(ChatError):
    pass


class CreditLimitError(ChatError):
    def __init__(self, info: Dict[str, Any]):
        self.info = info
        super().__init__(info.get("reason", "credit_limit"))


# ---------------------------------------------------------------------------
# Presets  (Tool Runner lite)
# ---------------------------------------------------------------------------


PresetId = Literal[
    "summarize",
    "extract_todos",
    "extract_decisions",
    "next_agenda",
    "short_share",
    "quiz_questions",
]

_PRESETS: Dict[str, Dict[str, str]] = {
    "summarize": {
        "label": "要点を要約",
        "instruction": (
            "このセッションの要点を日本語で3行に要約してください。"
            "各行は「・」で始め、1 行 80 字以内。根拠となる発言があれば"
            "その時刻を括弧書きで添えてください。"
        ),
    },
    "extract_todos": {
        "label": "TODOを抽出",
        "instruction": (
            "このセッションで言及されたTODO（誰が何をいつまでに）を箇条書きで抽出してください。"
            "担当者・期限が明示されていない場合は null と明記。"
            "根拠の時刻を必ず添えてください。"
        ),
    },
    "extract_decisions": {
        "label": "決定事項を抽出",
        "instruction": (
            "このセッションで下された決定事項だけを日本語で箇条書きにしてください。"
            "推測や未決は含めず、確定した決定のみ。根拠の時刻を添えてください。"
        ),
    },
    "next_agenda": {
        "label": "次回アジェンダ案",
        "instruction": (
            "このセッション内容をもとに、次回ミーティング/授業のアジェンダ案を5項目以内で作成してください。"
            "各項目に想定所要時間（分）と、扱うべき論点を添えてください。"
        ),
    },
    "short_share": {
        "label": "Slack用に短く",
        "instruction": (
            "このセッションの成果を、Slack 投稿向けに 300 字以内の日本語で整形してください。"
            "① 何の会議/授業か ② 決定/要点 ③ 宿題 ④ 次回アクション、の順で。"
        ),
    },
    "quiz_questions": {
        "label": "理解度チェックを作る",
        "instruction": (
            "このセッションの内容を理解しているかチェックするための設問を 3 問、"
            "4 択形式で作成してください。"
            "各問に正解と、根拠のセグメント時刻を添えてください。"
        ),
    },
}


def list_presets() -> List[Dict[str, str]]:
    return [{"id": pid, "label": p["label"]} for pid, p in _PRESETS.items()]


# ---------------------------------------------------------------------------
# Request / context
# ---------------------------------------------------------------------------


@dataclass
class ChatContext:
    user: Any  # CurrentUser
    scope: Dict[str, Any]  # {"type": "session"|"general", "sessionId"?: str}
    message: str
    preset: Optional[str] = None
    conversation_id: Optional[str] = None
    selected_context: Optional[Dict[str, Any]] = None  # {"tab","evidenceId","quote"...}
    history: Optional[List[Dict[str, str]]] = None  # [{"role","text"}]


def _scope_type(ctx: ChatContext) -> str:
    t = (ctx.scope or {}).get("type")
    if t not in ("session", "general"):
        raise ChatError("invalid scope type")
    return t


def _session_id(ctx: ChatContext) -> Optional[str]:
    if _scope_type(ctx) != "session":
        return None
    sid = (ctx.scope or {}).get("sessionId")
    if not sid:
        raise ChatError("sessionId required for session scope")
    return sid


def _effective_message(ctx: ChatContext) -> str:
    """Combine preset instruction with user message."""
    if ctx.preset:
        preset_def = _PRESETS.get(ctx.preset)
        if not preset_def:
            raise ChatError(f"unknown preset: {ctx.preset}")
        base = preset_def["instruction"]
        if ctx.message.strip():
            return f"{base}\n\n---\n追加の指示: {ctx.message.strip()}"
        return base
    return ctx.message


# ---------------------------------------------------------------------------
# Load / permissions
# ---------------------------------------------------------------------------


def _load_session(ctx: ChatContext) -> Dict[str, Any]:
    sid = _session_id(ctx)
    if not sid:
        return {}
    snap = db.collection("sessions").document(sid).get()
    if not snap.exists:
        raise NotFoundError("session not found")
    data = snap.to_dict() or {}
    data["id"] = sid
    perms = compute_permissions(data, ctx.user)
    if not perms["canView"]:
        raise ForbiddenError("permission denied")
    return data


def _load_transcript_chunks(session_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch transcript chunks for retrieval. Capped to avoid runaway reads."""
    try:
        docs = list(
            db.collection("sessions")
            .document(session_id)
            .collection("transcript_chunks")
            .order_by("index")
            .limit(limit)
            .stream()
        )
    except Exception as e:
        logger.warning(f"[session_chat] transcript chunks read failed: {e}")
        return []
    chunks = []
    for d in docs:
        dd = d.to_dict() or {}
        chunks.append(
            {
                "index": int(dd.get("index") or 0),
                "startMs": int(dd.get("startMs") or 0),
                "endMs": int(dd.get("endMs") or 0),
                "text": dd.get("text") or "",
                "speaker": dd.get("speaker"),
                "segmentIds": dd.get("segmentIds") or [],
            }
        )
    return chunks


# ---------------------------------------------------------------------------
# Conversation persistence  (Phase 7.3: sub-collection form)
#
# Layout:
#   sessions/{sid}/conversations/{conversationId}/messages/{messageId}
#   accounts/{accountId}/conversations/{conversationId}/messages/{messageId}
#
# Each message is its own Firestore document, so concurrent turns from
# multiple tabs / devices never clobber each other. The parent conversation
# doc only holds metadata (scope / createdAt / updatedAt / lastMessageAt /
# messageCount), never the message array itself.
#
# Reads for prompt context always go through `order_by(createdAt DESC) +
# limit(MAX_HISTORY)`, so old messages are ignored automatically.
# ---------------------------------------------------------------------------


MAX_HISTORY_TURNS = 12


def _conversation_ref(ctx: ChatContext, conversation_id: str):
    sid = _session_id(ctx)
    if sid:
        return (
            db.collection("sessions")
            .document(sid)
            .collection("conversations")
            .document(conversation_id)
        )
    return (
        db.collection("accounts")
        .document(ctx.user.account_id)
        .collection("conversations")
        .document(conversation_id)
    )


def _messages_ref(ctx: ChatContext, conversation_id: str):
    return _conversation_ref(ctx, conversation_id).collection("messages")


def _load_conversation(ctx: ChatContext) -> tuple[str, List[Dict[str, str]]]:
    """Return (conversation_id, history).

    Loads up to MAX_HISTORY_TURNS most-recent messages from the sub-collection
    in chronological order. If the client supplied explicit history and no
    prior messages exist, use that as the initial context (useful for fresh
    conversations that were kept client-side only).
    """
    conversation_id = ctx.conversation_id
    history: List[Dict[str, str]] = []
    if conversation_id:
        try:
            docs = list(
                _messages_ref(ctx, conversation_id)
                .order_by("createdAt", direction=firestore.Query.DESCENDING)
                .limit(MAX_HISTORY_TURNS)
                .stream()
            )
            # Reverse to chronological order before feeding to the LLM
            for doc in reversed(docs):
                m = doc.to_dict() or {}
                role = m.get("role")
                text = m.get("text")
                if role in ("user", "assistant") and isinstance(text, str) and text:
                    history.append({"role": role, "text": text})
        except Exception as e:
            logger.warning(f"[session_chat] conversation messages load failed: {e}")
    else:
        conversation_id = f"conv_{uuid.uuid4().hex[:16]}"
    # history from explicit request overrides on first turn only
    if ctx.history and not history:
        for h in ctx.history[-MAX_HISTORY_TURNS:]:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                text = str(h.get("text") or "")
                if text:
                    history.append({"role": h["role"], "text": text})
    return conversation_id, history


def _save_turn(
    ctx: ChatContext,
    conversation_id: str,
    user_message: str,
    assistant_answer: str,
    citations: List[Dict[str, Any]],
    mode: str,
    used_model: Optional[str],
) -> None:
    """Persist one user+assistant turn as two independent message docs.

    Uses a Firestore batched write so both messages land atomically. The
    parent conversation doc is set with merge=True to update the metadata
    without touching any other fields.
    """
    try:
        conv_ref = _conversation_ref(ctx, conversation_id)
        messages_ref = conv_ref.collection("messages")
        now_ms = int(time.time() * 1000)

        batch = db.batch()

        # Parent conversation metadata (never stores message bodies)
        batch.set(
            conv_ref,
            {
                "conversationId": conversation_id,
                "scope": ctx.scope,
                "ownerAccountId": ctx.user.account_id,
                "createdAt": firestore.SERVER_TIMESTAMP,  # no-op on merge if already set
                "updatedAt": firestore.SERVER_TIMESTAMP,
                "lastMessageAt": firestore.SERVER_TIMESTAMP,
                "messageCount": firestore.Increment(2),
                "schemaVersion": 2,   # sub-collection form
            },
            merge=True,
        )

        # User message
        user_doc = messages_ref.document()
        batch.set(
            user_doc,
            {
                "messageId": user_doc.id,
                "conversationId": conversation_id,
                "role": "user",
                "text": user_message,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "clientSortKey": now_ms,           # tie-breaker before serverTs resolves
                "authorUid": ctx.user.uid,
                "authorAccountId": ctx.user.account_id,
            },
        )

        # Assistant message (delay sort key by 1ms so chronological order is stable
        # even if the server timestamps land identically)
        asst_doc = messages_ref.document()
        batch.set(
            asst_doc,
            {
                "messageId": asst_doc.id,
                "conversationId": conversation_id,
                "role": "assistant",
                "text": assistant_answer,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "clientSortKey": now_ms + 1,
                "citations": citations,
                "mode": mode,
                "usedModel": used_model,
            },
        )

        batch.commit()
    except Exception as e:
        logger.warning(f"[session_chat] conversation save failed: {e}")


def fetch_conversation_messages(
    ctx: ChatContext,
    conversation_id: str,
    limit: int = 50,
    before_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Paginated message fetch for conversation history UI.

    Returns messages in reverse-chronological order. Use `before_ms` (the
    clientSortKey of the oldest message in the previous page) to paginate
    backwards.
    """
    try:
        q = _messages_ref(ctx, conversation_id).order_by(
            "clientSortKey", direction=firestore.Query.DESCENDING
        )
        if before_ms is not None:
            q = q.start_after({"clientSortKey": before_ms})
        docs = list(q.limit(max(1, min(limit, 200))).stream())
    except Exception as e:
        logger.warning(f"[session_chat] conversation fetch failed: {e}")
        return []
    out: List[Dict[str, Any]] = []
    for doc in docs:
        d = doc.to_dict() or {}
        created = d.get("createdAt")
        out.append(
            {
                "messageId": doc.id,
                "role": d.get("role"),
                "text": d.get("text"),
                "citations": d.get("citations") or [],
                "mode": d.get("mode"),
                "usedModel": d.get("usedModel"),
                "clientSortKey": d.get("clientSortKey"),
                "createdAt": created.isoformat() if created and hasattr(created, "isoformat") else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Citation building  (answer-text × transcript chunks)
# ---------------------------------------------------------------------------


def _build_citations(
    answer: str,
    transcript_chunks: List[Dict[str, Any]],
    limit: int = 5,
    min_score: float = 0.15,
) -> List[Dict[str, Any]]:
    """Map the assistant's answer back to transcript chunks for UI jump-to."""
    if not answer or not transcript_chunks:
        return []
    segments = normalize_segments(
        [
            {
                "segmentId": f"ch_{c['index']}",
                "startMs": c["startMs"],
                "endMs": c["endMs"],
                "text": c["text"],
                "speaker": c.get("speaker"),
            }
            for c in transcript_chunks
            if c.get("text")
        ]
    )
    # find_best_segments returns List[Tuple[segment_dict, score]].
    pairs = find_best_segments(answer, segments, top_k=limit)
    citations: List[Dict[str, Any]] = []
    for seg, score in pairs:
        if score < min_score:
            continue
        citations.append(
            {
                "type": "transcript",
                "segmentId": seg.get("segmentId"),
                "startMs": seg.get("startMs"),
                "endMs": seg.get("endMs"),
                "speaker": seg.get("speaker"),
                "quotePreview": (seg.get("text") or "")[:160],
                "score": round(float(score), 3),
            }
        )
    return citations


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def chat_once(ctx: ChatContext) -> Dict[str, Any]:
    scope_type = _scope_type(ctx)

    session_data: Dict[str, Any] = {}
    transcript_chunks: List[Dict[str, Any]] = []
    if scope_type == "session":
        session_data = _load_session(ctx)
        transcript_chunks = await asyncio.to_thread(
            _load_transcript_chunks, session_data["id"]
        )

    effective_message = _effective_message(ctx)

    # 1. Intent Router  (matches existing chat_router.classify_route signature)
    session_title = session_data.get("title") if scope_type == "session" else None
    route = await chat_router.classify_route(
        message=effective_message,
        session_titles=[session_title] if session_title else [],
        active_session_title=session_title,
        state={},  # MVP: no persistent routing state; conversation_state is layered separately
        freshness_hint=False,
        ui_scope="session_detail" if scope_type == "session" else "global_ai",
    )
    logger.info(
        f"[v1/chat] scope={scope_type} route={route.mode} needs_web={route.needs_web} "
        f"session={session_data.get('id')}"
    )

    # Override: explicit session scope forces session-first
    if scope_type == "session" and not route.needs_session:
        route.needs_session = True

    # 2-4. Context + retrieval + preset
    session_context = (
        context_builder.build_session_context(session_data, effective_message)
        if session_data
        else None
    )

    # 5. Credits  (chat cost depends on mode)
    mode_label = "session_grounded"
    if scope_type == "general" and route.needs_web:
        mode_label = "general_fresh"
    elif scope_type == "general":
        mode_label = "general_static"
    elif route.needs_web:
        mode_label = "session_plus_general"

    credit_cost = estimate_cost(mode_label)
    ok, info = ai_credits.consume(ctx.user.account_id, credit_cost, mode_label)
    if not ok:
        raise CreditLimitError({**(info or {}), "cost": credit_cost, "mode": mode_label})
    credits_remaining = (info or {}).get("remaining")

    # 6. LLM call
    conversation_id, history = _load_conversation(ctx)

    used_model: Optional[str] = None
    answer = ""
    t_start = time.monotonic()

    def _extract_answer(resp: Any) -> str:
        if isinstance(resp, dict):
            for key in ("answer", "text", "response", "result"):
                v = resp.get(key)
                if isinstance(v, str) and v:
                    return v
            return json.dumps(resp, ensure_ascii=False)
        return str(resp or "")

    try:
        if scope_type == "session" and not route.needs_web:
            turn_prompt = context_builder.build_turn_prompt(
                message=effective_message,
                mode=mode_label,
                contexts=[session_context] if session_context else [],
                history=history,
                conversation_summary=None,
            )
            resp = await asyncio.to_thread(call_gemini_chat, turn_prompt)
            answer = _extract_answer(resp)
            used_model = CHAT_MODEL_NAME
        elif scope_type == "session" and route.needs_web:
            session_summary_text = ""
            if session_context:
                parts = []
                if session_context.get("title"):
                    parts.append(f"タイトル: {session_context['title']}")
                if session_context.get("summary"):
                    parts.append(f"要約:\n{session_context['summary']}")
                if session_context.get("transcript_excerpt"):
                    parts.append(f"抜粋:\n{session_context['transcript_excerpt']}")
                session_summary_text = "\n\n".join(parts)
            hybrid_prompt = context_builder.build_hybrid_prompt(
                message=effective_message,
                session_summary=session_summary_text,
                history=history,
                conversation_summary=None,
            )
            resp = await asyncio.to_thread(call_gemini_search_hybrid, hybrid_prompt)
            answer = _extract_answer(resp)
            used_model = GENERAL_MODEL_NAME
        elif scope_type == "general" and route.needs_web:
            resp = await asyncio.to_thread(
                call_gemini_general_with_search, effective_message, history, None
            )
            answer = _extract_answer(resp)
            used_model = GENERAL_MODEL_NAME
        else:  # general static
            # call_gemini_general_chat takes a finished turn_prompt string.
            turn_prompt = context_builder.build_turn_prompt(
                message=effective_message,
                mode=mode_label,
                contexts=[],
                history=history,
                conversation_summary=None,
            )
            resp = await asyncio.to_thread(call_gemini_general_chat, turn_prompt)
            answer = _extract_answer(resp)
            used_model = GENERAL_MODEL_NAME

    except Exception as e:
        logger.exception(f"[v1/chat] LLM call failed: {e}")
        # refund the reserved credits
        try:
            ai_credits.refund(ctx.user.account_id, credit_cost, mode_label)
        except Exception:
            pass
        raise ChatError(f"AI 応答の生成に失敗しました: {e}") from e

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    # 7. Citation Builder
    citations = (
        _build_citations(answer, transcript_chunks) if scope_type == "session" else []
    )

    # 8. Persist conversation turn
    await asyncio.to_thread(
        _save_turn,
        ctx,
        conversation_id,
        ctx.message,
        answer,
        citations,
        mode_label,
        used_model,
    )

    return {
        "conversationId": conversation_id,
        "scope": ctx.scope,
        "preset": ctx.preset,
        "mode": mode_label,
        "usedModel": used_model,
        "answer": {"text": answer},
        "citations": citations,
        "creditCost": credit_cost,
        "creditsRemaining": credits_remaining,
        "latencyMs": elapsed_ms,
        "suggestedActions": _suggest_follow_up_actions(ctx.preset, scope_type),
    }


def _suggest_follow_up_actions(preset: Optional[str], scope_type: str) -> List[Dict[str, str]]:
    """Return 2-3 recommended next presets for the UI."""
    if scope_type != "session":
        return []
    rotation = [
        "summarize",
        "extract_todos",
        "extract_decisions",
        "next_agenda",
        "short_share",
        "quiz_questions",
    ]
    if preset and preset in rotation:
        rotation.remove(preset)
    return [{"id": pid, "label": _PRESETS[pid]["label"]} for pid in rotation[:3]]


# ---------------------------------------------------------------------------
# Preparation helper — shared by chat_once and chat_stream
# ---------------------------------------------------------------------------


@dataclass
class ChatPrep:
    scope_type: str
    session_data: Dict[str, Any]
    transcript_chunks: List[Dict[str, Any]]
    effective_message: str
    route_needs_web: bool
    mode_label: str
    credit_cost: int
    credits_remaining: Optional[int]
    conversation_id: str
    history: List[Dict[str, str]]
    session_context: Optional[Dict[str, Any]]


async def _prepare_chat(ctx: ChatContext) -> ChatPrep:
    """Shared preparation for non-stream and stream variants.

    Side effects: consumes AI credits. Caller is responsible for refunding
    them on LLM failure (the `credit_cost` + `mode_label` are returned here
    precisely so the caller can call ai_credits.refund in its except path).
    """
    scope_type = _scope_type(ctx)

    session_data: Dict[str, Any] = {}
    transcript_chunks: List[Dict[str, Any]] = []
    if scope_type == "session":
        session_data = _load_session(ctx)
        transcript_chunks = await asyncio.to_thread(
            _load_transcript_chunks, session_data["id"]
        )

    effective_message = _effective_message(ctx)

    session_title = session_data.get("title") if scope_type == "session" else None
    route = await chat_router.classify_route(
        message=effective_message,
        session_titles=[session_title] if session_title else [],
        active_session_title=session_title,
        state={},
        freshness_hint=False,
        ui_scope="session_detail" if scope_type == "session" else "global_ai",
    )
    if scope_type == "session" and not route.needs_session:
        route.needs_session = True

    session_context = (
        context_builder.build_session_context(session_data, effective_message)
        if session_data
        else None
    )

    mode_label = "session_grounded"
    if scope_type == "general" and route.needs_web:
        mode_label = "general_fresh"
    elif scope_type == "general":
        mode_label = "general_static"
    elif route.needs_web:
        mode_label = "session_plus_general"

    credit_cost = estimate_cost(mode_label)
    ok, info = ai_credits.consume(ctx.user.account_id, credit_cost, mode_label)
    if not ok:
        raise CreditLimitError({**(info or {}), "cost": credit_cost, "mode": mode_label})
    credits_remaining = (info or {}).get("remaining")

    conversation_id, history = _load_conversation(ctx)

    return ChatPrep(
        scope_type=scope_type,
        session_data=session_data,
        transcript_chunks=transcript_chunks,
        effective_message=effective_message,
        route_needs_web=route.needs_web,
        mode_label=mode_label,
        credit_cost=credit_cost,
        credits_remaining=credits_remaining,
        conversation_id=conversation_id,
        history=history,
        session_context=session_context,
    )


def _build_hybrid_session_summary(session_context: Optional[Dict[str, Any]]) -> str:
    if not session_context:
        return ""
    parts = []
    if session_context.get("title"):
        parts.append(f"タイトル: {session_context['title']}")
    if session_context.get("summary"):
        parts.append(f"要約:\n{session_context['summary']}")
    if session_context.get("transcript_excerpt"):
        parts.append(f"抜粋:\n{session_context['transcript_excerpt']}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Streaming entry point
# ---------------------------------------------------------------------------


async def chat_stream(ctx: ChatContext):
    """Async-generator version of chat_once; yields SSE-ready events.

    Events emitted in order:
      1. meta          — mode / conversationId / credits / usedModel
      2. token*        — incremental text deltas from Gemini
      3. done          — fullText + citations + suggestedActions + latencyMs

    On LLM failure: yields `error` event and refunds the consumed credits.
    Client is expected to implement reconnection / backoff independently.
    """
    prep = await _prepare_chat(ctx)
    used_model = (
        CHAT_MODEL_NAME
        if prep.scope_type == "session" and not prep.route_needs_web
        else GENERAL_MODEL_NAME
    )
    t_start = time.monotonic()

    yield {
        "event": "meta",
        "data": {
            "conversationId": prep.conversation_id,
            "scope": ctx.scope,
            "preset": ctx.preset,
            "mode": prep.mode_label,
            "usedModel": used_model,
            "creditCost": prep.credit_cost,
            "creditsRemaining": prep.credits_remaining,
        },
    }

    full_text_parts: List[str] = []

    def _run_sync_stream():
        """Pick the right gemini_stream variant based on the routing decision."""
        if prep.scope_type == "session" and not prep.route_needs_web:
            turn_prompt = context_builder.build_turn_prompt(
                message=prep.effective_message,
                mode=prep.mode_label,
                contexts=[prep.session_context] if prep.session_context else [],
                history=prep.history,
                conversation_summary=None,
            )
            return stream_gemini_chat(turn_prompt, model_name=CHAT_MODEL_NAME)
        if prep.scope_type == "session" and prep.route_needs_web:
            hybrid = context_builder.build_hybrid_prompt(
                message=prep.effective_message,
                session_summary=_build_hybrid_session_summary(prep.session_context),
                history=prep.history,
                conversation_summary=None,
            )
            return stream_gemini_search_hybrid(hybrid)
        if prep.scope_type == "general" and prep.route_needs_web:
            return stream_gemini_with_search(prep.effective_message)
        # general static
        turn_prompt = context_builder.build_turn_prompt(
            message=prep.effective_message,
            mode=prep.mode_label,
            contexts=[],
            history=prep.history,
            conversation_summary=None,
        )
        return stream_gemini_chat(turn_prompt, model_name=GENERAL_MODEL_NAME)

    try:
        iterator = await asyncio.to_thread(_run_sync_stream)
        # Drain the blocking iterator chunk-by-chunk off the event loop
        while True:
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            if not chunk:
                continue
            full_text_parts.append(chunk)
            yield {"event": "token", "data": {"text": chunk}}

    except Exception as e:
        logger.exception(f"[v1/chat:stream] LLM stream failed: {e}")
        try:
            ai_credits.refund(ctx.user.account_id, prep.credit_cost, prep.mode_label)
        except Exception:
            pass
        yield {
            "event": "error",
            "data": {
                "code": "CHAT_ERROR",
                "message": "AI 応答の生成に失敗しました",
            },
        }
        return

    full_text = "".join(full_text_parts)
    citations = (
        _build_citations(full_text, prep.transcript_chunks)
        if prep.scope_type == "session"
        else []
    )
    await asyncio.to_thread(
        _save_turn,
        ctx,
        prep.conversation_id,
        ctx.message,
        full_text,
        citations,
        prep.mode_label,
        used_model,
    )

    yield {
        "event": "done",
        "data": {
            "conversationId": prep.conversation_id,
            "answer": {"text": full_text},
            "citations": citations,
            "creditCost": prep.credit_cost,
            "creditsRemaining": prep.credits_remaining,
            "latencyMs": int((time.monotonic() - t_start) * 1000),
            "suggestedActions": _suggest_follow_up_actions(ctx.preset, prep.scope_type),
        },
    }
