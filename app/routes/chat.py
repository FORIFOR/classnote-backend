"""AI Chat API — session-first conversational AI with TODO awareness.

Every turn: session first → general fallback → next turn session first again.
Conversation state persists active session across turns (client-side).
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.dependencies import get_current_user, CurrentUser
from app.firebase import db
from app.services.scope_resolver import (
    resolve_referent,
    resolve_todo_aware,
    can_answer_from_session,
    is_todo_intent,
    has_session_intent,
    extract_topic,
    needs_fresh_grounding,
)
from app.services.chat_router import classify_route, judge_sufficiency, route_to_legacy_mode, get_display_scope, RouteDecision
from app.services.context_builder import build_session_context, build_turn_prompt, build_stream_prompt, build_todo_context, build_hybrid_prompt
from app.services.gemini_chat import (
    call_gemini_chat,
    call_gemini_general_chat,
    call_gemini_general_with_search,
    call_gemini_search_hybrid,
    CHAT_MODEL_NAME,
    GENERAL_MODEL_NAME,
)
from app.services.gemini_stream import stream_gemini_chat, stream_gemini_with_search, stream_gemini_search_hybrid
from app.services.ai_credits import ai_credits, estimate_cost
from app.services.ops_logger import log_ai_chat

logger = logging.getLogger("app.routes.chat")
router = APIRouter(prefix="/v1/chat", tags=["AI Chat"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class HistoryItem(BaseModel):
    role: str  # "user" | "assistant"
    text: str


class ChatRequest(BaseModel):
    chat_id: Optional[str] = None
    message: str
    mode: Optional[str] = None  # legacy — ignored
    current_session_id: Optional[str] = None
    recent_session_ids: Optional[List[str]] = None
    ui_scope: str = "global_ai"
    history: List[HistoryItem] = []
    conversation_summary: Optional[str] = None
    conversation_state: Optional[Dict[str, Any]] = None


class UsedSession(BaseModel):
    session_id: str
    title: str


class Citation(BaseModel):
    start_sec: int
    end_sec: int
    speaker: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    mode: Literal["session_grounded", "session_plus_general", "general_static", "general_fresh"]
    used_sessions: List[UsedSession]
    citations: List[Citation]
    confidence: float
    needs_general_knowledge: bool
    follow_up_suggestion: Optional[str] = None
    conversation_summary_next: Optional[str] = None
    used_search: bool = False
    used_model: Optional[str] = None
    display_scope: Optional[str] = None
    conversation_state: Optional[Dict[str, Any]] = None
    # AI Credit info
    credit_cost: int = 0
    credits_remaining: Optional[int] = None


class SuggestionResponse(BaseModel):
    chips: List[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_session_context(session_id: str, message: str, user, *, skip_access_check: bool = False) -> Optional[dict]:
    """Load a single session's context with access check."""
    try:
        doc = db.collection("sessions").document(session_id).get()
        if not doc.exists:
            logger.warning(f"[Chat] Session not found: {session_id}")
            return None

        data = doc.to_dict()
        data["id"] = doc.id

        if not skip_access_check:
            owner_uid = data.get("ownerUid") or data.get("userId")
            owner_account = data.get("ownerAccountId")
            shared_accounts = data.get("sharedWithAccountIds") or []
            shared_uids = data.get("sharedUserIds") or data.get("sharedWithUserIds") or []

            user_account = getattr(user, "account_id", None)
            has_access = (
                owner_uid == user.uid
                or (owner_account and user_account and owner_account == user_account)
                or (user_account and user_account in shared_accounts)
                or user.uid in shared_uids
            )
            if not has_access:
                logger.warning(
                    f"[Chat] Access denied: uid={user.uid} account={user_account} "
                    f"session={session_id} owner_uid={owner_uid} owner_account={owner_account}"
                )
                return None

        ctx = build_session_context(data, message)
        logger.info(
            f"[Chat] Session loaded: {session_id} title=\"{ctx.get('title', '')[:50]}\" "
            f"summary={len(ctx.get('summary', ''))} transcript={len(ctx.get('transcript_excerpt', ''))}"
        )
        return ctx
    except Exception as e:
        logger.error(f"[Chat] Failed to load session {session_id}: {e}")
        return None


def _load_session_contexts(session_ids: List[str], user, message: str, *, skip_access_check: bool = False) -> list:
    """Load multiple sessions with access control."""
    contexts = []
    for sid in session_ids:
        ctx = _load_session_context(sid, message, user, skip_access_check=skip_access_check)
        if ctx:
            contexts.append(ctx)
    logger.info(f"[Chat] Loaded {len(contexts)}/{len(session_ids)} session contexts (skip_acl={skip_access_check})")
    return contexts


