"""Gemini chat service — calls Vertex AI for AI chat responses."""

import json
import logging
import os
import re
from typing import Generator, Optional

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

■ 回答言語:
- ユーザーの入力言語に合わせて回答してください。
- 日本語で質問された場合は、参照元が英語でも必ず日本語で回答してください。
- 英語で質問された場合は英語で回答してください。

■ 出力形式:
- answer フィールドでは Markdown 記法を使わないでください。アスタリスク（*）やシャープ（#）は出力しないでください。
- 太字記法（**text**）や見出し記法（# ）は使わないでください。
- 見出しは短い自然な日本語で書き、空行で区切ってください。
- 箇条書きが必要な場合は「・」を使ってください。「- 」や「* 」は使わないでください。
- 順序付きのリスト（ランキング、手順、優先度順など）では「1. 」「2. 」「3. 」のように番号を付けてください。
- 強調のための記号は使わず、読みやすい改行で構成してください。

■ 共通:
- 回答は簡潔で実用的にしてください。"""

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

■ 回答言語:
- ユーザーの入力言語に合わせて回答してください。
- 日本語で質問された場合は、参照元が英語でも必ず日本語で回答してください。
- 英語で質問された場合は英語で回答してください。

■ 出力形式:
- answer フィールドでは Markdown 記法を使わないでください。アスタリスク（*）やシャープ（#）は出力しないでください。
- 箇条書きが必要な場合は「・」を使ってください。
- 順序付きのリスト（ランキング、手順、優先度順など）では「1. 」「2. 」「3. 」のように番号を付けてください。
- 見出しは短い自然な日本語で書き、空行で区切ってください。

■ 共通:
- 回答は簡潔で実用的にしてください。"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "mode": {"type": "string", "enum": ["session_grounded", "session_plus_general", "general_static", "general_fresh"]},
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

        # Extract token usage
        usage = getattr(response, "usage_metadata", None)
        if usage:
            result["_input_tokens"] = getattr(usage, "prompt_token_count", None)
            result["_output_tokens"] = getattr(usage, "candidates_token_count", None)

        logger.info(
            f"[GeminiChat] Response OK: mode={result.get('mode')} confidence={result.get('confidence')} "
            f"answer_len={answer_len} used_sessions={used_count} citations={citation_count} "
            f"needs_general={result.get('needs_general_knowledge')} "
            f"input_tokens={result.get('_input_tokens')} output_tokens={result.get('_output_tokens')} "
            f"follow_up=\"{follow_up[:60]}\" summary_next=\"{summary_next[:80]}\""
        )
        return result
    except json.JSONDecodeError as e:
        raw_preview = (response.text[:300] if response and response.text else "(empty)")
        logger.error(f"[GeminiChat] JSON parse failed: {e} raw_preview={raw_preview}")
        # Fallback: return raw text as answer
        return {
            "answer": response.text if response else "回答の生成に失敗しました。",
            "mode": "general_static",
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

        # Extract token usage
        usage = getattr(response, "usage_metadata", None)
        if usage:
            result["_input_tokens"] = getattr(usage, "prompt_token_count", None)
            result["_output_tokens"] = getattr(usage, "candidates_token_count", None)

        logger.info(
            f"[GeminiChat/general] Response OK: mode={result.get('mode')} confidence={result.get('confidence')} "
            f"answer_len={answer_len} "
            f"input_tokens={result.get('_input_tokens')} output_tokens={result.get('_output_tokens')} "
            f"follow_up=\"{result.get('follow_up_suggestion', '')[:60]}\""
        )
        return result
    except json.JSONDecodeError as e:
        raw_preview = (response.text[:300] if response and response.text else "(empty)")
        logger.error(f"[GeminiChat/general] JSON parse failed: {e} raw_preview={raw_preview}")
        return {
            "answer": response.text if response else "回答の生成に失敗しました。",
            "mode": "general_static",
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


# ---------------------------------------------------------------------------
# Google Search grounding (for fresh/latest-info questions)
# ---------------------------------------------------------------------------

GENERAL_FRESH_SYSTEM_INSTRUCTION = """あなたは DeepNote の会話型アシスタントです。
最新の公開情報を確認しながら回答してください。

■ 会話スタイル:
- 自然で親しみやすく回答してください。
- まず結論を短く伝え、その後に必要な補足を続けてください。
- 断定できない場合は、その旨を自然に伝えてください。

