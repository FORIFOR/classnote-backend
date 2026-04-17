"""
Overlay Assist endpoint — real-time AI assistance during recording.

Two-layer architecture:
  1. Incremental state update (POST /v1/sessions/{id}/meeting-state)
     - Called every 20-30 sec during recording
     - Lightweight extraction of TODOs, decisions, open questions
     - Updates rolling summary in Firestore

  2. Assist query (POST /v1/assist)
     - Called on-demand when user presses Assist button
     - Uses rolling summary + transcript excerpt for high-quality structured response
     - Returns JSON: answer, summary, TODOs, decisions, follow-up questions
"""
import logging
import json
import os
from typing import Optional, List
from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user, CurrentUser, ensure_can_view, ensure_is_owner
from app.services.ai_credits import ai_credits, estimate_cost
from app.firebase import db

logger = logging.getLogger("app.routes.assist")
router = APIRouter()


def _refund_assist_credits(account_id: str, amount: int) -> None:
    """Best-effort refund of AI credits consumed by /v1/assist on failure."""
    if not account_id or not amount:
        return
    try:
        ai_credits.refund(account_id, amount, "assist")
    except Exception as refund_err:
        logger.warning(f"[Assist] credit refund failed for {account_id}: {refund_err}")

# ────────────────────────────────────────────────
# Models
# ────────────────────────────────────────────────

PRESET_QUERIES = {
    "summary": "ここまでの内容を要約してください。重要な発言や決定事項を含めてください。",
    "keypoints": "ここまでの重要ポイントを優先度順に抽出してください。",
    "todos": "ここまでに出たTODO・アクションアイテムを担当者・期限付きで抽出してください。",
    "terms": "ここまでに出た専門用語・キーワードを定義と文脈付きでリストアップしてください。",
    "questions": "未解決の論点や、次に確認すべき質問を提案してください。",
    "review": "内容を復習用にまとめてください。試験に出そうなポイントも含めてください。",
    "fact_check": "直前の発言をFact Checkしてください。",
}

FACT_CHECK_QUERIES_BY_MODE = {
    "meeting": "直前の発言をFact Checkしてください。怪しい点と、その場で安全に確認するフレーズもください。",
    "lecture": "直前の説明をFact Checkしてください。厳密性と試験向けの理解も教えてください。",
    "interview": "直前の面接官の発言や前提をFact Checkしてください。安全な返し方もください。",
}

MODE_LABELS = {
    "meeting": "会議",
    "lecture": "講義",
    "translate": "翻訳",
}


class TodoCandidate(BaseModel):
    task: str
    owner: Optional[str] = None
    due: Optional[str] = None
    confidence: float = 0.0


class MeetingState(BaseModel):
    """Rolling summary stored per session in Firestore."""
    current_topics: List[str] = []
    compressed_memo: str = ""
    decisions: List[str] = []
    todo_candidates: List[dict] = []
    open_questions: List[str] = []
    followup_questions: List[str] = []
    last_segment_index: int = 0


class UpdateStateRequest(BaseModel):
    newSegments: str = Field(..., max_length=50_000, description="New transcript text since last update")
    segmentIndex: int = Field(0, description="Current segment index for tracking")
    mode: str = "meeting"
    elapsedSec: int = 0
    screen_digest: Optional['ScreenDigest'] = Field(None, description="画面変化時の要約 (25秒ごと)")


class UpdateStateResponse(BaseModel):
    success: bool
    state: Optional[dict] = None


class ScreenContext(BaseModel):
    """Screen context from local Gemma analysis (sent by desktop client)."""
    is_relevant: bool = False
    relevance_score: float = 0.0
    screen_type: Optional[str] = None  # slide, spreadsheet, document, code, browser, chat, desktop, other
    summary: Optional[str] = None
    key_items: List[str] = []
    claims_or_numbers: List[str] = []
    needs_fact_check: bool = False
    captured_at: Optional[str] = None
    image_hash: Optional[str] = None


class ScreenDigest(BaseModel):
    """Lightweight screen digest for meeting-state updates."""
    changed: bool = False
    is_relevant: bool = False
    summary: Optional[str] = None
    key_items: List[str] = []
    claims_or_numbers: List[str] = []


class AssistRequest(BaseModel):
    sessionId: Optional[str] = None
    transcript: str = Field(..., max_length=100_000)
    query: str = Field(..., max_length=500)
    preset: Optional[str] = None
    mode: str = "meeting"
    elapsedSec: int = 0
    # Screen context (from local Gemma analysis)
    screen_context: Optional[ScreenContext] = Field(None, description="画面解析結果 (デスクトップ Gemma)")
    # Fact Check fields
    factCheckTarget: Optional[str] = Field(None, max_length=2000)
    factCheckContextBefore: Optional[str] = Field(None, max_length=2000)
    partialText: Optional[str] = Field(None, max_length=5000)
    interviewMessages: Optional[List[dict]] = Field(None)


