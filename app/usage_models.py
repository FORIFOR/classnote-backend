"""
Usage Tracking Models for ClassnoteX
"""
from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime


# ---------- Event Logging ---------- #

class UsageEventPayload(BaseModel):
    """Flexible payload for usage events"""
    llm_model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    error_code: Optional[str] = None
    recording_sec: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None


class UsageEvent(BaseModel):
    """Raw usage event log entry"""
    id: Optional[str] = None
    user_id: str
    session_id: Optional[str] = None
    feature: Literal[
        "recording", "summary", "quiz", "highlights", 
        "playlist", "diarization", "qa", "share", "export"
    ]
    event_type: Literal["invoke", "success", "error", "cancel"]
    timestamp: datetime
    duration_ms: Optional[int] = None
    payload: Optional[UsageEventPayload] = None


# ---------- Daily Aggregates ---------- #

class UserDailyUsage(BaseModel):
    """Pre-aggregated daily usage per user"""
    user_id: str
    date: str  # yyyy-MM-dd
    
    # Sessions & Recording
    session_count: int = 0
    total_recording_sec: float = 0.0
    
    # Recording Time Breakdown (NEW)
    total_recording_cloud_sec: float = 0.0
    total_recording_ondevice_sec: float = 0.0
    
    # Summary
    summary_invocations: int = 0
    summary_success: int = 0
    summary_error: int = 0
    
    # Quiz
    quiz_invocations: int = 0
    quiz_success: int = 0
    quiz_error: int = 0
    
    # Diarization
    diarization_invocations: int = 0
    diarization_success: int = 0
    
    # Q&A
    qa_invocations: int = 0
    qa_success: int = 0
    
    # LLM Tokens
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    
    # Share/Export
    share_count: int = 0
    export_count: int = 0

class UserMonthlyUsage(BaseModel):
    """Aggregate monthly usage for billing limits (Triple Lock)"""
    user_id: str
    month: str # yyyy-MM
    updated_at: datetime
    
    # Cost Control Limits
    cloud_stt_sec: float = 0.0      # Limit: 100h (360,000s)
    llm_calls: int = 0              # Limit: 1000 calls
    translate_chars: int = 0        # Future limit
    
    # Additional Stats (Optional)
    session_count: int = 0



# ---------- API Response Models ---------- #

class UsageSummaryResponse(BaseModel):
    """Response for /me/usage-summary"""
    userId: str = Field(..., alias="user_id")
    fromDate: str = Field(..., alias="from_date")
    toDate: str = Field(..., alias="to_date")
    
    totalRecordingSec: float = Field(0.0, alias="total_recording_sec")
    sessionCount: int = Field(0, alias="session_count")
    
    # Recording Time Breakdown (NEW)
    # totalRecordingSec is the aggregate (legacy + new)
    totalRecordingCloudSec: float = Field(0.0, alias="total_recording_cloud_sec")
    totalRecordingOnDeviceSec: float = Field(0.0, alias="total_recording_ondevice_sec")
    
    summaryInvocations: int = Field(0, alias="summary_invocations")
    summarySuccess: int = Field(0, alias="summary_success")
    
    quizInvocations: int = Field(0, alias="quiz_invocations")
    quizSuccess: int = Field(0, alias="quiz_success")
    
    diarizationInvocations: int = Field(0, alias="diarization_invocations")
    qaInvocations: int = Field(0, alias="qa_invocations")
    
    llmInputTokens: int = Field(0, alias="llm_input_tokens")
    llmOutputTokens: int = Field(0, alias="llm_output_tokens")
    
    shareCount: int = Field(0, alias="share_count")
    exportCount: int = Field(0, alias="export_count")
    
    # Enhanced fields (already camelCase)
    passRate: Optional[float] = None
    topTags: Optional[list] = None
    timelineDaily: Optional[list] = Field(None, alias="timeline_daily")
    byMode: Optional[dict] = Field(None, alias="by_mode")
    
    class Config:
        populate_by_name = True  # Allow both snake_case input and camelCase output

class UsageTimelineItem(BaseModel):
    date: str
    recordingSec: float
    sessionCount: int

class UsageTimelineResponse(BaseModel):
    timeline: list[UsageTimelineItem]
