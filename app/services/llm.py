import os
import asyncio
import json
import math
import random
import time
from typing import Any, Dict, List, Optional

from app.services.profiling import get_profiler, Phase, PROFILING_ENABLED

# Lazy import for vertexai to prevent build/startup crashes if credentials/deps are missing
# import vertexai
# from vertexai.generative_models import GenerativeModel, GenerationConfig

# ── LLM Logging ─────────────────────────────────────────────────
# Sample rate for success logs (0.0=off, 1.0=100%). Error logs are always emitted.
# Priority: Firestore experiments.llmLogSampleRate > env LLM_LOG_SAMPLE_RATE > 1.0
_LLM_LOG_SAMPLE_RATE_ENV = float(os.environ.get("LLM_LOG_SAMPLE_RATE", "1.0"))


def _get_llm_log_sample_rate() -> float:
    """Get current LLM log sample rate. Firestore config overrides env var."""
    try:
        from app.services.app_config import get_experiment_value
        rate = get_experiment_value("llmLogSampleRate")
        if rate is not None:
            return float(rate)
    except Exception:
        pass
    return _LLM_LOG_SAMPLE_RATE_ENV


async def _timed_llm_call(model, prompt, generation_config, label: str = "llm"):
    """
    Wrapper to time LLM calls, emit structured logs, and record to profiler.
    - Error logs: ALWAYS emitted
    - Success logs: emitted at LLM_LOG_SAMPLE_RATE (default 1.0 = 100%)
    - No PII/prompt content logged — only token counts, latency, status
    """
    start = time.perf_counter()
    status = "success"
    response = None
    error_msg = None

    try:
        response = await model.generate_content_async(prompt, generation_config=generation_config)
        return response
    except Exception as e:
        status = "error"
        error_msg = str(e)[:200]
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        input_chars = len(prompt) if isinstance(prompt, str) else 0

        # Extract token counts from response usage_metadata if available
        input_tokens = 0
        output_tokens = 0
        output_chars = 0
        if response:
            output_chars = len(response.text) if response.text else 0
            usage = getattr(response, "usage_metadata", None)
            if usage:
                input_tokens = getattr(usage, "prompt_token_count", 0) or 0
                output_tokens = getattr(usage, "candidates_token_count", 0) or 0

        log_data = {
            "feature": label,
            "model": GEMINI_MODEL_NAME,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "latency_ms": round(duration_ms),
            "status": status,
        }
        if error_msg:
            log_data["error"] = error_msg

        # Error logs are always emitted; success logs are sampled
        if status == "error" or random.random() < _get_llm_log_sample_rate():
            logger.info("llm_call", extra=log_data)

        # Profiler recording (if enabled)
        if PROFILING_ENABLED:
            profiler = get_profiler()
            if profiler:
                profiler.record_phase(Phase.LLM_REQUEST, duration_ms, label=label, prompt_tokens=input_tokens or input_chars // 4)

# [FIX] Use ADC (Application Default Credentials) to get project_id reliably
# This works in Cloud Run without requiring env vars
def _get_project_id() -> str:
    """Get project ID from env vars or ADC."""
    from_env = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if from_env:
        return from_env
    try:
        import google.auth
        _, project = google.auth.default()
        return project
    except Exception:
        return None

PROJECT_ID = _get_project_id()

# [FIX] Gemini 2.0 Flash/Flash-Lite is NOT available in asia-northeast1
# Supported regions: us-central1, europe-west1, etc.
# Use "us-central1" as default (verified working)
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION") or os.environ.get("VERTEX_REGION", "us-central1")

# デフォルトは地域で利用可能性の高い新しい ID を優先
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.0-flash-lite")

import re
import logging

logger = logging.getLogger(__name__)

# Constants for transcript validation
MIN_TRANSCRIPT_LENGTH = 10  # [FIX] Lowered to 10 as requested
MAX_TRANSCRIPT_LENGTH = 100000  # Maximum to prevent excessive token usage
CHUNK_SIZE = 80000  # Chunk size for long transcripts (with overlap margin)
CHUNK_OVERLAP = 2000  # Overlap between chunks to preserve context
SUMMARY_JSON_VERSION = 2

# ── Hierarchical Map→Reduce tuning ──────────────────────────────
MAP_CHUNK_SIZE = 18000       # Larger chunks → fewer map calls per transcript
MAP_CHUNK_OVERLAP = 300      # Tighter overlap for extraction phase
HIERARCHICAL_THRESHOLD = 20000  # Use hierarchical for >20K chars (was 80K=CHUNK_SIZE)
MAX_CONCURRENT_MAP = 5       # Semaphore: max parallel LLM calls to avoid rate limits
REDUCE_BATCH_LIMIT = 40000   # Max chars of extracted data for single reduce

# ── Progressive summary/quiz constants ────────────────────────
QUICK_SUMMARY_MAX_TOKENS = 800   # Quick summary: small output for speed
FACTS_MAP_MAX_TOKENS = 1500      # Facts extraction: richer extraction per chunk
QUIZ_BATCH_SIZE = 2              # Quiz questions per batch (legacy)
QUIZ_BATCH_MAX_TOKENS = 600      # Tokens per 2-question batch (legacy)
QUIZ_SINGLE_MAX_TOKENS = 4096   # Single-shot 8-question JSON output


def _should_use_hierarchical(text_len: int) -> bool:
    """Use hierarchical Map→Reduce only when transcript produces 3+ chunks."""
    return math.ceil(text_len / MAP_CHUNK_SIZE) >= 3


# ── response_schema definitions (Structured Output) ──────────────
# NOTE: required は最小限に — 多いとモデルが「穴埋め」に寄り内容が薄くなる
_SCHEMA_MEETING_SUMMARY = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "bottomLine": {"type": "string"},
                "outcomeStatus": {"type": "string"},
                "whyItMatters": {"type": "string"},
                "highlights": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "category": {"type": "string"}, "needConfirm": {"type": "boolean"}}}},
                "decisions": {"type": "array", "items": {"type": "object", "properties": {
                    "text": {"type": "string"}, "owner": {"type": "string"}, "ownerSource": {"type": "string"},
                    "due": {"type": "string"}, "dueSource": {"type": "string"},
                    "status": {"type": "string"}, "reason": {"type": "string"},
                    "confidence": {"type": "number"}, "evidenceHint": {"type": "string"},
                    "needConfirm": {"type": "boolean"},
                }}},
                "todos": {"type": "array", "items": {"type": "object", "properties": {
                    "text": {"type": "string"}, "owner": {"type": "string"}, "ownerSource": {"type": "string"},
                    "due": {"type": "string"}, "dueSource": {"type": "string"},
                    "priority": {"type": "string"}, "blocking": {"type": "string"},
                    "confidence": {"type": "number"}, "evidenceHint": {"type": "string"},
                    "needConfirm": {"type": "boolean"},
                }}},
                "openQuestions": {"type": "array", "items": {"type": "object", "properties": {
                    "text": {"type": "string"}, "impact": {"type": "string"},
                    "whyOpen": {"type": "string"}, "owner": {"type": "string"}, "nextCheck": {"type": "string"},
                    "needConfirm": {"type": "boolean"},
                }}},
                "decisionLog": {"type": "array", "items": {"type": "object", "properties": {
                    "topic": {"type": "string"}, "conclusion": {"type": "string"},
                    "reason": {"type": "string"}, "remainingIssues": {"type": "string"},
                }}},
                "contextNotes": {"type": "array", "items": {"type": "object", "properties": {
                    "topic": {"type": "string"}, "summary": {"type": "string"},
                }}},
                "keywords": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}}}},
                "participants": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}}}},
                # Phase 7.9: natural-language highlight cards for the Summary tab.
                # Complementary to `highlights` (terse category-tagged bullets).
                # Each entry is one readable sentence with a single primary timestamp.
                "conversationHighlights": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "topic": {"type": "string"},
                    "importance": {"type": "string"},  # "high" | "medium" | "low"
                    "evidenceHint": {"type": "string"},
                    "primaryTimestampSec": {"type": "number"},
                }}},
            },
            "required": ["type", "highlights", "bottomLine"],
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "suggestedTitle": {"type": "string"},
    },
    "required": ["summary", "tags", "suggestedTitle"],
}

_SCHEMA_LECTURE_SUMMARY = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "highlights": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "needConfirm": {"type": "boolean"}}}},
                "overview": {"type": "string"},
                "theme": {"type": "object", "properties": {"text": {"type": "string"}, "needConfirm": {"type": "boolean"}}},
                "terms": {"type": "array", "items": {"type": "object", "properties": {"term": {"type": "string"}, "definition": {"type": "string"}, "examples": {"type": "array", "items": {"type": "string"}}, "needConfirm": {"type": "boolean"}}}},
                "sections": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}}, "commonMistakes": {"type": "array", "items": {"type": "string"}}, "needConfirm": {"type": "boolean"}}}},
                "formulasOrProcedures": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}, "needConfirm": {"type": "boolean"}}}},
                "keywords": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}}}},
                # Phase 7.9: lecture-friendly natural-language highlights
                # (e.g. "先生が〜と強調した / 学生が〜と質問した" と読める1文ずつ)
                "conversationHighlights": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "topic": {"type": "string"},
                    "importance": {"type": "string"},
                    "evidenceHint": {"type": "string"},
                    "primaryTimestampSec": {"type": "number"},
                }}},
            },
            "required": ["type", "highlights", "overview"],
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "suggestedTitle": {"type": "string"},
    },
    "required": ["summary", "tags", "suggestedTitle"],
}

_SCHEMA_MAP_FACTS = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {"type": "object", "properties": {"type": {"type": "string"}, "text": {"type": "string"}, "evidenceHint": {"type": "string"}}}},
        "terms": {"type": "array", "items": {"type": "object", "properties": {"term": {"type": "string"}, "definition": {"type": "string"}}}},
    },
    "required": ["facts"],
}

_SCHEMA_PLAYLIST = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "startSec": {"type": "number"},
            "endSec": {"type": "number"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["startSec", "endSec", "title"],
    },
}


def _get_summary_schema(mode: str) -> dict:
    return _SCHEMA_MEETING_SUMMARY if mode == "meeting" else _SCHEMA_LECTURE_SUMMARY


def _clean_json_response(raw: str) -> str:
    """
    LLMレスポンスからJSONを抽出・クリーンアップする。
    - コードフェンス(```json ... ```)を除去
    - 先頭の余計な文言を除去
    - 末尾の余計な文言を除去
    """
    text = raw.strip()

    # Remove code fences
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.rfind("```")
        if end > start:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.rfind("```")
        if end > start:
            text = text[start:end].strip()

    # Find first { and last }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        text = text[first_brace:last_brace + 1]

    return text


def _parse_json_with_retry(raw: str, max_retries: int = 2) -> dict:
    """
    JSONパースを試行し、失敗時はクリーンアップしてリトライする。
    """
    text = raw.strip()

    # Attempt 1: Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt 2: Clean and retry
    cleaned = _clean_json_response(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse failed after cleanup: {e}. Raw length={len(raw)}")

    # Attempt 3: More aggressive cleanup (remove trailing commas, fix quotes)
    try:
        # Remove trailing commas before } or ]
        fixed = re.sub(r',\s*([}\]])', r'\1', cleaned)
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed after all retries: {e}")
        return {}


def clean_quiz_markdown(raw: str) -> str:
    # 1. 先頭の「はい、承知いたしました」などを全部捨てて
    #    最初の "### Q" から始める
    lines = raw.splitlines()
    start_idx = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("### Q"):
            start_idx = i
            break
    cleaned = "\n".join(lines[start_idx:]).strip()

    # 2. 「1. 質問:」のような番号行が紛れていたら削る
    cleaned = re.sub(r"^\s*\d+\.\s*質問[:：].*$\n?", "", cleaned, flags=re.MULTILINE)

    return cleaned


_vertex_initialized = False
_model: Any = None


def _ensure_model():
    global _vertex_initialized, _model
    if _vertex_initialized and _model:
        return

    # Lazy import
    import vertexai
    import google.auth
    from vertexai.generative_models import GenerativeModel

    # [FIX] Use ADC to get project_id and credentials reliably
    # This works in Cloud Run without requiring env vars
    project_id = PROJECT_ID
    creds = None

    if not project_id:
        try:
            creds, project_id = google.auth.default()
            logger.info(f"[LLM] Using ADC project: {project_id}")
        except Exception as e:
            logger.error(f"[LLM] Failed to get credentials: {e}")
            raise RuntimeError("Failed to get project_id from env or ADC for Vertex AI")

    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT/GCP_PROJECT is not set and ADC failed for Vertex AI")

    # [FIX] Use VERTEX_LOCATION (defaults to "global" for Gemini 2.0 availability)
    location = VERTEX_LOCATION
    logger.info(f"[LLM] Initializing Vertex AI: project={project_id}, location={location}")

    if creds:
        vertexai.init(project=project_id, location=location, credentials=creds)
    else:
        vertexai.init(project=project_id, location=location)

    # モデル名のフォールバックリスト（環境変数が優先）
    # 2.0 系のみを使用
    candidates = [GEMINI_MODEL_NAME, "gemini-2.0-flash"]
    last_err = None
    for name in candidates:
        if not name:
            continue
        try:
            _model = GenerativeModel(name)
            _vertex_initialized = True
            logger.info(f"[LLM] Model initialized: {name}")
            return
        except Exception as e:
            logger.warning(f"[LLM] Failed to init model {name}: {e}")
            last_err = e
            continue
    # ここまで来たら初期化失敗
    raise RuntimeError(f"Failed to initialize Gemini model. Tried: {candidates}") from last_err


async def summarize_transcript(text: str, mode: str = "lecture") -> str:
    """
    Transcript を Vertex AI (Gemini) で要約する。
    """
    _ensure_model()
    from vertexai.generative_models import GenerationConfig

    prompt = _build_summary_prompt(text, mode)
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.6,
            max_output_tokens=4096,
        ),
        label="summarize",
    )
    return (resp.text or "").strip()


async def generate_quiz(text: str, mode: str = "lecture", count: int = 8, custom_instruction: str = "") -> str:
    """
    クイズを生成する。Markdown形式の出力を期待。
    """
    # Transcript length validation
    if len(text) < MIN_TRANSCRIPT_LENGTH:
        logger.warning(f"Transcript too short for quiz: {len(text)} chars")
        return "### 注意\n\n文字起こしの内容が短すぎるため、クイズを生成できませんでした。"

    if len(text) > MAX_TRANSCRIPT_LENGTH:
        logger.warning(f"Transcript truncated for quiz: {len(text)} -> {MAX_TRANSCRIPT_LENGTH} chars")
        text = text[:MAX_TRANSCRIPT_LENGTH]

    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    prompt = _build_quiz_prompt(text, mode, count)
    if custom_instruction:
        prompt += f"\n\n# ユーザーからの追加指示\n{custom_instruction}\n"
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.5,
            max_output_tokens=2048,
        ),
        label="quiz",
    )
    return (resp.text or "").strip()


# ── Quick Summary ─────────────────────────────────────────────

async def generate_quick_summary(text: str, mode: str = "lecture") -> dict:
    """
    30-60秒で返せる短い要約。highlights 3件 + topicSummary + keywords。
    CostGuard消費なし（Full成功時にまとめて消費）。
    """
    if len(text) < MIN_TRANSCRIPT_LENGTH:
        return {"markdown": "文字起こしが短すぎます。", "topicSummary": ""}

    # 長いテキストは先頭だけ使う（速度重視）
    truncated = text[:15000] if len(text) > 15000 else text

    _ensure_model()
    from vertexai.generative_models import GenerationConfig

    prompt = _build_quick_summary_prompt(truncated, mode)
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.3,
            max_output_tokens=QUICK_SUMMARY_MAX_TOKENS,
        ),
        label="summary_quick",
    )
    raw = (resp.text or "").strip()

    # Markdownをそのまま返す（JSON解析不要で壊れにくい）
    # 最初の非見出し行をtopicSummaryに
    topic = ""
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            topic = stripped[:100]
            break

    return {"markdown": raw, "topicSummary": topic}


def _build_quick_summary_prompt(text: str, mode: str) -> str:
    if mode == "meeting":
        return f"""以下の文字起こし（会議）を30秒で読める短い要約にしてください。

# 出力ルール（厳守）
- Markdownのみ出力（余計な挨拶・説明禁止）
- 最初に1行で会議のテーマを書く
- 「## 重要ポイント」で3点以内の箇条書き
- 「## キーワード」で重要語を3-6個
- 文字起こしに無い情報は作らない

=== 文字起こし ===
{text}
"""
    return f"""以下の文字起こし（講義）を30秒で読める短い要約にしてください。

# 出力ルール（厳守）
- Markdownのみ出力（余計な挨拶・説明禁止）
- 最初に1行で講義のテーマを書く
- 「## 重要ポイント」で3点以内の箇条書き
- 「## キーワード」で重要語を3-6個
- 文字起こしに無い情報は作らない

=== 文字起こし ===
{text}
"""


# ── Facts-based Quiz Batch ────────────────────────────────────

