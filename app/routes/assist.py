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
from app.dependencies import get_current_user, CurrentUser
from app.services.ai_credits import ai_credits, estimate_cost
from app.firebase import db

logger = logging.getLogger("app.routes.assist")
router = APIRouter()

# ────────────────────────────────────────────────
# Models
# ────────────────────────────────────────────────

PRESET_QUERIES = {
    "summary": "ここまでの内容を要約してください。",
    "keypoints": "ここまでの重要ポイントを抽出してください。",
    "todos": "ここまでに出たTODO・アクションアイテムを抽出してください。",
    "terms": "ここまでに出た専門用語を定義と共にリストアップしてください。",
    "questions": "確認した方がいい点を提案してください。",
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


class UpdateStateResponse(BaseModel):
    success: bool
    state: Optional[dict] = None


class AssistRequest(BaseModel):
    sessionId: Optional[str] = None
    transcript: str = Field(..., max_length=100_000)
    query: str = Field(..., max_length=500)
    preset: Optional[str] = None
    mode: str = "meeting"
    elapsedSec: int = 0


class AssistTodo(BaseModel):
    task: str
    owner: Optional[str] = None
    due: Optional[str] = None
    confidence: float = 0.0


class AssistResponse(BaseModel):
    answer: str
    shortSummary: Optional[str] = None
    decisions: List[str] = []
    todos: List[AssistTodo] = []
    followupQuestions: List[str] = []
    openQuestions: List[str] = []
    creditCost: int
    creditsRemaining: Optional[int] = None


# ────────────────────────────────────────────────
# Vertex AI setup (lazy init)
# ────────────────────────────────────────────────

_vertex_initialized = False

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
        model_name=os.environ.get("ASSIST_MODEL_NAME", "gemini-2.5-flash-lite"),
        system_instruction=system_prompt,
    )
    response = model.generate_content(
        user_message,
        generation_config=GenerationConfig(
            temperature=temperature,
            max_output_tokens=2048,
            response_mime_type="application/json",
        ),
    )
    text = response.text.strip()
    # Handle potential markdown code blocks
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _call_gemini_text(system_prompt: str, user_message: str, temperature: float = 0.5) -> str:
    """Call Gemini and return text response."""
    _ensure_vertex()
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    model = GenerativeModel(
        model_name=os.environ.get("ASSIST_MODEL_NAME", "gemini-2.5-flash-lite"),
        system_instruction=system_prompt,
    )
    response = model.generate_content(
        user_message,
        generation_config=GenerationConfig(
            temperature=temperature,
            max_output_tokens=1024,
        ),
    )
    return response.text.strip()


# ────────────────────────────────────────────────
# Endpoint 1: Incremental meeting state update
# ────────────────────────────────────────────────

