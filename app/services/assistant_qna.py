"""DeepNote Assistant — grounded Q&A engine.

Phase A: rules-first routing with Gemini Flash Lite as a fallback.

Intent priorities, in order:
  1. Decision queries → ``decisions[]`` array directly (no LLM)
  2. TODO queries → ``accounts/{id}/todos`` for this session (no LLM)
  3. Summary / 全体要約 → return ``summaryMarkdown`` (no LLM)
  4. Free-form question → Gemini Flash Lite with structured context

The "structured context" we feed the LLM is intentionally compact:
``summaryJson`` (~1-3 KB), top decisions, top todos, NEVER the full
transcript by default. If the question can't be answered from that
context the model is instructed to say so rather than confabulate, so
we don't fabricate citations that aren't in the structured data.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.firebase import db

logger = logging.getLogger("app.services.assistant_qna")


# ──────────────────────────────────────────────────────────────────────
# Intent classification (rules-first)
# ──────────────────────────────────────────────────────────────────────

_DECISION_KEYWORDS = ("決定事項", "決まった", "結論", "decision")
_TODO_KEYWORDS = ("todo", "タスク", "やること", "宿題", "アクション", "担当")
_SUMMARY_KEYWORDS = ("要約", "サマリ", "サマリー", "summary", "概要", "まとめ")
_HELP_KEYWORDS = ("ヘルプ", "help", "使い方")


def classify(question: str) -> str:
    if not question or not question.strip():
        return "unknown"
    q = question.strip().lower()
    if any(k in q for k in _HELP_KEYWORDS):
        return "help"
    if any(k in q for k in _DECISION_KEYWORDS):
        return "ask_session_decision"
    if any(k in q for k in _TODO_KEYWORDS):
        return "ask_session_todo"
    if any(k in q for k in _SUMMARY_KEYWORDS):
        return "ask_session_summary"
    return "ask_session_freeform"


# ──────────────────────────────────────────────────────────────────────
# Session resolution
# ──────────────────────────────────────────────────────────────────────

def resolve_session_id(session_id: Optional[str], owner_uid: str, owner_account_id: Optional[str]) -> Optional[str]:
    """Return ``session_id`` if provided, else the user's latest owned
    session. Returns None if no session exists for this user.
    """
    if session_id:
        return session_id
    if not owner_uid:
        return None
    try:
        # Prefer ownerAccountId scoping (multi-uid accounts) when possible
        rows: List[Tuple[str, datetime]] = []
        q = db.collection("sessions").where("ownerUserId", "==", owner_uid).limit(20)
        for s in q.stream():
            d = s.to_dict() or {}
            ts = d.get("createdAt")
            if isinstance(ts, datetime):
                rows.append((s.id, ts))
        if not rows:
            return None
        rows.sort(key=lambda kv: kv[1], reverse=True)
        return rows[0][0]
    except Exception as e:
        logger.warning("[assistant_qna] resolve_session_id failed: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────
# Structured-data answers (no LLM)
# ──────────────────────────────────────────────────────────────────────

def _fetch_session_doc(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        snap = db.collection("sessions").document(session_id).get()
        if not snap.exists:
            return None
        return snap.to_dict() or None
    except Exception as e:
        logger.warning("[assistant_qna] _fetch_session_doc(%s) failed: %s", session_id, e)
        return None


def _fetch_summary_v2(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        snap = db.collection("sessions").document(session_id).collection("derived").document("summary").get()
        if not snap.exists:
            return None
        d = snap.to_dict() or {}
        result = d.get("result") or {}
        return result if isinstance(result, dict) else None
    except Exception:
        return None


def _fetch_todos(account_id: str, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    if not account_id or not session_id:
        return []
    out: List[Dict[str, Any]] = []
    try:
        q = (
            db.collection("accounts").document(account_id)
            .collection("todos")
            .where("sessionId", "==", session_id)
            .limit(limit)
        )
        for t in q.stream():
            d = t.to_dict() or {}
            d["_id"] = t.id
            out.append(d)
    except Exception as e:
        logger.warning("[assistant_qna] _fetch_todos failed: %s", e)
    return out


def _format_decisions(decisions: List[Any]) -> Tuple[str, List[Dict[str, Any]]]:
    if not decisions:
        return ("この会議には決定事項が記録されていません。", [])
    lines = ["この会議の決定事項です:"]
    cites = []
    for i, d in enumerate(decisions[:8], 1):
        text = d.get("text") if isinstance(d, dict) else str(d)
        if not text:
            continue
        lines.append(f"{i}. {text}")
        cites.append({"type": "decision", "id": f"decisions[{i-1}]", "snippet": text[:120]})
    return ("\n".join(lines), cites)


def _format_todos(todos: List[Dict[str, Any]], assignee_filter: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    if not todos:
        return ("この会議に紐づく TODO はまだ抽出されていません。", [])
    if assignee_filter:
        filtered = [t for t in todos if assignee_filter in (t.get("assignee") or "") or assignee_filter in (t.get("title") or "")]
        if filtered:
            todos = filtered
    lines = []
    cites = []
    if assignee_filter:
        lines.append(f"「{assignee_filter}」に関連する TODO は {len(todos)} 件です:")
    else:
        lines.append(f"この会議の TODO は {len(todos)} 件です:")
    for i, t in enumerate(todos[:8], 1):
        title = t.get("title") or t.get("text") or "(無題)"
        due = t.get("dueDate") or t.get("due")
        due_str = f"（期限: {due}）" if due else ""
        lines.append(f"{i}. {title}{due_str}")
        cites.append({
            "type": "todo",
            "id": t.get("_id") or f"todos[{i-1}]",
            "snippet": title[:120],
        })
    return ("\n".join(lines), cites)


def _format_summary(session_data: Dict[str, Any], summary_v2: Optional[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    if summary_v2 and summary_v2.get("markdown"):
        text = summary_v2["markdown"]
    elif session_data.get("summaryMarkdown"):
        text = session_data["summaryMarkdown"]
    elif session_data.get("topicSummary"):
        text = session_data["topicSummary"]
    else:
        return ("この会議の要約はまだ生成されていません。", [])
    if len(text) > 1500:
        text = text[:1500] + "\n…(省略)"
    title = session_data.get("title") or "(無題)"
    return (
        f"会議「{title}」の要約です:\n\n{text}",
        [{"type": "summary", "id": "summary", "snippet": title[:120]}],
    )


def _extract_assignee_filter(question: str) -> Optional[str]:
    """Heuristic: extract a person name from ``田中さん`` / ``山田`` etc.
    so todo / decision queries can be filtered.
    """
    if not question:
        return None
    m = re.search(r"([一-鿿ァ-ヴーぁ-ん々]{1,8}?)(さん|氏|くん|ちゃん)", question)
    if m:
        return m.group(1)
    return None


# ──────────────────────────────────────────────────────────────────────
# LLM-backed free-form (small context only)
# ──────────────────────────────────────────────────────────────────────

async def _answer_freeform(
    question: str,
    session_data: Dict[str, Any],
    summary_v2: Optional[Dict[str, Any]],
    decisions: List[Any],
    todos: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Build a tight context (no full transcript) and ask Gemini once."""
    title = session_data.get("title") or "(無題)"
    summary_text = ""
    if summary_v2 and summary_v2.get("markdown"):
        summary_text = summary_v2["markdown"]
    elif session_data.get("summaryMarkdown"):
        summary_text = session_data["summaryMarkdown"]
    summary_text = (summary_text or "")[:2000]

    decisions_block = "\n".join(
        f"- {d.get('text') if isinstance(d, dict) else str(d)}"
        for d in (decisions or [])[:8] if d
    )
    todo_block = "\n".join(
        f"- {t.get('title') or t.get('text') or ''}"
        + (f" (期限: {t.get('dueDate') or t.get('due')})" if (t.get("dueDate") or t.get("due")) else "")
        + (f" (担当: {t.get('assignee')})" if t.get("assignee") else "")
        for t in (todos or [])[:8]
    )

    prompt = (
        f"あなたは DeepNote の議事録アシスタントです。以下の会議情報のみを根拠として、"
        f"日本語で簡潔に回答してください。情報が足りない場合は推測せず、"
        f"『記録された情報からは判断できません』と答えてください。\n\n"
        f"会議タイトル: {title}\n\n"
        f"=== 要約 ===\n{summary_text or '(なし)'}\n\n"
        f"=== 決定事項 ===\n{decisions_block or '(なし)'}\n\n"
        f"=== TODO ===\n{todo_block or '(なし)'}\n\n"
        f"=== 質問 ===\n{question}\n\n"
        f"=== 回答 ==="
    )

    try:
        from app.services import llm as _llm
        # Reuse the existing wrapper so we get the strong retry / quota
        # behaviour and profiling for free.
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig
        project_id = _llm._get_project_id()
        location = (
            __import__("os").environ.get("VERTEX_REGION")
            or __import__("os").environ.get("VERTEX_LOCATION")
            or "us-central1"
        )
        if project_id:
            vertexai.init(project=project_id, location=location)
        model_name = (
            __import__("os").environ.get("ASSISTANT_MODEL_NAME")
            or "gemini-2.0-flash-lite"
        )
        model = GenerativeModel(model_name)
        gen_cfg = GenerationConfig(temperature=0.2, max_output_tokens=512)
        resp = await _llm._timed_llm_call(model, prompt, gen_cfg, label="assistant_qna")
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            text = "回答を生成できませんでした。少し時間をおいて再度お試しください。"
        cites = []
        if summary_text:
            cites.append({"type": "summary", "id": "summary", "snippet": summary_text[:120]})
        for i, d in enumerate((decisions or [])[:3]):
            txt = d.get("text") if isinstance(d, dict) else str(d)
            if txt:
                cites.append({"type": "decision", "id": f"decisions[{i}]", "snippet": txt[:120]})
        for t in (todos or [])[:3]:
            txt = t.get("title") or t.get("text") or ""
            if txt:
                cites.append({"type": "todo", "id": t.get("_id") or "", "snippet": txt[:120]})
        return text, cites
    except Exception as e:
        logger.warning("[assistant_qna] freeform LLM failed: %s", e)
        return ("回答の生成中にエラーが発生しました。少し時間をおいて再度お試しください。", [])