async def generate_quiz_batch(
    facts: list[dict],
    mode: str = "lecture",
    count: int = 2,
    batch_index: int = 0,
    total_batches: int = 4,
    used_answers: str = "",
) -> str:
    """
    Facts配列から count 問（デフォルト2問）のクイズを生成する。
    batch_index で正解分布を制御し、偏りを防ぐ。
    """
    _ensure_model()
    from vertexai.generative_models import GenerationConfig

    facts_text = "\n".join(
        f"- [{f.get('type', 'key-point')}] {f.get('text', '')}"
        for f in facts
    )

    # バッチごとに正解の偏りを制御
    answer_hint = ["A,B", "C,D", "A,C", "B,D"][batch_index % 4]
    start_q = batch_index * count + 1

    prompt = f"""以下のFacts（重要事実リスト）から理解度確認クイズを {count} 問作成してください。

# 最重要（厳守）
- Markdownのクイズ本体のみ出力（余計な挨拶や説明文は一切禁止）
- Factsに根拠がない内容は作らない
- このバッチの正解は {answer_hint} に寄せる（全体で均等分布のため）
- 問題番号は Q{start_q} から開始
{f"- 前バッチの正解: {used_answers}（同じパターンを避ける）" if used_answers else ""}

# 出力フォーマット（厳守）
### Q{start_q}
質問文
- A. 選択肢A
- B. 選択肢B
- C. 選択肢C
- D. 選択肢D
**Answer:** A
**Explanation:** 1文で根拠

# 誤答の品質
- もっともらしいが誤りにする
- 明らかに不正解なダミー禁止

=== モード ===
{mode}

=== Facts ===
{facts_text}
"""

    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.5,
            max_output_tokens=QUIZ_BATCH_MAX_TOKENS,
        ),
        label=f"quiz_batch_{batch_index}",
    )
    raw = (resp.text or "").strip()
    return clean_quiz_markdown(raw) if raw else ""


# ── Single-shot Quiz (JSON, anti-duplication) ─────────────────

async def generate_quiz_json(
    source_text: str,
    facts: list[dict] | None = None,
    mode: str = "lecture",
    count: int = 8,
    custom_instruction: str = "",
) -> dict:
    """
    Facts または transcript から、重複防止ルール付きで count 問のクイズを
    JSON 形式で一括生成する。
    戻り値: {"questions": [...]} dict。パース失敗時は空 dict。
    """
    if not facts and len(source_text) < MIN_TRANSCRIPT_LENGTH:
        logger.warning(f"Source too short for quiz: {len(source_text)} chars")
        return {}

    _ensure_model()
    from vertexai.generative_models import GenerationConfig

    # 入力テキストの準備
    if facts:
        input_block = "\n".join(
            f"- [{f.get('type', 'key-point')}] {f.get('text', '')}"
            for f in facts
        )
        input_label = "Facts（重要事実リスト）"
    else:
        if len(source_text) > MAX_TRANSCRIPT_LENGTH:
            source_text = source_text[:MAX_TRANSCRIPT_LENGTH]
        input_block = source_text
        input_label = "文字起こし"

    mode_label = "会議の議事録" if mode == "meeting" else "講義の文字起こし"

    prompt = f"""あなたは「{mode_label}」から理解度テスト（{count}問）を作るプロです。

最重要要件：同一バッチ内（Q1〜Q{count}）の問題が、内容・観点・形式で被らないこと。
"似た問題""同じ論点の言い換え""同じ結論を別表現で問う"は禁止。

# 入力
- {input_label}

# 出力（JSONのみ — それ以外のテキストは一切出力しない）
{{"questions": [
  {{
    "id": "Q1",
    "topic": "議題タグ（例: 料金/機能/スケジュール/リスク/意思決定など）",
    "type": "fact|reason|compare|risk|process|what_if",
    "format": "mcq",
    "question": "問題文",
    "choices": ["A. ...", "B. ...", "C. ...", "D. ..."],
    "answer": "A",
    "rationale": "なぜそれが正解か",
    "evidence_span": "入力内の根拠となる原文の短い抜粋（20〜80字）"
  }}
]}}

# 絶対ルール（重複防止）
1) topicは可能な限り全問で分散。topicの同一繰り返しは最大2回まで。連続で同じtopicは禁止。
2) typeは分散：fact/reason/compare/risk/process/what_if を偏らせない（同一typeは全体の40%超禁止、連続禁止）。
3) 同一の結論・決定事項・数字/期限/担当者を中心にした質問は1回だけ。言い換えで増やすのは禁止。
4) 各問題は「異なる観点」を必ず持つこと：
   決定事項 / 理由・根拠 / 代替案の比較 / リスク・未決 / 手順・依存関係 / 条件変更(What-if)
5) evidence_spanは必須。入力に存在しない情報を作らない。推測で補完しない。
6) 正解(answer)の分布: A/B/C/Dが均等になるよう配置。同じ正解が3問以上連続禁止。
7) 禁止テンプレ（具体語なしの一般問は禁止）:
   「次のアクションは何ですか？」「課題は何ですか？」「要点は何ですか？」
   → 必ず固有名詞/数字/期限/決定内容を含む形にする。

# 手順（必ず守る）
A) まず入力から主要トピックを5〜8個抽出し、question設計図を作る。
   各questionに topic/type/観点 を割り当て、全体で被らないようにする。
B) 設計図に従い、Q1から順に1問ずつ生成。
   生成するたびに既出Qと「同じ内容の言い換え」でないか確認し、被るなら別トピック/別観点で作り直す。
C) 最後に全問を再点検し、被りがあれば差し替えてからJSONを出力する。

# 生成条件
- 質問は短く明確に。
- 選択肢問題の誤答は"それっぽいが入力と矛盾"するように作る（嘘の事実は作らない）。
- {count}問のうち、最低1問は compare(比較)、最低1問は risk(リスク)を入れる。
- 入力が短い場合は無理に{count}問作らず、根拠のある問題だけ出力する。
- 全て日本語で出力。

=== {input_label} ===
{input_block}
"""

    if custom_instruction:
        prompt += f"\n\n# ユーザーからの追加指示\n{custom_instruction}\n"

    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.5,
            max_output_tokens=QUIZ_SINGLE_MAX_TOKENS,
        ),
        label="quiz_json",
    )
    raw = (resp.text or "").strip()

    if not raw:
        return {}

    parsed = _parse_json_with_retry(raw)
    if not parsed or "questions" not in parsed:
        # JSON パース失敗時: コードブロック除去して再試行
        cleaned = raw.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        try:
            parsed = json.loads(cleaned)
        except Exception:
            logger.error(f"[quiz_json] Failed to parse LLM output as JSON")
            return {}

    # バリデーション: questions 配列の最低限チェック
    questions = parsed.get("questions", [])
    valid_questions = []
    for q in questions:
        if isinstance(q, dict) and q.get("question") and q.get("answer"):
            valid_questions.append(q)

    if not valid_questions:
        logger.warning(f"[quiz_json] No valid questions in LLM output")
        return {}

    return {"questions": valid_questions}


def quiz_json_to_markdown(quiz_data: dict) -> str:
    """
    generate_quiz_json() の出力を、既存の quizMarkdown 互換 Markdown に変換する。
    """
    questions = quiz_data.get("questions", [])
    if not questions:
        return ""

    parts = []
    for q in questions:
        qid = q.get("id", "Q?")
        question_text = q.get("question", "")
        answer = q.get("answer", "")
        rationale = q.get("rationale", "")

        lines = [f"### {qid}", question_text]

        choices = q.get("choices", [])
        if choices:
            for choice in choices:
                # 既に "A. ..." 形式なら "- " を付けるだけ
                c = choice.strip()
                if len(c) >= 2 and c[1] in ".)" and c[0] in "ABCD":
                    lines.append(f"- {c}")
                else:
                    lines.append(f"- {c}")

        lines.append(f"**Answer:** {answer}")
        if rationale:
            lines.append(f"**Explanation:** {rationale}")

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


async def generate_explanation(text: str, mode: str = "lecture") -> str:
    """
    Transcript を基に要点の解説を Markdown で生成する。
    """
    # Transcript length validation
    if len(text) < MIN_TRANSCRIPT_LENGTH:
        logger.warning(f"Transcript too short for explanation: {len(text)} chars")
        return "## ⚠️ 解説不可\n\n文字起こしの内容が短すぎるため、解説を生成できませんでした。"

    if len(text) > MAX_TRANSCRIPT_LENGTH:
        text = text[:MAX_TRANSCRIPT_LENGTH]

    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    prompt = _build_explanation_prompt(text, mode)
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.4,
            max_output_tokens=2048,
        ),
        label="explanation",
    )
    return (resp.text or "").strip()


async def generate_playlist_timeline(
    text: str,
    segments: Optional[List[dict]] = None,
    duration_sec: Optional[float] = None
) -> str:
    """
    再生リスト(タイムライン)を JSON 文字列で生成する。
    """
    # Transcript length validation
    if len(text) < MIN_TRANSCRIPT_LENGTH:
        logger.warning(f"Transcript too short for playlist: {len(text)} chars")
        return "[]"

    if len(text) > MAX_TRANSCRIPT_LENGTH:
        text = text[:MAX_TRANSCRIPT_LENGTH]

    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    prompt = _build_playlist_prompt(text, segments=segments, duration_sec=duration_sec)
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.5,
            max_output_tokens=4096,
            response_mime_type="application/json",
            response_schema=_SCHEMA_PLAYLIST,
        ),
        label="playlist",
    )
    # Gemini json mode returns text as JSON string
    return (resp.text or "").strip()

async def answer_question(text: str, question: str, mode: str = "lecture") -> dict:
    """
    与えられた transcript に基づき質問に回答する。
    短い回答と根拠となる引用箇所（文脈抜粋）を返す。
    """
    # Transcript length validation
    if len(text) < MIN_TRANSCRIPT_LENGTH:
        return {"answer": "文字起こしの内容が短すぎるため、回答できません。", "citations": []}

    if len(text) > MAX_TRANSCRIPT_LENGTH:
        text = text[:MAX_TRANSCRIPT_LENGTH]

    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    prompt = _build_qa_prompt(text, question, mode)
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.3,
            max_output_tokens=1024,
            response_mime_type="application/json",
        ),
        label="qa",
    )
    # Use retry-aware JSON parsing
    result = _parse_json_with_retry(resp.text or "{}")
    if not result:
        return {"answer": (resp.text or "").strip(), "citations": []}
    return result


async def translate_text(text: str, target_lang: str) -> str:
    """
    テキストを指定言語に翻訳する。
    多言語混在テキストはセグメント分割→個別翻訳→結合で処理。
    """
    _ensure_model()

    # 多言語混在判定
    if _is_multilingual_text(text) and target_lang in ("日本語", "ja"):
        logger.info(f"[translate] Multilingual detected, using segmented translation (len={len(text)})")
        return await _translate_segmented(text, target_lang)

    # 単一言語 or 短文: 一括翻訳
    return await _translate_chunk(text, target_lang)


# ── Multilingual detection ──

_RE_HANGUL = re.compile(r'[\uAC00-\uD7AF]')
_RE_CJK = re.compile(r'[\u4E00-\u9FFF\u3400-\u4DBF]')
_RE_LATIN = re.compile(r'[A-Za-z]{3,}')  # 3文字以上の英単語


def _is_multilingual_text(text: str) -> bool:
    """2つ以上の異なるスクリプトが含まれるか判定。"""
    scripts = 0
    if _RE_HANGUL.search(text):
        scripts += 1
    if _RE_CJK.search(text):
        scripts += 1
    if len(_RE_LATIN.findall(text)) > 3:
        scripts += 1
    return scripts >= 2


def _detect_lang(text: str) -> str:
    """ヒューリスティクスで主要言語を判定。"""
    hangul = len(_RE_HANGUL.findall(text))
    cjk = len(_RE_CJK.findall(text))
    latin_words = len(_RE_LATIN.findall(text))

    if hangul > cjk and hangul > latin_words:
        return "ko"
    if latin_words > hangul and latin_words > cjk:
        return "en"
    if cjk > 0:
        return "zh"
    return "unknown"


def _has_non_japanese_residue(text: str) -> bool:
    """翻訳結果に英語/韓国語が残っているか判定。"""
    latin_chars = len(re.findall(r'[A-Za-z]', text))
    hangul_chars = len(_RE_HANGUL.findall(text))
    total = len(text.replace(" ", "")) or 1
    # 英字が20%以上 or ハングルが3文字以上残っている
    return (latin_chars / total > 0.15) or hangul_chars > 2


# ── Segment splitting ──

def _split_into_segments(text: str) -> list[str]:
    """多言語テキストを言語境界・句読点で分割。"""
    # まず句読点で文分割
    sentences = re.split(r'(?<=[。！？.!?\n])\s*', text)

    segments = []
    current = ""
    current_lang = None

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        lang = _detect_lang(sentence)

        # 言語が変わった or 現セグメントが十分長い → 区切る
        if current_lang and lang != current_lang and current:
            segments.append(current.strip())
            current = sentence
            current_lang = lang
        elif len(current) > 1500:
            segments.append(current.strip())
            current = sentence
            current_lang = lang
        else:
            current = f"{current} {sentence}" if current else sentence
            if current_lang is None:
                current_lang = lang

    if current.strip():
        segments.append(current.strip())

    # 長い英語セグメントをさらに文単位に分割
    final_segments = []
    for seg in segments:
        if _detect_lang(seg) == "en" and len(seg) > 300:
            en_sentences = re.split(r'(?<=[.!?])\s+', seg)
            # 2-3文ずつまとめる
            batch = ""
            for s in en_sentences:
                if len(batch) + len(s) > 500:
                    if batch:
                        final_segments.append(batch.strip())
                    batch = s
                else:
                    batch = f"{batch} {s}" if batch else s
            if batch:
                final_segments.append(batch.strip())
        else:
            final_segments.append(seg)

    return [s for s in final_segments if s.strip()]


# ── Per-chunk translation ──

async def _translate_chunk(text: str, target_lang: str) -> str:
    """単一チャンクを翻訳。"""
    from vertexai.generative_models import GenerationConfig

    prompt = f"""以下のテキストを{target_lang}に翻訳してください。

ルール:
- 出力はすべて{target_lang}のみ。英語・韓国語・中国語を一切残さない。
- 固有名詞のみカタカナ表記可。
- 壊れた音声認識テキストは文脈から推測して翻訳。
- 推測不能な文字列は省略。
- 翻訳結果のテキストのみ出力。説明不要。

{text}"""

    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.3,
            max_output_tokens=4096,
        ),
        label="translate",
    )
    return (resp.text or "").strip()


# ── Segmented translation pipeline ──

async def _translate_segmented(text: str, target_lang: str) -> str:
    """多言語混在テキストをセグメント分割→個別翻訳→残留チェック→結合。"""
    import asyncio

    segments = _split_into_segments(text)
    logger.info(f"[translate/segmented] Split into {len(segments)} segments")

    # 並列翻訳 (最大5並列)
    semaphore = asyncio.Semaphore(5)

    async def _translate_with_limit(seg: str, idx: int) -> str:
        # 日本語のみのセグメントはスキップ
        if not _is_multilingual_text(seg) and _detect_lang(seg) == "unknown":
            # CJK文字が日本語かもしれない → そのまま返す
            if not _RE_HANGUL.search(seg) and not _RE_LATIN.search(seg):
                return seg

        async with semaphore:
            translated = await _translate_chunk(seg, target_lang)

            # 残留チェック: 英語/韓国語が残っていたら再翻訳
            if _has_non_japanese_residue(translated):
                logger.warning(f"[translate/segmented] Residue detected in segment {idx}, retranslating (len={len(seg)})")
                translated = await _translate_chunk(translated, target_lang)

            return translated

    tasks = [_translate_with_limit(seg, i) for i, seg in enumerate(segments)]
    results = await asyncio.gather(*tasks)

    return "\n".join(r for r in results if r)


async def generate_highlights_and_tags(text: str, segments: Optional[List[dict]] = None) -> dict:
    """
    ハイライトとタグを生成する。
    """
    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    prompt = _build_highlights_prompt(text, segments)
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=0.5,
            max_output_tokens=2048,
            response_mime_type="application/json",
        ),
        label="highlights",
    )
    try:
        data = json.loads(resp.text or "{}")
    except Exception:
        data = {}
    # 正規化: highlights は Highlight モデルの形に揃える
    highlights = []
    raw_highlights = data.get("highlights") or []
    for i, h in enumerate(raw_highlights):
        try:
            highlights.append({
                "id": h.get("id") or f"hl_{i}",
                "startSec": float(h.get("startSec", 0)),
                "endSec": float(h.get("endSec", 0)),
                "title": h.get("title") or h.get("summary") or "Highlight",
                "summary": h.get("summary"),
                "speakerIds": h.get("speakerIds") or [],
            })
        except Exception:
            continue

    tags = data.get("tags") or []
    return {"highlights": highlights, "tags": tags}


# ---------- Prompt Builders ---------- #

def _build_summary_prompt(text: str, mode: str) -> str:
    if mode == "lecture":
        return f"""あなたは優秀な講義ノート作成アシスタントです。以下の文字起こしをMarkdown形式で、学生が復習しやすい形に要約してください。
- 重要ポイントは箇条書きで簡潔に
- 不明瞭な箇所は「要確認」と記載
- Markdown記法（**太字**, ***強調***等）は使わない。平文で読みやすく書く
- **文字起こしに無い固有名詞・数値・定義は作らない。曖昧なら『要確認』とする**

=== 文字起こし ===
{text}
"""
    return f"""あなたは会議の議事録編集者です。以下の文字起こしから、枝（具体例・雑談・経緯の細部）を捨てて主要論点だけをMarkdown形式で要約してください。
- 主要論点（意思決定・争点・コスト/期限/リスクに影響する制約）のみ記載
- 具体例・エピソード・反復・相槌は除外
- 決定事項、TODO、未解決事項を明確に
- 箇条書きで簡潔に、抽象度高め
- 不明瞭な箇所は「要確認」と記載
- Markdown記法（**太字**, ***強調***等）は使わない。平文で読みやすく書く

=== 文字起こし ===
{text}
"""


