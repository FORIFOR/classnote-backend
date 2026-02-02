from enum import Enum

from pydantic import BaseModel, Field, computed_field
from typing import Optional, List, Any, Dict, Literal
from datetime import datetime

# --- Enums ---

class AudioStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"
    DELETED = "deleted"  # [FIX] Added for soft-delete support
    UNKNOWN = "unknown"


class AssetStatus(str, Enum):
    READY = "ready"
    PROCESSING = "processing"
    PENDING = "pending"  # [RENAME] Changed from not_started for iOS compatibility
    MISSING = "missing"  # Asset is missing/unavailable
    ERROR = "error"
    LOCKED = "locked"    # [NEW] Blocked by quota or paywall

class AssetItem(BaseModel):
    status: AssetStatus
    version: int = 1
    updatedAt: Optional[datetime] = None
    contentType: Optional[str] = None
    sizeBytes: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[str] = None
    lockedReason: Optional[Literal["paywall", "quota", "ownerOnly"]] = None # [NEW] Plan control

class AssetManifest(BaseModel):
    # Core assets
    audio: Optional[AssetItem] = None
    transcript: Optional[AssetItem] = None
    summary: Optional[AssetItem] = None
    quiz: Optional[AssetItem] = None
    playlist: Optional[AssetItem] = None
    # Flexible Map
    images: Dict[str, AssetItem] = {}

AssetResolveType = Literal["audio", "summary", "quiz", "transcript"]

class AssetResolveRequest(BaseModel):
    types: List[AssetResolveType]

class ResolvedAsset(BaseModel):
    url: str
    headers: Dict[str, str] = {}
    expiresAt: Optional[datetime] = None
    sha256: Optional[str] = None
    version: int = 1
    contentType: Optional[str] = None
    format: Optional[str] = None # "json", "markdown", "vtt", etc.

class AssetResolveResponse(BaseModel):
    assets: Dict[str, ResolvedAsset] # key=type (or image key)

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    LOCKED = "locked"    # [NEW] Job blocked by quota/billing
    # [NEW] Async job statuses
    QUEUED = "queued"
    SUCCEEDED = "succeeded"


class AsyncJobType(str, Enum):
    """Types of async jobs that can be queued."""
    SUMMARY = "summary"
    QUIZ = "quiz"
    TRANSCRIPT = "transcript"
    PLAYLIST = "playlist"


class AsyncJobStatus(str, Enum):
    """Status for async jobs (Cloud Tasks based)."""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GenerateRequest(BaseModel):
    """Request body for :generate endpoints."""
    promptVersion: Optional[str] = None
    mode: Optional[str] = None  # standard, short, detailed
    language: Optional[str] = None
    force: bool = False  # Force regeneration even if exists


class GenerateResponse(BaseModel):
    """Response for :generate endpoints (202 Accepted)."""
    jobId: str
    status: AsyncJobStatus
    statusUrl: str
    estimatedSeconds: Optional[int] = None
    existingResult: bool = False  # True if job was already completed


class JobStatusResponse(BaseModel):
    """Response for GET /jobs/{jobId}."""
    jobId: str
    type: AsyncJobType
    sessionId: str
    status: AsyncJobStatus
    createdAt: datetime
    updatedAt: datetime
    completedAt: Optional[datetime] = None
    resultUrl: Optional[str] = None
    errorReason: Optional[str] = None
    progress: Optional[float] = None  # 0.0 - 1.0


class TranscriptionMode(str, Enum):
    CLOUD_GOOGLE = "cloud_google"
    DEVICE_SHERPA = "device_sherpa"
    DEVICE_APPLE = "device_apple"
    DUAL_CLOUD_AND_DEVICE = "dual_cloud_and_device"
    IMPORT = "import"  # [NEW]

class CreateSessionRequest(BaseModel):
    title: str
    mode: str = "lecture"
    userId: Optional[str] = None  # Optional: Server uses Auth Token UID by default
    tags: Optional[List[str]] = None  # Max 4 tags
    status: Optional[str] = None  # 予定/未録音/録音中/録音済み/要約済み/テスト生成/テスト完了
    startAt: Optional[datetime] = None
    endAt: Optional[datetime] = None
    syncToGoogleCalendar: Optional[bool] = False
    visibility: str = "private"  # private, shared, org
    transcriptionMode: Optional[TranscriptionMode] = None
    purpose: Optional[str] = None
    importType: Optional[str] = None # [NEW] "transcript" | "audio"
    # [OFFLINE SYNC]
    clientSessionId: Optional[str] = None
    deviceId: Optional[str] = None
    createdAt: Optional[datetime] = None
    source: str = "ios" # [NEW] Tracking origin