■ 回答言語:
- ユーザーの入力言語に合わせて回答してください。
- 日本語で質問された場合は、検索結果が英語でも必ず日本語で回答してください。
- 検索ソースの言語と回答言語は分離してください。ソースが英語でも回答は日本語です。

■ 出力形式:
- Markdown記法は使わないでください。アスタリスク（*）やシャープ（#）は出力しないでください。
- 箇条書きが必要な場合は「・」を使ってください。
- 順序付きのリスト（ランキング、手順、優先度順など）では「1. 」「2. 」「3. 」のように番号を付けてください。
- 見出しは短い自然な日本語で書き、空行で区切ってください。

■ 共通:
- 回答は簡潔で実用的にしてください。"""

FRESHNESS_KEYWORDS = [
    "最新", "今", "現在", "今日", "最近", "ニュース",
    "動向", "支持率", "CEO", "社長", "株価", "発表",
    "アップデート", "現職", "価格", "速報", "リリース",
    "いつ", "何時", "天気", "為替", "レート",
]


def detect_answer_language(text: str) -> str:
    """Detect user's language from input text. Returns 'ja' or 'en'."""
    if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text):
        return "ja"
    return "en"


def is_fresh_question(message: str) -> bool:
    """Detect questions that need latest/real-time information."""
    lower = message.lower()
    return any(k.lower() in lower for k in FRESHNESS_KEYWORDS)