def _build_quiz_prompt(text: str, mode: str, count: int) -> str:
    return f"""あなたは学習クイズ作成アシスタントです。以下の文字起こし内容から理解度確認クイズを {count} 問作成してください。
文字起こしの内容量に応じて5〜10問の範囲で調整してください（内容が十分であれば {count} 問）。

# 最重要（厳守）
- **Markdownのクイズ本体のみ出力**（余計な挨拶や説明文は一切禁止）
- 文字起こしに根拠がない内容は作らない（推測で事実を足さない）
- **正解の分布**: 問題数に応じてA/B/C/Dが均等になるよう配置
- **連続禁止**: 同じ正解が3問以上連続しない

# 出力フォーマット（厳守）
### Q1
質問文
- A. 選択肢A
- B. 選択肢B
- C. 選択肢C
- D. 選択肢D
**Answer:** A
**Explanation:** 1-2文で根拠（文字起こしに基づく）

# 誤答（distractor）の品質
- 誤答は「もっともらしいが誤り」にする
- 正解と紛らわしいが、文字起こしを読めば明確に区別できること
- 明らかに不正解なダミー（例: 全く関係ない単語）は禁止

# 安全策
- 文字起こしが短すぎる/不明瞭で**根拠が不十分な場合**:
  - 作成可能な問題数だけ出力（無理に{count}問作らない）
  - Explanationに「要確認: 根拠が曖昧」と明記
- 文字起こしから**1問も作れない場合**:
  - 「### 注意: 文字起こしの内容が不十分なためクイズを生成できません」と出力

=== モード ===
{mode}

=== 文字起こし ===
{text}
"""

def _build_explanation_prompt(text: str, mode: str) -> str:
    if mode == "lecture":
        return f"""あなたは講義内容を噛み砕いて説明するチューターです。
以下の文字起こしを読み、重要概念を理解しやすい解説として Markdown でまとめてください。

- 冒頭に3〜5行の要点
- 必要なら短い具体例を追加
- Markdown記法（**太字**, ***強調***等）は使わない。平文で読みやすく書く

=== 文字起こし ===
{text}
"""
    return f"""あなたは会議内容をわかりやすく解説するアシスタントです。
以下の文字起こしを読み、背景・意図・論点を整理した解説を Markdown でまとめてください。

- 冒頭に3-5行の要点
- 必要なら短い具体例を追加
- Markdown記法（**太字**, ***強調***等）は使わない。平文で読みやすく書く

=== 文字起こし ===
{text}
"""


def _build_playlist_rules(
    segments: Optional[List[dict]] = None,
    duration_sec: Optional[float] = None,
) -> tuple[float, str, str]:
    cues = _build_playlist_cues(segments)
    if duration_sec:
        if duration_sec <= 120:
            chapter_hint = "2-4"
            min_sec = 10
        elif duration_sec <= 600:
            chapter_hint = "3-6"
            min_sec = 20
        else:
            chapter_hint = "4-8"
            min_sec = 30
        duration_line = f"- 収録時間は約 {duration_sec:.1f} 秒。目安のチャプター数は {chapter_hint} 件"
    else:
        min_sec = 20
        duration_line = "- 収録時間が不明なので、チャプターは内容量に応じて 3-6 件"

    cues_block = ""
    if cues:
        cues_block = f"""
=== タイムスタンプ付き断片 (参考) ===
{cues}
"""
    return min_sec, duration_line, cues_block


def _build_pseudo_cues(text: str, duration_sec: float, target_cues: int = 15) -> str:
    """セグメントがない場合、テキスト位置から擬似タイムスタンプcuesを生成する。"""
    if not text or not duration_sec or duration_sec <= 0:
        return ""
    # target_cues個程度のcueになるようにchunk_charsを動的に決定
    chunk_chars = max(100, len(text) // target_cues)
    chars_per_sec = len(text) / duration_sec
    sentences = text.replace("。", "。\n").split("\n")
    cues = []
    char_pos = 0
    buf_text = []
    buf_start_sec = 0.0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        buf_text.append(sentence)
        char_pos += len(sentence)
        current_sec = char_pos / chars_per_sec
        if sum(len(t) for t in buf_text) >= chunk_chars:
            cues.append(f"[{buf_start_sec:.1f}-{current_sec:.1f}] {' '.join(buf_text)[:150]}")
            buf_text = []
            buf_start_sec = current_sec
        if len(cues) >= target_cues:
            break
    if buf_text:
        end_sec = min(char_pos / chars_per_sec, duration_sec)
        cues.append(f"[{buf_start_sec:.1f}-{end_sec:.1f}] {' '.join(buf_text)[:150]}")
    return "\n".join(cues)


def _build_playlist_prompt(
    text: str,
    segments: Optional[List[dict]] = None,
    duration_sec: Optional[float] = None
) -> str:
    min_sec, duration_line, cues_block = _build_playlist_rules(
        segments=segments,
        duration_sec=duration_sec,
    )
    # セグメントがなくduration_secがある場合、擬似cuesを生成
    if not cues_block and duration_sec and duration_sec > 0:
        pseudo = _build_pseudo_cues(text, duration_sec)
        if pseudo:
            cues_block = f"""
=== 推定タイムスタンプ（テキスト位置から推定、参考） ===
{pseudo}
"""
    return f"""以下の文字起こしを、YouTube のチャプターのように「意味のまとまり」で再生リストに分割してください。
JSON 配列のみを返してください。形式:
[
  {{"startSec": 0.0, "endSec": 90.0, "title": "導入", "summary": "内容要約", "confidence": 0.9}},
  ...
]
ルール:
- チャプター数は必ず {min_sec} 秒以上のまとまりで、**合計4〜8件に収める**（これを超えない）
- 細かく分割しすぎない。複数の小話題が近い内容なら1つのチャプターにまとめる
- 5秒刻みの機械的な分割は禁止
- startSec/endSec は秒単位（浮動小数）
- title は短く重複禁止、summary で補足
- タイムスタンプ付き断片がある場合は参考にするが、断片の数だけチャプターを作らない
{duration_line}

=== 文字起こし ===
{text}
{cues_block}
"""


def _build_playlist_cues(segments: Optional[List[dict]], max_cues: int = 120, max_chars: int = 6000) -> str:
    if not segments:
        return ""

    cues = []
    buf_text = []
    buf_start = None
    buf_end = None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("startSec") or seg.get("start") or 0.0)
        end = float(seg.get("endSec") or seg.get("end") or 0.0)
        if buf_start is None:
            buf_start = start
        buf_end = end
        if len(buf_text) < 6:
            buf_text.append(text)
        duration = (buf_end or 0.0) - (buf_start or 0.0)
        if duration >= 25 or sum(len(t) for t in buf_text) >= 120:
            cues.append({
                "start": buf_start,
                "end": buf_end,
                "text": " ".join(buf_text)
            })
            buf_text = []
            buf_start = None
            buf_end = None
        if len(cues) >= max_cues:
            break

    if buf_text and buf_start is not None and buf_end is not None:
        cues.append({
            "start": buf_start,
            "end": buf_end,
            "text": " ".join(buf_text)
        })

    lines = []
    total = 0
    for cue in cues:
        line = f"[{cue['start']:.1f}-{cue['end']:.1f}] {cue['text']}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _build_qa_prompt(text: str, question: str, mode: str) -> str:
    return f"""あなたは議事録/講義ノートのQAアシスタントです。以下の文字起こしに基づいて質問に答えてください。
JSON のみ返してください。形式:
{{
  "answer": "短い回答。5文以内。",
  "citations": [
    {{"excerpt": "根拠となる抜粋", "reason": "なぜこの抜粋が根拠か"}}
  ]
}}
- 回答は日本語で、事実に基づき、憶測は避ける
- transcript に存在しない情報は「不明」と答える

# モード
{mode}

# 質問
{question}

# 文字起こし
{text}
"""


def _build_summary_tags_prompt(
    text: str,
    mode: str,
    *,
    segments: Optional[List[dict]] = None,
) -> str:
    """
    モードに応じた要約プロンプトを生成する。
    - meeting: 議事録形式（決定事項、TODO、議論ポイント）
    - lecture: 講義ノート形式（要点、キーワード、学習ポイント）
    要約・タグ・再生リストを一括で生成する。

    segments (Phase 7.10): transcript segment / chunk list with
    {id, text, speaker?, startMs, endMs}. When provided, we append a
    forward-path citation section that asks the LLM to return
    `sourceSegmentIds` per bullet. The existing plaintext transcript
    and existing prompts are left unchanged so behavior without
    segments is identical to before.
    """
    if mode == "lecture":
        base = _build_lecture_summary_prompt(text)
    else:
        base = _build_meeting_summary_prompt(text)

    if segments:
        base += _build_segment_citation_suffix(segments)
    return base


def _format_segments_for_prompt(
    segments: List[dict],
    limit: int = 300,
    max_chars: int = 45_000,
) -> str:
    """Render segments as `[seg_104 00:57:49] (speaker) text` lines.

    Hard caps on count and total characters so oversized sessions don't
    blow the prompt token budget. Returns empty string when no usable
    segments remain after filtering.
    """
    lines: List[str] = []
    total = 0
    for seg in segments[:limit]:
        sid = (
            seg.get("id")
            or seg.get("segmentId")
            or (seg.get("segmentIds") or [None])[0]
        )
        if not sid:
            continue
        sid = str(sid)
        start_ms = seg.get("startMs")
        if start_ms is None and seg.get("startSec") is not None:
            try:
                start_ms = int(float(seg["startSec"]) * 1000)
            except (TypeError, ValueError):
                start_ms = None
        if not isinstance(start_ms, (int, float)):
            continue
        start_sec = int(start_ms) // 1000
        hh, rem = divmod(start_sec, 3600)
        mm, ss = divmod(rem, 60)
        ts = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        speaker = seg.get("speaker") or ""
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        prefix = f"[{sid} {ts}]"
        if speaker:
            prefix += f" ({speaker})"
        line = f"{prefix} {text}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _build_segment_citation_suffix(segments: List[dict]) -> str:
    """Citation-aware suffix appended to the main summary prompt.

    Asks the LLM to:
      1. Return each bullet with an additional `sourceSegmentIds` array
         referencing the segment ids seen in the transcript_with_ids block.
      2. Use only ids that actually appear in that block — never fabricate.
      3. Keep arrays small (1-4 ids) so the citation is precise.

    The existing JSON schema intentionally does NOT declare
    sourceSegmentIds as required, so omission is tolerated (falls back
    to the text-matching anchor_resolver). Forward-path ids, when
    returned, are preferred over text-matching.
    """
    formatted = _format_segments_for_prompt(segments)
    if not formatted:
        return ""
    return f"""

# 文字起こし (segment id 付き)
以下は同じ文字起こしに segment id と発話時刻を付けたものです。要約の各項目について、
**根拠となる発話の segment id** を `sourceSegmentIds` 配列で返してください。

# segment-id 付き transcript
{formatted}

# 追加のルール (sourceSegmentIds)
- `highlights`, `decisions`, `todos`, `openQuestions`, `discussionPoints`,
  `conversationHighlights`, `sections[*].bullets` の各要素に
  `sourceSegmentIds: ["seg_XXX", ...]` を**可能なら**付ける。
- 上の transcript に **実在する id** だけ書く。存在しない id は**絶対に書かない**。
- 1 項目あたり 1〜4 個まで。広すぎる引用は避ける。
- 根拠がはっきりしない場合は空配列 `[]` でよい。
- 時刻 (startSec / endSec) はサーバ側で segment id から導出するので、項目側には書かない。
"""


def _build_meeting_summary_prompt(
    text: str,
) -> str:
    """議事録用の要約プロンプト（Outcome最優先＋薄さ防止 / Step1抽出→Step2整形）"""
    return f"""あなたは議事録の編集者です。以下の文字起こしから、ユーザが一番知りたい
「結局どうなったか（決定/合意/変更/次の一手）」を"薄くならない程度に具体"で要約してください。

# 絶対ルール（厳守）
1) 原文に無い固有名詞・数値・日付（YYYY-MM-DD）・論文・指標は一切書かない。
2) 期限は原文に具体日付が無い限り due="期限不明"。ただし本文には「3月末が厳しいため4月に後ろ倒し可否を確認中」など原文の曖昧表現を残す。
3) ASRノイズっぽい語（意味不明/文脈不一致/誤字と判断した語）は書かない。
4) 「結論は明確でない」は decisions=0 かつ todos=0 の時だけ許可。それ以外は禁止。
5) JSON内テキストに Markdown記法（**太字**, ***強調***, ##見出し, - 箇条書き 等）は一切使わない。構造はJSONで表現済みなので、text/overview等の値は平文で書く。

# "薄さ防止"の必須要件（これを満たさない出力は禁止）
- highlights先頭2行は必ず: A) [decision] 今回の結論（1行）、B) [todo] 次の一手（1行）
- decisions には原文の「購入する予定/購入しておかないと/大丈夫です/承知しました」等の"合意"を必ず反映する。
- todos は必ず 3〜6件出す（原文にある範囲で）。無理に増やさないが、1件だけにしない。
- "変更点（やめる/見直す/方針転換）" が原文にあるなら、highlights に [info] として必ず1行入れる。
- 購入対象が複数ある場合は decisions または todos に必ず列挙する。
- 優先順位の合意（〜より先に〜する）が原文にあるなら todos の priority に反映する。

# 作業手順（必ずこの順序）
Step1) 抽出（内部作業）：次を抽出し、各項目に evidence（原文の短い根拠フレーズ）を付ける
  - decisions（決定/合意/予定）
  - todos（次アクション）
  - changes（方針の見直し/やめる等）
  - priorities（優先順位の合意）
  - purchases（購入対象の一覧）
  - discussionTopics（主要トピック）
Step2) 整形：Step1で抽出した内容だけを使って、指定フォーマットで出力する。
  - evidence は最終出力に書かない（整合性チェック用）。

# 出力JSONスキーマ（厳守）
{{
  "summary": {{
    "type": "meeting",
    "highlights": [
      {{"text": "今回の結論: （最重要な合意を具体的に1行）", "category": "decision", "needConfirm": false}},
      {{"text": "次の一手: （最優先TODOを具体的に1行）", "category": "todo", "needConfirm": false}}
    ],
    "overview": "800〜1500文字。セッション全体を網羅する概要。冒頭で会議の目的・背景を1〜2文で述べ、続けて議論された主要トピック全てを順に要約する。「結論」「次の一手」「変更点」「購入」「優先順位」は必ず含め、さらに議論の経緯や判断理由、参加者間の合意形成プロセスも盛り込む。各トピックの要点を漏れなく拾い、具体的な数字・固有名詞・条件も省略せず記載する。読むだけで会議の全体像と詳細が掴めるようにする。読点で区切り、1文を短くする。長い1文より短い複数文で構成する。",
    "decisions": [
      {{"text": "〜することに決定", "owner": "担当or不明", "due": "YYYY-MM-DD|期限不明", "needConfirm": false}}
    ],
    "todos": [
      {{"text": "成果物ベースのTODO（具体的に）", "owner": "担当or不明", "due": "YYYY-MM-DD|期限不明", "priority": "high|mid|low", "needConfirm": false}}
    ],
    "openQuestions": [
      {{"text": "未決事項", "impact": "high|mid|low", "needConfirm": true}}
    ],
    "discussionPoints": [
      {{"topic": "...", "summary": "...", "conclusion": "「合意」or「保留（要確認）」", "nextAction": "TODOと一致させる", "needConfirm": false}}
    ],
    "keywords": [{{"text": "重要語（原文に出た語のみ）"}}],
    "participants": [{{"name": "話者名or不明", "role": "PM|Dev|Sales|Other|不明"}}],
    "conversationHighlights": [
      {{
        "id": "hl_1",
        "text": "最近、家賃が一気に上がったという懸念が共有された。",
        "topic": "生活費",
        "importance": "medium",
        "evidenceHint": "「家賃」「上がった」周辺の原文フレーズ",
        "primaryTimestampSec": 3469
      }}
    ],
    "uiHints": {{
      "topFocus": ["decisions", "todos"],
      "tone": "business",
      "suggestedBadges": ["要確認", "期限不明", "担当不明"]
    }}
  }},
  "tags": ["タグ1", "タグ2", "タグ3"],
  "suggestedTitle": "港区防災カタログ仕様と営業方針"
}}

# 制約
- highlights: 5〜10件。先頭は必ず「今回の結論:」、次に「次の一手:」。[decision][todo][info]のみ（[discussion]禁止）。会議で言及された重要事項は全て拾う。
- conversationHighlights: 5〜12件。**自然な日本語1文**で、あとで一覧として読み返せる読み物スタイル（例:「最近、家賃が一気に上がったという懸念が共有された」「A さんが新しい体重計を買ったという話題が出た」）。
  - `highlights` とは別物。`highlights` は短い[category]タグ付きの議事箇条書き、`conversationHighlights` はそれより柔らかい「会話ハイライトカード」。
  - 感情・悩み・生活・決定・驚き・共有事項など、見返し価値のある話題を優先する。
  - `importance` は "high" | "medium" | "low"。重要な決定や強い感情表明は high、雑談レベルは low。
  - `primaryTimestampSec` は代表的な原文箇所の秒数（整数）。原文に存在する箇所のみ記載し、不明な場合は省略。
  - `evidenceHint` は原文に出た短いフレーズ（evidence リンク用の手がかり、最大40字）。
  - decisions/todos と重複してよい（見返し価値があるなら残す）が、文体は「会話ハイライト」寄りに整える。
- decisions: 最大6件。原文に合意・承認・決定がある限り全て記載。
- todos: 3〜10件（原文にあれば必ず出す。1件だけにしない）。具体的なアクションは全て拾う。
- openQuestions: 0〜5件
- discussionPoints: 3〜8件。conclusionは「合意」or「保留（要確認）」で書く。議論された全トピックをカバーする。
- tags: 2〜5個
- todosの「検討/確認/調査」は成果物ベースに言い換える（例:「〜を検討」→「〜の方針案を整理」）
- owner/dueが不明な場合は "不明"/"期限不明" とし needConfirm=true
- suggestedTitle: 10〜25文字。内容が具体的にわかるタイトルにする。体言止め厳守。文章禁止。「〜について」「〜の件」等の助詞で終わらない。主要トピックや結論のキーワードを含める。例:「港区防災カタログ仕様と営業方針」「微分方程式の基礎と応用例」「Q2売上目標と採用計画の確定」

# 文字起こし
{text}
"""
def _build_lecture_summary_prompt(
    text: str,
) -> str:
    """講義ノート用の要約プロンプト（JSON厳格版 / UI直結スキーマ）"""
    return f"""あなたは優秀な講義ノート作成アシスタントです。以下の文字起こしから、リッチなUIに即座に変換できる「構造化JSON」を生成してください。

# 最重要（厳守）
- 出力は「次のJSONのみ」。前置き/説明/Markdown/コードフェンスは禁止
- JSONは構文的に正しいこと
- 文字起こしに無い固有名詞・数値・定義・因果は作らない
- 不明/曖昧/根拠不足は needConfirm=true を付け、文言にも「要確認」を含める
- クイズ形式（Q1/Answer/Explanation）は一切出さない
- 重要語の置換禁止：固有名詞を別語に置換しない。自信がなければ原文の語を採用
- JSON内テキストに Markdown記法（**太字**, ***強調***, ##見出し, - 箇条書き 等）は一切使わない。構造はJSONで表現済みなので、text/overview/definition等の値は平文で書く。

# UI目的
- 最上部に「今日のポイントのおさらい」を3〜7行で簡潔に（highlights）
- 次に「用語カード」「流れ（セクション）」「重要式・手順」「例題/範囲」を見やすく
- overview は800〜2000文字で、セッション内容を網羅的に要約する。「何を学んだか」「なぜ重要か」「全体像」に加え、各トピックの要点、具体的な説明や例示、トピック間のつながりも盛り込む。具体的な数字・固有名詞・定義・条件を省略せず記載し、overviewだけで講義の流れと内容の詳細が把握できるようにする

# 出力JSONスキーマ（厳守）
{{
  "summary": {{
    "type": "lecture",
    "highlights": [
      {{"text": "重要ポイント（短文）", "needConfirm": false}}
    ],
    "overview": "800〜2000文字。セッション内容を網羅的に要約する。冒頭でテーマと目的を述べ、各トピックの要点を順に説明し、具体例や重要な補足も含める。具体的な数字・固有名詞・定義・条件を省略せず記載する。overviewだけで講義全体の流れと内容の詳細が把握できるようにする。読点で区切り、1文を短くする。長い1文より短い複数文で構成する。",
    "theme": {{"text": "今日のテーマ（1〜2文）", "needConfirm": false}},
    "terms": [
      {{"term": "用語", "definition": "文字起こしに基づく説明", "examples": ["例があれば"], "needConfirm": false}}
    ],
    "sections": [
      {{
        "title": "セクション名",
        "bullets": ["要点1", "要点2"],
        "commonMistakes": ["誤解しやすい点があれば"],
        "needConfirm": false
      }}
    ],
    "formulasOrProcedures": [
      {{"title": "式/定義/手順", "content": "短く", "needConfirm": false}}
    ],
    "exercises": {{
      "examples": [{{"title": "例題", "point": "何を問うか", "needConfirm": false}}],
      "homework": [{{"text": "宿題", "needConfirm": false}}],
      "examScope": [{{"text": "試験範囲", "needConfirm": false}}]
    }},
    "studyGuide": {{
      "recommendedOrder": ["highlights", "terms", "sections", "formulasOrProcedures"],
      "memoryHooks": ["覚え方/比喩があれば（根拠がある範囲で）"]
    }},
    "conversationHighlights": [
      {{
        "id": "hl_1",
        "text": "先生が微分方程式の線形性について黒板で強調した。",
        "topic": "線形性",
        "importance": "high",
        "evidenceHint": "「線形」「足し合わせ」周辺の原文フレーズ",
        "primaryTimestampSec": 842
      }}
    ],
    "uiHints": {{
      "topFocus": ["highlights", "terms"],
      "tone": "study",
      "suggestedBadges": ["要確認"]
    }}
  }},
  "tags": ["タグ1", "タグ2", "タグ3"],
  "suggestedTitle": "港区防災カタログ仕様と営業方針"
}}

# 制約
- highlights は5〜10件。講義で言及された重要ポイントを漏れなく拾う。
- conversationHighlights は5〜12件。**自然な日本語1文**で、あとで一覧として読み返せる読み物スタイル（例:「先生が線形性について黒板で強調した」「学生が∫の導出で質問し、先生が部分積分で答えた」）。
  - `highlights` とは別物。`highlights` は短い箇条書き、`conversationHighlights` はそれより柔らかい「講義ハイライトカード」。
  - 新しく導入された概念、学生の質問、先生の強調、実演/例題、驚くべき結論を優先する。
  - `importance` は "high" | "medium" | "low"。中心概念・試験に出そうな話は high。
  - `primaryTimestampSec` は代表的な秒数（整数）、不明なら省略。
  - `evidenceHint` は原文に出た短いフレーズ（最大40字）。
- terms は最大15件。講義中に出た専門用語・概念は可能な限り全て記載する。
- sections は3〜10個。講義の流れに沿って全トピックをカバーする。各sectionのbulletsも3〜8件で詳しく記載する。
- どれも無理に埋めない。無ければ [] や空でOK。ただし highlights, terms, sections は講義内容に応じて十分な量を出す。
- tags は2〜6個、短く簡潔に（重複/ハッシュは避ける）
- suggestedTitle: 10〜25文字。内容が具体的にわかるタイトルにする。体言止め厳守。文章禁止。「〜について」「〜の件」等の助詞で終わらない。主要トピックや結論のキーワードを含める。例:「港区防災カタログ仕様と営業方針」「微分方程式の基礎と応用例」「Q2売上目標と採用計画の確定」

# 文字起こし
{text}
"""


