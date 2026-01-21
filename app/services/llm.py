import os
import asyncio
import json
from typing import List, Optional, Any

# Lazy import for vertexai to prevent build/startup crashes if credentials/deps are missing
# import vertexai
# from vertexai.generative_models import GenerativeModel, GenerationConfig

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "asia-northeast1")
# デフォルトは地域で利用可能性の高い新しい ID を優先し、後方互換で -flash もフォールバック
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.0-flash-lite")

import re
import logging

logger = logging.getLogger(__name__)

# Constants for transcript validation
MIN_TRANSCRIPT_LENGTH = 50  # Minimum characters for meaningful LLM processing
MAX_TRANSCRIPT_LENGTH = 100000  # Maximum to prevent excessive token usage
SUMMARY_JSON_VERSION = 1


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
    from vertexai.generative_models import GenerativeModel

    if not PROJECT_ID:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT/GCP_PROJECT is not set for Vertex AI")
    vertexai.init(project=PROJECT_ID, location=VERTEX_REGION)

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
            return
        except Exception as e:
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
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.6,
            max_output_tokens=2048,
        ),
    )
    return (resp.text or "").strip()


async def generate_quiz(text: str, mode: str = "lecture", count: int = 5) -> str:
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
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.5,
            max_output_tokens=2048,
        ),
    )
    return (resp.text or "").strip()

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
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.4,
            max_output_tokens=2048,
        ),
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
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.5,
            max_output_tokens=1024,
            response_mime_type="application/json",
        ),
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
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.3,
            max_output_tokens=1024,
            response_mime_type="application/json",
        ),
    )
    # Use retry-aware JSON parsing
    result = _parse_json_with_retry(resp.text or "{}")
    if not result:
        return {"answer": (resp.text or "").strip(), "citations": []}
    return result


async def translate_text(text: str, target_lang: str) -> str:
    """
    テキストを指定言語に翻訳する。
    """
    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    
    prompt = f"""あなたはプロの翻訳者です。以下のテキストを {target_lang} に翻訳してください。
出力は翻訳結果のテキストのみを返してください（説明は不要）。

=== テキスト ===
{text}
"""
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.3,
            max_output_tokens=2048,
        ),
    )
    return (resp.text or "").strip()


async def generate_highlights_and_tags(text: str, segments: Optional[List[dict]] = None) -> dict:
    """
    ハイライトとタグを生成する。
    """
    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    prompt = _build_highlights_prompt(text, segments)
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.5,
            max_output_tokens=2048,
            response_mime_type="application/json",
        ),
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
- キーワードを太字で強調
- 不明瞭な箇所は「要確認」と記載
- **文字起こしに無い固有名詞・数値・定義は作らない。曖昧なら『要確認』とする**

=== 文字起こし ===
{text}
"""
    return f"""あなたは会議議事録アシスタントです。以下の文字起こしをMarkdown形式で実務に使える議事録に要約してください。
- 決定事項、TODO、懸念点を明確に
- 箇条書きで簡潔に
- 不明瞭な箇所は「要確認」と記載

=== 文字起こし ===
{text}
"""


def _build_quiz_prompt(text: str, mode: str, count: int) -> str:
    return f"""あなたは学習クイズ作成アシスタントです。以下の文字起こし内容から理解度確認クイズを {count} 問作成してください。

# 最重要（厳守）
- **Markdownのクイズ本体のみ出力**（余計な挨拶や説明文は一切禁止）
- 文字起こしに根拠がない内容は作らない（推測で事実を足さない）
- **正解の分布**: {count}問中、A/B/C/Dが均等になるよう配置（例: 5問ならA1,B1,C1,D1,残り1問はランダム）
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
- 重要語は **太字**
- 必要なら短い具体例を追加

=== 文字起こし ===
{text}
"""
    return f"""あなたは会議内容をわかりやすく解説するアシスタントです。
以下の文字起こしを読み、背景・意図・論点を整理した解説を Markdown でまとめてください。

- 冒頭に3-5行の要点
- 重要語は **太字**
- 必要なら短い具体例を追加

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