class UpdateSessionRequest(BaseModel):
    """PATCH 部分更新。指定したフィールドだけ更新。"""
    title: Optional[str] = None
    tags: Optional[List[str]] = None  # Max 4 tags, empty list = clear all
    status: Optional[str] = None  # 予定/未録音/録音中/録音済み/要約済み/テスト生成/テスト完了
    visibility: Optional[str] = None

    # [NEW] Allow updating transcript (e.g. from local sync)
    transcriptText: Optional[str] = None
    transcriptDraft: Optional[str] = None
    transcriptSource: Optional[str] = None



class TranscriptUploadRequest(BaseModel):
    text: str
    source: str # device_sherpa, device_apple, etc.
    modelInfo: Optional[Dict[str, Any]] = None
    processingTimeSec: Optional[float] = None
    isFinal: bool = True

class ImportYouTubeRequest(BaseModel):
    url: str
    mode: Literal["lecture", "meeting"] = "lecture"
    title: Optional[str] = None
    language: Optional[str] = "ja"
    transcriptText: Optional[str] = None
    transcriptLang: Optional[str] = None
    isAutoGenerated: bool = False
    source: Optional[str] = None  # [FIX] Added missing field causing 500
    languages: Optional[List[str]] = None # [FIX] Allow client to pass list


class ImportYouTubeResponse(BaseModel):
    sessionId: str
    transcriptStatus: str = "ready"
    summaryStatus: str = "pending"
    quizStatus: str = "pending"
    sourceUrl: Optional[str] = None

class YouTubeTrack(BaseModel):
    language: str
    language_code: str
    is_generated: bool
    is_translatable: bool

class YouTubeCheckRequest(BaseModel):
    url: str # Use str for flexibility, validator handles parsing

class YouTubeCheckResponse(BaseModel):
    videoId: str
    available: bool
    tracks: Optional[List[YouTubeTrack]] = None
    reason: Optional[str] = None

class RetryTranscriptionRequest(BaseModel):
    mode: str = "cloud_google" # Currently only cloud_google supported


# Reaction Models
ReactionEmoji = Literal["🔥", "👏", "😇", "🤯", "🫶"]

class SetReactionRequest(BaseModel):
    emoji: Optional[ReactionEmoji] = None

class ReactionStateResponse(BaseModel):
    myEmoji: Optional[str] = None
    counts: Dict[str, int] = {}
    users: Optional[Dict[str, str]] = None  # [NEW] uid -> emoji mapping

class ChatCreateRequest(BaseModel):
    text: str

class SessionChatMessage(BaseModel):
    id: str
    sessionId: str
    userId: str
    userName: Optional[str] = None
    userPhotoUrl: Optional[str] = None
    text: str
    createdAt: datetime

class ChatMessagesResponse(BaseModel):
    messages: List[SessionChatMessage]
    
# Job Models
JobType = Literal["summary", "quiz", "calendar_sync", "transcribe", "diarize", "translate", "qa", "playlist"]

class JobRequest(BaseModel):
    type: JobType
    params: Dict[str, Any] = {}
    idempotencyKey: Optional[str] = None
    force: bool = False  # [FIX] Force re-generation even if completed

    model_config = {"populate_by_name": True}

class JobResponse(BaseModel):
    jobId: str
    type: JobType
    status: str # JobStatus
    createdAt: datetime
    errorReason: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    progress: Optional[float] = 0.0
    pollUrl: Optional[str] = None  # Full path for polling, e.g. /sessions/{sid}/jobs/{jobId}
    transcriptText: Optional[str] = None  # [NEW] For direct access if job type is transcribe


class DiarizedSegment(BaseModel):
    """話者分離セグメント（iOS オンデバイス STT から送信）"""
    startSec: float
    endSec: float
    speakerId: Optional[str] = None
    text: str

class TranscriptUpdateRequest(BaseModel):
    """文字起こしアップロード（iOS から transcript + segments を送信）"""
    transcriptText: str
    segments: Optional[List[DiarizedSegment]] = None
    source: Optional[str] = "device"  # "device" | "cloud"
    transcriptSha256: Optional[str] = None # [OFFLINE SYNC]
    isFinal: bool = False # [NEW] explicit final commit from client

class TranscriptChunkInput(BaseModel):
    id: Optional[str] = None
    startMs: Optional[float] = None
    endMs: Optional[float] = None
    speakerId: Optional[str] = None
    text: str
    kind: Optional[str] = "final"  # partial/final/batchFix
    version: Optional[int] = None

