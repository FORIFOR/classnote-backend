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
    クイズを生成する。JSON 文字列の出力を期待。
    """
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
    try:
        return json.loads(resp.text or "{}")
    except Exception:
        return {"answer": (resp.text or "").strip(), "citations": []}


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
    return f"""あなたは学習クイズ作成アシスタントです。
以下の文字起こし内容から理解度確認クイズを {count} 問作成してください。

# 重要:
- 余計な挨拶や説明文は一切書かず、
  **クイズ本体の Markdown だけ** を返してください。
- 「はい、承知しました」などの前置きは書かないでください。

# 出力フォーマット（必ずこの形にする）

各問は次の構造にしてください：

### Q1
質問文を書く

- A. 選択肢A
- B. 選択肢B
- C. 選択肢C
- D. 選択肢D

**Answer:** A
**Explanation:** なぜAが正解なのかを1〜2文で説明

### Q2
...

# 制約
- 各問題は 4 択（A/B/C/D）
- 正解は必ず A〜D のいずれか1つ
- 日本語で自然に書く

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

- 冒頭に3〜5行の要点
- 重要語は **太字**
- 必要なら短い具体例を追加

=== 文字起こし ===
{text}
"""


def _build_playlist_prompt(
    text: str,
    segments: Optional[List[dict]] = None,
    duration_sec: Optional[float] = None
) -> str:
    cues = _build_playlist_cues(segments)
    if duration_sec:
        if duration_sec <= 120:
            chapter_hint = "2〜4"
            min_sec = 10
        elif duration_sec <= 600:
            chapter_hint = "3〜6"
            min_sec = 20
        else:
            chapter_hint = "4〜8"
            min_sec = 30
        duration_line = f"- 収録時間は約 {duration_sec:.1f} 秒。目安のチャプター数は {chapter_hint} 件"
    else:
        min_sec = 20
        duration_line = "- 収録時間が不明なので、チャプターは内容量に応じて 3〜6 件"

    cues_block = ""
    if cues:
        cues_block = f"""
=== タイムスタンプ付き断片 (参考) ===
{cues}
"""

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


def _build_summary_tags_prompt(text: str, mode: str, segments: Optional[List[dict]]) -> str:
    seg_json = ""
    if segments:
        try:
            seg_json = json.dumps(segments)[:6000]
        except Exception:
            seg_json = ""
    
    constraints = """
# 制約
- summary.overview: 400〜600文字で会議の背景・目的・結論を含む充実した概要
- summary.decisions: 決定事項を具体的に列挙（なければ空配列）
- summary.todos: アクションアイテム（担当者・期限があれば含める）
- summary.discussionPoints: 議論のポイント3〜5件
- summary.keywords: 重要な専門用語・固有名詞を6件まで
- tags: 2〜6文字の名詞句を4件まで（ハッシュタグ用、#は付けない）
- 専門用語は噛み砕いて説明を加える
- 曖昧な発言も「〜という意見があった」と客観的に記録
- 話者が特定できる場合は「Aさんは〜」のように記載
""".strip()

    return f"""あなたは企業の議事録作成のプロフェッショナルです。
以下の会議音声の文字起こしから、**誰が読んでもすぐに内容が把握できる**リッチで分かりやすい議事録を作成してください。

# 出力フォーマット（必ずこの JSON のみを返してください）
{{
  "summary": {{
    "overview": "【概要】この会議は〇〇について議論するために開催されました。主な議題は△△で、結論として□□が決定しました。参加者からは××という意見が出され、今後の方針として▽▽を進めることになりました。（400〜600文字程度の充実した要約）",
    "decisions": [
      "【決定1】〇〇を△△までに実施する",
      "【決定2】□□の方針で進める"
    ],
    "todos": [
      "【TODO】Aさん: 〇〇の資料を来週までに準備",
      "【TODO】Bさん: △△の調査を実施"
    ],
    "discussionPoints": [
      "〇〇について、コスト削減の観点から△△案と□□案が比較検討された",
      "××の導入時期について、Q1とQ2で意見が分かれた"
    ],
    "keywords": ["専門用語1", "固有名詞2", "重要概念3"]
  }},
  "tags": ["プロジェクト名", "部署名", "トピック"]
}}