def _split_text_into_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """
    長いテキストを重複ありのチャンクに分割する。
    文の途中で切れないよう、句点・改行で区切る。
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        if end >= len(text):
            chunks.append(text[start:])
            break

        # 句点、改行、または句読点で区切りを探す
        best_break = end
        for sep in ["。\n", "。", ".\n", ".", "\n\n", "\n", "、", ","]:
            # 区切り位置を後方から探す
            pos = text.rfind(sep, start + chunk_size - 5000, end)
            if pos > start:
                best_break = pos + len(sep)
                break

        chunks.append(text[start:best_break])
        # オーバーラップを考慮して次の開始位置を決定
        start = max(start + 1, best_break - overlap)

    return chunks


# ============================================================================
# Hierarchical Map→Reduce Summarization
# ============================================================================

# Map phase: Extract key points (simpler, faster)
_MAP_PROMPT_LECTURE = """あなたは講義内容を整理するアシスタントです。以下の文字起こし（一部分）から、重要な事実（Facts）を漏れなく抽出してください。

# 出力ルール（厳守）
- JSON形式のみ出力
- facts: 重要な事実を最大25件抽出。内容が豊富な場合は多めに抽出する。各factは type + text + evidenceHint を持つ
  - type: "definition" | "procedure" | "key-point" | "metric" | "example" | "todo" | "exam-scope"
  - text: 事実の内容（1〜2文・最大150文字。具体的な数字・固有名詞・条件を省略しない）
  - evidenceHint: 根拠キーフレーズ（最大30文字）
- terms: 専門用語を5-12件抽出（term, definition）。講義中に出た専門用語は可能な限り全て抽出する。
- 文字起こしに無い情報は絶対に作らない
- 重要語の置換禁止：固有名詞を別語に置換しない。自信がなければ原文の語を採用
- 情報の取りこぼし防止：文字起こしに含まれる重要な事実・概念・手順・例は全て拾う。少なすぎる出力は禁止。

出力形式:
{{"facts": [{{"type": "key-point", "text": "...", "evidenceHint": "..."}}], "terms": [{{"term": "用語", "definition": "説明"}}]}}

# 文字起こし
{text}
"""

_MAP_PROMPT_MEETING = """あなたは会議内容を整理するアシスタントです。以下の文字起こし（一部分）から、"原文にある事実だけ" を抽出してください。

# 絶対ルール
1) 原文に無い固有名詞・製品名・数値・日付・期限・数量は一切書かない。
2) 用語保護：原文に出たキーワードは表記を変えない。似た語に置換しない。
3) evidenceHint は原文の短い引用フレーズを必ず付ける（捏造防止の根拠）。
4) ASRノイズっぽい語（意味不明・文脈不一致・誤字）は捨てる。確信が無ければ書かない。

# 枝（抽出しないもの）
- 具体例・エピソード・雑談・比喩・背景説明の細部
- 結論に直結しない経緯や個別事情
- 反復、相槌、同義の言い換え

# 採用条件（この3つのどれか）
A) 意思決定に直結する問い
B) 方針が変わる争点（トレードオフがある）
C) 成果/コスト/期限/リスクに影響する制約

# type ガイド
- decision: 実行が確定した決定のみ
- key-point: 方針/暫定合意/優先順位/評価基準（決定未満の幹）
- todo: 次アクション（成果物が想像できる粒度で。「検討する」は避け「方針案を整理」等に）
- open-question: 未決事項（何が未決か + 判断軸のヒント）
- risk: 大きなリスクや依存関係
- purchase: 購入対象（品名を具体的に。複数あれば各々1factにする）
- change: 方針の見直し/やめる/転換（具体的に何をどう変えたか）
- priority: 優先順位の合意（〜より先に〜する）
- info: 背景共有・補足知識・前提条件（結論には直結しないが文脈理解に必要）
- 製品名・固有名詞が複数出た場合は、それぞれ key-point として残す
- decision/todo には「誰が」「いつまでに」を text に含めよ（原文にあれば）

# 出力ルール（厳守）
- JSON形式のみ出力
- facts: 主要な事実を最大25件。内容が豊富な場合は多めに抽出する。各factは type + text + evidenceHint を持つ
  - text: 1〜2文・最大150文字。具体的な数字・固有名詞・条件を省略しない。
  - evidenceHint: 原文の引用フレーズ（最大30文字）
- type: "decision" | "todo" | "risk" | "key-point" | "metric" | "open-question" | "purchase" | "change" | "priority" | "info"

出力形式:
{{"facts": [{{"type": "key-point", "text": "...", "evidenceHint": "..."}}]}}

# 文字起こし
{text}
"""

# Reduce phase: Synthesize extracted points into structured JSON
_REDUCE_PROMPT_LECTURE = """あなたは講義ノート作成のプロです。以下は複数パートから抽出されたポイントです。これらを統合し、構造化された講義ノートを生成してください。

# 入力（抽出結果）
{extracted_points}

# 最重要（厳守）
- 出力は「JSONのみ」。前置き/説明/Markdown禁止
- 重複を排除し重要度順に整理
- 不明/曖昧は needConfirm=true
- 重要語の置換禁止：固有名詞を別語に置換しない。自信がなければ原文の語を採用
- JSON内テキストに Markdown記法（**太字**, ***強調***, ##見出し 等）は一切使わない。平文で書く。

# 出力スキーマ
{{
  "summary": {{
    "type": "lecture",
    "highlights": [{{"text": "重要ポイント", "needConfirm": false}}],
    "overview": "800〜2000文字の網羅的な概要。冒頭でテーマと目的を述べ、各トピックの要点を順に説明する。具体例や重要な補足も盛り込み、具体的な数字・固有名詞・定義・条件を省略しない。overviewだけで講義全体の流れと内容の詳細が把握できるようにする。読点で区切り1文を短くする。Markdown記法禁止。",
    "theme": {{"text": "今日のテーマ", "needConfirm": false}},
    "terms": [{{"term": "用語", "definition": "説明", "examples": [], "needConfirm": false}}],
    "sections": [{{"title": "セクション", "bullets": ["要点"], "commonMistakes": [], "needConfirm": false}}],
    "formulasOrProcedures": [],
    "exercises": {{"examples": [], "homework": [], "examScope": []}},
    "studyGuide": {{"recommendedOrder": ["highlights", "terms", "sections"], "memoryHooks": []}},
    "uiHints": {{"topFocus": ["highlights", "terms"], "tone": "study", "suggestedBadges": ["要確認"]}}
  }},
  "tags": ["タグ1", "タグ2"],
  "suggestedTitle": "港区防災カタログ仕様と営業方針"
}}

制約: highlights5-10件, terms最大15件, sections3-10件（各sectionのbulletsは3-8件で詳しく）。suggestedTitle: 10〜25文字・体言止め厳守。主要トピックや結論を含める（例:「港区防災カタログ仕様と営業方針」「微分方程式の基礎と応用例」）。抽出されたfactsを漏れなく反映し、具体的な数字・固有名詞・定義を省略しないこと。
"""

_REDUCE_PROMPT_MEETING = """あなたは議事録の編集者です。以下は複数パートから抽出された evidence付き facts です。
会議後にユーザーが最初に知りたいのは「何が決まり、自分は何をするか」である。
説明文より、決定・次アクション・未決事項を優先して構造化JSONにまとめよ。

# 入力（抽出済みfacts — evidenceHintが根拠）
{extracted_points}

# 絶対ルール（厳守）
1) 入力factsに無い固有名詞・数値・日付・期限は一切書かない。
2) due は具体日付がある時だけ。それ以外は "期限不明"。ただし曖昧表現（3月末→4月等）は text に残す。
3) 「結論は明確でない」は decisions=0 かつ todos=0 の時だけ許可。
4) ASRノイズっぽい語は捨てる。
5) highlights には [decision][todo][info][risk][open_question][change] を使用。[discussion]禁止。
6) JSON内テキストに Markdown記法（**太字**, ***強調***, ##見出し 等）は一切使わない。平文で書く。
7) ownerSource/dueSource: 原文で明示的に名前/日付が出ている場合は "explicit"、文脈から推定した場合は "inferred"、不明なら "unknown"。
8) 根拠のない decision/todo は作るな。必ず evidenceHint を付けよ。

# 設計思想
- bottomLine を最初に読むだけで「何が決まり、何をするか」が分かること。
- 結論が出ていない会議では無理に結論化せず outcomeStatus="discussion_only" または "blocked" を返せ。
- 重要だが未決のものは openQuestions に送れ。

# "薄さ防止"の必須要件
- highlights先頭2行は: A) [decision] 今回の結論、B) [todo] 次の一手
- type="purchase" のfactsは decisions または todos に必ず列挙する
- type="change" のfactsは highlights に [change] として必ず入れる
- type="priority" のfactsは todos の priority に反映する
- todos は 3〜8件出す。1件だけにしない。

# 整合チェック→整形
Step1（内部作業）: factsの evidenceHint を見て根拠が確かなものだけ採用。重複は統合。
Step2: Step1の材料だけで下記JSON形式で出力。

# 出力スキーマ
{{
  "summary": {{
    "type": "meeting",
    "bottomLine": "5〜10文（400〜800文字）。この会議の全体像を網羅的にまとめる。何が決まり、次に何をするかに加え、主要な議論テーマ全て、判断の背景・理由、重要な変更点や合意事項も含める。具体的な数字・固有名詞・条件を省略せず記載する。結論が出ていなければ『未決: 〜について継続検討』と書く。読むだけで会議の全容と詳細が掴めるようにする。",
    "outcomeStatus": "decided | partially_decided | discussion_only | blocked",
    "whyItMatters": "1文。この会議が何に効く/何の判断に必要だったか。",
    "highlights": [
      {{"text": "今回の結論: （具体的に1行）", "category": "decision", "needConfirm": false}},
      {{"text": "次の一手: （具体的に1行）", "category": "todo", "needConfirm": false}}
    ],
    "decisions": [{{
      "text": "決定事項（具体的に）",
      "status": "confirmed | tentative | inferred",
      "owner": "担当者名 or 担当不明",
      "ownerSource": "explicit | inferred | unknown",
      "due": "YYYY-MM-DD | 期限不明",
      "dueSource": "explicit | inferred | unknown",
      "reason": "なぜこの決定に至ったか（1文）",
      "confidence": 0.9,
      "evidenceHint": "根拠となる原文の引用フレーズ",
      "needConfirm": false
    }}],
    "todos": [{{
      "text": "成果物ベースのTODO（具体的に）",
      "owner": "担当者名 or 担当不明",
      "ownerSource": "explicit | inferred | unknown",
      "due": "YYYY-MM-DD | 期限不明",
      "dueSource": "explicit | inferred | unknown",
      "priority": "high | mid | low",
      "blocking": "false",
      "confidence": 0.85,
      "evidenceHint": "根拠となる原文の引用フレーズ",
      "needConfirm": false
    }}],
    "openQuestions": [{{
      "text": "未決事項",
      "impact": "high | mid | low",
      "whyOpen": "なぜ未決か（1文）",
      "owner": "確認担当 or 不明",
      "nextCheck": "次回確認タイミング or 不明",
      "needConfirm": true
    }}],
    "decisionLog": [{{
      "topic": "議論テーマ",
      "conclusion": "結論（合意 or 保留（要確認））",
      "reason": "結論に至った理由",
      "remainingIssues": "残課題があれば"
    }}],
    "contextNotes": [{{
      "topic": "背景共有テーマ",
      "summary": "共有された背景情報の要約"
    }}],
    "keywords": [{{"text": "重要語"}}],
    "participants": [{{"name": "話者名or不明", "role": "不明"}}]
  }},
  "tags": ["タグ1", "タグ2"],
  "suggestedTitle": "港区防災カタログ仕様と営業方針"
}}

