"""Scope resolver — session-first routing with anaphora and TODO awareness.

Design principle: "general に切り替える" のではなく "そのターンだけ general を使う"
Every turn: session first → general fallback → next turn back to session first.
"""

import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger("app.services.scope_resolver")

# ── Anaphora patterns (resolve to active session/todo) ──

SESSION_REFERENT_KEYWORDS = [
    "その会議", "この会議", "その議事録", "この議事録",
    "そのセッション", "このセッション", "その録音", "この録音",
    "その授業", "この授業", "その講義", "この講義",
    "さっきの会議", "さっきの授業", "さっきの講義", "さっきの",
]

TODO_REFERENT_KEYWORDS = [
    "そのTODO", "このTODO", "そのタスク", "このタスク",
]

GENERAL_REFERENT_KEYWORDS = [
    "それについて", "これについて",
]

# ── Session answerability keywords ──

SESSION_STRONG_KEYWORDS = [
    "この会議", "この授業", "この講義", "この内容", "ここで", "今回",
    "このセッション", "この録音", "さっきの", "先ほどの",
]

SESSION_ACTION_KEYWORDS = [
    "決定事項", "アクション", "要点", "要約",
    "抽出して", "まとめて", "リストアップ",
    "議事録", "誰が担当", "何が決まった", "誰が何を",
]

# ── General question indicators ──

GENERAL_EXPLAIN_KEYWORDS = [
    "とは", "どのような会社", "どのような企業", "どういう意味",
    "歴史", "背景", "仕組み", "の違い",
    "一般的に", "そもそも", "とは何",
    "調査して", "調べて", "リサーチ",
]

# ── Freshness indicators ──

FRESHNESS_KEYWORDS = [
    "最新", "現在", "今日", "今", "最近", "直近", "現時点",
    "ニュース", "動向", "支持率", "CEO", "社長", "株価", "発表",
    "アップデート", "現職", "価格", "速報", "リリース",
    "天気", "為替", "レート",
    "調べて", "検索して",
    "今どう", "どうなってる", "どうなっている",
]

# ── TODO keywords ──

TODO_LIST_KEYWORDS = [
    "未完了のTODO", "TODOを整理", "TODOリスト", "TODO一覧",
    "タスクを整理", "タスク一覧", "やることリスト",
    "TODOを確認", "TODO確認", "タスクを確認",
    "TODOの優先", "優先度の高いTODO", "期限が近いTODO",
    "TODOを教えて", "タスクを教えて",
]
TODO_VERBS = ["整理", "確認", "教えて", "見せて", "一覧", "リスト", "優先"]

# ── Session search (cold start) ──

SESSION_SEARCH_KEYWORDS = [
    "最近の会議", "最近の授業", "最近の講義", "最近のセッション",
    "会議で", "授業で", "講義で", "セッションで",
    "録音",
]

# ── Stop words for keyword matching ──

_STOP_WORDS = {
    "です", "ます", "ください", "して", "について", "から", "まで",
    "ので", "ため", "する", "した", "ある", "いる", "なる", "ない",
    "ている", "でした", "ました", "られ", "という", "こと", "もの",
    "その", "この", "あの", "どの", "それ", "これ", "あれ", "どれ",
}

# ── Japanese particle-based tokenizer ──

_JP_SPLIT_PATTERN = re.compile(
    r'[のをはがでにとへもやかなけれたてりるれろ'
    r'、。？！\?\!\s　,\.・]+'
)


def _tokenize(text: str) -> list[str]:
    """Split Japanese text by particles/punctuation into meaningful tokens.

    Unlike re.findall(r'[\\w]+'), this correctly splits Japanese sentences
    where CJK characters are all treated as \\w, producing one giant token.
    """
    text_lower = text.lower()
    # Split by particles, punctuation, whitespace
    raw_tokens = _JP_SPLIT_PATTERN.split(text_lower)
    # Filter: keep tokens with length >= 2, remove stop words
    return [t for t in raw_tokens if len(t) >= 2 and t not in _STOP_WORDS]


# ---------------------------------------------------------------------------
# 1. Referent resolution (anaphora)
# ---------------------------------------------------------------------------

def resolve_referent(message: str, state: dict) -> Optional[dict]:
    """Resolve anaphora (その会議, このTODO, etc.) to a concrete entity.

    Returns {"entity_type": "session"|"todo", "entity_id": "..."} or None.
    """
    # Session referents
    if any(w in message for w in SESSION_REFERENT_KEYWORDS):
        sid = state.get("active_session_id")
        if sid:
            logger.info(f"[ScopeResolver] Referent → session {sid}")
            return {"entity_type": "session", "entity_id": sid}

    # TODO referents
    if any(w in message for w in TODO_REFERENT_KEYWORDS):
        tid = state.get("active_todo_id")
        if tid:
            logger.info(f"[ScopeResolver] Referent → todo {tid}")
            return {"entity_type": "todo", "entity_id": tid}

    # General referents (それについて, これについて) — last referenced entity
    if any(w in message for w in GENERAL_REFERENT_KEYWORDS):
        etype = state.get("last_referenced_entity_type")
        eid = state.get("last_referenced_entity_id")
        if etype and eid:
            logger.info(f"[ScopeResolver] Referent (general) → {etype} {eid}")
            return {"entity_type": etype, "entity_id": eid}

    return None


# ---------------------------------------------------------------------------
# 2. TODO-aware resolution
# ---------------------------------------------------------------------------

