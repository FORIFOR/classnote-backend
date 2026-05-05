"""
LLM-based 2-stage chat router.

Replaces keyword-matching with structured JSON classification via gemini-2.5-flash-lite.
Priority: session-first, web only when clearly needed.
"""

import json
import logging
import os
import time
from typing import Optional

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from pydantic import BaseModel

logger = logging.getLogger("app.services.chat_router")

ROUTER_MODEL = os.environ.get("ROUTER_MODEL_NAME", "gemini-2.5-flash-lite")

_router_model: Optional[GenerativeModel] = None


def _ensure_router_model():
    global _router_model
    if _router_model is not None:
        return
    _router_model = GenerativeModel(ROUTER_MODEL)
    logger.info(f"[ChatRouter] Initialized model={ROUTER_MODEL}")


_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string"},
        "needs_session": {"type": "boolean"},
        "needs_web": {"type": "boolean"},
        "session_query": {"type": "string"},
        "web_query": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["mode", "needs_session", "needs_web", "reason"],
}


class RouteDecision(BaseModel):
    mode: str  # session_only, session_then_web, web_only, general_static, todo
    needs_session: bool
    needs_web: bool
    session_query: Optional[str] = None
    web_query: Optional[str] = None
    reason: str = ""


_CLASSIFIER_PROMPT = """あなたはチャットの質問を分類するルーターです。
ユーザーの質問を分析し、最適な回答モードを JSON で返してください。

# 5つのモード（必ずどれか1つ）

session_only: 会議・講義セッションの内容だけで答えられる質問
  例: 「この会議の決定事項は？」「Aさんの発言をまとめて」「さっきの議論のポイントは？」

session_then_web: セッション内容を参照しつつ、最新の外部情報で補強が必要な質問
  例: 「会議で出た○○の話、今も正しい？」「議事録に出てきた会社の最新情報は？」

web_only: リアルタイム情報が主題で、セッション参照が不要な質問
  例: 「今日のニュース」「現在の株価」「天気」「最新の○○」「〜を調べて」

general_static: 一般知識で答えられ、リアルタイム性もセッション参照も不要
  例: 「RAGとは？」「Pythonの文法」「○○と△△の違い」

todo: TODO・タスク管理に関する質問
  例: 「TODOを整理して」「未完了のタスクは？」「やることリスト」

# 重要な判定ルール
- セッションが存在し、質問がセッション内容に関連しうるなら session_only を優先
- 「最新」「今」等のキーワードがあっても、セッション内の文脈なら session_only
  例: 「最新の決定事項は？」→ session_only（会議内の最新決定）
- web_only は「セッションと無関係 AND リアルタイム性が必要」な場合のみ
- session_then_web は「セッション内容の事実確認を外部で行う」場合
- 「調べて」「検索して」でも「この会議の○○を調べて」は session_only
- セッションが「なし」で、一般的な質問なら general_static

# 入力情報
ユーザーの質問: {message}
現在のセッションタイトル: {active_session_title}
最近のセッション一覧: {session_titles}
鮮度ヒント（参考）: {freshness_hint}
前回の回答モード: {last_mode}
UIスコープ: {ui_scope}

# 出力（JSON のみ）
{{
  "mode": "session_only | session_then_web | web_only | general_static | todo",
  "needs_session": true/false,
  "needs_web": true/false,
  "session_query": "セッション検索用に書き換えたクエリ（不要ならnull）",
  "web_query": "Web検索用に書き換えたクエリ（不要ならnull）",
  "reason": "判定理由（1文）"
}}
"""


async def classify_route(
    message: str,
    session_titles: list[str],
    active_session_title: str | None,
    state: dict,
    freshness_hint: bool,
    ui_scope: str = "global_ai",
) -> RouteDecision:
    """Classify user message into one of 5 routing modes using LLM."""
    _ensure_router_model()

    last_mode = state.get("last_answer_mode", "none")
    titles_str = ", ".join(session_titles[:5]) if session_titles else "なし"

    prompt = _CLASSIFIER_PROMPT.format(
        message=message,
        active_session_title=active_session_title or "なし",
        session_titles=titles_str,
        freshness_hint="あり" if freshness_hint else "なし",
        last_mode=last_mode,
        ui_scope=ui_scope,
    )

    start = time.perf_counter()
    try:
        resp = await _router_model.generate_content_async(
            prompt,
            generation_config=GenerationConfig(
                temperature=0.0,
                max_output_tokens=200,
                response_mime_type="application/json",
                response_schema=_ROUTE_SCHEMA,
            ),
        )

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        raw = resp.text or "{}"
        data = json.loads(raw)

        mode = data.get("mode", "general_static")
        valid_modes = {"session_only", "session_then_web", "web_only", "general_static", "todo"}
        if mode not in valid_modes:
            mode = "general_static"

        decision = RouteDecision(
            mode=mode,
            needs_session=data.get("needs_session", mode in ("session_only", "session_then_web")),
            needs_web=data.get("needs_web", mode in ("web_only", "session_then_web")),
            session_query=data.get("session_query"),
            web_query=data.get("web_query"),
            reason=data.get("reason", ""),
        )

        logger.info(
            f"[ChatRouter] Classified in {elapsed_ms}ms: mode={decision.mode} "
            f"session={decision.needs_session} web={decision.needs_web} "
            f"reason={decision.reason[:60]}"
        )
        return decision

    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(f"[ChatRouter] Classification failed in {elapsed_ms}ms: {e}. Using fallback.")
        return _fallback_classify(message, active_session_title, freshness_hint, ui_scope)