def _fetch_user_todos(user, limit: int = 30) -> list:
    """Fetch user's open TODOs from Firestore."""
    try:
        account_id = getattr(user, "account_id", None) or user.uid
        todo_q = (
            db.collection("todos")
            .where("accountId", "==", account_id)
            .where("status", "in", ["open", "overdue"])
            .limit(limit)
        )
        todo_docs = list(todo_q.stream())
        todos = []
        for td in todo_docs:
            todo_data = td.to_dict()
            todo_data["id"] = td.id
            todos.append(todo_data)

        priority_order = {"high": 0, "mid": 1, "low": 2}
        todos.sort(key=lambda t: (
            priority_order.get(t.get("priority", "mid"), 1),
            t.get("dueDate") or "9999-99-99",
        ))
        logger.info(f"[Chat] Fetched {len(todos)} TODOs for {account_id}")
        return todos
    except Exception as e:
        logger.warning(f"[Chat] TODO fetch failed: {e}", exc_info=True)
        return []


def _clean_session_title(title: str) -> str:
    """Strip date/time prefix like '3/14 02:34_' from session titles."""
    import re as _re
    # Matches patterns like "3/14 02:34_", "3/16 18:14_- "
    cleaned = _re.sub(r"^\d{1,2}/\d{1,2}\s+\d{2}:\d{2}_[-\s]*", "", title)
    return cleaned.strip()


def _match_sessions_to_text(
    sessions: list, user_query: str, answer_text: str
) -> list:
    """Match sessions to user query and answer text.

    Strategy:
    1. Check if user query contains distinctive keywords from session titles
       (user explicitly asked about a specific session) → return those.
    2. If no query match, return [] and let caller fall back to all sessions.
       Answer-text matching is too unreliable for Japanese (common words cause
       false positives).
    """
    import re as _re

    query_lower = user_query.lower()
    query_matched = []

    for s in sessions:
        title = s.get("title", "")
        if not title or len(title) < 3:
            continue

        cleaned_title = _clean_session_title(title)
        # Split on Japanese/general punctuation for chunks
        chunks = _re.split(r"[、。：:／/\s　\-–—]+", cleaned_title)
        # Only keep distinctive chunks (4+ chars to avoid common word matches)
        chunks = [c for c in chunks if len(c) >= 4]

        # Also use cleaned title as whole for short titles like "デジマースミーティング"
        if len(cleaned_title) >= 4:
            chunks.append(cleaned_title)

        if any(c.lower() in query_lower for c in chunks):
            query_matched.append(s)

    if query_matched:
        logger.info(
            f"[_match_sessions] Query matched {len(query_matched)} sessions: "
            f"{[s.get('title', '?')[:30] for s in query_matched]}"
        )
        return query_matched

    # No query match → return empty; caller will fall back to all context sessions
    return []