# ──────────────────────────────────────────────────────────────────────
# Top-level entry point
# ──────────────────────────────────────────────────────────────────────

async def answer(
    *,
    question: str,
    session_id: str,
    owner_account_id: Optional[str],
) -> Dict[str, Any]:
    """Return the answer dict. Caller (route) supplies the resolved
    session_id and the account id of the requesting user.
    """
    intent = classify(question)
    if intent == "help":
        return {
            "intent": "help",
            "answer": "DeepNote Assistant は議事録について質問に答えます。\n例: 「決定事項は？」「田中さん担当のTODOは？」「要約して」",
            "citations": [],
            "tokenUsage": {"prompt": 0, "completion": 0},
        }
    if intent == "unknown":
        return {
            "intent": "unknown",
            "answer": "質問を入力してください。例: 「この会議の決定事項は？」",
            "citations": [],
            "tokenUsage": {"prompt": 0, "completion": 0},
        }

    sd = _fetch_session_doc(session_id) or {}
    if not sd:
        return {
            "intent": intent,
            "answer": "対象の会議が見つかりませんでした。",
            "citations": [],
            "tokenUsage": {"prompt": 0, "completion": 0},
        }
    sv2 = _fetch_summary_v2(session_id)

    decisions: List[Any] = []
    if sv2 and isinstance(sv2.get("json"), dict):
        decisions = sv2["json"].get("decisions") or []
    if not decisions and sd.get("summaryJson"):
        decisions = (sd["summaryJson"] or {}).get("decisions") or []

    todos = _fetch_todos(owner_account_id or "", session_id) if owner_account_id else []

    if intent == "ask_session_decision":
        text, cites = _format_decisions(decisions)
        return {"intent": intent, "answer": text, "citations": cites,
                "tokenUsage": {"prompt": 0, "completion": 0}}
    if intent == "ask_session_todo":
        assignee = _extract_assignee_filter(question)
        text, cites = _format_todos(todos, assignee_filter=assignee)
        return {"intent": intent, "answer": text, "citations": cites,
                "tokenUsage": {"prompt": 0, "completion": 0}}
    if intent == "ask_session_summary":
        text, cites = _format_summary(sd, sv2)
        return {"intent": intent, "answer": text, "citations": cites,
                "tokenUsage": {"prompt": 0, "completion": 0}}

    # Fallback: free-form, runs Gemini Flash Lite once.
    text, cites = await _answer_freeform(question, sd, sv2, decisions, todos)
    # We don't have token counts from the wrapper today; expose 0 so
    # callers can detect "LLM ran" via citations / latency profile.
    return {"intent": "ask_session_freeform", "answer": text, "citations": cites,
            "tokenUsage": {"prompt": 0, "completion": 0}}
