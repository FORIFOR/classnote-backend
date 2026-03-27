"""
Overlay Assist endpoint — real-time AI assistance during recording.

Accepts the current transcript and a user query, returns an AI-generated answer.
Consumes 2 AI credits per request.
"""
import logging
from typing import Optional
from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user, CurrentUser
from app.services.ai_credits import ai_credits, estimate_cost

logger = logging.getLogger("app.routes.assist")
router = APIRouter()

PRESET_QUERIES = {
    "summary": "ここまでの内容を200文字以内で要約してください。",
    "keypoints": "ここまでの重要ポイントを箇条書きで5つ以内で抽出してください。",
    "todos": "ここまでに出たTODO・アクションアイテムを箇条書きで抽出してください。",
    "terms": "ここまでに出た専門用語を定義と共に5つ以内でリストアップしてください。",
}

MODE_LABELS = {
    "meeting": "会議",
    "lecture": "講義",
    "translate": "翻訳",
}


class AssistRequest(BaseModel):
    sessionId: Optional[str] = None
    transcript: str = Field(..., max_length=100_000)
    query: str = Field(..., max_length=500)
    preset: Optional[str] = None
    mode: str = "meeting"
    elapsedSec: int = 0


class AssistResponse(BaseModel):
    answer: str
    creditCost: int
    creditsRemaining: Optional[int] = None


@router.post("/v1/assist", response_model=AssistResponse)
async def assist(
    body: AssistRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Generate AI assistance based on current transcript."""

    # 1. Check credits
    credit_cost = estimate_cost("assist")
    try:
        ai_credits.consume(current_user.account_id, credit_cost, "assist")
    except Exception as e:
        err_msg = str(e)
        if "insufficient" in err_msg.lower() or "limit" in err_msg.lower():
            raise HTTPException(status_code=429, detail={"error": "CREDIT_LIMIT", "message": "クレジットが不足しています"})
        raise HTTPException(status_code=500, detail=str(e))

    # 2. Build prompt
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

    system_prompt = f"""あなたは{mode_label}のリアルタイムアシスタントです。
以下は録音中の文字起こし（{elapsed_str}経過、{len(transcript)}文字）です。

# 制約
- 文字起こしの内容のみに基づいて回答する
- 文字起こしにない情報は「まだ言及されていません」と答える
- 簡潔に、箇条書きを活用する
- 日本語で回答する
- 最大300文字以内で回答する"""

    user_message = f"""# 文字起こし
{transcript[-50000:]}

# 質問
{query}"""

    # 3. Call LLM (Vertex AI)
    try:
        import os
        import google.auth
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig

        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        if not project_id:
            _, project_id = google.auth.default()
        vertex_region = os.environ.get("VERTEX_REGION", "us-central1")
        vertexai.init(project=project_id, location=vertex_region)

        model = GenerativeModel(
            model_name=os.environ.get("GEMINI_MODEL_NAME", "gemini-2.0-flash-lite"),
            system_instruction=system_prompt,
        )
        response = model.generate_content(
            user_message,
            generation_config=GenerationConfig(
                temperature=0.5,
                max_output_tokens=1024,
            ),
        )
        answer = response.text.strip()
    except Exception as e:
        logger.exception("[Assist] LLM call failed")
        raise HTTPException(status_code=500, detail=f"AI生成に失敗しました: {str(e)}")

    # 4. Get remaining credits
    remaining = None
    try:
        report = ai_credits.get_report(current_user.account_id)
        remaining = report.get("remaining")
    except Exception:
        pass

    logger.info(f"[Assist] session={body.sessionId}, preset={body.preset}, transcript_len={len(transcript)}, answer_len={len(answer)}")

    return AssistResponse(
        answer=answer,
        creditCost=int(credit_cost),
        creditsRemaining=remaining,
    )