def _fallback_classify(
    message: str,
    active_session_title: str | None,
    freshness_hint: bool,
    ui_scope: str,
) -> RouteDecision:
    """Keyword-based fallback when LLM classifier fails."""
    from app.services.scope_resolver import (
        needs_fresh_grounding,
        has_session_intent,
        is_todo_intent,
    )

    if is_todo_intent(message):
        return RouteDecision(mode="todo", needs_session=True, needs_web=False, reason="fallback: todo keywords")

    has_session = ui_scope == "session_detail" or bool(active_session_title) or has_session_intent(message)

    if needs_fresh_grounding(message) and not has_session:
        return RouteDecision(mode="web_only", needs_session=False, needs_web=True, reason="fallback: freshness + no session")

    if needs_fresh_grounding(message) and has_session:
        return RouteDecision(mode="session_then_web", needs_session=True, needs_web=True, reason="fallback: freshness + session")

    if has_session:
        return RouteDecision(mode="session_only", needs_session=True, needs_web=False, reason="fallback: session context")

    return RouteDecision(mode="general_static", needs_session=False, needs_web=False, reason="fallback: general")


# ── Stage 2: Session sufficiency (fast heuristic, no LLM) ──

class SufficiencyResult(BaseModel):
    answerable: bool
    confidence: float = 0.5
    needs_web_verification: bool = False
    reason: str = ""


def judge_sufficiency(
    message: str,
    session_context: dict | None,
) -> SufficiencyResult:
    """Check if loaded session context is sufficient for the question."""
    if session_context is None:
        return SufficiencyResult(answerable=False, confidence=0.0, needs_web_verification=True, reason="no session context")

    summary = session_context.get("summary", "") or ""
    transcript = session_context.get("transcript_excerpt", "") or ""
    combined = summary + transcript

    if not combined.strip():
        return SufficiencyResult(answerable=False, confidence=0.0, needs_web_verification=True, reason="empty session content")

    # Check if message keywords appear in session content
    msg_tokens = set(message.lower().split())
    stop_words = {"の", "は", "が", "を", "に", "で", "と", "も", "か", "って", "この", "その", "あの"}
    msg_tokens -= stop_words

    combined_lower = combined.lower()
    hit_count = sum(1 for t in msg_tokens if len(t) >= 2 and t in combined_lower)
    hit_ratio = hit_count / max(len(msg_tokens), 1)

    if hit_ratio >= 0.3:
        return SufficiencyResult(answerable=True, confidence=min(0.5 + hit_ratio, 1.0), reason=f"keyword hit {hit_ratio:.0%}")

    if len(combined) > 200:
        return SufficiencyResult(answerable=True, confidence=0.6, reason="substantial content")

    return SufficiencyResult(answerable=False, confidence=0.2, needs_web_verification=True, reason=f"low relevance ({hit_ratio:.0%})")


# ── Utility functions ──

def route_to_legacy_mode(route: RouteDecision) -> str:
    """Map RouteDecision to legacy mode string for credit calculation."""
    mapping = {
        "session_only": "session_grounded",
        "session_then_web": "general_fresh",
        "web_only": "general_fresh",
        "general_static": "general_static",
        "todo": "session_grounded",
    }
    return mapping.get(route.mode, "general_static")


def get_display_scope(route: RouteDecision, sufficiency: SufficiencyResult | None = None) -> str:
    """Return a display label for the reference source."""
    if route.mode == "web_only":
        return "Web検索"
    if route.mode == "session_then_web":
        if sufficiency and sufficiency.answerable and not sufficiency.needs_web_verification:
            return "この会議"
        return "この会議 + Web"
    if route.mode == "session_only":
        return "この会議"
    if route.mode == "todo":
        return "TODO"
    return "一般知識"