class TranscriptChunkAppendRequest(BaseModel):
    chunks: List[TranscriptChunkInput]
    source: Optional[str] = "device"
    updateSessionTranscript: bool = False
    finalize: bool = False
    ifVersion: Optional[int] = None  # [SYNC] Optimistic locking: reject if server version != ifVersion

class TranscriptChunkReplaceRequest(BaseModel):
    chunks: List[TranscriptChunkInput]
    source: Optional[str] = "batch"
    updateSessionTranscript: bool = False
    ifVersion: Optional[int] = None  # [SYNC] Optimistic locking: reject if server version != ifVersion
    replaceRange: Optional[Dict[str, int]] = None  # [SYNC] {fromMs, toMs} - delete chunks in range before insert

class TranscriptChunkAppendResponse(BaseModel):
    sessionId: str
    chunkIds: List[str]
    count: int
    status: JobStatus = JobStatus.COMPLETED
    transcriptVersion: int = 0  # [SYNC] New version after this operation

class VideoUrlUpdateRequest(BaseModel):
    videoUrl: str

class NotesUpdateRequest(BaseModel):
    notes: str

class BatchDeleteRequest(BaseModel):
    ids: List[str]

class DiarizationRequest(BaseModel):
    force: bool = False

class QaEnqueueResponse(BaseModel):
    qaId: str
    sessionId: str
    status: JobStatus = JobStatus.PENDING
    message: str = "QA processing started"

class QaStatusResponse(BaseModel):
    qaId: str
    sessionId: str
    status: JobStatus
    question: Optional[str] = None
    answer: Optional[str] = None
    citations: Optional[List[Any]] = None
    error: Optional[str] = None
    updatedAt: Optional[datetime] = None

class TranslateEnqueueResponse(BaseModel):
    sessionId: str
    status: JobStatus = JobStatus.PENDING
    language: str
    message: str = "Translation started"

class TranslateStatusResponse(BaseModel):
    sessionId: str
    status: JobStatus
    language: Optional[str] = None
    translatedText: Optional[str] = None
    error: Optional[str] = None
    updatedAt: Optional[datetime] = None

class StartTranscribeRequest(BaseModel):
    pass

class StartSTTGlobalRequest(BaseModel):
    sessionId: str


class StartTranscribeResponse(BaseModel):
    status: JobStatus
    sessionId: str

class UploadUrlRequest(BaseModel):
    sessionId: str
    contentType: str
    # Metadata for GCS object (Optional, for enforcing x-goog-meta headers if client supports it)
    duration: Optional[float] = None
    sampleRate: Optional[int] = None
    bitrate: Optional[int] = None
    codec: Optional[str] = None
    appVersion: Optional[str] = None

class UploadUrlResponse(BaseModel):
    uploadUrl: str
    method: str
    headers: dict
    storagePath: Optional[str] = None

class AudioMeta(BaseModel):
    variant: str = "compressed" # "original" or "compressed"
    codec: str # Required: e.g. "opus", "aac"
    container: str # Required: e.g. "ogg", "m4a"
    sampleRate: int # Required
    channels: int # Required
    sizeBytes: int # Required
    payloadSha256: str # Required for integrity
    bitrate: Optional[int] = None
    durationSec: Optional[float] = None
    originalSha256: Optional[str] = None # Optional: Checksum of original audio

class AudioPrepareRequest(BaseModel):
    contentType: str
    durationSec: Optional[float] = None
    fileSize: Optional[int] = None # [Security] Check upload size
    sampleRate: Optional[int] = None
    bitrate: Optional[int] = None
    codec: Optional[str] = None
    appVersion: Optional[str] = None

class AudioPrepareResponse(BaseModel):
    uploadUrl: str
    method: str
    headers: dict
    storagePath: Optional[str] = None
    deleteAfterAt: Optional[Any] = None

class AudioCommitRequest(BaseModel):
    storagePath: Optional[str] = None
    sizeBytes: Optional[int] = None
    contentType: Optional[str] = None
    durationSec: Optional[float] = None
    metadata: Optional[AudioMeta] = None
    expectedSizeBytes: int # Required for strict validation
    expectedPayloadSha256: str # Required for strict validation

class AudioCommitResponse(BaseModel):
    status: AudioStatus
    deleteAfterAt: Optional[datetime] = None

class QaRequest(BaseModel):
    question: str

class QaCitation(BaseModel):
    excerpt: Optional[str] = None
    reason: Optional[str] = None

class QaResponse(BaseModel):
    answer: str
    citations: Optional[List[QaCitation]] = None

# --- Sharing Models ---

