"""Gemini chat service — calls Vertex AI for AI chat responses."""

import json
import logging
import os
from typing import Optional

import google.auth
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

logger = logging.getLogger("app.services.gemini_chat")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "us-central1")
CHAT_MODEL_NAME = os.environ.get("CHAT_MODEL_NAME", "gemini-2.0-flash-lite")
GENERAL_MODEL_NAME = os.environ.get("GENERAL_MODEL_NAME", "gemini-2.5-flash-lite")

_vertex_initialized = False
_chat_model: Optional[GenerativeModel] = None
_general_model: Optional[GenerativeModel] = None

SYSTEM_INSTRUCTION = """あなたは DeepNote の会話型アシスタントです。
DeepNote は録音セッションの文字起こし・要約・TODO抽出を行うアプリです。

■ 会話スタイル:
- 人とチャットしているように自然で親しみやすく回答してください。
- ただし冗長にはせず、まず結論を短く伝え、その後に必要な補足を続けてください。
- 回答の構造は「結論 → 根拠の要約 → 次の自然な提案」の3層にしてください。
- 会話を続けやすいよう、follow_up_suggestion に次の提案を1つ必ず入れてください。
- 質問が曖昧な時は、無駄に確認を増やさず、最も妥当な解釈で答えたうえで別候補があれば軽く添えてください。

■ セッション文脈がある場合（chat_mode が session_grounded または session_plus_general）:
- セッション文脈を最優先して回答してください。
- セッション文脈に根拠がある内容と、一般知識として補う内容は分けて扱ってください。
- セッション内に存在しない内容を、あたかもセッションに書かれていたかのように断定しないでください。
- セッション由来の回答には、可能ならタイムスタンプや話者情報を添えてください。

■ セッション文脈がない場合（chat_mode が general_only）:
- 一般的な知識に基づいて回答してください。
- セッションへの言及は不要です。「セッションでは確認できません」のような表現は使わないでください。
- ユーザーの質問に直接的に、役立つ回答をしてください。
- 必要に応じて「関連する会議があれば探せます」と提案してください。

■ 会話サマリー:
- conversation_summary_next に、次のターンで使える短い会話サマリーを書いてください。
- これは内部用です。ユーザーの意図・話題・確認済みの内容を1〜2文でまとめてください。

■ ユーザーのTODOリストがある場合（[user_todos] セクション）:
- これはユーザーが登録済みのTODOリストです。セッションから新たに抽出するのではなく、このリストを参照して回答してください。
- 優先度（high/mid/low）、期限、出典セッションなどの情報を活用してください。
- 「未完了のTODOを整理して」等の質問には、優先度順に整理して簡潔にリストアップしてください。
- TODOの内容を要約・グループ化・優先順位付けするのは構いませんが、リストにないTODOを勝手に追加しないでください。

■ 共通:
- 回答は簡潔で実用的にし、可能なら箇条書きで整理してください。
- 日本語で回答してください。"""