class AssistTodo(BaseModel):
    task: str
    owner: Optional[str] = None
    due: Optional[str] = None
    confidence: float = 0.0


class FactCheckSource(BaseModel):
    url: str
    title: str
    snippet: Optional[str] = None


class FactCheckResult(BaseModel):
    targetText: str
    verdict: str  # likely_true, partially_true, insufficient, likely_false, outdated, not_enough_context
    verdictLabel: str
    summary: str
    reasons: List[str] = []
    safeReply: Optional[str] = None
    suggestedQuestions: List[str] = []
    needsWebVerification: bool = False
    riskType: List[str] = []
    # Screen cross-reference (if available)
    screenMismatch: Optional[bool] = None  # True if spoken claim != screen data
    spokenClaim: Optional[str] = None
    screenClaim: Optional[str] = None
    # Google Search grounding (Gemini 2.5)
    sources: List[FactCheckSource] = []
    searchQueries: List[str] = []
    groundingSupported: bool = False


class AssistResponse(BaseModel):
    answer: str
    shortSummary: Optional[str] = None
    decisions: List[str] = []
    todos: List[AssistTodo] = []
    followupQuestions: List[str] = []
    openQuestions: List[str] = []
    creditCost: int
    creditsRemaining: Optional[int] = None
    factCheck: Optional[FactCheckResult] = None


# ────────────────────────────────────────────────
# Vertex AI setup (lazy init)
# ────────────────────────────────────────────────

_vertex_initialized = False
ASSIST_MODEL = os.environ.get("ASSIST_MODEL_NAME", "gemini-2.0-flash")
# Fact Check uses a grounded model with Google Search
FACT_CHECK_MODEL = os.environ.get("FACT_CHECK_MODEL_NAME", "gemini-2.5-flash")

def _ensure_vertex():
    global _vertex_initialized
    if _vertex_initialized:
        return
    import google.auth
    import vertexai
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if not project_id:
        _, project_id = google.auth.default()
    vertex_region = os.environ.get("VERTEX_REGION", "us-central1")
    vertexai.init(project=project_id, location=vertex_region)
    _vertex_initialized = True