class ShareToUserRequest(BaseModel):
    email: str

class ShareResponse(BaseModel):
    sessionId: str
    sharedUserIds: List[str]

class ShareLinkResponse(BaseModel):
    url: str

class PublicUser(BaseModel):
    uid: str
    displayName: Optional[str] = None
    username: Optional[str] = None  # [NEW]
    email: Optional[str] = None
    photoUrl: Optional[str] = None
    providers: Optional[List[str]] = None
    allowSearch: Optional[bool] = None

class ShareSessionRequest(BaseModel):
    userIds: List[str]
    revoke: bool = False

class SharedSessionDTO(BaseModel):
    sessionId: str
    title: str
    transcriptText: Optional[str] = None
    summaryMarkdown: Optional[str] = None
    ownerDisplayName: Optional[str] = None
    createdAt: Optional[datetime] = None

class ShareByCodeRequest(BaseModel):
    targetShareCode: str

# --- Session Members ---

class SharedUserSummary(BaseModel):
    uid: str
    username: Optional[str] = None
    displayName: Optional[str] = None
    photoUrl: Optional[str] = None
    isShareable: bool = True

class SessionMemberResponse(BaseModel):
    sessionId: str
    userId: str
    role: str
    joinedAt: Optional[datetime] = None
    source: Optional[str] = None
    displayNameSnapshot: Optional[str] = None
    # [NEW] Live Profile Fields
    username: Optional[str] = None
    displayName: Optional[str] = None
    photoUrl: Optional[str] = None

class SessionMemberUpdateRequest(BaseModel):
    role: str

class SessionMemberInviteRequest(BaseModel):
    userId: Optional[str] = None
    email: Optional[str] = None
    role: str = "viewer"

# --- Me / Profile Models ---

class CloudUsageReport(BaseModel):
    """
    [vNext] Monthly Cloud Transcription Quota Report
    Used to inform the UI about remaining minutes and sessions.

    Supports both camelCase (native) and snake_case (iOS CodingKeys fallback) keys.
    """
    limitSeconds: float
    usedSeconds: float
    remainingSeconds: float
    sessionLimit: int
    sessionsStarted: int
    canStart: bool
    reasonIfBlocked: Optional[str] = None

    # [FIX] iOS互換性: snake_case キーも出力（CodingKeys両対応）
    @computed_field
    @property
    def limit_seconds(self) -> float:
        return self.limitSeconds

    @computed_field
    @property
    def used_seconds(self) -> float:
        return self.usedSeconds

    @computed_field
    @property
    def remaining_seconds(self) -> float:
        return self.remainingSeconds

    @computed_field
    @property
    def session_limit(self) -> int:
        return self.sessionLimit

    @computed_field
    @property
    def sessions_started(self) -> int:
        return self.sessionsStarted

    @computed_field
    @property
    def can_start(self) -> bool:
        return self.canStart

    @computed_field
    @property
    def reason_if_blocked(self) -> Optional[str]:
        return self.reasonIfBlocked

class MeResponse(BaseModel):
    id: Optional[str] = None  # iOS expects this field (alias for uid)
    uid: str
    displayName: Optional[str] = None
    username: Optional[str] = None  # [NEW]
    hasUsername: bool = False       # [NEW]
    email: Optional[str] = None
    photoUrl: Optional[str] = None
    providers: List[str] = []
    provider: Optional[str] = None
    allowSearch: bool = True
    shareCode: Optional[str] = None
    isShareable: bool = True
    plan: str = "free"
    createdAt: Optional[datetime] = None
    
    # [Security] Tracking
    securityState: str = "normal"
    riskScore: int = 0
    
    # [NEW] Free Plan Credits (DEPRECATED - use session counts below)
    freeCloudCreditsRemaining: Optional[int] = None
    freeSummaryCreditsRemaining: Optional[int] = None
    freeQuizCreditsRemaining: Optional[int] = None
    activeSessionCount: Optional[int] = None  # Compat alias for serverSessionCount
    
    # [NEW 2026-01] Session Limits for Free Plan
    serverSessionCount: Optional[int] = None  # Current count of server-saved sessions
    serverSessionLimit: Optional[int] = None  # Max allowed (5 for free)
    cloudSessionCount: Optional[int] = None   # Current count of cloud-entitled sessions
    cloudSessionLimit: Optional[int] = None   # Max allowed (3 for free)
    
    # [vNext] Consolidated Cloud Usage Report
    cloud: Optional[CloudUsageReport] = None

    # [FIX] iOS互換性: cloudMinutesUsed/cloudMinutesLimit フィールド
    cloudMinutesUsed: Optional[float] = None
    cloudMinutesLimit: Optional[float] = None

    # [Security] App Store Receipt Validation
    appAccountToken: Optional[str] = None  # [NEW] UUID for StoreKit 2

    # [NEW 2026-01] Account Unification
    needsPhoneVerification: Optional[bool] = None  # Hard gate (login blocked) - now always False for SNS users
    needsSnsLogin: bool = False # [NEW]
    accountId: Optional[str] = None
    phoneE164: Optional[str] = None
    credits: Optional[Dict[str, Any]] = None
    accountResolution: Optional[Dict[str, Any]] = None # [NEW] {action: attached|created|none}

    # [NEW 2026-01] Feature-level phone gate (soft gate)
    # List of features that require phone verification to use
    # Possible values: "share", "publicProfile", "subscriptionRestore", "accountMerge"
    phoneRequiredFor: Optional[List[str]] = None

    # [NEW 2026-01] Account Suspension (BAN)
    suspended: bool = False
    suspendedAt: Optional[datetime] = None
    suspendedReason: Optional[str] = None