# 制約
- bottomLine: 必須。最初に結論を書く。
- outcomeStatus: 必須。decided / partially_decided / discussion_only / blocked のいずれか。
- highlights: 5〜10件。先頭「今回の結論:」→「次の一手:」。[discussion]禁止。会議で言及された重要事項は全て拾う。
- decisions: 最大8件。status/confidence/evidenceHint 必須。原文の合意・承認・決定は全て記載。
- todos: 3〜12件（1件だけにしない）。confidence/evidenceHint 必須。具体的なアクションは全て拾う。
- openQuestions: 0〜5件
- decisionLog: 3〜8件。議論された全トピックについて結局どうなったかを書く。
- contextNotes: 0〜5件。背景共有や補足情報。
- todosの「検討/確認/調査」は成果物ベースに言い換える
- suggestedTitle: 10〜25文字。内容が具体的にわかるタイトルにする。体言止め厳守。文章禁止。「〜について」「〜の件」等の助詞で終わらない。主要トピックや結論のキーワードを含める。例:「港区防災カタログ仕様と営業方針」「微分方程式の基礎と応用例」「Q2売上目標と採用計画の確定」
"""


async def _map_extract_chunk(chunk: str, mode: str, chunk_index: int) -> dict:
    """
    Map phase: Extract key points from a single chunk.
    Uses simpler prompt for faster extraction.
    """
    from vertexai.generative_models import GenerationConfig

    prompt_template = _MAP_PROMPT_LECTURE if mode == "lecture" else _MAP_PROMPT_MEETING
    prompt = prompt_template.format(text=chunk)

    try:
        resp = await _timed_llm_call(
            _model,
            prompt,
            GenerationConfig(
                temperature=0.3,
                max_output_tokens=FACTS_MAP_MAX_TOKENS,
                response_mime_type="application/json",
                response_schema=_SCHEMA_MAP_FACTS,
            ),
            label=f"map_{chunk_index}",
        )
        return _parse_json_with_retry(resp.text or "{}")
    except Exception as e:
        logger.warning(f"[Hierarchical] Map chunk {chunk_index} failed: {e}")
        return {}


async def _reduce_synthesize(
    extracted: List[dict],
    mode: str,
    source_len: int,
    custom_instruction: str = "",
    segments: Optional[List[dict]] = None,
) -> dict:
    """
    Reduce phase: Synthesize all extracted points into structured JSON.

    segments (Phase 7.11): transcript segments/chunks with id+time. When
    provided, the reduce prompt is appended with a segment-id-tagged
    transcript and the LLM is asked to include `sourceSegmentIds` on each
    produced bullet. Falls back to text-matching anchor_resolver (existing
    behavior) when the LLM doesn't ground a bullet.
    """
    from vertexai.generative_models import GenerationConfig

    # Combine all extracted points
    combined = json.dumps(extracted, ensure_ascii=False, indent=1)

    logger.info(f"[reduce] extracted_points_size={len(combined)} chars, n_extracts={len(extracted)}")

    # Limit size if needed (should be much smaller than original)
    if len(combined) > 40000:
        combined = combined[:40000] + "\n...(truncated)"

    prompt_template = _REDUCE_PROMPT_LECTURE if mode == "lecture" else _REDUCE_PROMPT_MEETING
    prompt = prompt_template.format(extracted_points=combined)

    # Phase 7.11: segment-id citation suffix. Shares the same contract as
    # the single-path forward-path implementation (Phase 7.10) so the
    # downstream _hydrate_source_segment_ids step handles both paths
    # uniformly.
    if segments:
        prompt += _build_segment_citation_suffix(segments)

    # Inject user custom instruction
    if custom_instruction:
        prompt += f"\n\n# ユーザーからの追加指示\n{custom_instruction}\n"

    reduce_temp = 0.3 if mode == "meeting" else 0.5
    resp = await _timed_llm_call(
        _model,
        prompt,
        GenerationConfig(
            temperature=reduce_temp,
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=_get_summary_schema(mode),
        ),
        label="reduce",
    )

    data = _parse_json_with_retry(resp.text or "{}")

    # Normalize
    summary_payload = data.get("summary") if isinstance(data, dict) else None
    summary_raw = summary_payload if isinstance(summary_payload, dict) else data
    summary_json = _normalize_summary_json(summary_raw, mode)

    ok, reason = _validate_summary_json(summary_json, mode, source_len=source_len)
    if not ok:
        logger.warning(f"[Hierarchical] Reduce validation issue: {reason}")

    tags = data.get("tags", []) if isinstance(data, dict) else []
    suggested_title = data.get("suggestedTitle") if isinstance(data, dict) else None
    if suggested_title:
        suggested_title = _truncate_title(suggested_title)

    result = {
        "summary": summary_json,
        "tags": tags if isinstance(tags, list) else [],
    }
    if suggested_title:
        result["suggestedTitle"] = suggested_title
    return result


def _verify_meeting_summary(summary_json: dict, all_facts: list) -> list:
    """Post-LLM verification pass for meeting summaries (no extra LLM call)."""
    warnings = []
    fact_hints = set()
    fact_texts = set()
    for f in all_facts:
        if isinstance(f, dict):
            hint = f.get("evidenceHint", "")
            if hint:
                fact_hints.add(hint.lower())
            text_val = f.get("text", "")
            if text_val:
                fact_texts.add(text_val.lower())

    # Check decisions grounded in facts
    for item in _coerce_list(summary_json.get("decisions")):
        if not isinstance(item, dict):
            continue
        hint = (item.get("evidenceHint") or "").lower()
        if hint and hint not in fact_hints:
            # Check partial match
            if not any(hint in ft for ft in fact_texts):
                warnings.append(f"decision evidenceHint not found in facts: {item.get('text', '')[:40]}")

    # Check todos grounded in facts
    for item in _coerce_list(summary_json.get("todos")):
        if not isinstance(item, dict):
            continue
        hint = (item.get("evidenceHint") or "").lower()
        if hint and hint not in fact_hints:
            if not any(hint in ft for ft in fact_texts):
                warnings.append(f"todo evidenceHint not found in facts: {item.get('text', '')[:40]}")

    # Count inferred owners
    inferred_count = 0
    total_count = 0
    for items_key in ("decisions", "todos"):
        for item in _coerce_list(summary_json.get(items_key)):
            if not isinstance(item, dict):
                continue
            total_count += 1
            if item.get("ownerSource") == "inferred":
                inferred_count += 1
    if total_count > 0 and inferred_count / total_count > 0.5:
        warnings.append(f"High inference rate: {inferred_count}/{total_count} owners are inferred")

    # outcomeStatus consistency
    outcome = summary_json.get("outcomeStatus", "")
    decisions = _coerce_list(summary_json.get("decisions"))
    if outcome == "decided" and len(decisions) == 0:
        warnings.append("outcomeStatus=decided but no decisions found")
    if outcome == "discussion_only" and len(decisions) > 0:
        warnings.append("outcomeStatus=discussion_only but decisions exist")

    return warnings


async def _generate_summary_hierarchical(
    text: str,
    mode: str,
    progress_callback=None,
    custom_instruction: str = "",
    segments: Optional[List[dict]] = None,
) -> dict:
    """
    Hierarchical Map→Reduce summarization with optimised chunking.

    segments (Phase 7.11): transcript segment list with {id, text, startMs,
    endMs, speaker?}. Threaded into the FINAL reduce pass only — the
    per-chunk map phase and per-group intermediate reduces keep their
    current lean prompts. The final reduce appends a segment-id-tagged
    transcript and asks the LLM to return `sourceSegmentIds` on each
    bullet; downstream `_hydrate_source_segment_ids` validates and
    attaches startSec/endSec. Bullets the LLM didn't ground still fall
    back to anchor_resolver text matching.

    Key optimisations vs previous version:
    - MAP_CHUNK_SIZE (12K) instead of CHUNK_SIZE (80K) → many small, fast map calls
    - asyncio.Semaphore caps concurrent LLM calls → avoids 429 / rate-limit
    - Multi-level reduce when extracted data exceeds REDUCE_BATCH_LIMIT
    - progress_callback(done, total, phase) for real-time progress updates
    - Returns extracted facts alongside summary for downstream quiz generation
    """
    import time as time_module
    start_time = time_module.perf_counter()

    # ── Split into SMALLER chunks for fast parallel extraction ──
    chunks = _split_text_into_chunks(text, chunk_size=MAP_CHUNK_SIZE, overlap=MAP_CHUNK_OVERLAP)
    total_chunks = len(chunks)
    logger.info(
        f"[Hierarchical] {len(text)} chars → {total_chunks} chunks "
        f"(chunk_size={MAP_CHUNK_SIZE}, overlap={MAP_CHUNK_OVERLAP})"
    )

    if progress_callback:
        await progress_callback(0, total_chunks, "map")

    # ── Map phase: semaphore-controlled parallel extraction with progress ──
    map_start = time_module.perf_counter()
    sem = asyncio.Semaphore(MAX_CONCURRENT_MAP)
    done_count = 0

    async def _guarded_map(chunk: str, idx: int) -> dict:
        nonlocal done_count
        async with sem:
            result = await _map_extract_chunk(chunk, mode, idx)
            done_count += 1
            if progress_callback:
                await progress_callback(done_count, total_chunks, "map")
            return result

    tasks = [_guarded_map(chunk, i) for i, chunk in enumerate(chunks)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    extracted = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"[Hierarchical] Map chunk {i} exception: {result}")
            continue
        if isinstance(result, dict) and result:
            extracted.append(result)

    map_time = time_module.perf_counter() - map_start
    logger.info(
        f"[Hierarchical] Map phase: {len(extracted)}/{total_chunks} ok "
        f"in {map_time:.2f}s ({map_time/max(total_chunks,1):.2f}s/chunk avg)"
    )

    if not extracted:
        raise ValueError("All map extractions failed")

    # ── Collect all facts from Map results ──
    all_facts = []
    for ex in extracted:
        for f in (ex.get("facts") or ex.get("points") or []):
            if isinstance(f, dict):
                all_facts.append(f)
            elif isinstance(f, str):
                all_facts.append({"type": "key-point", "text": f})

    if progress_callback:
        await progress_callback(total_chunks, total_chunks, "reduce")

    # ── Reduce phase: single or multi-level ──
    reduce_start = time_module.perf_counter()
    combined_size = sum(len(json.dumps(e, ensure_ascii=False)) for e in extracted)
    logger.info(f"[Hierarchical] Combined extracted size: {combined_size} chars")

    if combined_size > REDUCE_BATCH_LIMIT and len(extracted) > 6:
        result = await _multi_level_reduce(
            extracted, mode, source_len=len(text),
            custom_instruction=custom_instruction, segments=segments,
        )
    else:
        result = await _reduce_synthesize(
            extracted, mode, source_len=len(text),
            custom_instruction=custom_instruction, segments=segments,
        )

    reduce_time = time_module.perf_counter() - reduce_start
    logger.info(f"[Hierarchical] Reduce phase completed in {reduce_time:.2f}s")

    summary_json = result.get("summary", {})

    # ── Verify pass (meeting mode only) ──
    if mode == "meeting":
        warnings = _verify_meeting_summary(summary_json, all_facts)
        if warnings:
            summary_json["verifyWarnings"] = warnings
            logger.info(f"[Hierarchical] Verify warnings: {warnings}")

    tags = result.get("tags", [])

    # Normalize tags
    keyword_candidates: List[str] = []
    for item in _coerce_list(summary_json.get("keywords")):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            keyword_candidates.append(item["text"])
    for item in _coerce_list(summary_json.get("terms")):
        if isinstance(item, dict) and isinstance(item.get("term"), str):
            keyword_candidates.append(item["term"])

    if tags:
        tags = _normalize_tags(tags, keyword_candidates, mode)
    else:
        tags = _extract_tags_from_summary_json(summary_json, mode)
        if not tags:
            tags = _normalize_tags([], keyword_candidates, mode)

    if mode == "lecture" and not _coerce_list(summary_json.get("keywords")) and tags:
        summary_json["keywords"] = [{"text": tag} for tag in tags]

    summary_markdown = _summary_json_to_markdown_v2(summary_json)

    if progress_callback:
        await progress_callback(total_chunks, total_chunks, "done")

    total_time = time_module.perf_counter() - start_time
    logger.info(
        f"[Hierarchical] DONE {len(text)} chars in {total_time:.2f}s "
        f"(map={map_time:.2f}s [{total_chunks} chunks], reduce={reduce_time:.2f}s), "
        f"facts={len(all_facts)}"
    )

    final_result = {
        "summaryJson": summary_json,
        "summaryType": summary_json.get("type"),
        "summaryJsonVersion": SUMMARY_JSON_VERSION,
        "summaryMarkdown": summary_markdown,
        "tags": tags,
        "facts": all_facts,
    }
    # Extract suggestedTitle from reduce result
    suggested_title = result.get("suggestedTitle")
    if suggested_title:
        final_result["suggestedTitle"] = suggested_title
    return final_result


async def _multi_level_reduce(
    extracted: List[dict],
    mode: str,
    source_len: int,
    custom_instruction: str = "",
    segments: Optional[List[dict]] = None,
) -> dict:
    """
    Multi-level reduce for large numbers of map results.
    Groups extracted data → parallel partial reduces → final reduce.

    Phase 7.11: segments flow only through the FINAL reduce pass.
    Intermediate group-level reduces don't get the segment-id transcript
    to keep their prompts small; they just collapse extracted facts into
    extraction-format, which the final reduce then grounds against the
    real transcript via sourceSegmentIds.
    """
    GROUP_SIZE = 6  # chunks per reduce group

    groups = [extracted[i:i + GROUP_SIZE] for i in range(0, len(extracted), GROUP_SIZE)]
    logger.info(f"[Hierarchical] Multi-level reduce: {len(extracted)} chunks → {len(groups)} groups")

    sem = asyncio.Semaphore(MAX_CONCURRENT_MAP)

    async def _guarded_reduce(group: List[dict]) -> dict:
        async with sem:
            return await _reduce_synthesize(group, mode, source_len=source_len)

    tasks = [_guarded_reduce(g) for g in groups]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect intermediate summaries as extraction-format for final reduce
    intermediate = []
    fallback_tags = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"[Hierarchical] Group reduce failed: {r}")
            continue
        if not isinstance(r, dict):
            continue
        summary = r.get("summary", {})
        fallback_tags.extend(r.get("tags", []))
        # Convert summary back to extraction-like format
        points = []
        for h in _coerce_list(summary.get("highlights")):
            if isinstance(h, dict) and h.get("text"):
                points.append(h["text"])
        overview = summary.get("overview", "")
        if overview:
            points.insert(0, overview[:500])
        entry: dict = {"points": points}
        terms = _coerce_list(summary.get("terms"))
        if terms:
            entry["terms"] = terms
        todos = _coerce_list(summary.get("todos"))
        if todos:
            entry["todos"] = todos
        decisions = _coerce_list(summary.get("decisions"))
        if decisions:
            entry["decisions"] = decisions
        questions = _coerce_list(summary.get("openQuestions"))
        if questions:
            entry["questions"] = questions
        intermediate.append(entry)

    if not intermediate:
        raise ValueError("All reduce groups failed")

    # Final reduce (apply custom instruction + segment-id citation only here)
    logger.info(f"[Hierarchical] Final reduce from {len(intermediate)} intermediate results")
    final = await _reduce_synthesize(
        intermediate,
        mode,
        source_len=source_len,
        custom_instruction=custom_instruction,
        segments=segments,
    )
    if not final.get("tags") and fallback_tags:
        final["tags"] = fallback_tags
    return final


# Feature flag for hierarchical summarization
USE_HIERARCHICAL_SUMMARY = os.environ.get("USE_HIERARCHICAL_SUMMARY", "1") == "1"


def _merge_summary_jsons(summaries: List[dict], mode: str) -> dict:
    """
    複数のチャンク要約を1つに統合する。
    """
    if not summaries:
        return {}
    if len(summaries) == 1:
        return summaries[0]

    merged = {
        "type": mode,
        "highlights": [],
        "overview": "",
    }

    # 全チャンクからハイライトを収集（重複排除）
    seen_highlights = set()
    all_highlights = []
    for s in summaries:
        for h in _coerce_list(s.get("highlights")):
            text = h.get("text", "") if isinstance(h, dict) else ""
            if text and text not in seen_highlights:
                seen_highlights.add(text)
                all_highlights.append(h)
    merged["highlights"] = all_highlights[:7]  # 最大7件

    # オーバービューを結合
    overviews = [s.get("overview", "") for s in summaries if s.get("overview")]
    merged["overview"] = "\n\n".join(overviews)[:2000]  # 最大2000文字

    if mode == "lecture":
        # 用語を統合（重複排除）
        seen_terms = set()
        all_terms = []
        for s in summaries:
            for t in _coerce_list(s.get("terms")):
                term = t.get("term", "") if isinstance(t, dict) else ""
                if term and term not in seen_terms:
                    seen_terms.add(term)
                    all_terms.append(t)
        merged["terms"] = all_terms[:10]

        # セクションを順番に結合
        all_sections = []
        for i, s in enumerate(summaries):
            for sec in _coerce_list(s.get("sections")):
                if isinstance(sec, dict):
                    # チャンク番号をタイトルに追加（任意）
                    all_sections.append(sec)
        merged["sections"] = all_sections[:8]

        # その他のフィールド
        merged["theme"] = summaries[0].get("theme", {"text": "", "needConfirm": True})
        merged["formulasOrProcedures"] = []
        for s in summaries:
            merged["formulasOrProcedures"].extend(_coerce_list(s.get("formulasOrProcedures")))
        merged["formulasOrProcedures"] = merged["formulasOrProcedures"][:6]

        merged["exercises"] = {"examples": [], "homework": [], "examScope": []}
        for s in summaries:
            ex = s.get("exercises", {})
            if isinstance(ex, dict):
                merged["exercises"]["examples"].extend(_coerce_list(ex.get("examples")))
                merged["exercises"]["homework"].extend(_coerce_list(ex.get("homework")))
                merged["exercises"]["examScope"].extend(_coerce_list(ex.get("examScope")))

        merged["studyGuide"] = summaries[0].get("studyGuide", {
            "recommendedOrder": ["highlights", "terms", "sections"],
            "memoryHooks": []
        })
        merged["uiHints"] = summaries[0].get("uiHints", {
            "topFocus": ["highlights", "terms"],
            "tone": "study",
            "suggestedBadges": ["要確認"]
        })

        # キーワード統合
        seen_kw = set()
        all_keywords = []
        for s in summaries:
            for kw in _coerce_list(s.get("keywords")):
                text = kw.get("text", "") if isinstance(kw, dict) else ""
                if text and text not in seen_kw:
                    seen_kw.add(text)
                    all_keywords.append(kw)
        merged["keywords"] = all_keywords[:6]

    else:  # meeting mode
        # 決定事項を統合
        merged["decisions"] = []
        for s in summaries:
            merged["decisions"].extend(_coerce_list(s.get("decisions")))

        # TODOを統合
        merged["todos"] = []
        for s in summaries:
            merged["todos"].extend(_coerce_list(s.get("todos")))

        # 未決事項を統合
        merged["openQuestions"] = []
        for s in summaries:
            merged["openQuestions"].extend(_coerce_list(s.get("openQuestions")))

        # 議論ポイントを統合
        merged["discussionPoints"] = []
        for s in summaries:
            merged["discussionPoints"].extend(_coerce_list(s.get("discussionPoints")))

        # キーワード統合
        seen_kw = set()
        all_keywords = []
        for s in summaries:
            for kw in _coerce_list(s.get("keywords")):
                text = kw.get("text", "") if isinstance(kw, dict) else ""
                if text and text not in seen_kw:
                    seen_kw.add(text)
                    all_keywords.append(kw)
        merged["keywords"] = all_keywords[:6]

        # 参加者を統合（重複排除）
        seen_participants = set()
        all_participants = []
        for s in summaries:
            for p in _coerce_list(s.get("participants")):
                name = p.get("name", "") if isinstance(p, dict) else ""
                if name and name not in seen_participants:
                    seen_participants.add(name)
                    all_participants.append(p)
        merged["participants"] = all_participants

        merged["timeline"] = []
        for s in summaries:
            merged["timeline"].extend(_coerce_list(s.get("timeline")))

        merged["uiHints"] = summaries[0].get("uiHints", {
            "topFocus": ["decisions", "todos"],
            "tone": "business",
            "suggestedBadges": ["要確認"]
        })

    return merged


async def _summarize_single_chunk(
    text: str,
    mode: str,
    chunk_index: int,
    total_chunks: int,
) -> dict:
    """
    単一チャンクを要約する内部関数。
    """
    from vertexai.generative_models import GenerationConfig

    # チャンク情報をプロンプトに追加
    chunk_note = ""
    if total_chunks > 1:
        chunk_note = f"\n\n【注意】これは全{total_chunks}パートのうち、パート{chunk_index + 1}です。このパートの内容のみを要約してください。"

    prompt = _build_summary_tags_prompt(text, mode) + chunk_note

    max_attempts = 2
    summary_json: dict = {}

    for attempt in range(max_attempts):
        resp = await _timed_llm_call(
            _model,
            prompt,
            GenerationConfig(
                temperature=0.6,
                max_output_tokens=4096,
                response_mime_type="application/json",
                response_schema=_get_summary_schema(mode),
            ),
            label=f"summary_chunk_{chunk_index}",
        )

        data = _parse_json_with_retry(resp.text or "{}")
        summary_payload = data.get("summary") if isinstance(data, dict) else None
        summary_raw = summary_payload if isinstance(summary_payload, dict) else data
        summary_json = _normalize_summary_json(summary_raw, mode)
        ok, reason = _validate_summary_json(summary_json, mode, source_len=len(text))
        if ok:
            # タグも返す
            tags_raw = data.get("tags") if isinstance(data, dict) else []
            return {"summary": summary_json, "tags": tags_raw if isinstance(tags_raw, list) else []}
        summary_json = {}

    # フォールバック
    return {"summary": _build_summary_json_fallback(mode, reason=f"チャンク{chunk_index + 1}の要約に失敗"), "tags": []}


def _normalize_tags(raw_tags: List[Any], keywords: List[Any], mode: str) -> List[str]:
    """タグを正規化し、不足時は補完する"""
    tags: List[str] = []
    
    # 1. Clean raw tags
    import re
    cleaned_candidates = []
    
    # Merge sources: raw_tags -> keywords
    sources = list(raw_tags)
    if keywords:
        sources.extend(keywords)

    for t in sources:
        if not isinstance(t, str):
            continue
        s = t.strip()
        if not s:
            continue
        # Remove leading hashes
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        
        # Remove trailing punctuation
        s = re.sub(r"[#、。,.!\s]+$", "", s)
        # Remove common suffixes like "のテスト", "の確認"
        s = re.sub(r"(のテスト|の確認|テスト|確認)$", "", s)
        
        if s:
            cleaned_candidates.append(s)

    # 3. Default fallback if absolutely empty
    if not cleaned_candidates:
        if mode == "meeting":
            cleaned_candidates = ["会議"]
        elif mode == "lecture":
            cleaned_candidates = ["講義"]
        else:
            cleaned_candidates = ["メモ"]
            
    # 4. Dedup and limit
    seen = set()
    deduped = []
    for t in cleaned_candidates:
        if t in seen:
            continue
        seen.add(t)
        deduped.append(t)
        if len(deduped) >= 4:
            break
            
    return deduped





def get_user_custom_prompts(user_id: str | None) -> tuple[str, str]:
    """Read custom summary/quiz prompts from Firestore user doc."""
    if not user_id:
        return "", ""
    try:
        from app.firebase import db
        doc = db.collection("users").document(user_id).get(field_paths=["customSummaryPrompt", "customQuizPrompt"])
        if not doc.exists:
            return "", ""
        data = doc.to_dict() or {}
        return (
            (data.get("customSummaryPrompt") or "")[:500],
            (data.get("customQuizPrompt") or "")[:500],
        )
    except Exception as e:
        logger.warning(f"Failed to read custom prompts for user {user_id}: {e}")
        return "", ""


async def generate_summary_and_tags(
    text: str,
    mode: str = "lecture",
    progress_callback=None,
    custom_instruction: str = "",
    segments: Optional[List[dict]] = None,
) -> dict:
    """
    要約・タグ・再生リストを1回の Gemini 呼び出しで生成する。
    長い文字起こしの場合はチャンク分割して並列処理する。

    progress_callback: async (done, total, phase) for real-time progress.
    segments: transcript chunks/diarized segments. When provided, each
        generated bullet is post-matched against segments to attach
        anchorMs / segmentIds (grounded timestamp links for the client).
    Returns dict with summaryJson, summaryMarkdown, tags, and optionally 'facts'.
    """
    # Transcript length validation
    if len(text) < MIN_TRANSCRIPT_LENGTH:
        logger.warning(f"Transcript too short for summary: {len(text)} chars")
        summary_json = _build_summary_json_fallback(mode, reason="入力がありません")
        return {
            "summaryJson": summary_json,
            "summaryType": summary_json.get("type"),
            "summaryJsonVersion": SUMMARY_JSON_VERSION,
            "summaryMarkdown": _summary_json_to_markdown_v2(summary_json),
            "tags": ["要確認"],
        }

    _ensure_model()

    n_chunks = math.ceil(len(text) / MAP_CHUNK_SIZE)
    use_hierarchical = _should_use_hierarchical(len(text))
    logger.info(
        f"[summary] len={len(text)} mode={mode} chunk_size={MAP_CHUNK_SIZE} "
        f"overlap={MAP_CHUNK_OVERLAP} n_chunks={n_chunks} hierarchical={use_hierarchical} "
        f"segments={len(segments) if segments else 0}"
    )

    if use_hierarchical:
        if USE_HIERARCHICAL_SUMMARY:
            logger.info("[Hierarchical] Using Map→Reduce summarization")
            # Phase 7.11: segments flow through to the FINAL reduce so long
            # sessions also get forward-path citation (sourceSegmentIds).
            result = await _generate_summary_hierarchical(
                text,
                mode,
                progress_callback=progress_callback,
                custom_instruction=custom_instruction,
                segments=segments,
            )
        else:
            logger.info("Using traditional chunked summarization")
            result = await _generate_summary_chunked(text, mode, custom_instruction=custom_instruction)
    else:
        # 通常の要約処理（singleパス）— Phase 7.10: segments を渡して LLM に
        # sourceSegmentIds を返してもらう forward-path を有効化
        result = await _generate_summary_single(
            text, mode, custom_instruction=custom_instruction, segments=segments
        )

    # Post-process (always runs when segments are available):
    #   1. sourceSegmentIds validation + startSec/endSec derivation
    #      (resolves LLM-returned ids against the real segment time range)
    #   2. anchor_resolver fallback for bullets without sourceSegmentIds
    #      (existing text-matching behavior; now ids-preferred)
    if segments and isinstance(result, dict) and isinstance(result.get("summaryJson"), dict):
        try:
            _hydrate_source_segment_ids(result["summaryJson"], segments)
        except Exception as e:
            logger.warning(f"[summary] sourceSegmentIds hydration failed (non-fatal): {e}")
        try:
            from app.services.anchor_resolver import enrich_summary_with_anchors
            enrich_summary_with_anchors(result["summaryJson"], segments)
        except Exception as e:
            logger.warning(f"[summary] anchor enrichment failed (non-fatal): {e}")

    return result


# ---------------------------------------------------------------------------
# Phase 7.10 — forward-path citation hydration
# ---------------------------------------------------------------------------


_BULLET_LIST_KEYS = (
    "highlights",
    "decisions",
    "todos",
    "openQuestions",
    "discussionPoints",
    "conversationHighlights",
)


def _hydrate_source_segment_ids(
    summary_json: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> None:
    """Validate LLM-returned sourceSegmentIds and attach start/endSec.

    For each bullet in every known list:
      - Drop any `sourceSegmentIds` entry that isn't in the real segment map.
      - When ≥1 valid id remains, set:
          `segmentId` = first id (stable representative for UI jump)
          `startSec`, `endSec` = min/max over matched segments
          `startMs`, `endMs` = same but in ms
          `sourceCount` = number of matched ids
      - Bullets without LLM-provided ids are left untouched here so the
        downstream anchor_resolver can still fill them via text matching.
    """
    if not isinstance(summary_json, dict):
        return

    seg_by_id: Dict[str, Dict[str, Any]] = {}
    for seg in segments:
        sid = (
            seg.get("id")
            or seg.get("segmentId")
            or (seg.get("segmentIds") or [None])[0]
        )
        if not sid:
            continue
        sid = str(sid)
        # Prefer startMs if present, otherwise derive from startSec
        start_ms = seg.get("startMs")
        if start_ms is None and seg.get("startSec") is not None:
            try:
                start_ms = int(float(seg["startSec"]) * 1000)
            except (TypeError, ValueError):
                start_ms = None
        end_ms = seg.get("endMs")
        if end_ms is None and seg.get("endSec") is not None:
            try:
                end_ms = int(float(seg["endSec"]) * 1000)
            except (TypeError, ValueError):
                end_ms = None
        if start_ms is None:
            continue
        seg_by_id[sid] = {
            "startMs": int(start_ms),
            "endMs": int(end_ms) if isinstance(end_ms, (int, float)) else int(start_ms),
        }

    if not seg_by_id:
        return

    def _apply(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_ids = item.get("sourceSegmentIds")
            if not isinstance(raw_ids, list) or not raw_ids:
                continue
            matched = [str(x) for x in raw_ids if isinstance(x, (str, int)) and str(x) in seg_by_id]
            if not matched:
                # LLM hallucinated ids — drop entirely so anchor_resolver can
                # retry via text matching.
                item["sourceSegmentIds"] = []
                continue
            # Cap at 4 to enforce prompt-time constraint on the server side too.
            matched = matched[:4]
            item["sourceSegmentIds"] = matched
            start_ms = min(seg_by_id[sid]["startMs"] for sid in matched)
            end_ms = max(seg_by_id[sid]["endMs"] for sid in matched)
            item["segmentId"] = matched[0]
            item["segmentIds"] = matched        # maintained for v1 clients
            item["startMs"] = int(start_ms)
            item["endMs"] = int(end_ms)
            item["startSec"] = round(start_ms / 1000.0, 2)
            item["endSec"] = round(end_ms / 1000.0, 2)
            item["sourceCount"] = len(matched)
            # anchorMs retained for back-compat; anchor_resolver won't override
            item.setdefault("anchorMs", int(start_ms))

    for key in _BULLET_LIST_KEYS:
        _apply(summary_json.get(key))

    # sections[*].bullets (lecture mode)
    sections = summary_json.get("sections")
    if isinstance(sections, list):
        for sec in sections:
            if isinstance(sec, dict):
                _apply(sec.get("bullets"))


async def _generate_summary_chunked(text: str, mode: str, custom_instruction: str = "") -> dict:
    """
    長い文字起こしをチャンク分割して並列要約し、統合する。
    """
    chunks = _split_text_into_chunks(text)
    total_chunks = len(chunks)
    logger.info(f"Split into {total_chunks} chunks for summarization")

    # 並列でチャンクを要約
    tasks = [
        _summarize_single_chunk(chunk, mode, i, total_chunks)
        for i, chunk in enumerate(chunks)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 成功した結果を収集
    summaries = []
    all_tags = []
    suggested_title = None
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Chunk {i} summarization failed: {result}")
            continue
        if isinstance(result, dict):
            summaries.append(result.get("summary", {}))
            all_tags.extend(result.get("tags", []))
            if not suggested_title and result.get("suggestedTitle"):
                suggested_title = result["suggestedTitle"]

    if not summaries:
        raise ValueError("All chunk summarizations failed")

    # 要約を統合
    merged_summary = _merge_summary_jsons(summaries, mode)
    merged_summary = _normalize_summary_json(merged_summary, mode)

    # タグを正規化
    keyword_candidates: List[str] = []
    for item in _coerce_list(merged_summary.get("keywords")):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            keyword_candidates.append(item["text"])
    for item in _coerce_list(merged_summary.get("terms")):
        if isinstance(item, dict) and isinstance(item.get("term"), str):
            keyword_candidates.append(item["term"])

    if all_tags:
        tags = _normalize_tags(all_tags, keyword_candidates, mode)
    else:
        tags = _extract_tags_from_summary_json(merged_summary, mode)
        if not tags:
            tags = _normalize_tags([], keyword_candidates, mode)

    if mode == "lecture" and not _coerce_list(merged_summary.get("keywords")) and tags:
        merged_summary["keywords"] = [{"text": tag} for tag in tags]

    summary_markdown = _summary_json_to_markdown_v2(merged_summary)

    logger.info(f"Chunked summarization complete: {total_chunks} chunks merged")

    result = {
        "summaryJson": merged_summary,
        "summaryType": merged_summary.get("type"),
        "summaryJsonVersion": SUMMARY_JSON_VERSION,
        "summaryMarkdown": summary_markdown,
        "tags": tags,
    }
    if suggested_title:
        result["suggestedTitle"] = suggested_title
    return result


async def _generate_summary_single(
    text: str,
    mode: str,
    custom_instruction: str = "",
    segments: Optional[List[dict]] = None,
) -> dict:
    """
    単一テキストの要約処理（通常の短いテキスト用）。

    segments (Phase 7.10): when provided, the prompt is appended with a
    segment-id-tagged transcript and the LLM is asked to return
    `sourceSegmentIds` per bullet.
    """
    from vertexai.generative_models import GenerationConfig

    prompt = _build_summary_tags_prompt(text, mode, segments=segments)
    if custom_instruction:
        prompt += f"\n\n# ユーザーからの追加指示\n{custom_instruction}\n"

    max_attempts = 2
    last_error = "summary_json_invalid"
    summary_json: dict = {}
    data: dict = {}

    for attempt in range(max_attempts):
        resp = await _timed_llm_call(
            _model,
            prompt,
            GenerationConfig(
                temperature=0.6,
                max_output_tokens=8192,
                response_mime_type="application/json",
                response_schema=_get_summary_schema(mode),
            ),
            label="summary",
        )

        data = _parse_json_with_retry(resp.text or "{}")
        summary_payload = data.get("summary") if isinstance(data, dict) else None
        summary_raw = summary_payload if isinstance(summary_payload, dict) else data
        summary_json = _normalize_summary_json(summary_raw, mode)
        ok, reason = _validate_summary_json(summary_json, mode, source_len=len(text))
        if ok:
            break
        last_error = reason
        summary_json = {}

    if not summary_json:
        raise ValueError(f"Summary JSON validation failed: {last_error}")

    tags_raw = data.get("tags") if isinstance(data, dict) else []
    if not isinstance(tags_raw, list):
        tags_raw = []

    suggested_title = data.get("suggestedTitle") if isinstance(data, dict) else None

    keyword_candidates: List[str] = []
    for item in _coerce_list(summary_json.get("keywords")):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            keyword_candidates.append(item["text"])
    for item in _coerce_list(summary_json.get("terms")):
        if isinstance(item, dict) and isinstance(item.get("term"), str):
            keyword_candidates.append(item["term"])

    if tags_raw:
        tags = _normalize_tags(tags_raw, keyword_candidates, mode)
    else:
        tags = _extract_tags_from_summary_json(summary_json, mode)
        if not tags:
            tags = _normalize_tags([], keyword_candidates, mode)

    if mode == "lecture" and not _coerce_list(summary_json.get("keywords")) and tags:
        summary_json["keywords"] = [{"text": tag} for tag in tags]

    summary_markdown = _summary_json_to_markdown_v2(summary_json)
    result = {
        "summaryJson": summary_json,
        "summaryType": summary_json.get("type"),
        "summaryJsonVersion": SUMMARY_JSON_VERSION,
        "summaryMarkdown": summary_markdown,
        "tags": tags,
    }
    if suggested_title:
        result["suggestedTitle"] = suggested_title
    return result


def _build_summary_json_fallback(mode: str, reason: str = None) -> dict:
    mode_key = "lecture" if mode == "lecture" else "meeting"
    msg = reason or "要確認: 文字起こしの内容が短すぎるため要約不可"
    base = {
        "type": mode_key,
        "highlights": [
            {"text": msg, "needConfirm": True}
        ],
        "overview": ""
    }
    if mode_key == "meeting":
        base["highlights"][0]["category"] = "info"
        base.update({
            "decisions": [],
            "todos": [],
            "openQuestions": [],
            "discussionPoints": [],
            "keywords": [],
            "participants": [],
            "timeline": [],
            "uiHints": {
                "topFocus": ["decisions", "todos"],
                "tone": "business",
                "suggestedBadges": ["要確認", "期限不明", "担当不明"]
            }
        })
    else:
        base.update({
            "theme": {"text": "", "needConfirm": True},
            "terms": [],
            "sections": [],
            "formulasOrProcedures": [],
            "keywords": [],
            "exercises": {"examples": [], "homework": [], "examScope": []},
            "studyGuide": {"recommendedOrder": ["highlights", "terms", "sections", "formulasOrProcedures"], "memoryHooks": []},
            "uiHints": {"topFocus": ["highlights", "terms"], "tone": "study", "suggestedBadges": ["要確認"]}
        })
    return base


def _ensure_text_has_confirm(item: dict, key: str = "text") -> None:
    text = item.get(key)
    if isinstance(text, str) and "要確認" not in text:
        item[key] = f"{text}（要確認）" if text else "要確認"


def _ensure_need_confirm(item: dict, key: str = "text") -> dict:
    if "needConfirm" not in item:
        item["needConfirm"] = False
    if item.get("needConfirm"):
        _ensure_text_has_confirm(item, key=key)
    return item


def _coerce_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _truncate_title(title: str, max_len: int = 30) -> str:
    """Truncate suggested title to max_len chars, removing verbose patterns."""
    import re
    # Remove common verbose prefixes (LLM sometimes copies highlight prefixes)
    title = re.sub(r'^(今回の結論:\s*|次の一手:\s*|結論:\s*|概要:\s*)', '', title)
    # Remove verbose suffixes
    title = re.sub(r'(について|に関して|の件|についての議論|に関する検討|を深めます|の共有|についての共有|を強化する|の強化|の検討|の議論|の確認|の報告|することを決定|に向けて)$', '', title)
    # Remove trailing punctuation
    title = re.sub(r'[。、．.]+$', '', title)
    title = title.strip()
    if len(title) > max_len:
        title = title[:max_len]
    return title


def _normalize_summary_json(data: dict, mode: str) -> dict:
    if not isinstance(data, dict):
        return {}

    # ── ASR誤認識の用語正規化（JSON全体を文字列置換してから再パース）──
    _ASR_CORRECTIONS = {
        "AI芸術都": "AIエージェント",
        "AI芸術": "AIエージェント",
        "芸術都側": "エージェント側",
        "ロボットアム": "ロボットアーム",
    }
    data_str = json.dumps(data, ensure_ascii=False)
    corrected = False
    for wrong, right in _ASR_CORRECTIONS.items():
        if wrong in data_str:
            data_str = data_str.replace(wrong, right)
            corrected = True
    if corrected:
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            pass  # 置換で壊れた場合は元のまま

    normalized = dict(data)
    # Always force type to match the requested mode (LLM may generate wrong type)
    normalized["type"] = mode

    highlights = []
    for item in _coerce_list(normalized.get("highlights")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        if mode != "lecture":
            item.setdefault("category", "info")
        highlights.append(item)
    normalized["highlights"] = highlights[:7]

    # Phase 7.9: conversationHighlights — natural-sentence summary cards
    # with a single primaryTimestamp. LLM is asked to fill primaryTimestampSec;
    # we cap to 12 items, drop invalid ids, and coerce importance.
    conv_highlights = []
    for idx, item in enumerate(_coerce_list(normalized.get("conversationHighlights"))):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        importance = item.get("importance")
        if importance not in ("high", "medium", "low"):
            importance = "medium"
        hl = {
            "id": str(item.get("id") or f"hl_{idx + 1}"),
            "text": text.strip(),
            "topic": (str(item.get("topic")).strip() or None) if item.get("topic") else None,
            "importance": importance,
        }
        ts_sec = item.get("primaryTimestampSec")
        if isinstance(ts_sec, (int, float)) and ts_sec >= 0:
            hl["primaryTimestampMs"] = int(round(float(ts_sec) * 1000))
        evidence_hint = item.get("evidenceHint")
        if isinstance(evidence_hint, str) and evidence_hint.strip():
            hl["evidenceHint"] = evidence_hint.strip()[:80]
        # Explicit empty evidence array — Phase 5 evidence-first contract
        hl["evidence"] = []
        conv_highlights.append(hl)
    normalized["conversationHighlights"] = conv_highlights[:12]

    overview = normalized.get("overview")
    normalized["overview"] = overview if isinstance(overview, str) else ""

    if mode == "lecture":
        theme = normalized.get("theme") if isinstance(normalized.get("theme"), dict) else {}
        normalized["theme"] = _ensure_need_confirm(theme, key="text")

        terms = []
        for item in _coerce_list(normalized.get("terms")):
            if not isinstance(item, dict):
                continue
            item = _ensure_need_confirm(item, key="term")
            item["examples"] = _coerce_list(item.get("examples"))
            terms.append(item)
        normalized["terms"] = terms[:8]

        sections = []
        for item in _coerce_list(normalized.get("sections")):
            if not isinstance(item, dict):
                continue
            item = _ensure_need_confirm(item, key="title")
            item["bullets"] = _coerce_list(item.get("bullets"))
            item["commonMistakes"] = _coerce_list(item.get("commonMistakes"))
            sections.append(item)
        normalized["sections"] = sections[:6]

        formulas = []
        for item in _coerce_list(normalized.get("formulasOrProcedures")):
            if not isinstance(item, dict):
                continue
            item = _ensure_need_confirm(item, key="title")
            formulas.append(item)
        normalized["formulasOrProcedures"] = formulas

        exercises = normalized.get("exercises") if isinstance(normalized.get("exercises"), dict) else {}
        examples = []
        for item in _coerce_list(exercises.get("examples")):
            if not isinstance(item, dict):
                continue
            examples.append(_ensure_need_confirm(item, key="title"))
        homework = []
        for item in _coerce_list(exercises.get("homework")):
            if not isinstance(item, dict):
                continue
            homework.append(_ensure_need_confirm(item, key="text"))
        exam_scope = []
        for item in _coerce_list(exercises.get("examScope")):
            if not isinstance(item, dict):
                continue
            exam_scope.append(_ensure_need_confirm(item, key="text"))
        normalized["exercises"] = {"examples": examples, "homework": homework, "examScope": exam_scope}

        study_guide = normalized.get("studyGuide") if isinstance(normalized.get("studyGuide"), dict) else {}
        study_guide.setdefault("recommendedOrder", ["highlights", "terms", "sections", "formulasOrProcedures"])
        study_guide["recommendedOrder"] = _coerce_list(study_guide.get("recommendedOrder"))
        study_guide["memoryHooks"] = _coerce_list(study_guide.get("memoryHooks"))
        normalized["studyGuide"] = study_guide

        ui_hints = normalized.get("uiHints") if isinstance(normalized.get("uiHints"), dict) else {}
        ui_hints.setdefault("topFocus", ["highlights", "terms"])
        ui_hints.setdefault("tone", "study")
        ui_hints.setdefault("suggestedBadges", ["要確認"])
        normalized["uiHints"] = ui_hints

        keywords = []
        for item in _coerce_list(normalized.get("keywords")):
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                keywords.append({"text": text})
        if keywords:
            normalized["keywords"] = keywords[:6]
        return normalized

    # meeting mode — 結論ファースト＋TODO成果物化
    import re

    # ── New v2 fields ──
    bl = normalized.get("bottomLine")
    normalized["bottomLine"] = bl if isinstance(bl, str) else ""
    os_val = normalized.get("outcomeStatus", "")
    if os_val not in ("decided", "partially_decided", "discussion_only", "blocked"):
        normalized["outcomeStatus"] = "discussion_only"
    else:
        normalized["outcomeStatus"] = os_val
    wim = normalized.get("whyItMatters")
    normalized["whyItMatters"] = wim if isinstance(wim, str) else ""
    # decisions/highlights 用: 未確定表現を含むものを除外
    _INCONCLUSIVE_RE = re.compile(r"検討|確認する|調査|再検討|依頼|相談|考える|についても|についての")
    # todos 用: 禁止語を成果物表現に置換（削除ではなく変換）
    _TODO_VAGUE_PATTERNS = [
        (re.compile(r"(.+?)を?検討(?:する)?$"), r"\1の方針案を整理"),
        (re.compile(r"(.+?)を?調査(?:する)?$"), r"\1について事例を収集して要点をまとめる"),
        (re.compile(r"(.+?)を?確認(?:する)?$"), r"\1の可否・条件を整理して結論をまとめる"),
        (re.compile(r"(.+?)を?再検討(?:する)?$"), r"\1の代替案を含め方針を再整理"),
        (re.compile(r"(.+?)を?依頼(?:する)?$"), r"\1の依頼内容と期待成果をまとめる"),
        (re.compile(r"(.+?)(?:について)?相談(?:する)?$"), r"\1の論点と選択肢を整理"),
        (re.compile(r"(.+?)(?:について)?考える$"), r"\1の方針案を整理"),
    ]
    # フォールバック: パターンマッチしなかった場合の単純置換
    _TODO_VAGUE_WORDS = re.compile(r"検討する?|確認する|調査する?|再検討する?|依頼する?|相談する?|考える")

    def _is_conclusive(text: str) -> bool:
        """未確定表現を含む項目を除外する (decisions/highlights用)"""
        if not isinstance(text, str):
            return False
        return not _INCONCLUSIVE_RE.search(text)

    def _deliverablize_todo(text: str) -> str:
        """TODO文言の禁止語を成果物表現に変換する (削除しない)"""
        if not isinstance(text, str):
            return text
        # Strip trailing suffixes like （要確認） before pattern matching, re-add after
        suffix = ""
        stripped = text
        if stripped.endswith("（要確認）"):
            suffix = "（要確認）"
            stripped = stripped[:-len(suffix)].rstrip()
        for pattern, replacement in _TODO_VAGUE_PATTERNS:
            m = pattern.search(stripped)
            if m:
                result = pattern.sub(replacement, stripped)
                return f"{result}{suffix}" if suffix else result
        # フォールバック: 単純に禁止語を除去して「〜を整理」に
        if _TODO_VAGUE_WORDS.search(stripped):
            cleaned = _TODO_VAGUE_WORDS.sub("", stripped).rstrip("をにてし、。")
            result = f"{cleaned}の方針を整理" if cleaned else stripped
            return f"{result}{suffix}" if suffix else result
        return text

    decisions = []
    for item in _coerce_list(normalized.get("decisions")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        item.setdefault("owner", "担当不明")
        item.setdefault("due", "期限不明")
        # v2 fields
        status = item.get("status", "")
        if status not in ("confirmed", "tentative", "inferred"):
            item["status"] = "inferred"
        item.setdefault("ownerSource", "unknown")
        if item["ownerSource"] not in ("explicit", "inferred", "unknown"):
            item["ownerSource"] = "unknown"
        item.setdefault("dueSource", "unknown")
        if item["dueSource"] not in ("explicit", "inferred", "unknown"):
            item["dueSource"] = "unknown"
        item.setdefault("reason", "")
        conf = item.get("confidence")
        item["confidence"] = float(conf) if isinstance(conf, (int, float)) else 0.5
        item.setdefault("evidenceHint", "")
        if _is_conclusive(item.get("text", "")):
            decisions.append(item)
    normalized["decisions"] = decisions

    # highlights: [discussion]禁止 + 未確定表現を除外 + テキスト内タグ除去
    _HIGHLIGHT_BANNED_CATEGORIES = {"discussion", "question"}
    _EMBEDDED_TAG_RE = re.compile(r"^\s*\[(?:decision|todo|info|risk|discussion)\]\s*", re.IGNORECASE)
    filtered_highlights = []
    for item in normalized.get("highlights", []):
        if not isinstance(item, dict):
            continue
        # テキスト内の埋め込みタグ [info] [decision] 等を除去
        text = item.get("text", "")
        if isinstance(text, str):
            item["text"] = _EMBEDDED_TAG_RE.sub("", text).strip()
        # [discussion] カテゴリを除外
        cat = (item.get("category") or "").lower()
        if cat in _HIGHLIGHT_BANNED_CATEGORIES:
            continue
        # 未確定表現を除外
        if not _is_conclusive(item.get("text", "")):
            continue
        # カテゴリを正規化（decision/todo/info/risk/open_question/change 許可）
        if cat not in ("decision", "todo", "info", "risk", "open_question", "change"):
            item["category"] = "info"
        filtered_highlights.append(item)
    if not filtered_highlights:
        filtered_highlights = [{"text": "結論は明確でない（要確認）", "category": "info", "needConfirm": True}]
    normalized["highlights"] = filtered_highlights[:7]

    # todos: 禁止語を成果物表現に変換（削除しない）
    todos = []
    for item in _coerce_list(normalized.get("todos")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        item.setdefault("owner", "担当不明")
        item.setdefault("due", "期限不明")
        item.setdefault("priority", "mid")
        # v2 fields
        item.setdefault("ownerSource", "unknown")
        if item["ownerSource"] not in ("explicit", "inferred", "unknown"):
            item["ownerSource"] = "unknown"
        item.setdefault("dueSource", "unknown")
        if item["dueSource"] not in ("explicit", "inferred", "unknown"):
            item["dueSource"] = "unknown"
        item.setdefault("blocking", "false")
        conf = item.get("confidence")
        item["confidence"] = float(conf) if isinstance(conf, (int, float)) else 0.5
        item.setdefault("evidenceHint", "")
        item["text"] = _deliverablize_todo(item.get("text", ""))
        todos.append(item)
    normalized["todos"] = todos[:8]

    # openQuestions
    open_questions = []
    for item in _coerce_list(normalized.get("openQuestions")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        item.setdefault("impact", "mid")
        item.setdefault("whyOpen", "")
        item.setdefault("owner", "不明")
        item.setdefault("nextCheck", "不明")
        item["needConfirm"] = True
        open_questions.append(item)
    normalized["openQuestions"] = open_questions[:3]

    # decisionLog (v2)
    decision_log = []
    for item in _coerce_list(normalized.get("decisionLog")):
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        if not isinstance(topic, str) or not topic:
            continue
        item.setdefault("conclusion", "")
        item.setdefault("reason", "")
        item.setdefault("remainingIssues", "")
        decision_log.append(item)
    normalized["decisionLog"] = decision_log[:5]

    # contextNotes (v2)
    context_notes = []
    for item in _coerce_list(normalized.get("contextNotes")):
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        if not isinstance(topic, str) or not topic:
            continue
        item.setdefault("summary", "")
        context_notes.append(item)
    normalized["contextNotes"] = context_notes[:3]

    # discussionPoints: conclusion を「合意」or「保留（要確認）」に正規化 (kept for backward compat)
    _AMBIGUOUS_CONCLUSION_RE = re.compile(r"明確でない|不明確|未定|未決定|今後|これから")
    discussion_points = []
    for item in _coerce_list(normalized.get("discussionPoints")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="topic")
        # conclusion が曖昧なら「保留（要確認）」に正規化
        conclusion = item.get("conclusion", "")
        if isinstance(conclusion, str) and _AMBIGUOUS_CONCLUSION_RE.search(conclusion):
            item["conclusion"] = "保留（要確認）"
            item["needConfirm"] = True
        discussion_points.append(item)
    normalized["discussionPoints"] = discussion_points[:4]

    keywords = []
    for item in _coerce_list(normalized.get("keywords")):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            keywords.append({"text": text})
    normalized["keywords"] = keywords[:6]

    participants = []
    for item in _coerce_list(normalized.get("participants")):
        if not isinstance(item, dict):
            continue
        name = item.get("name") if isinstance(item.get("name"), str) else "不明"
        role = item.get("role") if isinstance(item.get("role"), str) else "不明"
        participants.append({"name": name, "role": role})
    normalized["participants"] = participants

    timeline = []
    for item in _coerce_list(normalized.get("timeline")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="event")
        item.setdefault("timeHint", "不明")
        timeline.append(item)
    normalized["timeline"] = timeline

    ui_hints = normalized.get("uiHints") if isinstance(normalized.get("uiHints"), dict) else {}
    ui_hints.setdefault("topFocus", ["decisions", "todos"])
    ui_hints.setdefault("tone", "business")
    ui_hints.setdefault("suggestedBadges", ["要確認", "期限不明", "担当不明"])
    normalized["uiHints"] = ui_hints

    # ── 後処理ルール (meeting mode) ──────────────────
    # (A) 日付捏造を無効化: YYYY-MM-DD形式のdueが現在より過去 → 期限不明
    import datetime as _dt
    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    _today = _dt.date.today()
    for item_list in [normalized.get("decisions", []), normalized.get("todos", [])]:
        for item in item_list:
            due = item.get("due", "")
            if isinstance(due, str) and _DATE_RE.match(due):
                try:
                    due_date = _dt.date.fromisoformat(due)
                    if due_date < _today:
                        item["due"] = "期限不明"
                        item["needConfirm"] = True
                except ValueError:
                    item["due"] = "期限不明"

    # (B) 決定事項の矛盾解消: decisions空なのにtodosに確定表現があれば昇格
    _DECISION_KEYWORDS_RE = re.compile(r"購入|進める|開始|採用|決定|実施|導入|発注")
    if not normalized.get("decisions"):
        for todo in list(normalized.get("todos", [])):
            text = todo.get("text", "")
            if _DECISION_KEYWORDS_RE.search(text):
                normalized["decisions"] = [{
                    "text": text,
                    "owner": todo.get("owner", "担当不明"),
                    "due": todo.get("due", "期限不明"),
                    "needConfirm": True,
                }]
                break  # 1件だけ昇格

    # (C) 「結論は明確でない」の誤判定を修正: decisions/todosがあるなら削除
    if normalized.get("decisions") or normalized.get("todos"):
        hl = normalized.get("highlights", [])
        filtered = [h for h in hl if h.get("text", "") != "結論は明確でない（要確認）"]
        if filtered:
            normalized["highlights"] = filtered
        else:
            # 全highlightが除去された場合、decisions/todosからhighlightを生成
            replacement = []
            for d in normalized.get("decisions", [])[:2]:
                replacement.append({"text": d.get("text", ""), "category": "decision", "needConfirm": d.get("needConfirm", False)})
            for t in normalized.get("todos", [])[:2]:
                replacement.append({"text": t.get("text", ""), "category": "todo", "needConfirm": t.get("needConfirm", False)})
            normalized["highlights"] = replacement[:5] if replacement else hl

    # (D) highlights に「次の一手:」行がなければ、todos先頭から補完
    hl = normalized.get("highlights", [])
    has_next_step = any("次の一手" in h.get("text", "") for h in hl if isinstance(h, dict))
    if not has_next_step and normalized.get("todos"):
        top_todo = normalized["todos"][0]
        next_step_text = f"次の一手: {top_todo.get('text', '')}"
        # 2番目に挿入（先頭は「今回の結論:」）
        insert_pos = min(1, len(hl))
        hl.insert(insert_pos, {"text": next_step_text, "category": "todo", "needConfirm": top_todo.get("needConfirm", False)})
        normalized["highlights"] = hl[:7]

    return normalized


def _validate_summary_json(data: dict, mode: str, source_len: int) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not_dict"
    data_type = data.get("type")
    if data_type != mode:
        return False, "type_mismatch"
    highlights = data.get("highlights")
    if not isinstance(highlights, list):
        return False, "highlights_missing"
    if len(highlights) < 1 or len(highlights) > 15:
        return False, f"highlights_count (got {len(highlights)})"
    # v2 meeting: check bottomLine instead of overview
    if mode == "meeting":
        bottom_line = data.get("bottomLine")
        overview = data.get("overview")
        if isinstance(bottom_line, str) and len(bottom_line) >= 5:
            return True, "ok"
        # Fallback: accept overview for backward compat
        if isinstance(overview, str) and len(overview) >= 10:
            return True, "ok_overview_fallback"
        if source_len < 1000:
            return True, "ok_short_source"
        return False, "bottomLine_missing_or_too_short"

    # lecture mode: check overview
    overview = data.get("overview")
    if not isinstance(overview, str):
        return False, "overview_type"
    min_len, max_len = (10, 3000)

    if len(overview) > max_len:
        if len(overview) > 5000:
             return False, "overview_length_max_critical"
        pass

    if len(overview) < min_len:
        if source_len < 1000:
             return True, "ok_short_source"
        return False, "overview_length_min"
    return True, "ok"


def _extract_tags_from_summary_json(summary_json: dict, mode: str) -> List[str]:
    tags: List[str] = []
    if mode == "lecture":
        for item in _coerce_list(summary_json.get("terms")):
            term = item.get("term") if isinstance(item, dict) else None
            if isinstance(term, str) and term and term not in tags:
                tags.append(term)
            if len(tags) >= 4:
                break
    else:
        for item in _coerce_list(summary_json.get("keywords")):
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text and text not in tags:
                tags.append(text)
            if len(tags) >= 4:
                break
    if not tags:
        for item in _coerce_list(summary_json.get("highlights")):
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text:
                tags.append(text[:20])
            if len(tags) >= 2:
                break
    return tags


def _summary_json_to_markdown_v2(summary_json: dict) -> str:
    if not isinstance(summary_json, dict):
        return ""
    mode = summary_json.get("type")
    if mode == "lecture":
        return _lecture_json_to_markdown(summary_json)
    if mode == "meeting":
        return _meeting_json_to_markdown(summary_json)
    return ""


def _format_need_confirm(text: str, need_confirm: bool) -> str:
    if not isinstance(text, str):
        text = ""
    if need_confirm and "要確認" not in text:
        return f"{text}（要確認）" if text else "要確認"
    return text


def _meeting_json_to_markdown(summary_json: dict) -> str:
    lines: List[str] = []
    def _append_heading(title: str) -> None:
        if lines:
            lines.append("")
        lines.append(f"## {title}")

    def _append_placeholder() -> None:
        lines.append("- （該当なし）")

    # ── 結論 (v2: bottomLine) ──
    bottom_line = summary_json.get("bottomLine") or ""
    outcome_status = summary_json.get("outcomeStatus") or ""
    why_it_matters = summary_json.get("whyItMatters") or ""
    if bottom_line:
        _append_heading("結論")
        status_badge = {"decided": "確定", "partially_decided": "一部確定", "discussion_only": "議論のみ", "blocked": "ブロック中"}.get(outcome_status, "")
        if status_badge:
            lines.append(f"【{status_badge}】")
        lines.append(bottom_line)
        if why_it_matters:
            lines.append(f"→ {why_it_matters}")
    else:
        # Fallback to overview (v1)
        overview = summary_json.get("overview") or ""
        if isinstance(overview, list):
            overview = "\n".join(str(item) for item in overview if item)
        if overview:
            _append_heading("概要")
            lines.append(str(overview))

    # ── 決定事項 ──
    _append_heading("決定事項")
    decisions = _coerce_list(summary_json.get("decisions"))
    if decisions:
        for item in decisions:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), bool(item.get("needConfirm")))
            owner = item.get("owner") or "担当不明"
            due = item.get("due") or "期限不明"
            status = item.get("status") or ""
            status_mark = {"confirmed": "✓", "tentative": "△", "inferred": "?"}.get(status, "")
            evidence = item.get("evidenceHint") or ""
            line = f"- {status_mark} {text}（担当: {owner} / 期限: {due}）" if status_mark else f"- {text}（担当: {owner} / 期限: {due}）"
            lines.append(line)
            reason = item.get("reason") or ""
            if reason:
                lines.append(f"  - 理由: {reason}")
            if evidence:
                lines.append(f"  - 根拠: 「{evidence}」")
    else:
        _append_placeholder()

    # ── TODO ──
    _append_heading("TODO")
    todos = _coerce_list(summary_json.get("todos"))
    if todos:
        for item in todos:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), bool(item.get("needConfirm")))
            owner = item.get("owner") or "担当不明"
            due = item.get("due") or "期限不明"
            priority = item.get("priority") or "mid"
            blocking = item.get("blocking", "false")
            block_mark = " 🚫ブロック" if blocking == "true" else ""
            lines.append(f"- {text}（担当: {owner} / 期限: {due} / 優先度: {priority}{block_mark}）")
    else:
        _append_placeholder()

    # ── 未決事項・要確認 ──
    _append_heading("未決事項・要確認")
    open_questions = _coerce_list(summary_json.get("openQuestions"))
    if open_questions:
        for item in open_questions:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), True)
            impact = item.get("impact") or "mid"
            lines.append(f"- {text}（影響度: {impact}）")
            why_open = item.get("whyOpen") or ""
            if why_open:
                lines.append(f"  - 理由: {why_open}")
    else:
        _append_placeholder()

    # ── 議論の経緯 (v2: decisionLog) ──
    decision_log = _coerce_list(summary_json.get("decisionLog"))
    if decision_log:
        _append_heading("議論の経緯")
        for item in decision_log:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic", "")
            conclusion = item.get("conclusion") or ""
            reason = item.get("reason") or ""
            remaining = item.get("remainingIssues") or ""
            lines.append(f"- {topic}")
            if conclusion:
                lines.append(f"  - 結論: {conclusion}")
            if reason:
                lines.append(f"  - 理由: {reason}")
            if remaining:
                lines.append(f"  - 残課題: {remaining}")
    else:
        # Fallback to discussionPoints (v1)
        discussion_points = _coerce_list(summary_json.get("discussionPoints"))
        if discussion_points:
            _append_heading("議論ポイント")
            for item in discussion_points:
                if not isinstance(item, dict):
                    continue
                topic = _format_need_confirm(item.get("topic", ""), bool(item.get("needConfirm")))
                summary = item.get("summary") or ""
                conclusion = item.get("conclusion") or ""
                next_action = item.get("nextAction") or ""
                lines.append(f"- {topic}")
                if summary:
                    lines.append(f"  - 要約: {summary}")
                if conclusion:
                    lines.append(f"  - 結論/現状: {conclusion}")
                if next_action:
                    lines.append(f"  - 次アクション: {next_action}")

    # ── 補足・背景 (v2: contextNotes) ──
    context_notes = _coerce_list(summary_json.get("contextNotes"))
    if context_notes:
        _append_heading("補足・背景")
        for item in context_notes:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic", "")
            summary = item.get("summary") or ""
            lines.append(f"- {topic}")
            if summary:
                lines.append(f"  - {summary}")

    # ── 重要ポイント ──
    _append_heading("重要ポイント")
    highlights = _coerce_list(summary_json.get("highlights"))
    if highlights:
        for item in highlights:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), bool(item.get("needConfirm")))
            category = item.get("category")
            if category:
                lines.append(f"- [{category}] {text}")
            else:
                lines.append(f"- {text}")
    else:
        _append_placeholder()

    # ── キーワード ──
    _append_heading("キーワード")
    keywords = _coerce_list(summary_json.get("keywords"))
    if keywords:
        for item in keywords:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text:
                lines.append(f"- {text}")
    else:
        _append_placeholder()

    return "\n".join(lines).strip()


def _lecture_json_to_markdown(summary_json: dict) -> str:
    lines: List[str] = []
    def _append_heading(title: str) -> None:
        if lines:
            lines.append("")
        lines.append(f"## {title}")

    def _append_placeholder() -> None:
        lines.append("- （該当なし）")

    _append_heading("重要ポイント")
    highlights = _coerce_list(summary_json.get("highlights"))
    if highlights:
        for item in highlights:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), bool(item.get("needConfirm")))
            lines.append(f"- {text}")
    else:
        _append_placeholder()

    _append_heading("今日のテーマ")
    theme = summary_json.get("theme") if isinstance(summary_json.get("theme"), dict) else {}
    if theme.get("text"):
        lines.append(f"- {_format_need_confirm(theme.get('text', ''), bool(theme.get('needConfirm')))}")
    else:
        _append_placeholder()

    _append_heading("学ぶべき用語・概念")
    terms = _coerce_list(summary_json.get("terms"))
    if terms:
        for item in terms:
            if not isinstance(item, dict):
                continue
            term = _format_need_confirm(item.get("term", ""), bool(item.get("needConfirm")))
            definition = item.get("definition") or ""
            if definition:
                lines.append(f"- **{term}**：{definition}")
            else:
                lines.append(f"- **{term}**")
            examples = _coerce_list(item.get("examples"))
            for ex in examples:
                if isinstance(ex, str) and ex:
                    lines.append(f"  - 例: {ex}")
    else:
        _append_placeholder()

    _append_heading("講義の流れ")
    sections = _coerce_list(summary_json.get("sections"))
    has_flow_content = False
    if sections:
        for idx, item in enumerate(sections, start=1):
            if not isinstance(item, dict):
                continue
            has_flow_content = True
            title = _format_need_confirm(item.get("title", f"セクション{idx}"), bool(item.get("needConfirm")))
            lines.append(f"### {title}")
            bullets = _coerce_list(item.get("bullets"))
            has_section_detail = False
            for bullet in bullets:
                if isinstance(bullet, str) and bullet:
                    lines.append(f"- {bullet}")
                    has_section_detail = True
            mistakes = _coerce_list(item.get("commonMistakes"))
            for mistake in mistakes:
                if isinstance(mistake, str) and mistake:
                    lines.append(f"- 注意: {mistake}")
                    has_section_detail = True
            if not has_section_detail:
                lines.append("- （詳細なし）")

    formulas = _coerce_list(summary_json.get("formulasOrProcedures"))
    if formulas:
        has_flow_content = True
        lines.append("### 重要な式・定義・手順")
        for item in formulas:
            if not isinstance(item, dict):
                continue
            title = _format_need_confirm(item.get("title", ""), bool(item.get("needConfirm")))
            content = item.get("content") or ""
            lines.append(f"- {title}: {content}" if title else f"- {content}")

    exercises = summary_json.get("exercises") if isinstance(summary_json.get("exercises"), dict) else {}
    if exercises:
        has_flow_content = True
        lines.append("### 例題・宿題/試験範囲")
        examples = _coerce_list(exercises.get("examples"))
        for item in examples:
            if not isinstance(item, dict):
                continue
            title = _format_need_confirm(item.get("title", ""), bool(item.get("needConfirm")))
            point = item.get("point") or ""
            lines.append(f"- 例題: {title}（{point}）" if title else f"- 例題: {point}")
        homework = _coerce_list(exercises.get("homework"))
        for item in homework:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), bool(item.get("needConfirm")))
            lines.append(f"- 宿題: {text}")
        exam_scope = _coerce_list(exercises.get("examScope"))
        for item in exam_scope:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), bool(item.get("needConfirm")))
            lines.append(f"- 試験範囲: {text}")

    if not has_flow_content:
        _append_placeholder()

    _append_heading("概要")
    overview = summary_json.get("overview") or ""
    if isinstance(overview, list):
        overview = "\n".join(str(item) for item in overview if item)
    if overview:
        lines.append(str(overview))
    else:
        _append_placeholder()

    _append_heading("キーワード")
    keywords = _coerce_list(summary_json.get("keywords"))
    if keywords:
        for item in keywords:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text:
                lines.append(f"- {text}")
    else:
        _append_placeholder()

    return "\n".join(lines).strip()

def _extract_tags_from_lecture_markdown(text: str) -> List[str]:
    """Markdownからタグ(キーワード)を抽出する簡易ロジック"""
    import re
    tags = []
    # Extract **Keyword** from "## 学ぶべき用語・概念" section
    try:
        # Simple regex to find **Word** patterns effectively
        matches = re.findall(r"\*\*(.+?)\*\*", text)
        for m in matches:
            clean = m.strip()
            if clean and clean not in tags:
                tags.append(clean)
        
        # Limit tags
        return tags[:5]
    except Exception:
        return []



def _build_highlights_prompt(text: str, segments: Optional[List[dict]]) -> str:
    seg_json = ""
    if segments:
        try:
            seg_json = json.dumps(segments)[:4000]  # prompt size抑制
        except Exception:
            seg_json = ""
    return f"""以下の文字起こしから重要なハイライトとタグを抽出してください。
