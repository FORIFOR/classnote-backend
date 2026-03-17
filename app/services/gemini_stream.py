"""Gemini streaming service — yields text chunks from Vertex AI."""

import logging
import os
import re
from typing import Generator, Optional

import google.auth
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

logger = logging.getLogger("app.services.gemini_stream")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "us-central1")
CHAT_MODEL_NAME = os.environ.get("CHAT_MODEL_NAME", "gemini-2.0-flash-lite")
GENERAL_MODEL_NAME = os.environ.get("GENERAL_MODEL_NAME", "gemini-2.5-flash-lite")

_vertex_initialized = False

# ── System instructions (plain text output, no JSON) ──

STREAM_SYSTEM_INSTRUCTION = """あなたは DeepNote の会話型アシスタントです。
DeepNote は録音セッションの文字起こし・要約・TODO抽出を行うアプリです。

■ 会話スタイル:
- 人とチャットしているように自然で親しみやすく回答してください。
- まず結論を短く伝え、その後に必要な補足を続けてください。
- 質問が曖昧な時は、最も妥当な解釈で答えたうえで別候補があれば軽く添えてください。

■ セッション文脈がある場合:
- セッション文脈を最優先して回答してください。
- セッション内に存在しない内容を、あたかもセッションに書かれていたかのように断定しないでください。
- セッション由来の回答には、可能ならタイムスタンプや話者情報を添えてください。

■ セッション文脈がない場合:
- 一般的な知識に基づいて回答してください。
- 「セッションでは確認できません」のような表現は使わないでください。
- ユーザーの質問に直接的に、役立つ回答をしてください。

■ ユーザーのTODOリストがある場合:
- このリストを参照して回答してください。リストにないTODOを勝手に追加しないでください。

■ 回答言語:
- ユーザーの入力言語に合わせて回答してください。
- 日本語で質問された場合は必ず日本語で回答してください。

■ 出力形式:
- Markdown記法は使わないでください。アスタリスク（*）やシャープ（#）は出力しないでください。
- 太字記法（**text**）や見出し記法（# ）は絶対に使わないでください。
- 見出しは短い自然な日本語で書き、空行で区切ってください。
- 箇条書きが必要な場合は「・」を使ってください。「- 」や「* 」は使わないでください。
- 順序付きのリスト（ランキング、手順、優先度順など）では「1. 」「2. 」「3. 」のように番号を付けてください。
- 強調のための記号は使わず、読みやすい改行で構成してください。
- 回答はそのままアプリで表示される前提で、整った文章として返してください。
- JSON形式では返さないでください。回答本文のみを出力してください。"""

STREAM_FRESH_SYSTEM_INSTRUCTION = """あなたは DeepNote の会話型アシスタントです。
最新の公開情報を確認しながら回答してください。

■ 会話スタイル:
- 自然で親しみやすく回答してください。
- まず結論を短く伝え、その後に必要な補足を続けてください。
- 断定できない場合は、その旨を自然に伝えてください。

■ 回答言語:
- ユーザーの入力言語に合わせて回答してください。
- 日本語で質問された場合は、検索結果が英語でも必ず日本語で回答してください。

■ 出力形式:
- Markdown記法は使わないでください。アスタリスク（*）やシャープ（#）は出力しないでください。
- 箇条書きが必要な場合は「・」を使ってください。
- 順序付きのリスト（ランキング、手順、優先度順など）では「1. 」「2. 」「3. 」のように番号を付けてください。
- 見出しは短い自然な日本語で書き、空行で区切ってください。
- JSON形式では返さないでください。"""


def _ensure_initialized():
    global _vertex_initialized, PROJECT_ID
    if _vertex_initialized:
        return
    if not PROJECT_ID:
        try:
            _, PROJECT_ID = google.auth.default()
        except Exception as e:
            logger.warning(f"Failed to get project from ADC: {e}")
    if not PROJECT_ID:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")
    vertexai.init(project=PROJECT_ID, location=VERTEX_REGION)
    _vertex_initialized = True
    logger.info(f"[GeminiStream] Initialized: project={PROJECT_ID} region={VERTEX_REGION}")


def stream_gemini_chat(turn_prompt: str, model_name: str = None) -> Generator[str, None, None]:
    """Stream text chunks from Gemini (session-grounded or general).

    Yields plain text fragments as they arrive.
    """
    _ensure_initialized()
    model_name = model_name or CHAT_MODEL_NAME

    model = GenerativeModel(
        model_name,
        system_instruction=STREAM_SYSTEM_INSTRUCTION,
    )
    config = GenerationConfig(temperature=0.4, top_p=0.9)

    logger.info(f"[GeminiStream] Streaming model={model_name} prompt_len={len(turn_prompt)}")

    response = model.generate_content(turn_prompt, generation_config=config, stream=True)

    for chunk in response:
        text = chunk.text if hasattr(chunk, "text") and chunk.text else ""
        if text:
            yield text


def stream_gemini_with_search(turn_prompt: str) -> Generator[str, None, None]:
    """Stream text chunks from Gemini with Google Search grounding."""
    _ensure_initialized()

    from google.cloud.aiplatform_v1beta1.types.tool import Tool as ProtoTool

    search_tool = ProtoTool(google_search=ProtoTool.GoogleSearch())

    model = GenerativeModel(
        GENERAL_MODEL_NAME,
        system_instruction=STREAM_FRESH_SYSTEM_INSTRUCTION,
        tools=[search_tool],
    )
    config = GenerationConfig(temperature=1.0)

    logger.info(f"[GeminiStream] Streaming SEARCH model={GENERAL_MODEL_NAME} prompt_len={len(turn_prompt)}")

    response = model.generate_content(turn_prompt, generation_config=config, stream=True)

    for chunk in response:
        if chunk.candidates:
            candidate = chunk.candidates[0]
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if hasattr(part, "text") and part.text:
                        yield part.text
