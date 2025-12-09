from typing import Literal, Optional, List
from pydantic import BaseModel, Field
from datetime import datetime


class CreateSessionRequest(BaseModel):
    title: str
    mode: Literal["lecture", "meeting"] = "lecture"
    userId: str = Field(..., description="Firebase Auth の uid など")


# ---------- 話者分離 (Diarization) Models ---------- #

class Speaker(BaseModel):
    """話者情報"""
    id: str
    label: str
    displayName: str
    colorHex: Optional[str] = None


class DiarizedSegment(BaseModel):
    """話者分離されたセグメント"""
    id: str
    start: float
    end: float
    speakerId: str
    text: str




class SessionResponse(BaseModel):
    id: str
    title: str
    mode: str
    userId: str
    status: str
    createdAt: datetime
    startedAt: Optional[datetime] = None
    endedAt: Optional[datetime] = None
    durationSec: Optional[float] = None
    
    # コンテンツ
    transcriptText: Optional[str] = None
    notes: Optional[str] = None
    summaryMarkdown: Optional[str] = None
    quizMarkdown: Optional[str] = None
    
    # メディア
    audioPath: Optional[str] = None
    
    # フラグ (一覧用)
    hasSummary: Optional[bool] = False
    hasQuiz: Optional[bool] = False
    
    # 話者分離
    speakers: List[Speaker] = []
    diarizationStatus: Optional[str] = "none"


class SessionDetailResponse(SessionResponse):
    diarizedSegments: Optional[List[DiarizedSegment]] = None


class SummaryResponse(BaseModel):
    sessionId: str
    summary: str
    bullets: Optional[List[str]] = None
    decisions: Optional[List[str]] = None
    todos: Optional[List[str]] = None


class QuizItem(BaseModel):
    question: str
    choices: Optional[List[str]] = None
    answer: str
    explanation: Optional[str] = None


class QuizResponse(BaseModel):
    sessionId: str
    items: List[QuizItem]


class TranscriptUpdateRequest(BaseModel):
    """iOS からのトランスクリプトアップロード用リクエスト"""
    transcriptText: str


class NotesUpdateRequest(BaseModel):
    """録音中のメモ更新用リクエスト"""
    notes: str


class BatchDeleteRequest(BaseModel):
    """セッション一括削除用リクエスト"""
    ids: List[str]


# ---------- 話者分離 (Diarization) ---------- #

# (Moved to top)


class DiarizationRequest(BaseModel):
    """話者分離リクエスト"""
    force: bool = False  # 既存の分離結果を上書きするか


class DiarizationResponse(BaseModel):
    """話者分離レスポンス"""
    sessionId: str
    status: Literal["pending", "processing", "done", "failed", "none"]
    speakers: Optional[List[Speaker]] = None
    segments: Optional[List[DiarizedSegment]] = None
    speakerStats: Optional[dict] = None  # {"spk_0": {"totalSec": 120.5, "turns": 15}, ...}


class UploadUrlRequest(BaseModel):
    sessionId: str
    mode: Literal["lecture", "meeting"] = "lecture"
    contentType: str

class UploadUrlResponse(BaseModel):
    uploadUrl: str
    method: str
    headers: dict


class StartTranscribeRequest(BaseModel):
    mode: Literal["lecture", "meeting"]

class StartTranscribeResponse(BaseModel):
    status: str
    sessionId: str

class TranscriptRefreshResponse(BaseModel):
    status: Literal["pending", "running", "completed", "failed"]
    transcriptText: Optional[str] = None
    speakers: Optional[List[Speaker]] = None
    segments: Optional[List[DiarizedSegment]] = None

class QaRequest(BaseModel):
    question: str

class QaCitation(BaseModel):
    startSec: float
    endSec: float
    text: str

class QaResponse(BaseModel):
    answer: str
    citations: List[QaCitation]