{constraints}

# 重要な注意事項
- 会議に参加していない人でも内容が理解できるように書く
- 略語や社内用語は正式名称も併記する
- 数字やデータは正確に記録する
- 発言の意図が不明確な場合は「〜という趣旨の発言があった」と記載
- ネガティブな内容も客観的に記録する

# モード
{mode}

# 文字起こし
{text}

# セグメント (話者情報など)
{seg_json}
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





async def generate_summary_and_tags(text: str, mode: str = "lecture", segments: Optional[List[dict]] = None) -> dict:
    """
    要約・タグを1回の Gemini 呼び出しで生成する。
    （以前はPlaylistも混在していたが分離）
    """
    _ensure_model()
    from vertexai.generative_models import GenerationConfig
    # Use the new prompt builder (renamed to avoid confusion, or reused name)
    # I'll rename the builder above to _build_summary_tags_prompt
    prompt = _build_summary_tags_prompt(text, mode, segments)
    resp = await _model.generate_content_async(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.6,
            max_output_tokens=4096,
            response_mime_type="application/json",
        ),
    )
    try:
        data = json.loads(resp.text or "{}")
    except Exception:
        data = {}

    summary_data = data.get("summary") or {}
    raw_tags = data.get("tags") or []
    
    # Ensure points/keywords/tags fallback
    overview = summary_data.get("overview") or ""
    points = summary_data.get("points") or []
    keywords = summary_data.get("keywords") or []

    # Fallback 1: Overview to points if points empty
    if not points and overview:
        try:
            sentences = overview.replace("。", "。\n").split("\n")
            points = [s.strip() for s in sentences if s.strip()][:3]
        except Exception:
            points = []
    
    # Fallback 2: Tags to keywords if keywords empty
    if not keywords and raw_tags:
        keywords = raw_tags[:5]
    
    # Fallback 3: Keywords to tags if tags empty
    if not raw_tags and keywords:
        raw_tags = keywords[:4]

    # Re-normalize tags with new potential source
    tags = _normalize_tags(raw_tags, keywords, mode)
    
    # Update summary data for response consistency
    summary_data["points"] = points
    summary_data["keywords"] = keywords

    summary_md = _summary_json_to_markdown(summary_data)

    return {
        "summaryMarkdown": summary_md,
        "tags": tags
    }



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


def _summary_json_to_markdown(summary: dict) -> str:
    if not summary:
        return ""
    overview = summary.get("overview") or ""
    decisions = summary.get("decisions") or []
    todos = summary.get("todos") or []
    discussion_points = summary.get("discussionPoints") or summary.get("points") or []
    keywords = summary.get("keywords") or []

    lines = []
    lines.append("## 📋 会議サマリー")
    lines.append("")
    
    if overview:
        if isinstance(overview, list):
            lines.append("\n".join(str(o) for o in overview if o))
        else:
            cleaned = str(overview).replace("#", "").strip()
            lines.append(cleaned)
        lines.append("")

    if decisions:
        lines.append("### ✅ 決定事項")
        for d in decisions:
            lines.append(f"- {d}")
        lines.append("")

    if todos:
        lines.append("### 📌 アクションアイテム")
        for t in todos:
            lines.append(f"- {t}")
        lines.append("")

    if discussion_points:
        lines.append("### 💬 議論のポイント")
        for p in discussion_points:
            lines.append(f"- {p}")
        lines.append("")

    if keywords:
        lines.append("### 🔑 キーワード")
        lines.append(", ".join(keywords))
        lines.append("")
        
    return "\n".join(lines).strip()