def _build_playlist_prompt(
    text: str,
    segments: Optional[List[dict]] = None,
    duration_sec: Optional[float] = None
) -> str:
    min_sec, duration_line, cues_block = _build_playlist_rules(
        segments=segments,
        duration_sec=duration_sec,
    )
    return f"""以下の文字起こしを、YouTube のチャプターのように「意味のまとまり」で再生リストに分割してください。
JSON 配列のみを返してください。形式:
[
  {{"startSec": 0.0, "endSec": 90.0, "title": "導入", "summary": "内容要約", "confidence": 0.9}},
  ...
]
ルール:
- 5秒刻みの機械的な分割は禁止
- startSec/endSec は秒単位（浮動小数）
- 1チャプターの最小長は {min_sec} 秒
- title は短く、summary で補足
- もしタイムスタンプ付き断片がある場合は、その時刻に合わせて startSec/endSec を選ぶ
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
) -> str:
    """
    モードに応じた要約プロンプトを生成する。
    - meeting: 議事録形式（決定事項、TODO、議論ポイント）
    - lecture: 講義ノート形式（要点、キーワード、学習ポイント）
    要約・タグ・再生リストを一括で生成する。
    """
    if mode == "lecture":
        return _build_lecture_summary_prompt(text)
    return _build_meeting_summary_prompt(text)


def _build_meeting_summary_prompt(
    text: str,
) -> str:
    """議事録用の要約プロンプト（JSON厳格版 / UI直結スキーマ）"""
    return f"""あなたは企業の議事録作成のプロです。以下の文字起こしから、リッチなUIに即座に変換できる「構造化JSON」を生成してください。

# 最重要（厳守）
- 出力は「次のJSONのみ」。前置き/説明/Markdown/箇条書き/コードフェンスは禁止
- JSONは構文的に正しいこと（ダブルクォート、末尾カンマ禁止）
- 文字起こしに無い固有名詞・数値・因果・決定は作らない
- 不明/曖昧/根拠不足は needConfirm=true を付け、文言にも「要確認」を含める

# UI目的
- 画面最上部に「結論だけ」が3〜7行で出ること（highlights）
- 次に「決定事項」と「TODO」がカード表示できること
- その下に overview を300〜1500文字で提供（背景→議論→結論→次アクションの順）

# 出力JSONスキーマ（厳守）
{{
  "summary": {{
    "type": "meeting",
    "highlights": [
      {{"text": "短い結論", "category": "decision|todo|risk|info", "needConfirm": false}}
    ],
    "overview": "300〜1500文字",
    "decisions": [
      {{"text": "〜することに決定", "owner": "名前or担当不明", "due": "YYYY-MM-DD|期限不明", "needConfirm": false}}
    ],
    "todos": [
      {{"text": "やること", "owner": "担当", "due": "YYYY-MM-DD|期限不明", "priority": "high|mid|low", "needConfirm": false}}
    ],
    "openQuestions": [
      {{"text": "未決事項/要確認", "impact": "high|mid|low", "needConfirm": true}}
    ],
    "discussionPoints": [
      {{"topic": "論点", "summary": "要約（短文）", "conclusion": "結論/現状", "nextAction": "次アクション", "needConfirm": false}}
    ],
    "keywords": [{{"text": "重要語"}}],
    "participants": [
      {{"name": "話者名or不明", "role": "PM|Dev|Sales|Other|不明"}}
    ],
    "timeline": [
      {{"timeHint": "冒頭/中盤/終盤/不明", "event": "出来事", "needConfirm": false}}
    ],
    "uiHints": {{
      "topFocus": ["decisions", "todos"],
      "tone": "business",
      "suggestedBadges": ["要確認", "期限不明", "担当不明"]
    }}
  }},
  "tags": ["タグ1", "タグ2", "タグ3"]
}}

# 制約
- highlights は3〜7件、短文。decision/todoを優先
- decisions/todos が無ければ [] でOK。ただし highlights は info/risk で埋めてよい
- overview は必ず範囲内。長すぎたら削る
- tags は2〜6個、短く簡潔に（重複/ハッシュは避ける）

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

# UI目的
- 最上部に「今日のポイントのおさらい」を3〜7行で簡潔に（highlights）
- 次に「用語カード」「流れ（セクション）」「重要式・手順」「例題/範囲」を見やすく
- overview は200〜1200文字で「何を学び、なぜ重要か、全体像」を説明

# 出力JSONスキーマ（厳守）
{{
  "summary": {{
    "type": "lecture",
    "highlights": [
      {{"text": "重要ポイント（短文）", "needConfirm": false}}
    ],
    "overview": "200〜1200文字",
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
    "uiHints": {{
      "topFocus": ["highlights", "terms"],
      "tone": "study",
      "suggestedBadges": ["要確認"]
    }}
  }},
  "tags": ["タグ1", "タグ2", "タグ3"]
}}

# 制約
- highlights は3〜7件
- terms は最大8件
- sections は2〜6個
- どれも無理に埋めない。無ければ [] や空でOK。ただし highlights は可能な範囲で出す
- tags は2〜6個、短く簡潔に（重複/ハッシュは避ける）