def _auto_resolve_sessions(user, message: str, limit: int = 20) -> list:
    """Fetch recent session IDs for cold-start queries."""
    try:
        _epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
        fetch_limit = max(limit * 2, 60)

        try:
            q = (
                db.collection("sessions")
                .where("ownerUid", "==", user.uid)
                .order_by("createdAt", direction="DESCENDING")
                .limit(fetch_limit)
            )
            docs = list(q.stream())
        except Exception:
            q = db.collection("sessions").where("ownerUid", "==", user.uid).limit(200)
            docs = list(q.stream())
            docs.sort(
                key=lambda d: d.to_dict().get("createdAt") or d.to_dict().get("startedAt") or _epoch,
                reverse=True,
            )
            docs = docs[:fetch_limit]

        seen_ids = {d.id for d in docs}
        if hasattr(user, "account_id") and user.account_id and user.account_id != user.uid:
            try:
                acct_q = (
                    db.collection("sessions")
                    .where("ownerAccountId", "==", user.account_id)
                    .order_by("createdAt", direction="DESCENDING")
                    .limit(30)
                )
                for d in acct_q.stream():
                    if d.id not in seen_ids:
                        docs.append(d)
            except Exception:
                acct_q = db.collection("sessions").where("ownerAccountId", "==", user.account_id).limit(30)
                for d in acct_q.stream():
                    if d.id not in seen_ids:
                        docs.append(d)

        docs.sort(
            key=lambda d: d.to_dict().get("createdAt") or d.to_dict().get("startedAt") or _epoch,
            reverse=True,
        )

        # Mode-aware filtering
        mode_hints = {
            "meeting": ["会議", "打ち合わせ", "ミーティング"],
            "lecture": ["講義", "授業", "レクチャー"],
        }
        preferred_mode = None
        for m, keywords in mode_hints.items():
            if any(k in message for k in keywords):
                preferred_mode = m
                break

        if preferred_mode:
            preferred = [d for d in docs if d.to_dict().get("mode") == preferred_mode]
            others = [d for d in docs if d.to_dict().get("mode") != preferred_mode]
            selected = preferred[:limit]
            remaining = limit - len(selected)
            if remaining > 0:
                selected += others[:remaining]
        else:
            selected = docs[:limit]

        session_ids = [d.id for d in selected]
        titles = [d.to_dict().get("title", "?")[:40] for d in selected]
        logger.info(
            f"[Chat] Auto-resolved {len(session_ids)} sessions "
            f"(total_docs={len(docs)} preferred_mode={preferred_mode}) "
            f"titles={titles}"
        )
        return session_ids
    except Exception as e:
        logger.warning(f"[Chat] Auto-resolve failed: {e}", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Main endpoint: session-first routing
# ---------------------------------------------------------------------------

@router.post("/send", response_model=ChatResponse)
async def send_chat(
    req: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Send a chat message — session-first every turn."""
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 chars)")

    t0 = time.monotonic()

    # ── 1. Restore conversation state ──
    state = dict(req.conversation_state or {})

    # If UI provides current_session_id, always override (user changed session)
    if req.current_session_id:
        state["active_session_id"] = req.current_session_id

    logger.info(
        f"[Chat/send] START uid={user.uid} scope={req.ui_scope} "
        f"msg_len={len(message)} state={state}"
    )
    logger.info(f"[Chat/send] Q: \"{message[:500]}\"")

    # ── 2. Resolve referent (その会議, このTODO, etc.) ──
    referent = resolve_referent(message, state)

    # ── 3. TODO handling ──
    todos_raw = []
    todo_context_str = None
    todo_ref = None
    want_todo = is_todo_intent(message)

    if want_todo or state.get("active_todo_id"):
        todos_raw = _fetch_user_todos(user)
        if want_todo:
            todo_context_str = build_todo_context(todos_raw)

    # Try TODO-aware resolution (match message to a specific TODO's source session)
    if todos_raw:
        todo_ref = resolve_todo_aware(message, state, todos_raw)

    # ── 4. Determine active_session_id (priority: TODO > referent > state) ──
    active_session_id = None

    if todo_ref and todo_ref.get("session_id"):
        active_session_id = todo_ref["session_id"]
        state["active_todo_id"] = todo_ref.get("todo_id")
        state["active_session_id"] = active_session_id
        state["active_session_title"] = todo_ref.get("session_title")
        logger.info(f"[Chat/send] Active session from TODO: {active_session_id}")
    elif referent and referent.get("entity_type") == "session":
        active_session_id = referent["entity_id"]
        logger.info(f"[Chat/send] Active session from referent: {active_session_id}")
    else:
        active_session_id = state.get("active_session_id")
        if active_session_id:
            logger.info(f"[Chat/send] Active session from state: {active_session_id}")

    # ── 5. Load active session context ──
    session_context = None
    if active_session_id:
        session_context = _load_session_context(active_session_id, message, user)

    t_ctx = time.monotonic()
    logger.info(
        f"[Chat/send] Context phase: {(t_ctx - t0)*1000:.0f}ms "
        f"session_loaded={session_context is not None}"
    )

    # ── 5.5. Pre-check AI credits (min cost = 1) ──
    account_id = user.account_id
    pre_allowed, pre_info = ai_credits.can_consume(account_id, 1)
    if not pre_allowed:
        reason = pre_info.get("reason", "credit_limit")
        logger.warning(f"[Chat/send] Credit pre-check blocked: {reason} account={account_id}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": reason,
                "credits_remaining": pre_info.get("remaining", 0),
                "daily_used": pre_info.get("dailyUsed", 0),
                "daily_soft_cap": pre_info.get("dailySoftCap", 20),
            },
        )

    # ── 6. 2-stage intent routing ──
    contexts = [session_context] if session_context else []
    mode = "general_static"
    model_label = GENERAL_MODEL_NAME

    # Gather session titles for classifier context
    all_session_titles = []
    if session_context:
        all_session_titles.append(session_context.get("title", ""))

    # Stage 1: Classify intent
    route = classify_route(
        message=message,
        session_titles=all_session_titles if all_session_titles else None,
        has_active_session=session_context is not None,
        conversation_context=state.get("active_session_title", ""),
    )
    logger.info(f"[Chat/send] Route: {route}")

    # UI explicitly selected session → force session_only
    if req.ui_scope == "session_detail" and req.current_session_id and session_context:
        route.mode = "session_only"
        route.needs_session = True
        route.needs_web = False
        logger.info("[Chat/send] UI session_detail → forced session_only")
    elif referent and referent.get("entity_type") == "session" and session_context:
        route.mode = "session_only"
        route.needs_session = True
        logger.info("[Chat/send] Referent → forced session_only")

    # ── 7. Route — 2-stage: session retrieval + sufficiency check ──
    t_gemini_start = time.monotonic()
    result = None
    sufficiency = None
    display_scope = get_display_scope(route)

    try:
        # ─── A. web_only: go straight to search grounding ───
        if route.mode == "web_only":
            mode = "general_fresh"
            model_label = GENERAL_MODEL_NAME
            display_scope = get_display_scope(route)
            logger.info("[Chat/send] → web_only (search grounding)")
            result = call_gemini_general_with_search(
                message,
                history=[h.model_dump() for h in req.history],
                conversation_summary=req.conversation_summary,
            )
            state["last_answer_mode"] = "general_fresh"

        # ─── B. session_only / session_then_web: need session context ───
        elif route.needs_session:
            # Load session if not yet loaded
            contexts = []
            if session_context:
                contexts = [session_context]
            elif has_session_intent(message) or req.recent_session_ids:
                if req.recent_session_ids:
                    session_ids = req.recent_session_ids[:20]
                else:
                    session_ids = _auto_resolve_sessions(user, message)
                contexts = _load_session_contexts(session_ids, user, message, skip_access_check=True)

            # Stage 2: Judge sufficiency
            if contexts:
                sufficiency = judge_sufficiency(message, contexts[0])
                display_scope = get_display_scope(route, sufficiency)
                logger.info(f"[Chat/send] Sufficiency: answerable={sufficiency.answerable} conf={sufficiency.confidence:.2f} web_needed={sufficiency.needs_web_verification}")

                # Decide: session-only or session+web
                if route.mode == "session_only" and sufficiency.answerable:
                    # Pure session answer
                    mode = "session_grounded"
                    model_label = CHAT_MODEL_NAME
                    turn_prompt = build_turn_prompt(
                        message=message, mode=mode, contexts=contexts,
                        history=[h.model_dump() for h in req.history],
                        conversation_summary=req.conversation_summary,
                        todo_context=todo_context_str,
                    )
                    logger.info(f"[Chat/send] → session_only (sufficient)")
                    result = call_gemini_chat(turn_prompt)

                elif route.mode == "session_then_web" or (route.mode == "session_only" and not sufficiency.answerable):
                    # Session + web supplementation
                    if sufficiency.answerable and not sufficiency.needs_web_verification:
                        # Session is enough after all
                        mode = "session_grounded"
                        model_label = CHAT_MODEL_NAME
                        turn_prompt = build_turn_prompt(
                            message=message, mode=mode, contexts=contexts,
                            history=[h.model_dump() for h in req.history],
                            conversation_summary=req.conversation_summary,
                            todo_context=todo_context_str,
                        )
                        logger.info(f"[Chat/send] → session_then_web → sufficient, session_grounded")
                        result = call_gemini_chat(turn_prompt)
                    else:
                        # Need web supplementation
                        mode = "general_fresh"
                        model_label = GENERAL_MODEL_NAME
                        display_scope = "この会議 + 最新Web情報"
                        # Build prompt with session context + web search
                        session_summary = "\n".join([
                            f"【{c.get('title', '')}】{c.get('summary', '')[:500]}"
                            for c in contexts
                        ])
                        augmented_message = (
                            f"以下は関連する会議の内容です:\n{session_summary}\n\n"
                            f"この情報を踏まえて、以下の質問に答えてください。"
                            f"会議内容で答えられる部分はそれを使い、最新情報が必要な部分はWeb検索で補強してください。\n\n"
                            f"質問: {message}"
                        )
                        logger.info(f"[Chat/send] → session_then_web → web supplementation")
                        result = call_gemini_general_with_search(
                            augmented_message,
                            history=[h.model_dump() for h in req.history],
                            conversation_summary=req.conversation_summary,
                        )

                # Update state
                if contexts:
                    if len(contexts) == 1:
                        state["active_session_id"] = contexts[0].get("session_id") or active_session_id
                        state["active_session_title"] = contexts[0].get("title")
                    else:
                        state.pop("active_session_id", None)
                        state.pop("active_session_title", None)
                    state["last_referenced_entity_type"] = "session"
                state["last_answer_mode"] = mode if result else "general_static"
            else:
                logger.info("[Chat/send] No session contexts found, falling through to general")

        # ─── C. general_static ───
        if result is None and route.mode == "general_static":
            mode = "general_static"
            model_label = GENERAL_MODEL_NAME
            display_scope = "一般知識"
            logger.info("[Chat/send] → general_static")
            turn_prompt = build_turn_prompt(
                message=message, mode=mode, contexts=[],
                history=[h.model_dump() for h in req.history],
                conversation_summary=req.conversation_summary,
                todo_context=todo_context_str,
            )
            result = call_gemini_general_chat(turn_prompt)
            state["last_answer_mode"] = "general_static"

        # ─── D. Final fallback ───
        if result is None:
            mode = "general_static"
            model_label = GENERAL_MODEL_NAME
            display_scope = "一般知識"
            logger.info("[Chat/send] → fallback general_static")
            turn_prompt = build_turn_prompt(
                message=message, mode=mode, contexts=[],
                history=[h.model_dump() for h in req.history],
                conversation_summary=req.conversation_summary,
                todo_context=todo_context_str,
            )
            result = call_gemini_general_chat(turn_prompt)
            state["last_answer_mode"] = "general_static"

    except Exception as e:
        t_fail = time.monotonic()
        fail_ms = int((t_fail - t_gemini_start) * 1000)
        logger.error(f"[Chat/send] Gemini failed after {fail_ms}ms: {e}")
        try:
            log_ai_chat(
                uid=user.uid,
                mode=mode,
                model=model_label,
                credit_cost=0,
                credits_remaining=0,
                latency_ms=fail_ms,
                session_ids=[active_session_id] if active_session_id else None,
                endpoint="chat/send",
                error_message=str(e)[:200],
            )
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="AI service temporarily unavailable")

    t_end = time.monotonic()

    # ── 7.5. Consume AI credits ──
    credit_cost = estimate_cost(mode)
    credits_remaining = None

    allowed, credit_info = ai_credits.can_consume(account_id, credit_cost)
    if not allowed:
        reason = credit_info.get("reason", "credit_limit")
        remaining = credit_info.get("remaining", 0)
        logger.warning(f"[Chat/send] Credit blocked: {reason} account={account_id} cost={credit_cost} remaining={remaining}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": reason,
                "credit_cost": credit_cost,
                "credits_remaining": remaining,
                "daily_used": credit_info.get("dailyUsed", 0),
                "daily_soft_cap": credit_info.get("dailySoftCap", 20),
            },
        )

    consume_ok, consume_info = ai_credits.consume(account_id, credit_cost, mode)
    if consume_ok:
        credits_remaining = consume_info.get("remaining", 0)
        logger.info(f"[Chat/send] Credits consumed: cost={credit_cost} remaining={credits_remaining} mode={mode}")
    else:
        # Race condition — passed pre-check but failed consume
        logger.warning(f"[Chat/send] Credit consume race: {consume_info}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": consume_info.get("reason", "credit_limit"),
                "credit_cost": credit_cost,
                "credits_remaining": consume_info.get("remaining", 0),
            },
        )

    # ── 8. Update topic in state ──
    state["last_topic"] = extract_topic(message)
    if want_todo and todo_context_str:
        state["last_referenced_entity_type"] = "todo"

    # ── 9. Build display_scope ──
    # Filter used_sessions: only include sessions that were actually in context
    raw_used_sessions = result.get("used_sessions", [])
    if mode in ("general_static", "general_fresh"):
        # General mode — no session was referenced
        used_sessions_list = []
    elif contexts:
        # Only keep sessions that were actually provided as context
        context_ids = {c.get("session_id") for c in contexts}
        used_sessions_list = [s for s in raw_used_sessions if s.get("session_id") in context_ids]
    else:
        used_sessions_list = []
    logger.info(f"[Chat/send] used_sessions: raw={len(raw_used_sessions)} filtered={len(used_sessions_list)}")

    # Override display_scope for special cases (TODO, etc.)
    if todo_context_str and used_sessions_list:
        display_scope = "TODOの出典セッションを参照して回答"
    elif todo_context_str:
        display_scope = "TODOリストを参照して回答"
    # Otherwise use the display_scope from the router (already set above)

    answer = result.get("answer", "")
    latency_ms = int((t_end - t0) * 1000)
    logger.info(
        f"[Chat/send] DONE uid={user.uid} model={model_label} mode={mode} "
        f"total={latency_ms}ms gemini={(t_end - t_gemini_start)*1000:.0f}ms "
        f"answer_len={len(answer)}"
    )
    logger.info(f"[Chat/send] A: \"{answer[:300]}\"")
    logger.info(f"[Chat/send] Updated state: {state}")

    # ── Structured ops log ──
    try:
        log_ai_chat(
            uid=user.uid,
            mode=mode,
            model=model_label,
            credit_cost=credit_cost,
            credits_remaining=credits_remaining or 0,
            latency_ms=latency_ms,
            session_ids=[active_session_id] if active_session_id else None,
            used_search=(mode == "general_fresh"),
            scope_score=None,  # not easily available here without refactor
            fallback_reason=(
                "no_session" if not session_context
                else "low_score" if not session_answerable
                else None
            ),
            input_tokens=result.get("_input_tokens"),
            output_tokens=result.get("_output_tokens"),
            answer_len=len(answer),
            endpoint="chat/send",
        )
    except Exception as e:
        logger.warning(f"[Chat/send] OpsLog failed: {e}")

    return ChatResponse(
        answer=answer,
        mode=mode,
        used_sessions=[UsedSession(**s) for s in used_sessions_list],
        citations=[Citation(**c) for c in result.get("citations", [])],
        confidence=result.get("confidence", 0.0),
        needs_general_knowledge=result.get("needs_general_knowledge", mode != "session_grounded"),
        follow_up_suggestion=result.get("follow_up_suggestion"),
        conversation_summary_next=result.get("conversation_summary_next"),
        used_search=result.get("used_search", mode == "general_fresh"),
        used_model=model_label,
        display_scope=display_scope,
        conversation_state=state,
        credit_cost=credit_cost,
        credits_remaining=credits_remaining,
    )


# ---------------------------------------------------------------------------
# AI Credits
# ---------------------------------------------------------------------------

class CreditReport(BaseModel):
    plan: str
    monthly_limit: int
    topup_credits: int
    used: int
    remaining: int
    daily_used: int
    daily_soft_cap: int


@router.get("/credits", response_model=CreditReport)
async def get_credits(
    user: CurrentUser = Depends(get_current_user),
):
    """Get AI credit status for the current user."""
    report = ai_credits.get_credit_report(user.account_id)
    return CreditReport(
        plan=report["plan"],
        monthly_limit=report["monthlyLimit"],
        topup_credits=report["topupCredits"],
        used=report["used"],
        remaining=report["remaining"],
        daily_used=report["dailyUsed"],
        daily_soft_cap=report["dailySoftCap"],
    )


# ---------------------------------------------------------------------------
# Streaming endpoint (SSE)
# ---------------------------------------------------------------------------

def _sse(event_type: str, data: dict) -> str:
    """Format a single SSE event line."""
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Stream a chat response using SSE — session-first every turn.

    Events:
      meta   — mode, model, used_sessions, display_scope, credit info
      token  — text fragment
      done   — full_text, conversation_state, follow_up_suggestion
      error  — message
    """
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 chars)")

    t0 = time.monotonic()
    account_id = user.account_id

    # ── 1. Restore conversation state ──
    state = dict(req.conversation_state or {})
    if req.current_session_id:
        state["active_session_id"] = req.current_session_id

    logger.info(f"[Chat/stream] START uid={user.uid} scope={req.ui_scope} msg_len={len(message)}")

    # ── 2. Pre-check AI credits ──
    pre_allowed, pre_info = ai_credits.can_consume(account_id, 1)
    if not pre_allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": pre_info.get("reason", "credit_limit"),
                "credits_remaining": pre_info.get("remaining", 0),
            },
        )

    # ── 3. Resolve referent / TODO / active session ──
    referent = resolve_referent(message, state)

    todos_raw = []
    todo_context_str = None
    todo_ref = None
    want_todo = is_todo_intent(message)

    if want_todo or state.get("active_todo_id"):
        todos_raw = _fetch_user_todos(user)
        if want_todo:
            todo_context_str = build_todo_context(todos_raw)

    if todos_raw:
        todo_ref = resolve_todo_aware(message, state, todos_raw)

    # ── 4. Determine active_session_id ──
    active_session_id = None

    if todo_ref and todo_ref.get("session_id"):
        active_session_id = todo_ref["session_id"]
        state["active_todo_id"] = todo_ref.get("todo_id")
        state["active_session_id"] = active_session_id
        state["active_session_title"] = todo_ref.get("session_title")
    elif referent and referent.get("entity_type") == "session":
        active_session_id = referent["entity_id"]
    else:
        active_session_id = state.get("active_session_id")

    # ── 5. Load session context ──
    session_context = None
    if active_session_id:
        session_context = _load_session_context(active_session_id, message, user)

    # ── 6. Session answerability ──
    session_answerable = False
    suggested_mode = "general_static"

    if session_context:
        if req.ui_scope == "session_detail" and req.current_session_id:
            session_answerable = True
            suggested_mode = "session_grounded"
        elif referent and referent.get("entity_type") == "session":
            session_answerable = True
            suggested_mode = "session_grounded"
        else:
            session_answerable, suggested_mode = can_answer_from_session(
                message, session_context, state
            )

    # ── 7. LLM-based route classification ──
    freshness_hint = needs_fresh_grounding(message)
    session_titles_hint = []
    if session_context:
        session_titles_hint.append(session_context.get("title", ""))

    route = await classify_route(
        message=message,
        session_titles=session_titles_hint,
        active_session_title=state.get("active_session_title"),
        state=state,
        freshness_hint=freshness_hint,
        ui_scope=req.ui_scope,
    )

    # UI session_detail override
    if req.ui_scope == "session_detail" and req.current_session_id and session_context:
        if not route.needs_web:
            route.mode = "session_only"
            route.needs_session = True

    mode = route_to_legacy_mode(route)
    model_label = ""
    contexts = []
    use_search = route.needs_web
    display_scope = get_display_scope(route)

    if route.mode == "web_only":
        model_label = GENERAL_MODEL_NAME
    elif route.needs_session:
        if session_context:
            contexts = [session_context]
        elif has_session_intent(message) or req.recent_session_ids:
            if req.recent_session_ids:
                session_ids = req.recent_session_ids[:50]
            else:
                session_ids = _auto_resolve_sessions(user, message)
            contexts = _load_session_contexts(session_ids, user, message, skip_access_check=True)

        if contexts:
            suff = judge_sufficiency(message, contexts[0])
            if route.mode == "session_only" and suff.answerable:
                mode = "session_grounded"
                model_label = CHAT_MODEL_NAME
                use_search = False
                display_scope = "この会議"
            elif route.mode == "session_then_web" or not suff.answerable:
                mode = "general_fresh"
                model_label = GENERAL_MODEL_NAME
                use_search = True
                display_scope = "この会議 + Web"
            else:
                mode = "session_plus_general"
                model_label = GENERAL_MODEL_NAME
                display_scope = "この会議"
        else:
            mode = "general_static"
            model_label = GENERAL_MODEL_NAME
            display_scope = "一般知識"
    else:
        mode = "general_static"
        model_label = GENERAL_MODEL_NAME
        display_scope = "一般知識"

    if not model_label:
        model_label = GENERAL_MODEL_NAME

    logger.info(f"[Chat/stream] Route: mode={mode} model={model_label} search={use_search} sessions={len(contexts)} display={display_scope}")

    # ── 8. Credit check + consume ──
    credit_cost = estimate_cost(mode)
    allowed, credit_info = ai_credits.can_consume(account_id, credit_cost)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": credit_info.get("reason", "credit_limit"),
                "credit_cost": credit_cost,
                "credits_remaining": credit_info.get("remaining", 0),
            },
        )

    consume_ok, consume_info = ai_credits.consume(account_id, credit_cost, mode)
    if not consume_ok:
        raise HTTPException(
            status_code=429,
            detail={
                "error": consume_info.get("reason", "credit_limit"),
                "credit_cost": credit_cost,
                "credits_remaining": consume_info.get("remaining", 0),
            },
        )
    credits_remaining = consume_info.get("remaining", 0)

    # ── 9. Build display scope ──
    # For meta event: only show definitive sessions (single grounded).
    # For multi-session, we defer to done event after seeing the answer.
    all_context_sessions = [{"session_id": c["session_id"], "title": c["title"]} for c in contexts]
    if mode in ("general_static", "general_fresh"):
        used_sessions_list = []
    elif mode == "session_grounded" and len(contexts) == 1:
        used_sessions_list = all_context_sessions
    else:
        # Multi-session or session_plus_general: defer to done event
        used_sessions_list = []

    if todo_context_str and used_sessions_list:
        display_scope = "TODOの出典セッションを参照して回答"
    elif todo_context_str:
        display_scope = "TODOリストを参照して回答"
    elif mode == "session_grounded" and used_sessions_list:
        display_scope = None
    elif mode == "session_plus_general" and used_sessions_list:
        display_scope = "セッションを参考に回答"
    elif mode == "general_fresh":
        display_scope = "公開情報を確認して回答"
    elif mode == "general_static":
        display_scope = "一般知識に基づいて回答"
    else:
        display_scope = None

    # ── 10. Build prompt ──
    history_dicts = [h.model_dump() for h in req.history]

    # For session_then_web with contexts: build hybrid prompt
    if use_search and contexts:
        session_summary = "\n".join([
            f"【{c.get('title', '')}】{c.get('summary', '')[:500]}"
            for c in contexts
        ])
        turn_prompt = build_hybrid_prompt(
            message=message,
            session_summary=session_summary,
            history=history_dicts,
            conversation_summary=req.conversation_summary,
        )
    else:
        turn_prompt = build_stream_prompt(
            message=message,
            mode=mode,
            contexts=contexts,
            history=history_dicts,
            conversation_summary=req.conversation_summary,
            todo_context=todo_context_str,
        )

    logger.info(
        f"[Chat/stream] Route: mode={mode} model={model_label} search={use_search} "
        f"sessions={len(contexts)} credit_cost={credit_cost}"
    )

    # ── 11. Stream response ──
    def event_stream():
        # Meta event (first)
        yield _sse("meta", {
            "mode": mode,
            "model": model_label,
            "used_search": use_search,
            "used_sessions": used_sessions_list,
            "display_scope": display_scope,
            "credit_cost": credit_cost,
            "credits_remaining": credits_remaining,
        })

        full_text = ""
        try:
            if use_search and contexts:
                gen = stream_gemini_search_hybrid(turn_prompt)
            elif use_search:
                gen = stream_gemini_with_search(turn_prompt)
            else:
                gen = stream_gemini_chat(turn_prompt, model_name=model_label)

            for piece in gen:
                full_text += piece
                yield _sse("token", {"text": piece})

        except Exception as e:
            logger.error(f"[Chat/stream] Gemini error: {e}", exc_info=True)
            # Refund credit on failure
            ai_credits.refund(account_id, credit_cost, mode)
            try:
                log_ai_chat(
                    uid=user.uid,
                    mode=mode,
                    model=model_label,
                    credit_cost=0,
                    credits_remaining=credits_remaining,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    session_ids=[c["session_id"] for c in contexts] if contexts else None,
                    used_search=use_search,
                    endpoint="chat/stream",
                    error_message=str(e)[:200],
                )
            except Exception:
                pass
            yield _sse("error", {"message": "AI service temporarily unavailable"})
            return

        # Update conversation state
        if contexts and len(contexts) == 1:
            # Only pin active_session_id for single-session grounded mode
            state["active_session_id"] = contexts[0].get("session_id")
            state["active_session_title"] = contexts[0].get("title")
            state["last_referenced_entity_type"] = "session"
            state["last_referenced_entity_id"] = contexts[0].get("session_id")
        elif contexts:
            # Multi-session: clear active_session_id so next turn re-resolves
            state.pop("active_session_id", None)
            state.pop("active_session_title", None)
            state["last_referenced_entity_type"] = "session"
        state["last_answer_mode"] = mode
        state["last_topic"] = extract_topic(message)
        if want_todo and todo_context_str:
            state["last_referenced_entity_type"] = "todo"

        t_end = time.monotonic()
        stream_latency_ms = int((t_end - t0) * 1000)
        logger.info(
            f"[Chat/stream] DONE uid={user.uid} mode={mode} model={model_label} "
            f"total={stream_latency_ms}ms answer_len={len(full_text)}"
        )

        # Structured ops log
        try:
            log_ai_chat(
                uid=user.uid,
                mode=mode,
                model=model_label,
                credit_cost=credit_cost,
                credits_remaining=credits_remaining,
                latency_ms=stream_latency_ms,
                session_ids=[c["session_id"] for c in contexts] if contexts else None,
                used_search=use_search,
                fallback_reason=(
                    "no_session" if not session_context
                    else "low_score" if not session_answerable
                    else None
                ),
                answer_len=len(full_text),
                endpoint="chat/stream",
            )
        except Exception as e:
            logger.warning(f"[Chat/stream] OpsLog failed: {e}")

        # Generate conversation summary for next turn continuity
        summary_next = f"ユーザー: {message[:100]}。回答: {full_text[:200]}"

        # Determine which sessions were actually referenced
        final_used_sessions = used_sessions_list  # default from meta
        if all_context_sessions and not used_sessions_list:
            # Multi-session: match against BOTH user query and answer
            matched = _match_sessions_to_text(
                all_context_sessions, message, full_text
            )
            if matched:
                final_used_sessions = matched
                titles = [s.get("title", "?")[:30] for s in matched]
                logger.info(f"[Chat/stream] Matched {len(matched)}/{len(all_context_sessions)} sessions: {titles}")
            elif mode in ("session_grounded", "session_plus_general"):
                # Session mode but no match — include all as fallback
                final_used_sessions = all_context_sessions

        # Done event (last)
        yield _sse("done", {
            "full_text": full_text,
            "conversation_state": state,
            "conversation_summary_next": summary_next,
            "credits_remaining": credits_remaining,
            "used_sessions": final_used_sessions,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

@router.get("/suggestions", response_model=SuggestionResponse)
async def get_suggestions(
    session_id: Optional[str] = None,
    user: CurrentUser = Depends(get_current_user),
):
    if session_id:
        return SuggestionResponse(chips=[
            "この会議の要点を教えて",
            "決定事項を抽出して",
            "TODOを整理して",
            "内容を簡単に説明して",
        ])
    else:
        return SuggestionResponse(chips=[
            "最近の会議を探して",
            "TODOを整理して",
            "一般的な質問をする",
        ])


# ---------------------------------------------------------------------------
# Candidate Resolution
# ---------------------------------------------------------------------------

class CandidateRequest(BaseModel):
    query: str
    current_session_id: Optional[str] = None
    limit: int = 5

class CandidateSession(BaseModel):
    session_id: str
    title: str
    started_at: Optional[str] = None
    mode: Optional[str] = None
    score: float = 0.0

class CandidateResponse(BaseModel):
    candidates: List[CandidateSession]
    status: str = "ok"


@router.post("/candidates", response_model=CandidateResponse)
async def resolve_candidates(
    req: CandidateRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Resolve candidate sessions for a chat query."""
    query = req.query.strip()
    if not query:
        return CandidateResponse(candidates=[], status="no_match")

    t0 = time.monotonic()

    try:
        sessions_ref = db.collection("sessions")
        try:
            q = sessions_ref.where("ownerUid", "==", user.uid)
            q = q.order_by("createdAt", direction="DESCENDING")
            q = q.limit(50)
            docs = list(q.stream())
        except Exception:
            q = sessions_ref.where("ownerUid", "==", user.uid).limit(100)
            _epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
            docs = sorted(
                list(q.stream()),
                key=lambda d: d.to_dict().get("createdAt") or d.to_dict().get("startedAt") or _epoch,
                reverse=True,
            )[:50]

        query_lower = query.lower()
        query_tokens = set(query_lower.replace("?", "").replace("？", "").split())

        scored = []
        for doc in docs:
            data = doc.to_dict()
            sid = doc.id
            title = data.get("title") or "無題"
            summary = data.get("summaryMarkdown") or data.get("topicSummary") or ""
            tags = data.get("tags") or []

            score = 0.0
            title_lower = title.lower()

            if query_lower in title_lower:
                score += 10.0
            for t in query_tokens:
                if len(t) >= 2 and t in title_lower:
                    score += 3.0
            summary_lower = summary.lower()
            for t in query_tokens:
                if len(t) >= 2 and t in summary_lower:
                    score += 1.0
            for tag in tags:
                if query_lower in tag.lower():
                    score += 5.0

            if score > 0:
                started_at = data.get("startedAt")
                scored.append(CandidateSession(
                    session_id=sid,
                    title=title,
                    started_at=started_at.isoformat() if hasattr(started_at, 'isoformat') else str(started_at) if started_at else None,
                    mode=data.get("mode", "lecture"),
                    score=score,
                ))

        scored.sort(key=lambda x: -x.score)
        candidates = scored[:req.limit]
        return CandidateResponse(candidates=candidates, status="ok" if candidates else "no_match")

    except Exception as e:
        logger.error(f"[Chat/candidates] Failed: {e}", exc_info=True)
        return CandidateResponse(candidates=[], status="no_match")