@router.post("/v1/sessions/{session_id}/meeting-state", response_model=UpdateStateResponse)
async def update_meeting_state(
    session_id: str,
    body: UpdateStateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Incrementally update the rolling meeting summary.

    Called every 20-30 seconds during recording. Uses Flash-Lite to extract
    new information and merge with existing state.
    """
    new_text = body.newSegments.strip()
    if not new_text or len(new_text) < 10:
        return UpdateStateResponse(success=True, state=None)

    # Load existing state from Firestore
    doc_ref = db.collection("sessions").document(session_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Session not found")

    existing_data = doc.to_dict().get("meetingState", {})
    existing_state = MeetingState(**existing_data) if existing_data else MeetingState()

    mode_label = MODE_LABELS.get(body.mode, body.mode)
    elapsed_min = body.elapsedSec // 60

    system_prompt = f"""あなたは{mode_label}の内容を増分整理するアシスタントです。
既存の会議状態と新しい発話を受け取り、更新された会議状態をJSON形式で返してください。

# ルール
- 既存の情報を維持しつつ、新しい情報をマージする
- TODO候補は発話に基づくもののみ。担当者・期限が不明なら null
- confidence は 0.0〜1.0（明確に指示されたもの = 高、推測 = 低）
- open_questions は未解決の論点・確認事項
- followup_questions は「次に確認すべき質問」候補（3つ以内）
- compressed_memo は会議全体の圧縮要約（200文字以内）
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

    user_message = f"""# 既存の会議状態
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
        # Non-fatal: just save the segment index
        doc_ref.update({"meetingState.last_segment_index": body.segmentIndex})
        return UpdateStateResponse(success=True, state=None)

    # Update Firestore
    updated_state = {
        "current_topics": result.get("current_topics", existing_state.current_topics),
        "compressed_memo": result.get("compressed_memo", existing_state.compressed_memo),
        "decisions": result.get("decisions", existing_state.decisions),
        "todo_candidates": result.get("todo_candidates", existing_state.todo_candidates),
        "open_questions": result.get("open_questions", existing_state.open_questions),
        "followup_questions": result.get("followup_questions", []),
        "last_segment_index": body.segmentIndex,
    }

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
    credit_cost = estimate_cost("assist")
    try:
        ai_credits.consume(current_user.account_id, credit_cost, "assist")
    except Exception as e:
        err_msg = str(e)
        if "insufficient" in err_msg.lower() or "limit" in err_msg.lower():
            raise HTTPException(status_code=429, detail={"error": "CREDIT_LIMIT", "message": "クレジットが不足しています"})
        raise HTTPException(status_code=500, detail=str(e))

    # 2. Load meeting state if session exists
    meeting_state = None
    if body.sessionId:
        try:
            doc = db.collection("sessions").document(body.sessionId).get()
            if doc.exists:
                raw = doc.to_dict().get("meetingState")
                if raw:
                    meeting_state = raw
        except Exception as e:
            logger.warning(f"[Assist] Failed to load meeting state: {e}")

    # 3. Build prompt
    query = body.query
    if body.preset and body.preset in PRESET_QUERIES:
        query = PRESET_QUERIES[body.preset]

    mode_label = MODE_LABELS.get(body.mode, body.mode)
    elapsed_min = body.elapsedSec // 60
    elapsed_sec = body.elapsedSec % 60
    elapsed_str = f"{elapsed_min}分{elapsed_sec}秒" if elapsed_min > 0 else f"{elapsed_sec}秒"

    transcript = body.transcript.strip()
    if not transcript:
        raise HTTPException(status_code=422, detail="文字起こしが空です")

    # Build context from meeting state + transcript
    state_context = ""
    if meeting_state:
        state_context = f"""# 会議状態（自動更新済み）
- 議題: {', '.join(meeting_state.get('current_topics', []))}
- 要約: {meeting_state.get('compressed_memo', '')}
- 決定事項: {json.dumps(meeting_state.get('decisions', []), ensure_ascii=False)}
- TODO候補: {json.dumps(meeting_state.get('todo_candidates', []), ensure_ascii=False)}
- 未解決事項: {json.dumps(meeting_state.get('open_questions', []), ensure_ascii=False)}

"""

    system_prompt = f"""あなたは{mode_label}のリアルタイムアシスタントです。
{elapsed_str}経過した{mode_label}の内容に基づいて回答してください。

# 役割
- 会議アシスタントとして振る舞う
- 文字起こしにないことは断定しない
- TODO は根拠発話に基づく
- 不明な担当者や期限は null にする
- ユーザーが次に確認すべき質問を3件以内で出す
- 日本語で回答する

# 出力JSONスキーマ
{{
  "answer": "ユーザー質問への直接回答（300文字以内）",
  "short_summary": "ここまでの簡潔な要約（200文字以内）",
  "decisions": ["決定された事項のリスト"],
  "todos": [{{"task": "タスク内容", "owner": "担当者|null", "due": "期限|null", "confidence": 0.0}}],
  "followup_questions": ["次に確認すべき具体的な質問（3つ以内）"],
  "open_questions": ["未解決の論点"]
}}"""

    # Use last 30K chars of transcript (recent context is most relevant)
    recent_transcript = transcript[-30000:]
    user_message = f"""{state_context}# 文字起こし（直近）
{recent_transcript}

# 質問
{query}"""

    # 4. Call LLM
    try:
        result = _call_gemini_json(system_prompt, user_message, temperature=0.3)
    except json.JSONDecodeError:
        # Fallback: get text response
        logger.warning("[Assist] JSON parse failed, falling back to text")
        try:
            answer = _call_gemini_text(system_prompt, user_message)
            result = {"answer": answer}
        except Exception as e:
            logger.exception("[Assist] Fallback text call failed")
            raise HTTPException(status_code=500, detail=f"AI生成に失敗しました: {str(e)}")
    except Exception as e:
        logger.exception("[Assist] LLM call failed")
        raise HTTPException(status_code=500, detail=f"AI生成に失敗しました: {str(e)}")

    # 5. Get remaining credits
    remaining = None
    try:
        report = ai_credits.get_report(current_user.account_id)
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
    logger.info(
        f"[Assist] session={body.sessionId}, preset={body.preset}, "
        f"transcript_len={len(transcript)}, answer_len={len(answer)}, "
        f"todos={len(todos)}, has_state={'yes' if meeting_state else 'no'}"
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
    )