def _call_gemini_json(system_prompt: str, user_message: str, temperature: float = 0.3) -> dict:
    """Call Gemini and parse JSON response."""
    _ensure_vertex()
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    model = GenerativeModel(
        model_name=ASSIST_MODEL,
        system_instruction=system_prompt,
    )
    response = model.generate_content(
        user_message,
        generation_config=GenerationConfig(
            temperature=temperature,
            max_output_tokens=4096,
            response_mime_type="application/json",
        ),
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _call_gemini_grounded(system_prompt: str, user_message: str, temperature: float = 0.3) -> tuple[str, list, list]:
    """
    Call Gemini 2.5 Flash with Google Search grounding enabled.

    Returns:
        (response_text, sources, search_queries)
        - response_text: The model's text output
        - sources: List of {"url", "title", "snippet"} from grounding_chunks
        - search_queries: List of queries the model actually searched
    """
    _ensure_vertex()
    from vertexai.generative_models import GenerativeModel, GenerationConfig, Tool, grounding

    # Enable Google Search grounding
    google_search_tool = Tool.from_google_search_retrieval(
        grounding.GoogleSearchRetrieval()
    )

    model = GenerativeModel(
        model_name=FACT_CHECK_MODEL,
        system_instruction=system_prompt,
        tools=[google_search_tool],
    )
    response = model.generate_content(
        user_message,
        generation_config=GenerationConfig(
            temperature=temperature,
            max_output_tokens=4096,
        ),
    )

    text = response.text.strip() if response.text else ""

    # Extract grounding metadata
    sources = []
    search_queries = []
    try:
        candidate = response.candidates[0]
        gm = getattr(candidate, "grounding_metadata", None)
        if gm:
            # Search queries
            search_queries = list(getattr(gm, "web_search_queries", []) or [])

            # Grounding chunks (sources)
            chunks = getattr(gm, "grounding_chunks", []) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web:
                    sources.append({
                        "url": getattr(web, "uri", "") or "",
                        "title": getattr(web, "title", "") or "",
                        "snippet": "",
                    })

            # Grounding supports — map text segments to source indices
            supports = getattr(gm, "grounding_supports", []) or []
            for sup in supports:
                segment = getattr(sup, "segment", None)
                indices = getattr(sup, "grounding_chunk_indices", []) or []
                if segment and indices:
                    segment_text = getattr(segment, "text", "") or ""
                    for idx in indices:
                        if 0 <= idx < len(sources) and segment_text:
                            if not sources[idx]["snippet"]:
                                sources[idx]["snippet"] = segment_text[:200]
    except Exception as e:
        logger.warning(f"[Assist] Grounding metadata parse failed: {e}")

    return text, sources, search_queries


def _call_gemini_text(system_prompt: str, user_message: str, temperature: float = 0.5) -> str:
    """Call Gemini and return text response."""
    _ensure_vertex()
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    model = GenerativeModel(
        model_name=ASSIST_MODEL,
        system_instruction=system_prompt,
    )
    response = model.generate_content(
        user_message,
        generation_config=GenerationConfig(
            temperature=temperature,
            max_output_tokens=2048,
        ),
    )
    return response.text.strip()


# ────────────────────────────────────────────────
# Fact Check: target extraction logic
# ────────────────────────────────────────────────

import re

# Minimum length to attempt fact check (too short = unreliable)
_FC_MIN_TARGET_LEN = 15

# Claim indicators: sentences containing these are more likely worth checking
_CLAIM_INDICATORS = re.compile(
    r"(無料|無制限|必ず|絶対|全て|全員|法律|制度|義務|禁止|違法|合法|"
    r"今年から|来年から|先月|昨年|最新|改正|変更|廃止|"
    r"\d+万|\d+円|\d+%|\d+倍|\d+人|"
    r"完全|フル|業界初|日本初|世界初|"
    r"一番|最も|唯一|No\.\s?1)"
)


def _extract_fact_check_target(body: 'AssistRequest') -> tuple[Optional[str], str]:
    """
    Determine what text to fact-check based on priority rules.
    Returns (target_text, source_description) or (None, rejection_reason).

    Priority:
    1. Interview mode: last interviewer message
    2. User-provided factCheckTarget
    3. Last finalized transcript segment (句点で終わる文)
    4. Last complete sentence from partialText
    5. Last 50-200 chars as claim candidate
    """
    # Priority 1: Interview mode — last interviewer message
    if body.mode == "interview" and body.interviewMessages:
        for msg in reversed(body.interviewMessages):
            if msg.get("role") == "interviewer" and msg.get("content", "").strip():
                text = msg["content"].strip()
                if len(text) >= _FC_MIN_TARGET_LEN:
                    return text, "面接官の直近発話"
                break

    # Priority 2: User explicitly provided target
    if body.factCheckTarget and body.factCheckTarget.strip():
        target = body.factCheckTarget.strip()
        if len(target) >= _FC_MIN_TARGET_LEN:
            return target, "ユーザー指定"
        return None, f"検証対象が短すぎます（{len(target)}文字）。もう少し具体的な発言を指定してください。"

    # Priority 3: Last finalized sentence from transcript
    transcript = body.transcript.strip()
    if transcript:
        # Split by Japanese period, find last complete sentence
        sentences = [s.strip() for s in transcript.replace("\n", "。").split("。") if s.strip()]
        # Walk backwards to find a meaningful sentence
        for sent in reversed(sentences[-10:]):
            if len(sent) >= _FC_MIN_TARGET_LEN:
                return sent, "直近の確定発話"

    # Priority 4: Last complete sentence from partialText
    if body.partialText and body.partialText.strip():
        partial = body.partialText.strip()
        # Extract complete sentences (ending with 。！？.)
        complete = re.split(r'[。！？\.]\s*', partial)
        complete = [s.strip() for s in complete if s.strip()]
        if complete and len(complete[-1]) >= _FC_MIN_TARGET_LEN:
            # The last element after split may be incomplete — use second-to-last if available
            for sent in reversed(complete[:-1] if len(complete) > 1 else complete):
                if len(sent) >= _FC_MIN_TARGET_LEN:
                    return sent, "直近の発話（確定前）"

    # Priority 5: Claim candidate from tail of transcript
    if transcript:
        tail = transcript[-300:]
        # Find sentences with claim indicators
        sentences = [s.strip() for s in tail.replace("\n", "。").split("。") if s.strip()]
        for sent in reversed(sentences):
            if len(sent) >= _FC_MIN_TARGET_LEN and _CLAIM_INDICATORS.search(sent):
                return sent, "主張候補（自動検出）"

    return None, "検証対象の発言が見つかりませんでした。もう少し話が進んでから、または気になる発言を入力欄に書いてお試しください。"


def _is_checkable_claim(text: str) -> bool:
    """Check if text contains a factual claim worth verifying (not just opinion/small talk)."""
    if len(text) < _FC_MIN_TARGET_LEN:
        return False
    # Opinions and small talk patterns
    opinion_patterns = re.compile(
        r"^(なんか|たぶん|微妙|すごい|いいね|そうですね|はい|うん|ありがとう|お疲れ)"
    )
    if opinion_patterns.match(text) and len(text) < 30:
        return False
    return True


# ────────────────────────────────────────────────
# Mode-specific system prompts
# ────────────────────────────────────────────────

_MEETING_SYSTEM = """あなたは会議のリアルタイムアシスタントです。
文字起こしの内容に基づいて、正確で実用的な回答を返してください。

■ 回答ルール:
- 文字起こしに明確な根拠がある事項のみ回答に含める
- 推測が必要な場合は「〜と思われます」等で明示する
- TODO・アクションアイテムは発言に基づくもののみ抽出する
- 担当者・期限が不明なら null にする
- confidence は 0.0〜1.0（明確な指示=高、推測=低）

■ 出力形式:
- answer は質問への直接的な回答。簡潔だが必要な情報は省略しない
- short_summary は会議全体の流れを200文字程度で要約
- followup_questions は次に確認すべき具体的な質問（3つ以内）
- 日本語で出力

■ 出力JSON:
{
  "answer": "質問への回答",
  "short_summary": "ここまでの要約",
  "decisions": ["決定事項"],
  "todos": [{"task": "内容", "owner": "担当者|null", "due": "期限|null", "confidence": 0.8}],
  "followup_questions": ["確認すべき質問"],
  "open_questions": ["未解決の論点"]
}"""

_LECTURE_SYSTEM = """あなたは講義のリアルタイムアシスタントです。
文字起こしの内容に基づいて、学習を支援する回答を返してください。

■ 回答ルール:
- 講義内容の理解を深める回答を心がける
- 専門用語には簡潔な解説を添える
- 重要なポイントは強調して伝える
- 文字起こしにない内容を断定しない
- 試験対策として役立つ情報を含める

■ 出力形式:
- answer は質問への回答。学生が理解しやすい表現で、必要な情報を十分に含める
- short_summary は講義の要点を200文字程度で要約
- todos は「復習すべき項目」「調べるべき用語」等の学習タスク
- followup_questions は理解を深めるための質問（3つ以内）
- 日本語で出力

■ 出力JSON:
{
  "answer": "質問への回答",
  "short_summary": "講義の要点",
  "decisions": ["重要な定義・結論"],
  "todos": [{"task": "学習タスク", "owner": null, "due": null, "confidence": 0.8}],
  "followup_questions": ["理解を深める質問"],
  "open_questions": ["講義で触れられたが未解説の点"]
}"""

_TRANSLATE_SYSTEM = """あなたは翻訳セッションのリアルタイムアシスタントです。
多言語の文字起こし内容に基づいて回答してください。

■ 回答ルール:
- 原文の言語と内容を正確に把握して回答する
- 文化的な背景や慣用表現の解説を含める
- 翻訳の質や表現のニュアンスに関する質問に答える
- 回答は必ず日本語で返す

■ 出力形式:
- answer は質問への回答。翻訳内容に基づいて日本語で回答
- short_summary は話されている内容の要約
- todos は「確認すべき表現」「復習すべき語彙」等
- 日本語で出力

■ 出力JSON:
{
  "answer": "質問への回答",
  "short_summary": "内容の要約",
  "decisions": ["重要な表現・フレーズ"],
  "todos": [{"task": "学習すべき語彙・表現", "owner": null, "due": null, "confidence": 0.8}],
  "followup_questions": ["確認すべき質問"],
  "open_questions": ["不明な表現・文脈"]
}"""


_FACT_CHECK_SYSTEM = """あなたは発言の確からしさを検証するFact Checkアシスタントです。
会話の文字起こしから、指定された発言の正確性を2段階で評価してください。

■ 重要な前提:
- 文字起こし結果を元にしているため、聞き取り誤差がある可能性があります
- あなた自身の知識にも限界があります。「正しい」と断定しすぎないでください
- 迷ったら「要確認」に倒してください。誤って「正しい」と言うより安全です
- Google検索ツールが利用可能な場合は積極的に使って最新情報を確認してください
- 検索結果を根拠として使う場合は、reasons に出典を含める形で書いてください

■ 2段階判定プロセス:
Step 1 — 主張の再構成
  まず検証対象の発言から以下を整理する:
  - この発言の核心的な主張は何か
  - どの部分が断定的か（数値、時期、条件、比較）
  - 最新性に依存するか
  - 事実主張なのか感想なのか
  → 感想や挨拶の場合は verdict: not_enough_context で「事実主張ではない」と返す

Step 2 — 検証
  Step 1 で特定した主張だけを検証する:
  - 一般知識で確認できるか
  - 条件付きか（例: 「無料」→ 無料枠のみかもしれない）
  - 最新情報に依存するか（料金、制度、法律、仕様 → 厳しく判定）
  - 比較やランキングが根拠付きか

■ verdict の判定基準（厳しめに判定）:
- likely_true: 高い確率で正しい（教科書的事実、普遍的定理、明白な事実）
- partially_true: 一部正しいが条件や例外がある
- insufficient: 根拠が不十分。特に以下は自動的にこれにする:
  → 料金・価格、法律・制度、会社情報、製品仕様、採用条件、年度依存情報
- likely_false: 一般知識から見て誤りの可能性が高い
- outdated: 以前は正しかったが変更されている可能性
- not_enough_context: 発言が短すぎる/曖昧/感想であり判定困難

■ 出力ルール:
- 「〜です」ではなく「〜の可能性があります」を使う
- 相手を否定せず確認を促す表現にする
- safe_reply は「その場で失礼なく確認できるフレーズ」（必須）
- reasons は3〜5件。短く具体的に
- suggested_questions は深掘り質問候補 (3〜4件)
- risk_type は [pricing, freshness, legal, specification, comparison, company_info, statistics, interview_assumption, definition] から該当するものを選ぶ
- needs_web_verification: 最新性依存 or 検索で確認可能 → true

■ 出力JSON:
{
  "answer": "Fact Check結果の要約テキスト (通常のanswer互換)",
  "short_summary": "一文の判定結果",
  "fact_check": {
    "target_text": "検証対象の発言",
    "claim_analysis": "Step 1の主張再構成（この発言は〜という主張をしている）",
    "verdict": "insufficient",
    "verdict_label": "根拠不十分 / 要確認",
    "summary": "検証結果の説明文 (50-100文字)",
    "reasons": ["理由1", "理由2", "理由3"],
    "safe_reply": "失礼なく確認するフレーズ",
    "suggested_questions": ["深掘り質問1", "深掘り質問2"],
    "needs_web_verification": true,
    "risk_type": ["pricing", "freshness"],
    "screen_mismatch": false,
    "spoken_claim": "発言中の数値や主張",
    "screen_claim": "画面に表示されている数値（あれば）"
  },
  "followup_questions": ["Fact Checkの深掘り質問をここにも入れる"],
  "decisions": [],
  "todos": [],
  "open_questions": []
}"""

_FACT_CHECK_LECTURE_EXTRA = """
■ 講義モード追加ルール:
- 教科書的にはどうか、厳密にはどうかを区別する
- 試験ではどう書くのが正しいかも補足する
- 先生の説明が省略や簡略化の場合はその旨指摘する
"""

_FACT_CHECK_INTERVIEW_EXTRA = """
■ 面接モード追加ルール:
- 面接官の説明(会社情報、制度、福利厚生等)の前提が安全かを評価する
- 鵜呑みにすると危ない場合は指摘する
- 相手を否定せずに確認する言い方を safe_reply に含める
- 「入社後に違った」を防ぐ確認ポイントを挙げる
"""


def _get_system_prompt(mode: str, elapsed_str: str, preset: Optional[str] = None) -> str:
    """Get mode-specific system prompt with elapsed time."""
    if preset == "fact_check":
        base = _FACT_CHECK_SYSTEM
        if mode == "lecture":
            base += _FACT_CHECK_LECTURE_EXTRA
        elif mode == "interview":
            base += _FACT_CHECK_INTERVIEW_EXTRA
    elif mode == "lecture":
        base = _LECTURE_SYSTEM
    elif mode == "translate":
        base = _TRANSLATE_SYSTEM
    else:
        base = _MEETING_SYSTEM
    return f"{base}\n\n経過時間: {elapsed_str}"


# ────────────────────────────────────────────────
# Endpoint 1: Incremental meeting state update
# ────────────────────────────────────────────────

@router.post("/v1/sessions/{session_id}/meeting-state", response_model=UpdateStateResponse)
async def update_meeting_state(
    session_id: str,
    body: UpdateStateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Incrementally update the rolling meeting summary."""
    new_text = body.newSegments.strip()
    if not new_text or len(new_text) < 10:
        return UpdateStateResponse(success=True, state=None)

    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")

    session_data = doc.to_dict() or {}
    # SECURITY: meetingState is a mutation; require ownership (matches assets/notes policy)
    ensure_is_owner(session_data, current_user, session_id)

    existing_data = session_data.get("meetingState", {})
    existing_state = MeetingState(**existing_data) if existing_data else MeetingState()

    mode_label = MODE_LABELS.get(body.mode, body.mode)
    elapsed_min = body.elapsedSec // 60

    system_prompt = f"""あなたは{mode_label}の内容を増分整理するアシスタントです。
既存の状態と新しい発話を受け取り、更新された状態をJSON形式で返してください。

# ルール
- 既存の情報を維持しつつ、新しい情報をマージする
- TODO候補は発話に基づくもののみ。担当者・期限が不明なら null
- confidence は 0.0〜1.0（明確に指示されたもの = 高、推測 = 低）
- open_questions は未解決の論点・確認事項
- followup_questions は「次に確認すべき質問」候補（3つ以内）
- compressed_memo は全体の圧縮要約（300文字以内）
- current_topics は現在議論中のトピック（3つ以内）
- 日本語で出力

# 出力JSONスキーマ
{{
  "current_topics": ["string"],
  "compressed_memo": "string",
  "decisions": ["string"],
  "todo_candidates": [{{"task": "string", "owner": "string|null", "due": "string|null", "confidence": 0.0}}],
  "open_questions": ["string"],
  "followup_questions": ["string"]
}}"""

    user_message = f"""# 既存の状態
{json.dumps({
    "current_topics": existing_state.current_topics,
    "compressed_memo": existing_state.compressed_memo,
    "decisions": existing_state.decisions,
    "todo_candidates": existing_state.todo_candidates,
    "open_questions": existing_state.open_questions,
}, ensure_ascii=False)}

# 新しい発話（{elapsed_min}分経過時点）
{new_text}"""

    try:
        result = _call_gemini_json(system_prompt, user_message, temperature=0.2)
    except Exception as e:
        logger.warning(f"[MeetingState] LLM call failed: {e}")
        doc_ref.update({"meetingState.last_segment_index": body.segmentIndex})
        return UpdateStateResponse(success=True, state=None)

    updated_state = {
        "current_topics": result.get("current_topics", existing_state.current_topics),
        "compressed_memo": result.get("compressed_memo", existing_state.compressed_memo),
        "decisions": result.get("decisions", existing_state.decisions),
        "todo_candidates": result.get("todo_candidates", existing_state.todo_candidates),
        "open_questions": result.get("open_questions", existing_state.open_questions),
        "followup_questions": result.get("followup_questions", []),
        "last_segment_index": body.segmentIndex,
    }

    # Persist screen digest if provided
    if body.screen_digest and body.screen_digest.changed and body.screen_digest.is_relevant:
        updated_state["screen_digest"] = body.screen_digest.model_dump()

    doc_ref.update({"meetingState": updated_state})
    logger.info(f"[MeetingState] Updated session={session_id}, topics={len(updated_state['current_topics'])}, todos={len(updated_state['todo_candidates'])}")

    return UpdateStateResponse(success=True, state=updated_state)


# ────────────────────────────────────────────────
# Endpoint 2: Assist query (structured response)
# ────────────────────────────────────────────────

@router.post("/v1/assist", response_model=AssistResponse)
async def assist(
    body: AssistRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Generate structured AI assistance based on meeting state + transcript."""

    # 1. Check credits
    # NOTE: ai_credits.consume returns (ok, info); it does NOT raise on limit hit.
    # Previous try/except-only implementation silently skipped the limit check.
    credit_cost = estimate_cost("assist")
    try:
        consume_ok, consume_info = ai_credits.consume(current_user.account_id, credit_cost, "assist")
    except Exception as e:
        logger.exception("[Assist] ai_credits.consume raised unexpectedly")
        raise HTTPException(status_code=500, detail=str(e))

    if not consume_ok:
        info = consume_info or {}
        raise HTTPException(
            status_code=429,
            detail={
                "error": info.get("reason", "CREDIT_LIMIT"),
                "message": "クレジットが不足しています",
                "creditCost": credit_cost,
                "creditsRemaining": info.get("remaining", 0),
                "dailyUsed": info.get("dailyUsed"),
                "dailySoftCap": info.get("dailySoftCap"),
            },
        )
    credits_consumed_amount = credit_cost

    # 2a. Pre-fetch remaining credits (needed for early returns in fact_check)
    remaining = None
    try:
        report = ai_credits.get_credit_report(current_user.account_id)
        remaining = report.get("remaining")
    except Exception:
        pass

    # 2. Load meeting state if session exists
    meeting_state = None
    if body.sessionId:
        try:
            doc = db.collection("sessions").document(body.sessionId).get()
            if doc.exists:
                sess_data = doc.to_dict() or {}
                # SECURITY: verify viewer access before reading meetingState/transcript into the prompt
                ensure_can_view(sess_data, current_user, body.sessionId)
                raw = sess_data.get("meetingState")
                if raw:
                    meeting_state = raw
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"[Assist] Failed to load meeting state: {e}")

    # 3. Build prompt
    is_fact_check = body.preset == "fact_check"

    query = body.query
    if is_fact_check:
        query = FACT_CHECK_QUERIES_BY_MODE.get(body.mode, PRESET_QUERIES["fact_check"])
    elif body.preset and body.preset in PRESET_QUERIES:
        query = PRESET_QUERIES[body.preset]

    elapsed_min = body.elapsedSec // 60
    elapsed_sec = body.elapsedSec % 60
    elapsed_str = f"{elapsed_min}分{elapsed_sec}秒" if elapsed_min > 0 else f"{elapsed_sec}秒"

    transcript = body.transcript.strip()
    if not transcript:
        raise HTTPException(status_code=422, detail="文字起こしが空です")

    # Build context from meeting state
    state_context = ""
    if meeting_state:
        state_context = f"""# 蓄積された会議状態
- 議題: {', '.join(meeting_state.get('current_topics', []))}
- 要約: {meeting_state.get('compressed_memo', '')}
- 決定事項: {json.dumps(meeting_state.get('decisions', []), ensure_ascii=False)}
- TODO候補: {json.dumps(meeting_state.get('todo_candidates', []), ensure_ascii=False)}
- 未解決事項: {json.dumps(meeting_state.get('open_questions', []), ensure_ascii=False)}

"""

    system_prompt = _get_system_prompt(body.mode, elapsed_str, preset=body.preset)

    # Use last 40K chars of transcript
    recent_transcript = transcript[-40000:]

    # Fact Check: extract and validate target text
    fact_check_section = ""
    _fc_resolved_target = None
    if is_fact_check:
        target, source_desc = _extract_fact_check_target(body)

        if target is None:
            # Cannot determine target — return early with helpful message
            return AssistResponse(
                answer=source_desc,  # Contains the rejection reason
                shortSummary="検証対象が特定できませんでした",
                creditCost=0,
                creditsRemaining=remaining,
                factCheck=FactCheckResult(
                    targetText="",
                    verdict="not_enough_context",
                    verdictLabel="対象不足",
                    summary=source_desc,
                    reasons=["検証対象の発言が短すぎるか、事実主張が含まれていません"],
                    suggestedQuestions=["気になる発言を入力欄に書いてお試しください"],
                ),
            )

        if not _is_checkable_claim(target):
            return AssistResponse(
                answer=f"「{target[:50]}」は事実主張というよりも感想や相槌に近いため、Fact Checkの対象外です。",
                shortSummary="事実主張ではないためFact Check対象外",
                creditCost=0,
                creditsRemaining=remaining,
                factCheck=FactCheckResult(
                    targetText=target,
                    verdict="not_enough_context",
                    verdictLabel="事実主張ではない",
                    summary="この発言は感想や相槌であり、事実検証の対象ではありません。",
                    reasons=["事実を断定する主張が含まれていません"],
                    suggestedQuestions=["具体的な数字や制度に関する発言をFact Checkしてみてください"],
                ),
            )

        _fc_resolved_target = target
        fact_check_section = f"""
# 検証対象の発言 ({source_desc})
「{target}」
"""
        if body.factCheckContextBefore:
            fact_check_section += f"""
# 前後の文脈
{body.factCheckContextBefore}
"""
        # Screen cross-reference for Fact Check
        screen_claims = []
        if body.screen_context and body.screen_context.is_relevant:
            screen_claims = body.screen_context.claims_or_numbers or []
        elif meeting_state and meeting_state.get("screen_digest", {}).get("claims_or_numbers"):
            screen_claims = meeting_state["screen_digest"]["claims_or_numbers"]

        if screen_claims:
            fact_check_section += f"""
# 画面に表示されている数値・情報（照合用）
{chr(10).join(f'- {c}' for c in screen_claims[:10])}
（発言と画面の数値が食い違う場合は必ず指摘してください）
"""

    # Screen context section (from local Gemma analysis)
    screen_section = ""
    if body.screen_context and body.screen_context.is_relevant and body.screen_context.summary:
        sc = body.screen_context
        screen_section = f"""
# 画面に表示されている資料 (screen_type: {sc.screen_type or 'unknown'})
要約: {sc.summary}
主な項目: {', '.join(sc.key_items[:10]) if sc.key_items else 'なし'}
数値・主張: {', '.join(sc.claims_or_numbers[:10]) if sc.claims_or_numbers else 'なし'}
"""
    # Also check meeting_state for latest screen digest
    elif meeting_state and meeting_state.get("screen_digest"):
        sd = meeting_state["screen_digest"]
        if sd.get("is_relevant") and sd.get("summary"):
            screen_section = f"""
# 直近の画面要約
{sd['summary']}
"""

    user_message = f"""{state_context}{screen_section}# 文字起こし（直近）
{recent_transcript}
{fact_check_section}
# 質問
{query}"""

    # 4. Call LLM
    # Fact Check uses Gemini 2.5 Flash with Google Search grounding
    # Other presets use the standard JSON-mode call
    fc_sources = []
    fc_search_queries = []
    fc_grounding_supported = False

    try:
        if is_fact_check:
            # Grounded call returns free-form text — need to parse JSON from it
            try:
                grounded_text, fc_sources, fc_search_queries = _call_gemini_grounded(
                    system_prompt, user_message, temperature=0.3
                )
                fc_grounding_supported = len(fc_sources) > 0 or len(fc_search_queries) > 0

                # Parse JSON from grounded response (may be wrapped in markdown)
                text = grounded_text.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                # Find first { ... } block
                import re
                m = re.search(r'\{.*\}', text, re.DOTALL)
                if m:
                    result = json.loads(m.group(0))
                else:
                    result = {"answer": grounded_text}
            except Exception as ground_err:
                logger.warning(f"[Assist] Grounded fact_check failed, falling back to standard: {ground_err}")
                result = _call_gemini_json(system_prompt, user_message, temperature=0.3)
        else:
            result = _call_gemini_json(system_prompt, user_message, temperature=0.3)
    except json.JSONDecodeError:
        logger.warning("[Assist] JSON parse failed, falling back to text")
        try:
            answer = _call_gemini_text(system_prompt, user_message)
            result = {"answer": answer}
        except Exception as e:
            logger.exception("[Assist] Fallback text call failed")
            _refund_assist_credits(current_user.account_id, credits_consumed_amount)
            raise HTTPException(status_code=500, detail=f"AI生成に失敗しました: {str(e)}")
    except Exception as e:
        logger.exception("[Assist] LLM call failed")
        _refund_assist_credits(current_user.account_id, credits_consumed_amount)
        raise HTTPException(status_code=500, detail=f"AI生成に失敗しました: {str(e)}")

    # 5. Refresh remaining credits (may have changed after consumption)
    try:
        report = ai_credits.get_credit_report(current_user.account_id)
        remaining = report.get("remaining")
    except Exception:
        pass

    # 6. Parse and validate response
    todos = []
    for t in result.get("todos", []):
        if isinstance(t, dict) and t.get("task"):
            todos.append(AssistTodo(
                task=t["task"],
                owner=t.get("owner"),
                due=t.get("due"),
                confidence=float(t.get("confidence", 0.0)),
            ))

    answer = result.get("answer", "回答を生成できませんでした。")

    # Parse fact_check result if present
    fact_check_result = None
    fc_raw = result.get("fact_check")
    if fc_raw and isinstance(fc_raw, dict):
        # Map verdict to label
        verdict = fc_raw.get("verdict", "not_enough_context")
        verdict_labels = {
            "likely_true": "概ね正しい",
            "partially_true": "一部正しい / 条件付き",
            "insufficient": "根拠不十分 / 要確認",
            "likely_false": "誤りの可能性が高い",
            "outdated": "古い可能性あり",
            "not_enough_context": "文脈不足で判定不能",
        }
        fact_check_result = FactCheckResult(
            targetText=fc_raw.get("target_text", _fc_resolved_target or body.factCheckTarget or ""),
            verdict=verdict,
            verdictLabel=fc_raw.get("verdict_label", verdict_labels.get(verdict, verdict)),
            summary=fc_raw.get("summary", ""),
            reasons=fc_raw.get("reasons", []),
            safeReply=fc_raw.get("safe_reply"),
            suggestedQuestions=fc_raw.get("suggested_questions", []),
            needsWebVerification=fc_raw.get("needs_web_verification", False),
            riskType=fc_raw.get("risk_type", []),
            screenMismatch=fc_raw.get("screen_mismatch"),
            spokenClaim=fc_raw.get("spoken_claim"),
            screenClaim=fc_raw.get("screen_claim"),
            sources=[FactCheckSource(**s) for s in fc_sources],
            searchQueries=fc_search_queries,
            groundingSupported=fc_grounding_supported,
        )
        # Merge suggested questions into followup_questions too
        followup = result.get("followup_questions", [])
        for sq in fc_raw.get("suggested_questions", []):
            if sq not in followup:
                followup.append(sq)
        result["followup_questions"] = followup

    logger.info(
        f"[Assist] session={body.sessionId}, preset={body.preset}, mode={body.mode}, "
        f"transcript_len={len(transcript)}, answer_len={len(answer)}, "
        f"todos={len(todos)}, has_state={'yes' if meeting_state else 'no'}, "
        f"fact_check={'yes' if fact_check_result else 'no'}"
    )

    return AssistResponse(
        answer=answer,
        shortSummary=result.get("short_summary"),
        decisions=result.get("decisions", []),
        todos=todos,
        followupQuestions=result.get("followup_questions", []),
        openQuestions=result.get("open_questions", []),
        creditCost=int(credit_cost),
        creditsRemaining=remaining,
        factCheck=fact_check_result,
    )