def resolve_todo_aware(message: str, state: dict, todos: List[dict]) -> Optional[dict]:
    """Match message to a specific TODO and extract its source session.

    Uses bidirectional substring matching + token matching for fuzzy matching.
    Returns {"todo_id", "session_id", "session_title"} or None.
    """
    if not todos:
        return None

    message_lower = message.lower()
    msg_tokens = _tokenize(message)
    best_match = None
    best_score = 0

    for todo in todos:
        title = todo.get("title", "")
        title_lower = title.lower()

        score = 0

        # Token-based matching (both directions)
        title_tokens = _tokenize(title)
        # Message tokens found in TODO title (strong signal → weight 2)
        score += sum(2 for t in msg_tokens if t in title_lower)
        # TODO title tokens found in message
        score += sum(1 for t in title_tokens if t in message_lower)

        # Bidirectional substring match bonus
        if len(title_lower) >= 4 and title_lower in message_lower:
            score += 5
        # Partial: TODO title substring in message (e.g. "LP制作" in "LP制作はどうなった")
        for t_token in title_tokens:
            if len(t_token) >= 3 and t_token in message_lower:
                score += 2
        # Common prefix overlap (e.g. "予算申請どう" shares "予算申請" with "予算申請書")
        for mt in msg_tokens:
            for length in range(min(len(mt), 10), 2, -1):
                if mt[:length] in title_lower:
                    score += 2
                    break

        if score > best_score:
            best_score = score
            best_match = todo

    if best_match and best_score >= 2:
        source = best_match.get("source") or {}
        result = {
            "todo_id": best_match.get("id"),
            "session_id": source.get("sessionId"),
            "session_title": source.get("sessionTitle"),
        }
        logger.info(
            f"[ScopeResolver] TODO matched: '{best_match.get('title', '')[:40]}' "
            f"score={best_score} → session={result['session_id']}"
        )
        return result

    return None


# ---------------------------------------------------------------------------
# 3. Session answerability
# ---------------------------------------------------------------------------

def can_answer_from_session(
    message: str,
    session_context: Optional[dict],
    state: dict,
) -> Tuple[bool, str]:
    """Check if the active session can answer this question.

    Returns (answerable, suggested_mode).
    suggested_mode: "session_grounded" | "session_plus_general" | "general_static"
    """
    if not session_context:
        return False, "general_static"

    score = 0

    # Strong session indicators (+30)
    if any(k in message for k in SESSION_STRONG_KEYWORDS):
        score += 30

    # Session action keywords (+25)
    if any(k in message for k in SESSION_ACTION_KEYWORDS):
        score += 25

    # Keyword hit in session content (+25)
    if _session_has_keyword_hit(message, session_context):
        score += 25

    # Topic continuity (+10)
    last_topic = state.get("last_topic", "")
    if last_topic and len(last_topic) >= 2 and last_topic in message:
        score += 10

    # General explanation penalty (-20)
    if any(k in message for k in GENERAL_EXPLAIN_KEYWORDS):
        score -= 20

    # Freshness penalty (-30)
    if any(k in message for k in FRESHNESS_KEYWORDS):
        score -= 30

    if score >= 40:
        mode = "session_grounded"
        answerable = True
    elif score >= 25:
        mode = "session_plus_general"
        answerable = True
    else:
        mode = "general_static"
        answerable = False

    logger.info(
        f"[ScopeResolver] can_answer: score={score} answerable={answerable} mode={mode} "
        f"session={session_context.get('session_id', '?')}"
    )
    return answerable, mode


def _session_has_keyword_hit(message: str, session_context: dict) -> bool:
    """Check if message keywords appear in session title/summary/transcript.

    Uses weighted scoring: title hit=3, summary hit=2, transcript hit=1.
    Returns True if total score >= 2.
    """
    tokens = _tokenize(message)
    if not tokens:
        return False

    title = session_context.get("title", "").lower()
    summary = session_context.get("summary", "").lower()
    transcript = session_context.get("transcript_excerpt", "")[:2000].lower()

    score = 0
    for t in tokens:
        if t in title:
            score += 3
        elif t in summary:
            score += 2
        elif t in transcript:
            score += 1

    logger.debug(
        f"[ScopeResolver] keyword_hit: tokens={tokens} score={score}"
    )
    return score >= 2


# ---------------------------------------------------------------------------
# 4. Intent detectors
# ---------------------------------------------------------------------------

def is_todo_intent(message: str) -> bool:
    """Check if the message is about the user's TODO list."""
    if any(k in message for k in TODO_LIST_KEYWORDS):
        return True
    if "TODO" in message and any(v in message for v in TODO_VERBS):
        return True
    return False


def needs_fresh_grounding(message: str) -> bool:
    """Check if the question needs latest/real-time information."""
    return any(k in message for k in FRESHNESS_KEYWORDS)


def has_session_intent(message: str) -> bool:
    """Check if the message implies session search (for cold-start auto-resolve)."""
    if any(k in message for k in SESSION_SEARCH_KEYWORDS):
        return True
    if any(k in message for k in SESSION_STRONG_KEYWORDS):
        return True
    if any(k in message for k in SESSION_ACTION_KEYWORDS):
        return True
    return False


# ---------------------------------------------------------------------------
# 5. Topic extraction
# ---------------------------------------------------------------------------

def extract_topic(message: str) -> str:
    """Extract the main topic from a message for state tracking."""
    topic = message
    for pattern in [
        "とは何ですか", "とはなんですか", "について教えてください",
        "について教えて", "を教えてください", "を教えて",
        "ですか？", "ですか", "？", "?",
        "とは", "について", "はどう", "はなぜ",
        "を調べて", "を調査して", "ってなに", "って何",
    ]:
        topic = topic.replace(pattern, "")
    topic = topic.strip()
    return topic[:50] if topic else message[:50]