class FeatureGates(BaseModel):
    """Feature availability flags for the current user/plan."""
    cloudStt: bool = True
    summarization: bool = True
    quiz: bool = True
    cloudSync: bool = True
    export: bool = True
    share: bool = True


class MeLiteResponse(BaseModel):
    """
    Lightweight /users/me response for app startup.

    Design principles:
    - No JIT writes (read-only)
    - No usage calculation (just plan-based gates)
    - Minimal Firestore reads (users + accounts only)
    - Target response time: <100ms
    """
    uid: str
    accountId: Optional[str] = None
    plan: str = "free"
    displayName: Optional[str] = None
    username: Optional[str] = None
    hasUsername: bool = False
    photoUrl: Optional[str] = None
    provider: Optional[str] = None
    providers: List[str] = []

    # Feature gates (based on plan, no usage check)
    featureGates: FeatureGates = Field(default_factory=FeatureGates)

    # Minimal flags for UI
    needsPhoneVerification: bool = False
    needsSnsLogin: bool = False
    suspended: bool = False

    # Cache hint for client
    cacheValidUntil: Optional[datetime] = None


class MeUpdateRequest(BaseModel):
    displayName: Optional[str] = None
    email: Optional[str] = None
    allowSearch: Optional[bool] = None
    isShareable: Optional[bool] = None

class UserProfileResponse(BaseModel):
    uid: str
    displayName: Optional[str] = None
    shareCode: Optional[str] = None
    isShareable: bool = True

class UserProfileUpdateRequest(BaseModel):
    displayName: Optional[str] = None
    isShareable: Optional[bool] = None

class ShareCodeResponse(BaseModel):
    shareCode: str

class ShareLookupRequest(BaseModel):
    code: str

class ClaimUsernameRequest(BaseModel):
    username: str = Field(..., description="3-20 chars: a-z0-9_")

class ShareLookupResponse(BaseModel):
    found: bool
    targetUserId: Optional[str] = None
    displayName: Optional[str] = None
    username: Optional[str] = None  # [NEW]
    photoUrl: Optional[str] = None  # [NEW] for UI

class ShareCodeLookupResponse(BaseModel):
    userId: str
    displayName: Optional[str] = None
    username: Optional[str] = None  # [NEW]
    email: Optional[str] = None
    email: Optional[str] = None

# --- Consent Log Models --- #
class ConsentRequest(BaseModel):
    termsVersion: str = Field(..., description="Version string of accepted Terms of Service")
    privacyVersion: str = Field(..., description="Version string of accepted Privacy Policy")
    acceptedAt: Optional[datetime] = None  # Client timestamp (server will override)
    appVersion: Optional[str] = None
    build: Optional[str] = None
    platform: Optional[str] = "ios"
    locale: Optional[str] = None

class ConsentResponse(BaseModel):
    ok: bool
    termsVersion: str
    privacyVersion: str
    acceptedAt: datetime

class HighlightType(str, Enum):
    important = "important"
    question = "question"
    todo = "todo"
    other = "other"

class SummaryRequest(BaseModel):
    summary: str

class TagUpdateRequest(BaseModel):
    tags: List[str]

class PlaylistItem(BaseModel):
    id: str
    title: str
    startSec: float
    endSec: float
    summary: Optional[str] = None
    label: Optional[str] = None
    segments: Optional[List[dict]] = None
    speakerId: Optional[str] = None
    snippet: Optional[str] = None
    type: Optional[str] = None
    confidence: Optional[float] = None
    order: Optional[int] = None

