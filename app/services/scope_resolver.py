"""Scope resolver — determines which session(s) to use for AI chat."""

import logging
from typing import Optional

logger = logging.getLogger("app.services.scope_resolver")

def resolve_scope(message: str, current_session_id: Optional[str], ui_scope: str) -> dict:
    """Resolve the chat scope based on message content and context.

    Returns dict with:
      - mode: "session_grounded" | "session_plus_general" | "general_only"
      - session_ids: list of session IDs to use
      - auto_resolve: bool — if True, caller should auto-fetch recent sessions
      - todo_list: bool — if True, caller should fetch user's TODO list
    """
    # Keywords that indicate "look at my TODO list" (not session extraction)
    todo_list_keywords = [
        "未完了のTODO", "TODOを整理", "TODOリスト", "TODO一覧",
        "タスクを整理", "タスク一覧", "やることリスト",
        "TODOを確認", "TODO確認", "タスクを確認",
        "TODOの優先", "優先度の高いTODO", "期限が近いTODO",
        "TODOを教えて", "タスクを教えて",
    ]

    # Check for TODO list intent first
    todo_list = any(k in message for k in todo_list_keywords)
    # Also match standalone patterns like "TODOを整理して" or "TODOを見せて"
    if not todo_list and "TODO" in message:
        todo_verbs = ["整理", "確認", "教えて", "見せて", "一覧", "リスト", "優先"]
        todo_list = any(v in message for v in todo_verbs)

    # Keywords that strongly indicate "look at my session data"
    data_keywords = [
        # This session references
        "この会議", "この授業", "この講義", "この内容", "ここで", "今回",
        "このセッション", "この録音", "さっきの", "先ほどの",
        # Action/extraction requests — need session data
        "決定事項", "アクション", "要点", "要約",
        "抽出して", "まとめて", "リストアップ",
        # Session search hints
        "最近の会議", "最近の授業", "最近の講義", "最近のセッション",
        "会議で", "授業で", "講義で", "セッションで",
        "録音", "議事録",
    ]
    # Only treat "TODO" and "整理して" as session data keywords
    # when NOT in todo_list mode (avoid session extraction for TODO queries)
    if not todo_list:
        data_keywords.extend(["TODO", "整理して"])

    # Keywords that indicate pure general knowledge (no session data needed)
    general_only_keywords = [
        "一般的に", "そもそも", "とは何", "仕組み", "の違い",
    ]

    matched_data_kw = [k for k in data_keywords if k in message]
    matched_general_kw = [k for k in general_only_keywords if k in message]

    # --- TODO list mode (no session needed, fetch user's TODOs) ---
    if todo_list and not current_session_id:
        logger.info(
            f"[ScopeResolver] todo_list: matched TODO intent, "
            f"data_kw={matched_data_kw} general_kw={matched_general_kw}"
        )
        return {"mode": "general_only", "session_ids": [], "todo_list": True}

    # --- With session from detail screen ---
    if current_session_id and ui_scope == "session_detail":
        mode = "session_plus_general" if matched_general_kw else "session_grounded"
        logger.info(
            f"[ScopeResolver] session_detail: session={current_session_id} "
            f"mode={mode} data_kw={matched_data_kw} general_kw={matched_general_kw} todo_list={todo_list}"
        )
        return {"mode": mode, "session_ids": [current_session_id], "todo_list": todo_list}

    # --- With session from other context ---
    if current_session_id:
        if matched_general_kw and not matched_data_kw:
            mode = "session_plus_general"
        else:
            mode = "session_grounded" if matched_data_kw else "session_plus_general"
        logger.info(
            f"[ScopeResolver] with_session: session={current_session_id} "
            f"mode={mode} data_kw={matched_data_kw} general_kw={matched_general_kw} todo_list={todo_list}"
        )
        return {"mode": mode, "session_ids": [current_session_id], "todo_list": todo_list}

    # --- No session context ---
    # If data keywords matched, we need to auto-resolve recent sessions
    if matched_data_kw:
        logger.info(
            f"[ScopeResolver] auto_resolve: data_kw={matched_data_kw} "
            f"general_kw={matched_general_kw} → will fetch recent sessions"
        )
        return {"mode": "session_plus_general", "session_ids": [], "auto_resolve": True}

    # If only general keywords or no keywords, use general_only
    logger.info(
        f"[ScopeResolver] general_only: data_kw={matched_data_kw} general_kw={matched_general_kw}"
    )
    return {"mode": "general_only", "session_ids": []}