JSON で返してください。形式:
{{
  "highlights": [
    {{"startSec": 0.0, "endSec": 30.0, "title": "要点", "summary": "詳細", "speakerIds": []}},
    ...
  ],
  "tags": ["キーワード1", "キーワード2"]
}}
- startSec/endSec は秒単位
- タグは最大5個

=== 文字起こし ===
{text}

=== セグメント（あれば） ===
{seg_json}
"""


def _summary_json_to_markdown(summary: dict, mode: str = "meeting") -> str:
    """
    JSON要約をMarkdown形式に変換する。
    モードに応じて適切なフォーマットを使用。
    """
    if not summary:
        return ""

    overview = summary.get("overview") or ""
    keywords = summary.get("keywords") or []

    lines = []

    # ヘッダー（モード別）
    if mode == "lecture":
        lines.append("## 📚 講義ノート")
    else:
        lines.append("## 📋 会議サマリー")
    lines.append("")

    # TL;DR (Meeting only, at the top)
    tldr = summary.get("tldr") or []
    if tldr and mode != "lecture":
        lines.append("### 💡 重要ポイント (TL;DR)")
        for t in tldr:
            lines.append(f"- {t}")
        lines.append("")

    # Overview
    if overview:
        if isinstance(overview, list):
            lines.append("\n".join(str(o) for o in overview if o))
        else:
            cleaned = str(overview).replace("#", "").strip()
            lines.append(cleaned)
        lines.append("")

    # Open Questions / Risks (Meeting only)
    open_questions = summary.get("openQuestions") or []
    if open_questions and mode != "lecture":
        lines.append("### ⚠️ 未決事項・要確認")
        for q in open_questions:
            lines.append(f"- {q}")
        lines.append("")

    # Meeting format: decisions, todos, discussionPoints
    if mode != "lecture":
        # Legacy sections support (for backward compatibility)
        sections = summary.get("sections") or []
        if sections:
            for section in sections:
                if not isinstance(section, dict):
                    continue
                title = section.get("title")
                bullets = section.get("bullets") or []
                if title and bullets:
                    lines.append(f"### 🔹 {title}")
                    for b in bullets:
                        lines.append(f"- {b}")
                    lines.append("")

        decisions = summary.get("decisions") or []
        if decisions:
            lines.append("### ✅ 決定事項")
            for d in decisions:
                lines.append(f"- {d}")
            lines.append("")

        todos = summary.get("todos") or []
        if todos:
            lines.append("### 📌 アクションアイテム")
            for t in todos:
                lines.append(f"- {t}")
            lines.append("")

        discussion_points = summary.get("discussionPoints") or summary.get("points") or []
        if discussion_points:
            lines.append("### 💬 議論のポイント")
            for p in discussion_points:
                lines.append(f"- {p}")
            lines.append("")

    # Lecture format: keyPoints, concepts, questions
    else:
        key_points = summary.get("keyPoints") or summary.get("points") or []
        if key_points:
            lines.append("### 📝 重要ポイント")
            for p in key_points:
                lines.append(f"- {p}")
            lines.append("")

        concepts = summary.get("concepts") or []
        if concepts:
            lines.append("### 💡 重要概念")
            for c in concepts:
                lines.append(f"- {c}")
            lines.append("")

        questions = summary.get("questions") or []
        if questions:
            lines.append("### ❓ 復習用質問")
            for q in questions:
                lines.append(f"- {q}")
            lines.append("")

    # Keywords (common)
    if keywords:
        lines.append("### 🔑 キーワード")
        lines.append(", ".join(keywords))
        lines.append("")

    return "\n".join(lines).strip()