class PlaylistRefreshResponse(BaseModel):
    playlist: List[PlaylistItem]

class Highlight(BaseModel):
    id: str
    type: HighlightType
    startSec: float
    endSec: float
    text: str

class HighlightsResponse(BaseModel):
    highlights: List[Highlight]

class TriggerHighlightsRequest(BaseModel):
    force: bool = False

# --- Me / Profile Models ---

class ShareCodeLookupResponse(BaseModel):
    userId: str
    displayName: Optional[str] = None
    username: Optional[str] = None  # [NEW]
    email: Optional[str] = None

# --- Task & Decision Models ---

class TaskResponse(BaseModel):
    id: str
    sessionId: str
    userId: str
    title: str
    assignee: Optional[str] = None
    dueDate: Optional[str] = None
    status: str = "open"
    createdAt: Optional[datetime] = None
    source: Optional[str] = "ai"

class DecisionResponse(BaseModel):
    id: str
    sessionId: str
    content: str
    createdAt: Optional[datetime] = None

# --- Session Models ---

class SessionResponse(BaseModel):
    id: str
    clientSessionId: Optional[str] = None # [OFFLINE SYNC]
    source: Optional[str] = None # [NEW]
    title: str
    mode: str
    userId: str
    status: str
    createdAt: datetime
    tags: Optional[List[str]] = None  # User-defined tags (max 4)
    # Sharing fields
    isOwner: Optional[bool] = None
    sharedWithCount: Optional[int] = None
    sharedWithCount: Optional[int] = None
    sharedUserIds: Optional[List[str]] = []
    reactionCounts: Optional[Dict[str, int]] = {} # [NEW]
    
    # [NEW] Source of Truth fields
    canManage: Optional[bool] = None # [NEW] Explicit permission flag
    ownerUserId: Optional[str] = None
    ownerId: Optional[str] = None # [NEW] Legacy alias for backward compatibility
    ownerAccountId: Optional[str] = None # [NEW] Account-based ownership
    participantUserIds: List[str] = []
    participants: Optional[dict] = None  # [NEW] Map of uid -> role/joinedAt
    visibility: str = "private"
    autoTags: List[str] = []
    topicSummary: Optional[str] = None
    
    # [NEW] Job Statuses per feature
    summaryStatus: Optional[str] = "pending"
    quizStatus: Optional[str] = "pending"
    diarizationStatus: Optional[str] = "pending"
    highlightsStatus: Optional[str] = "pending"
    
    # [NEW] User specific meta fields
    isArchived: bool = False
    lastOpenedAt: Optional[datetime] = None
    
    # [NEW] Moved from Detail for Insights Calculation in Lists
    startedAt: Optional[datetime] = None
    endedAt: Optional[datetime] = None
    durationSec: Optional[float] = None
    hasTranscript: bool = False # Helper for client efficiency
    
    # [Security] Cloud Ticket System
    cloudTicket: Optional[str] = None
    cloudAllowedUntil: Optional[datetime] = None
    cloudStatus: Optional[str] = None # "none"|"allowed"|"limited"|"blocked"

class SessionMetaUpdateRequest(BaseModel):
    isPinned: Optional[bool] = None
    isArchived: Optional[bool] = None
    lastOpenedAt: Optional[datetime] = None

# --- Image Note Models ---

class ImagePrepareRequest(BaseModel):
    contentType: str = "image/jpeg"
    localId: Optional[str] = None # [NEW] For client-side tracking


class ImagePrepareResponse(BaseModel):
    imageId: str
    uploadUrl: str
    storagePath: str
    method: str = "PUT"
    headers: Dict[str, str] = {}

class ImageCommitRequest(BaseModel):
    imageId: str

class CloudSTTStartResponse(BaseModel):
    allowed: bool
    remainingSeconds: float
    lockedUntil: Optional[str] = None
    ticket: Optional[str] = None

class ImageNoteDTO(BaseModel):
    id: str
    url: str
    status: str = "ready"
    createdAt: Optional[datetime] = None
    localId: Optional[str] = None # [NEW]


class SessionMemberSummary(BaseModel):
    """[NEW] Lightweight member info for session detail response"""
    uid: str
    username: Optional[str] = None
    displayName: Optional[str] = None
    displayNameSnapshot: Optional[str] = None
    role: str = "viewer"
    photoUrl: Optional[str] = None

class ReactionsSummary(BaseModel):
    """[NEW] Reaction counts with English keys for iOS compatibility"""
    fire: int = 0
    clap: int = 0
    angel: int = 0
    mindblown: int = 0
    heartHands: int = 0

