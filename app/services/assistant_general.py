"""DeepNote Assistant — General mode (Phase B, env-flag gated).

Off by default. Turned on per-environment via ``ASSISTANT_GENERAL_MODE=on``.
The 1:1 LINE / Slack DM still routes ``ask_session_*`` queries to
``assistant_qna``; this module handles the rare "what's the weather like"
catch-all so DeepNote isn't confused with a generic chatbot.

Cost guard: each call is one Gemini Flash Lite request, no transcript
context, no fan-out. Throttled by the existing per-account
``cost_guard.guard_can_consume`` if the caller wants strict accounting
(Phase C will wire that in; Phase B keeps it informational).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger("app.services.assistant_general")


async def answer(*, question: str) -> Dict[str, Any]:
    if not question or not question.strip():
        return {"intent": "ask_general", "answer": "質問を入力してください。",
                "citations": [], "tokenUsage": {"prompt": 0, "completion": 0}}
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig
        from app.services import llm as _llm
        project_id = _llm._get_project_id()
        location = (
            os.environ.get("VERTEX_REGION")
            or os.environ.get("VERTEX_LOCATION")
            or "us-central1"
        )
        if project_id:
            vertexai.init(project=project_id, location=location)
        model_name = os.environ.get("ASSISTANT_GENERAL_MODEL") or "gemini-2.0-flash-lite"
        model = GenerativeModel(model_name)
        gen_cfg = GenerationConfig(temperature=0.2, max_output_tokens=512)
        prompt = (
            "あなたは DeepNote の汎用アシスタントです。日本語で簡潔に回答してください。"
            "DeepNote の議事録や会議に関する質問の場合は『DeepNote の議事録に関する具体的な"
            "質問は、対象の会議を選択してから再度お試しください』と案内してください。\n\n"
            f"質問: {question}\n\n回答:"
        )
        resp = await _llm._timed_llm_call(model, prompt, gen_cfg, label="assistant_general")
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            text = "回答を生成できませんでした。"
        return {"intent": "ask_general", "answer": text, "citations": [],
                "tokenUsage": {"prompt": 0, "completion": 0}}
    except Exception as e:
        logger.warning("[assistant_general] failed: %s", e)
        return {"intent": "ask_general_failed", "answer": "回答の生成中にエラーが発生しました。",
                "citations": [], "tokenUsage": {"prompt": 0, "completion": 0}}