GENERAL_SYSTEM_INSTRUCTION = """あなたは DeepNote の会話型アシスタントです。
DeepNote は録音セッションの文字起こし・要約・TODO抽出を行うアプリですが、
このモードではセッションに限定されず、あらゆる質問に自然に回答してください。

■ 会話スタイル:
- 人とチャットしているように自然で親しみやすく回答してください。
- まず結論を短く伝え、その後に必要な補足を続けてください。
- 回答の構造は「結論 → 根拠の要約 → 次の自然な提案」の3層にしてください。
- 会話を続けやすいよう、follow_up_suggestion に次の提案を1つ必ず入れてください。

■ 一般質問:
- ユーザーの質問に直接的に、役立つ回答をしてください。
- 最新の情報が必要な場合は、知っている範囲で回答し、情報が古い可能性があれば明記してください。
- 必要に応じて関連するセッション候補にも軽く触れてください。

■ セッション候補がある場合:
- scope_candidates に候補セッションがある場合は、関連する内容があれば補助的に参照してください。
- ただし一般チャットモードなので、セッション参照は必須ではありません。

■ ユーザーのTODOリストがある場合（[user_todos] セクション）:
- このリストを参照して回答してください。リストにないTODOを勝手に追加しないでください。

■ 会話サマリー:
- conversation_summary_next に、次のターンで使える短い会話サマリーを1〜2文で書いてください。

■ 共通:
- 回答は簡潔で実用的にし、可能なら箇条書きで整理してください。
- 日本語で回答してください。"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "mode": {"type": "string", "enum": ["session_grounded", "session_plus_general", "general_only"]},
        "used_sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["session_id", "title"],
            },
        },
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sec": {"type": "integer"},
                    "end_sec": {"type": "integer"},
                    "speaker": {"type": "string"},
                },
                "required": ["start_sec", "end_sec"],
            },
        },
        "confidence": {"type": "number"},
        "needs_general_knowledge": {"type": "boolean"},
        "follow_up_suggestion": {"type": "string"},
        "conversation_summary_next": {"type": "string"},
    },
    "required": [
        "answer", "mode", "used_sessions", "citations",
        "confidence", "needs_general_knowledge",
        "follow_up_suggestion", "conversation_summary_next",
    ],
}


def _ensure_chat_model():
    global _vertex_initialized, _chat_model, _general_model, PROJECT_ID
    if not _vertex_initialized:
        if not PROJECT_ID:
            try:
                _, PROJECT_ID = google.auth.default()
            except Exception as e:
                logger.warning(f"Failed to get project from ADC: {e}")

        if not PROJECT_ID:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")

        logger.info(f"[GeminiChat] Initializing: project={PROJECT_ID} region={VERTEX_REGION} model={CHAT_MODEL_NAME} general={GENERAL_MODEL_NAME}")
        vertexai.init(project=PROJECT_ID, location=VERTEX_REGION)
        _chat_model = GenerativeModel(
            CHAT_MODEL_NAME,
            system_instruction=SYSTEM_INSTRUCTION,
        )
        _general_model = GenerativeModel(
            GENERAL_MODEL_NAME,
            system_instruction=GENERAL_SYSTEM_INSTRUCTION,
        )
        _vertex_initialized = True
        logger.info("[GeminiChat] Models initialized successfully (session + general)")


def call_gemini_chat(turn_prompt: str) -> dict:
    """Call Gemini for a chat response. Returns structured JSON."""
    _ensure_chat_model()

    if _chat_model is None:
        raise RuntimeError("Chat model not initialized")

    config = GenerationConfig(
        temperature=0.4,
        top_p=0.9,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
    )

    prompt_len = len(turn_prompt)
    logger.info(f"[GeminiChat] Calling model={CHAT_MODEL_NAME} prompt_len={prompt_len} temp=0.4")

    try:
        response = _chat_model.generate_content(
            turn_prompt,
            generation_config=config,
        )

        raw_text = response.text if response else ""
        logger.debug(f"[GeminiChat] Raw response ({len(raw_text)} chars): {raw_text[:500]}")

        result = json.loads(raw_text)

        answer_len = len(result.get("answer", ""))
        used_count = len(result.get("used_sessions", []))
        citation_count = len(result.get("citations", []))
        follow_up = result.get("follow_up_suggestion", "")
        summary_next = result.get("conversation_summary_next", "")

        logger.info(
            f"[GeminiChat] Response OK: mode={result.get('mode')} confidence={result.get('confidence')} "
            f"answer_len={answer_len} used_sessions={used_count} citations={citation_count} "
            f"needs_general={result.get('needs_general_knowledge')} "
            f"follow_up=\"{follow_up[:60]}\" summary_next=\"{summary_next[:80]}\""
        )
        return result
    except json.JSONDecodeError as e:
        raw_preview = (response.text[:300] if response and response.text else "(empty)")
        logger.error(f"[GeminiChat] JSON parse failed: {e} raw_preview={raw_preview}")
        # Fallback: return raw text as answer
        return {
            "answer": response.text if response else "回答の生成に失敗しました。",
            "mode": "general_only",
            "used_sessions": [],
            "citations": [],
            "confidence": 0.0,
            "needs_general_knowledge": True,
            "follow_up_suggestion": "",
            "conversation_summary_next": "",
        }
    except Exception as e:
        logger.error(f"[GeminiChat] Call failed: {e}", exc_info=True)
        raise


def call_gemini_general_chat(turn_prompt: str) -> dict:
    """Call Gemini 2.5 Flash-Lite for general (non-session) chat. Returns structured JSON."""
    _ensure_chat_model()

    if _general_model is None:
        raise RuntimeError("General chat model not initialized")

    config = GenerationConfig(
        temperature=0.4,
        top_p=0.9,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
    )

    prompt_len = len(turn_prompt)
    logger.info(f"[GeminiChat] Calling GENERAL model={GENERAL_MODEL_NAME} prompt_len={prompt_len} temp=0.4")

    try:
        response = _general_model.generate_content(
            turn_prompt,
            generation_config=config,
        )

        raw_text = response.text if response else ""
        logger.debug(f"[GeminiChat/general] Raw response ({len(raw_text)} chars): {raw_text[:500]}")

        result = json.loads(raw_text)

        answer_len = len(result.get("answer", ""))
        logger.info(
            f"[GeminiChat/general] Response OK: mode={result.get('mode')} confidence={result.get('confidence')} "
            f"answer_len={answer_len} follow_up=\"{result.get('follow_up_suggestion', '')[:60]}\""
        )
        return result
    except json.JSONDecodeError as e:
        raw_preview = (response.text[:300] if response and response.text else "(empty)")
        logger.error(f"[GeminiChat/general] JSON parse failed: {e} raw_preview={raw_preview}")
        return {
            "answer": response.text if response else "回答の生成に失敗しました。",
            "mode": "general_only",
            "used_sessions": [],
            "citations": [],
            "confidence": 0.0,
            "needs_general_knowledge": True,
            "follow_up_suggestion": "",
            "conversation_summary_next": "",
        }
    except Exception as e:
        logger.error(f"[GeminiChat/general] Call failed: {e}", exc_info=True)
        raise