class SessionDetailResponse(SessionResponse):
    transcriptText: Optional[str] = None
    transcriptChunkCount: int = 0  # [NEW] For sync status / chunked loading
    notes: Optional[str] = None
    assets: Optional[AssetResolveResponse] = None # For full asset paths
    googleCalendar: Optional[dict] = None # Legacy
    reactionIncr: Optional[int] = 0 # For UI optimization

    # [NEW] Members list for iOS compatibility
    members: Optional[List[SessionMemberSummary]] = None
    # [NEW] Reaction summary with English keys
    reactionsSummary: Optional[ReactionsSummary] = None

    # Also include raw segments if needed by UI (optional)
    segments: Optional[List[dict]] = None
    diarizedSegments: Optional[List[dict]] = None
    
    # Audio availability
    audioStatus: Optional[AudioStatus] = AudioStatus.UNKNOWN
    audioMeta: Optional[AudioMeta] = None  # [NEW]
    
    # AI Results
    summaryStatus: Optional[JobStatus] = JobStatus.PENDING
    summaryError: Optional[str] = None
    summaryMarkdown: Optional[str] = None
    summaryJson: Optional[dict] = None
    summaryJsonVersion: Optional[int] = None
    summaryType: Optional[str] = None
    tags: List[str] = []
    imageNotes: List[ImageNoteDTO] = [] # [NEW]

    
    # [NEW] Batch Retranscribe State
    transcriptState: str = "partial" # "partial" | "final"
    transcriptTextLen: int = 0
    batchRetranscribeState: str = "idle" # "idle"|"running"|"completed"|"failed"
    batchRetranscribeUsed: bool = False
    
    quizStatus: Optional[JobStatus] = JobStatus.PENDING
    quizError: Optional[str] = None
    quizMarkdown: Optional[str] = None
    quizJson: Optional[str] = None  # JSON string of quiz data
    
    playlistStatus: Optional[JobStatus] = None # Optional, as older sessions might not have it
    playlist: Optional[List[PlaylistItem]] = None
    
    audioPath: Optional[str] = None
    speakers: Optional[List[dict]] = None
    diarizedSegments: Optional[List[dict]] = None
    
    # Flags
    hasSummary: bool = False
    hasQuiz: bool = False


# --- Auth Models ---

class LineAuthRequest(BaseModel):
    idToken: str
    nonce: Optional[str] = None

class LineAuthResponse(BaseModel):
    firebaseCustomToken: str




# --- Audio & Highlights Models ---

class SignedCompressedAudioResponse(BaseModel):
    audioUrl: str
    expiresAt: datetime
    compressionMetadata: Optional[AudioMeta] = None



class HighlightsResponse(BaseModel):
    status: JobStatus
    highlights: Optional[List[Highlight]] = None
    tags: Optional[List[str]] = None

class TriggerHighlightsRequest(BaseModel):
    mode: str = "fast" # "fast" or "full"
    source: str = "client"

class TagUpdateRequest(BaseModel):
    tags: List[str]

# --- Derived Generation Models ---

class DerivedEnqueueRequest(BaseModel):
    idempotencyKey: Optional[str] = None

class DerivedEnqueueResponse(BaseModel):
    status: JobStatus
    alreadyQueued: bool = False
    idempotencyKey: Optional[str] = None

class DerivedStatusResponse(BaseModel):
    status: str # ready, running, pending, error
    result: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None
    updatedAt: Optional[datetime] = None
    errorReason: Optional[str] = None
    modelInfo: Optional[Dict[str, Any]] = None
    idempotencyKey: Optional[str] = None
    jobId: Optional[str] = None # [NEW] Ensure client can track job

class PlaylistArtifactResponse(BaseModel):
    status: JobStatus
    jobId: Optional[str] = None
    updatedAt: Optional[datetime] = None
    items: Optional[List[PlaylistItem]] = None
    errorReason: Optional[str] = None
    modelInfo: Optional[dict] = None
    idempotencyKey: Optional[str] = None
    version: Optional[int] = None

# --- Playlist / Device Sync Models ---

class DeviceSyncRequest(BaseModel):
    transcriptText: Optional[str] = None
    segments: Optional[List[DiarizedSegment]] = None
    notes: Optional[str] = None
    durationSec: Optional[float] = None
    audioPath: Optional[str] = None
    audioMeta: Optional[AudioMeta] = None  # [NEW]
    needsPlaylist: bool = True
    # [OFFLINE-FIRST] Session creation fields - used when session doesn't exist yet
    createIfMissing: bool = True  # If True, create session if not found (upsert behavior)
    title: Optional[str] = None  # Required if createIfMissing and session doesn't exist
    mode: Optional[str] = "lecture"  # lecture / meeting
    transcriptionMode: Optional[TranscriptionMode] = TranscriptionMode.DEVICE_SHERPA
    deviceId: Optional[str] = None
    clientCreatedAt: Optional[datetime] = None  # Original creation timestamp from device
    source: Optional[str] = "ios"