# 文字起こし
{text}
"""


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





async def generate_summary_and_tags(
    text: str,
    mode: str = "lecture",
) -> dict:
    """
    要約・タグ・再生リストを1回の Gemini 呼び出しで生成する。
    """
    # Transcript length validation
    if len(text) < MIN_TRANSCRIPT_LENGTH:
        logger.warning(f"Transcript too short for summary: {len(text)} chars")
        summary_json = _build_summary_json_fallback(mode)
        return {
            "summaryJson": summary_json,
            "summaryType": summary_json.get("type"),
            "summaryJsonVersion": SUMMARY_JSON_VERSION,
            "summaryMarkdown": _summary_json_to_markdown_v2(summary_json),
            "tags": ["要確認"],
        }

    if len(text) > MAX_TRANSCRIPT_LENGTH:
        logger.warning(f"Transcript truncated: {len(text)} -> {MAX_TRANSCRIPT_LENGTH} chars")
        text = text[:MAX_TRANSCRIPT_LENGTH]

    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    
    prompt = _build_summary_tags_prompt(text, mode)

    max_attempts = 2
    last_error = "summary_json_invalid"
    summary_json: dict = {}

    for attempt in range(max_attempts):
        resp = await _model.generate_content_async(
            prompt,
            generation_config=GenerationConfig(
                temperature=0.6,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
        )

        # Use retry-aware JSON parsing
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
    return {
        "summaryJson": summary_json,
        "summaryType": summary_json.get("type"),
        "summaryJsonVersion": SUMMARY_JSON_VERSION,
        "summaryMarkdown": summary_markdown,
        "tags": tags,
    }


def _build_summary_json_fallback(mode: str) -> dict:
    mode_key = "lecture" if mode == "lecture" else "meeting"
    base = {
        "type": mode_key,
        "highlights": [
            {"text": "要確認: 文字起こしの内容が短すぎるため要約不可", "needConfirm": True}
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


def _normalize_summary_json(data: dict, mode: str) -> dict:
    if not isinstance(data, dict):
        return {}

    normalized = dict(data)
    if isinstance(normalized.get("type"), str):
        normalized["type"] = normalized["type"].strip().lower()

    highlights = []
    for item in _coerce_list(normalized.get("highlights")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        if mode != "lecture":
            item.setdefault("category", "info")
        highlights.append(item)
    normalized["highlights"] = highlights[:7]

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

    # meeting mode
    decisions = []
    for item in _coerce_list(normalized.get("decisions")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        item.setdefault("owner", "担当不明")
        item.setdefault("due", "期限不明")
        decisions.append(item)
    normalized["decisions"] = decisions

    todos = []
    for item in _coerce_list(normalized.get("todos")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        item.setdefault("owner", "担当不明")
        item.setdefault("due", "期限不明")
        item.setdefault("priority", "mid")
        todos.append(item)
    normalized["todos"] = todos

    open_questions = []
    for item in _coerce_list(normalized.get("openQuestions")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="text")
        item.setdefault("impact", "mid")
        open_questions.append(item)
    normalized["openQuestions"] = open_questions

    discussion_points = []
    for item in _coerce_list(normalized.get("discussionPoints")):
        if not isinstance(item, dict):
            continue
        item = _ensure_need_confirm(item, key="topic")
        discussion_points.append(item)
    normalized["discussionPoints"] = discussion_points

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
    if len(highlights) < 3 or len(highlights) > 7:
        return False, "highlights_count"
    overview = data.get("overview")
    if not isinstance(overview, str):
        return False, "overview_type"
    min_len, max_len = (200, 1200) if mode == "lecture" else (300, 1500)
    if len(overview) > max_len:
        return False, "overview_length_max"
    if len(overview) < min_len:
        # Allow shorter overview when source transcript is short
        if source_len < min_len * 2:
            return True, "ok_short_overview"
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

    _append_heading("決定事項")
    decisions = _coerce_list(summary_json.get("decisions"))
    if decisions:
        for item in decisions:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), bool(item.get("needConfirm")))
            owner = item.get("owner") or "担当不明"
            due = item.get("due") or "期限不明"
            lines.append(f"- {text}（担当: {owner} / 期限: {due}）")
    else:
        _append_placeholder()

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
            lines.append(f"- {text}（担当: {owner} / 期限: {due} / 優先度: {priority}）")
    else:
        _append_placeholder()

    _append_heading("未決事項・要確認")
    open_questions = _coerce_list(summary_json.get("openQuestions"))
    if open_questions:
        for item in open_questions:
            if not isinstance(item, dict):
                continue
            text = _format_need_confirm(item.get("text", ""), True)
            impact = item.get("impact") or "mid"
            lines.append(f"- {text}（影響度: {impact}）")
    else:
        _append_placeholder()

    _append_heading("議論ポイント")
    discussion_points = _coerce_list(summary_json.get("discussionPoints"))
    if discussion_points:
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
    else:
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
