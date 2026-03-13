"""AI Chat API — session-aware conversational AI."""

import logging
import time
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_current_user, CurrentUser
from app.firebase import db
from app.services.scope_resolver import resolve_scope
from app.services.context_builder import build_session_context, build_turn_prompt, build_todo_context
from app.services.gemini_chat import call_gemini_chat, call_gemini_general_chat

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
    mode: Optional[str] = None  # "session" | "general" — determines model routing
    current_session_id: Optional[str] = None
    recent_session_ids: Optional[List[str]] = None
    ui_scope: str = "global_ai"  # "global_ai" | "session_detail" | "global_all"
    history: List[HistoryItem] = []
    conversation_summary: Optional[str] = None


class UsedSession(BaseModel):
    session_id: str
    title: str


class Citation(BaseModel):
    start_sec: int
    end_sec: int
    speaker: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    mode: Literal["session_grounded", "session_plus_general", "general_only"]
    used_sessions: List[UsedSession]
    citations: List[Citation]
    confidence: float
    needs_general_knowledge: bool
    follow_up_suggestion: Optional[str] = None
    conversation_summary_next: Optional[str] = None
    used_search: bool = False  # Whether the response used web search grounding


class SuggestionResponse(BaseModel):
    chips: List[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/send", response_model=ChatResponse)
async def send_chat(
    req: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Send a chat message and get an AI response."""
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 chars)")

    has_summary = bool(req.conversation_summary)
    history_len = len(req.history)
    is_general_mode = req.mode == "general"
    logger.info(
        f"[Chat/send] START uid={user.uid} session={req.current_session_id} "
        f"scope={req.ui_scope} client_mode={req.mode} msg_len={len(message)} history={history_len} "
        f"has_summary={has_summary} chat_id={req.chat_id}"
    )
    logger.info(f"[Chat/send] Q: \"{message[:500]}\"")

    t0 = time.monotonic()

    # ── General chat mode: use gemini-2.5-flash-lite, minimal session context ──
    if is_general_mode:
        logger.info("[Chat/send] General mode → using gemini-2.5-flash-lite")

        # Optionally fetch TODO context
        todo_context_str = None
        if any(k in message for k in ["todo", "TODO", "タスク", "やること"]):
            try:
                account_id = getattr(user, "account_id", None) or user.uid
                todo_q = (
                    db.collection("todos")
                    .where("accountId", "==", account_id)
                    .where("status", "in", ["open", "overdue"])
                    .limit(30)
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
                todo_context_str = build_todo_context(todos)
                logger.info(f"[Chat/send] General mode: fetched {len(todos)} TODOs")
            except Exception as e:
                logger.warning(f"[Chat/send] General mode TODO fetch failed: {e}")

        # Build prompt for general chat
        turn_prompt = build_turn_prompt(
            message=message,
            mode="general_only",
            contexts=[],
            history=[h.model_dump() for h in req.history],
            conversation_summary=req.conversation_summary,
            todo_context=todo_context_str,
        )
        logger.info(f"[Chat/send] General prompt built: {len(turn_prompt)} chars")

        t_gemini_start = time.monotonic()
        try:
            result = call_gemini_general_chat(turn_prompt)
        except Exception as e:
            logger.error(f"[Chat/send] General Gemini failed: {e}")
            raise HTTPException(status_code=503, detail="AI service temporarily unavailable")

        t_end = time.monotonic()
        logger.info(
            f"[Chat/send] DONE (general) uid={user.uid} total={((t_end - t0)*1000):.0f}ms "
            f"gemini={((t_end - t_gemini_start)*1000):.0f}ms answer_len={len(result.get('answer', ''))}"
        )
        logger.info(f"[Chat/send] A: \"{result.get('answer', '')}\"")

        return ChatResponse(
            answer=result.get("answer", ""),
            mode="general_only",
            used_sessions=[UsedSession(**s) for s in result.get("used_sessions", [])],
            citations=[Citation(**c) for c in result.get("citations", [])],
            confidence=result.get("confidence", 0.0),
            needs_general_knowledge=True,
            follow_up_suggestion=result.get("follow_up_suggestion"),
            conversation_summary_next=result.get("conversation_summary_next"),
            used_search=False,
        )

    # ── Session mode: existing logic with gemini-2.0-flash-lite ──

    # 1. Resolve scope
    scope = resolve_scope(message, req.current_session_id, req.ui_scope)
    mode = scope["mode"]
    session_ids = scope["session_ids"]
    auto_resolve = scope.get("auto_resolve", False)
    t_scope = time.monotonic()
    logger.info(f"[Chat/send] Scope resolved: mode={mode} session_ids={session_ids} auto_resolve={auto_resolve} ({(t_scope - t0)*1000:.0f}ms)")

    # 1b. Auto-resolve: fetch recent sessions when no session specified but data-dependent query
    if auto_resolve and not session_ids:
        try:
            _epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)

            # Try ordered query first (requires composite index: ownerUid + createdAt DESC)
            try:
                uid_q = (
                    db.collection("sessions")
                    .where("ownerUid", "==", user.uid)
                    .order_by("createdAt", direction="DESCENDING")
                    .limit(30)
                )
                docs = list(uid_q.stream())
                logger.info(f"[Chat/send] Auto-resolve: ordered query returned {len(docs)} docs")
            except Exception as idx_err:
                # Fallback: no index — fetch more docs and sort client-side
                logger.warning(f"[Chat/send] Auto-resolve ordered query failed (index?): {idx_err}")
                uid_q = db.collection("sessions").where("ownerUid", "==", user.uid).limit(200)
                docs = list(uid_q.stream())
                docs.sort(
                    key=lambda d: d.to_dict().get("createdAt") or d.to_dict().get("startedAt") or _epoch,
                    reverse=True,
                )
                docs = docs[:30]
                logger.info(f"[Chat/send] Auto-resolve: fallback query returned {len(docs)} docs (sorted client-side)")

            seen_ids = {d.id for d in docs}

            # Also try ownerAccountId if available (handles account migration)
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

            # Final sort by createdAt descending
            docs.sort(
                key=lambda d: d.to_dict().get("createdAt") or d.to_dict().get("startedAt") or _epoch,
                reverse=True,
            )

            # Mode-aware filtering: prefer sessions matching the query intent
            # e.g. "最近の会議" → prefer mode="meeting", "最近の講義" → prefer mode="lecture"
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
                # Take up to 5 from preferred mode, fill remainder with others
                selected = preferred[:5]
                remaining = 5 - len(selected)
                if remaining > 0:
                    selected += others[:remaining]
                logger.info(
                    f"[Chat/send] Mode filter: preferred={preferred_mode} "
                    f"matched={len(preferred)} selected={len(selected)}"
                )
            else:
                selected = docs[:5]

            session_ids = [d.id for d in selected]
            top_titles = [(d.id[:8], (d.to_dict().get("title") or "?")[:30], d.to_dict().get("mode", "?")) for d in selected]
            logger.info(f"[Chat/send] Auto-resolved {len(session_ids)} recent sessions (scanned {len(docs)}): {top_titles}")
        except Exception as e:
            logger.warning(f"[Chat/send] Auto-resolve failed: {e}", exc_info=True)

    # 2. Load session data
    contexts = []
    for sid in session_ids:
        try:
            doc = db.collection("sessions").document(sid).get()
            if doc.exists:
                data = doc.to_dict()
                data["id"] = doc.id
                # Verify access: user must be owner or shared member
                owner_uid = data.get("ownerUid") or data.get("userId")
                owner_account = data.get("ownerAccountId")
                shared_accounts = data.get("sharedWithAccountIds") or []
                shared_uids = data.get("sharedUserIds") or data.get("sharedWithUserIds") or []

                has_access = (
                    owner_uid == user.uid
                    or owner_account == user.account_id
                    or user.account_id in shared_accounts
                    or user.uid in shared_uids
                )
                if not has_access:
                    logger.warning(f"[Chat/send] Access denied: uid={user.uid} session={sid} owner={owner_uid}")
                    continue

                ctx = build_session_context(data, message)
                ctx_summary_len = len(ctx.get("summary", ""))
                ctx_transcript_len = len(ctx.get("transcript_excerpt", ""))
                logger.info(
                    f"[Chat/send] Context loaded: session={sid} title=\"{ctx.get('title', '')[:50]}\" "
                    f"summary_len={ctx_summary_len} transcript_len={ctx_transcript_len}"
                )
                contexts.append(ctx)
            else:
                logger.warning(f"[Chat/send] Session not found in Firestore: {sid}")
        except Exception as e:
            logger.error(f"[Chat/send] Failed to load session {sid}: {e}")

    t_ctx = time.monotonic()
    logger.info(f"[Chat/send] Contexts loaded: {len(contexts)}/{len(session_ids)} ({(t_ctx - t_scope)*1000:.0f}ms)")

    # If session was requested but not found, fall back to general
    if session_ids and not contexts:
        logger.info(f"[Chat/send] Fallback: session requested but no context → general_only")
        mode = "general_only"

    # 2b. Fetch user's TODO list if requested
    todo_context_str = None
    if scope.get("todo_list"):
        try:
            account_id = getattr(user, "account_id", None) or user.uid
            todo_q = (
                db.collection("todos")
                .where("accountId", "==", account_id)
                .where("status", "in", ["open", "overdue"])
                .limit(30)
            )
            todo_docs = list(todo_q.stream())
            todos = []
            for td in todo_docs:
                todo_data = td.to_dict()
                todo_data["id"] = td.id
                todos.append(todo_data)
            # Sort by priority (high > mid > low) then by dueDate
            priority_order = {"high": 0, "mid": 1, "low": 2}
            todos.sort(key=lambda t: (
                priority_order.get(t.get("priority", "mid"), 1),
                t.get("dueDate") or "9999-99-99",
            ))
            todo_context_str = build_todo_context(todos)
            logger.info(f"[Chat/send] Fetched {len(todos)} open TODOs for user {account_id}")
        except Exception as e:
            logger.warning(f"[Chat/send] TODO fetch failed: {e}", exc_info=True)

    # 3. Build prompt (with conversation summary for continuity)
    turn_prompt = build_turn_prompt(
        message=message,
        mode=mode,
        contexts=contexts,
        history=[h.model_dump() for h in req.history],
        conversation_summary=req.conversation_summary,
        todo_context=todo_context_str,
    )
    logger.info(f"[Chat/send] Prompt built: {len(turn_prompt)} chars")
    logger.info(f"[Chat/send] === PROMPT START ===\n{turn_prompt}\n=== PROMPT END ===")

    # 4. Call Gemini
    t_gemini_start = time.monotonic()
    try:
        result = call_gemini_chat(turn_prompt)
    except Exception as e:
        t_fail = time.monotonic()
        logger.error(f"[Chat/send] Gemini failed after {(t_fail - t_gemini_start)*1000:.0f}ms: {e}")
        raise HTTPException(status_code=503, detail="AI service temporarily unavailable")

    t_end = time.monotonic()
    total_ms = (t_end - t0) * 1000
    gemini_ms = (t_end - t_gemini_start) * 1000
    answer_len = len(result.get("answer", ""))
    answer_text = result.get("answer", "")
    logger.info(
        f"[Chat/send] DONE uid={user.uid} total={total_ms:.0f}ms gemini={gemini_ms:.0f}ms "
        f"mode={result.get('mode', mode)} confidence={result.get('confidence', 0)} "
        f"answer_len={answer_len} used_sessions={len(result.get('used_sessions', []))} "
        f"citations={len(result.get('citations', []))}"
    )
    logger.info(f"[Chat/send] A: \"{answer_text}\"")
    logger.info(
        f"[Chat/send] Full response: used_sessions={result.get('used_sessions', [])} "
        f"follow_up={result.get('follow_up_suggestion', '')} "
        f"summary_next={result.get('conversation_summary_next', '')}"
    )

    return ChatResponse(
        answer=result.get("answer", ""),
        mode=result.get("mode", mode),
        used_sessions=[
            UsedSession(**s) for s in result.get("used_sessions", [])
        ],
        citations=[
            Citation(**c) for c in result.get("citations", [])
        ],
        confidence=result.get("confidence", 0.0),
        needs_general_knowledge=result.get("needs_general_knowledge", mode != "session_grounded"),
        follow_up_suggestion=result.get("follow_up_suggestion"),
        conversation_summary_next=result.get("conversation_summary_next"),
        used_search=False,
    )


@router.get("/suggestions", response_model=SuggestionResponse)
async def get_suggestions(
    session_id: Optional[str] = None,
    user: CurrentUser = Depends(get_current_user),
):
    """Get suggestion chips for the chat input."""
    if session_id:
        return SuggestionResponse(chips=[
            "この会議の要点を教えて",
            "決定事項を抽出して",
            "TODOを整理して",
            "内容を簡単に説明して",
            "一般的に補足して",
        ])
    else:
        return SuggestionResponse(chips=[
            "最近の会議を探して",
            "TODOを整理して",
            "一般的な質問をする",
        ])


# ---------------------------------------------------------------------------
# Candidate Resolution Models
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
    status: str = "ok"  # "ok" | "searching" | "no_match"


# ---------------------------------------------------------------------------
# Candidate Resolution Endpoint
# ---------------------------------------------------------------------------

@router.post("/candidates", response_model=CandidateResponse)
async def resolve_candidates(
    req: CandidateRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Resolve candidate sessions for a chat query.
    Lightweight search — no AI, just Firestore query + keyword scoring."""
    query = req.query.strip()
    if not query:
        logger.debug("[Chat/candidates] Empty query, returning no_match")
        return CandidateResponse(candidates=[], status="no_match")

    t0 = time.monotonic()
    logger.info(
        f"[Chat/candidates] START uid={user.uid} query=\"{query[:80]}\" "
        f"current_session={req.current_session_id} limit={req.limit}"
    )

    try:
        # Fetch recent sessions (owner only for speed)
        sessions_ref = db.collection("sessions")
        try:
            q = sessions_ref.where("ownerUid", "==", user.uid)
            q = q.order_by("createdAt", direction="DESCENDING")
            q = q.limit(50)
            docs = list(q.stream())
        except Exception as idx_err:
            # Fallback: index may not exist yet — query without order_by
            logger.warning(f"[Chat/candidates] Index query failed, using fallback: {idx_err}")
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
        doc_count = 0
        for doc in docs:
            doc_count += 1
            data = doc.to_dict()
            sid = doc.id
            title = data.get("title") or "無題"
            summary = data.get("summaryMarkdown") or data.get("topicSummary") or ""
            tags = data.get("tags") or []
            mode = data.get("mode", "lecture")
            started_at = data.get("startedAt")

            # Score: title match (heavy), summary match, tag match
            score = 0.0
            title_lower = title.lower()

            # Exact substring match in title
            if query_lower in title_lower:
                score += 10.0

            # Token matches in title
            for t in query_tokens:
                if len(t) >= 2 and t in title_lower:
                    score += 3.0

            # Token matches in summary
            summary_lower = summary.lower()
            for t in query_tokens:
                if len(t) >= 2 and t in summary_lower:
                    score += 1.0

            # Tag matches
            for tag in tags:
                if query_lower in tag.lower():
                    score += 5.0
                for t in query_tokens:
                    if len(t) >= 2 and t in tag.lower():
                        score += 2.0

            # Date hint matching (e.g., "3/11", "昨日")
            if started_at:
                started_str = str(started_at)
                for t in query_tokens:
                    if t in started_str:
                        score += 4.0

            # Boost if this is the current session
            if req.current_session_id and sid == req.current_session_id:
                score += 5.0

            # Only include if some relevance
            if score > 0:
                scored.append(CandidateSession(
                    session_id=sid,
                    title=title,
                    started_at=started_at.isoformat() if hasattr(started_at, 'isoformat') else str(started_at) if started_at else None,
                    mode=mode,
                    score=score,
                ))

        # Sort by score descending
        scored.sort(key=lambda x: -x.score)
        candidates = scored[:req.limit]

        status = "ok" if candidates else "no_match"
        t_end = time.monotonic()

        top_candidates = [(c.session_id, c.title[:30], c.score) for c in candidates[:3]]
        logger.info(
            f"[Chat/candidates] DONE uid={user.uid} query=\"{query[:40]}\" "
            f"scanned={doc_count} scored={len(scored)} returned={len(candidates)} "
            f"status={status} tokens={query_tokens} "
            f"top={top_candidates} ({(t_end - t0)*1000:.0f}ms)"
        )

        return CandidateResponse(candidates=candidates, status=status)

    except Exception as e:
        logger.error(f"[Chat/candidates] Failed: uid={user.uid} query=\"{query[:80]}\" error={e}", exc_info=True)
        # Graceful fallback — don't fail the UX
        return CandidateResponse(candidates=[], status="no_match")