class DeviceSyncResponse(BaseModel):
    """[OFFLINE-FIRST] Response from /device_sync endpoint"""
    status: str = "accepted"
    sessionCreated: bool = False  # True if session was created during this sync
    sessionId: str


class CapabilitiesResponse(BaseModel):
    id: str = "capabilities"  # iOS expects this field
    plan: str # "free", "pro", "basic"
    canRealtimeTranslate: bool
    sttPostEngine: str # "whisper_large_v3", "gcp_speech", "none"
    monthlyRecordingLimitMin: int
    remainingRecordingMin: int
    canRegenerateTranscript: bool
    maxSessions: Optional[int] = None # None means unlimited
    maxSummaries: Optional[int] = None
    maxQuizzes: Optional[int] = None

class SubscriptionVerifyRequest(BaseModel):
    isSubscribed: bool
    originalTransactionId: Optional[str] = None
    productId: Optional[str] = None
    purchaseDate: Optional[datetime] = None
    expirationDate: Optional[datetime] = None
    environment: Optional[str] = None # "Sandbox", "Production"
    maxImageNotes: int = 3
    transactionId: Optional[str] = None
    signedTransactionInfo: Optional[str] = None
    receipt_data: Optional[str] = None

class BillingConfirmRequest(BaseModel):
    signedTransaction: str

class AppStoreNotificationRequest(BaseModel):
    signedPayload: str

class BillingConfirmResponse(BaseModel):
    ok: bool
    plan: str
    status: str
    entitled: bool
    expiresAt: Optional[int] = None # Timestamp (ms)
    originalTransactionId: Optional[str] = None
    transactionId: Optional[str] = None
    productId: Optional[str] = None
    requestId: str

class EntitlementResponse(BaseModel):
    entitled: bool
    plan: str # free, basic, pro
    expiresAt: Optional[int] = None # Timestamp in ms


class SubscriptionClaimSubscriptionInfo(BaseModel):
    """Nested subscription info for SubscriptionClaimResponse"""
    plan: Optional[str] = None
    productId: Optional[str] = None
    originalTransactionId: Optional[str] = None
    expiresAt: Optional[int] = None  # Timestamp in ms
    environment: Optional[str] = None
    source: str = "apple"


class SubscriptionClaimResponse(BaseModel):
    """
    Response for /subscription/apple:claim endpoint.
    Matches iOS SubscriptionSyncResponse structure.
    """
    status: str  # "verified", "pending", "failed"
    accountId: Optional[str] = None
    subscription: Optional[SubscriptionClaimSubscriptionInfo] = None
    retryAfter: Optional[int] = None  # Seconds to wait before retry
    message: Optional[str] = None
    transactionId: Optional[str] = None


# --- Quiz Analytics Models ---

class QuizAttemptCreate(BaseModel):
    quizVersion: int = 1
    total: int
    answered: int
    correct: int
    durationSec: float
    completed: bool
    answers: Optional[Dict[str, str]] = None

class QuizAttempt(QuizAttemptCreate):
    id: str
    sessionId: str
    createdAt: datetime
    userId: str

class QuizAnalytics(BaseModel):
    attempts: int
    completedAttempts: int
    completionRate: float
    avgAccuracy: float
    avgAnswered: float
    sessionsWithQuiz: int = 0
    sessionsTested: int = 0

class SessionQuizStat(BaseModel):
    sessionId: str
    title: str
    createdAt: datetime
    attemptsCount: int
    bestAccuracy: Optional[float] = None
    lastAccuracy: Optional[float] = None
    completionRate: Optional[float] = None
    lastAttemptAt: Optional[datetime] = None


# --- Entitlement Models (vNext) ---

class IosEntitlement(BaseModel):
    """
    Represents the server-side source of truth for an Apple subscription entitlement.
    Stored in /ios_entitlements/{originalTransactionId}
    """
    ownerUserId: str
    originalTransactionId: str
    productId: str
    environment: str # "Sandbox" or "Production"
    status: str # "active", "expired", "revoked", etc.
    latestExpiresAt: datetime
    appAccountToken: Optional[str] = None
    createdAt: datetime
    updatedAt: datetime