def call_gemini_general_with_search(
    message: str,
    history: Optional[list] = None,
    conversation_summary: Optional[str] = None,
) -> dict:
    """Call Gemini 2.5 Flash-Lite with Google Search grounding.

    Uses Vertex AI grounding tool for real-time web information.
    Cannot use JSON response schema with tools, so returns a simplified dict.
    Includes conversation history for multi-turn continuity.
    """
    _ensure_chat_model()

    from google.cloud.aiplatform_v1beta1.types.tool import Tool as ProtoTool
    from vertexai.generative_models import GenerativeModel

    # Use proto-level Tool with google_search field (not deprecated google_search_retrieval)
    search_tool = ProtoTool(google_search=ProtoTool.GoogleSearch())

    model = GenerativeModel(
        GENERAL_MODEL_NAME,
        system_instruction=GENERAL_FRESH_SYSTEM_INSTRUCTION,
        tools=[search_tool],
    )

    # Google recommends temperature=1.0 for grounding
    config = GenerationConfig(temperature=1.0)

    # Detect answer language from user input
    answer_lang = detect_answer_language(message)
    answer_lang_label = "日本語" if answer_lang == "ja" else "English"

    # Build prompt with conversation context
    parts = []
    parts.append(f"[answer_language]\n{answer_lang_label}\n")
    if conversation_summary:
        parts.append(f"[会話サマリー]\n{conversation_summary}\n")
    if history:
        parts.append("[会話履歴]")
        for turn in history[-8:]:
            role_label = "ユーザー" if turn.get("role") == "user" else "アシスタント"
            parts.append(f"{role_label}: {turn.get('text', '')}")
        parts.append("")
    parts.append(f"[ユーザーの質問]\n{message}")
    parts.append(f"\n[output_rule]\n最終回答は必ず{answer_lang_label}で返してください。検索結果が他の言語でも、回答は{answer_lang_label}で出力してください。")
    full_prompt = "\n".join(parts)

    prompt_len = len(full_prompt)
    logger.info(
        f"[GeminiChat] Calling GENERAL+SEARCH model={GENERAL_MODEL_NAME} "
        f"prompt_len={prompt_len} temp=1.0 grounding=GoogleSearch history={len(history or [])}"
    )

    try:
        response = model.generate_content(full_prompt, generation_config=config)

        # Extract text — grounding responses may have multiple content parts
        answer = ""
        if response and response.candidates:
            candidate = response.candidates[0]
            if candidate.content and candidate.content.parts:
                text_parts = []
                for part in candidate.content.parts:
                    if hasattr(part, "text") and part.text:
                        text_parts.append(part.text)
                answer = "".join(text_parts)

        # Extract token usage
        usage = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
        output_tokens = getattr(usage, "candidates_token_count", None) if usage else None

        logger.info(
            f"[GeminiChat/search] Response OK: answer_len={len(answer)} "
            f"parts={len(response.candidates[0].content.parts) if response and response.candidates else 0} "
            f"input_tokens={input_tokens} output_tokens={output_tokens} "
            f"grounding_metadata={bool(getattr(response, 'grounding_metadata', None))}"
        )

        return {
            "answer": answer or "回答の生成に失敗しました。",
            "mode": "general_static",
            "used_sessions": [],
            "citations": [],
            "confidence": 0.7,
            "needs_general_knowledge": True,
            "follow_up_suggestion": "他に気になることはありますか？",
            "conversation_summary_next": f"ユーザーが最新情報について質問。Google検索で回答。",
            "used_search": True,
            "_input_tokens": input_tokens,
            "_output_tokens": output_tokens,
        }
    except Exception as e:
        logger.error(f"[GeminiChat/search] Call failed: {e}", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Streaming variants (SSE)
# ---------------------------------------------------------------------------

STREAM_SYSTEM_INSTRUCTION = """あなたは DeepNote の会話型アシスタントです。
DeepNote は録音セッションの文字起こし・要約・TODO抽出を行うアプリです。

■ 会話スタイル:
- 人とチャットしているように自然で親しみやすく回答してください。
- まず結論を短く伝え、その後に必要な補足を続けてください。
- 質問が曖昧な時は最も妥当な解釈で答えてください。

■ セッション文脈がある場合:
- セッション文脈を最優先して回答してください。
- セッション内に存在しない内容を断定しないでください。

■ セッション文脈がない場合:
- 一般的な知識に基づいて回答してください。
- 「セッションでは確認できません」のような表現は使わないでください。

■ 回答言語:
- ユーザーの入力言語に合わせて回答してください。
- 日本語で質問された場合は、参照元が英語でも必ず日本語で回答してください。

■ 出力形式:
- Markdown記法は使わないでください。アスタリスク（*）やシャープ（#）は出力しないでください。
- 箇条書きが必要な場合は「・」を使ってください。
- 順序付きのリスト（ランキング、手順、優先度順など）では「1. 」「2. 」「3. 」のように番号を付けてください。
- 見出しは短い自然な日本語で書き、空行で区切ってください。
- JSON形式では返さないでください。回答本文のみを出力してください。"""


def stream_gemini_chat(turn_prompt: str, model_name: Optional[str] = None) -> Generator[str, None, None]:
    """Stream Gemini response as text chunks.

    Args:
        turn_prompt: The full prompt to send.
        model_name: Model ID to use. Defaults to GENERAL_MODEL_NAME.
    """
    _ensure_chat_model()

    model_name = model_name or GENERAL_MODEL_NAME
    model = GenerativeModel(
        model_name,
        system_instruction=STREAM_SYSTEM_INSTRUCTION,
    )

    config = GenerationConfig(
        temperature=0.4,
        top_p=0.9,
    )

    logger.info(f"[GeminiChat/stream] model={model_name} mode={mode} prompt_len={len(turn_prompt)}")

    try:
        response_stream = model.generate_content(
            turn_prompt,
            generation_config=config,
            stream=True,
        )
        for chunk in response_stream:
            text = chunk.text if hasattr(chunk, "text") and chunk.text else ""
            if text:
                yield text
    except Exception as e:
        logger.error(f"[GeminiChat/stream] Failed: {e}", exc_info=True)
        raise


def stream_gemini_with_search(
    prompt: str,
) -> Generator[str, None, None]:
    """Stream Gemini with Google Search grounding.

    Args:
        prompt: Pre-built prompt (from build_stream_prompt).
    """
    _ensure_chat_model()

    from google.cloud.aiplatform_v1beta1.types.tool import Tool as ProtoTool

    search_tool = ProtoTool(google_search=ProtoTool.GoogleSearch())

    model = GenerativeModel(
        GENERAL_MODEL_NAME,
        system_instruction=GENERAL_FRESH_SYSTEM_INSTRUCTION,
        tools=[search_tool],
    )

    config = GenerationConfig(temperature=1.0)

    logger.info(f"[GeminiChat/stream+search] model={GENERAL_MODEL_NAME} prompt_len={len(prompt)}")

    try:
        response_stream = model.generate_content(
            prompt,
            generation_config=config,
            stream=True,
        )
        for chunk in response_stream:
            if chunk.candidates:
                candidate = chunk.candidates[0]
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, "text") and part.text:
                            yield part.text
    except Exception as e:
        logger.error(f"[GeminiChat/stream+search] Failed: {e}", exc_info=True)
        raise
