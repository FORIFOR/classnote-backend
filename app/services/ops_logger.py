"""
ops_logger.py - 運用イベントロガー

ops_events コレクションに構造化されたイベントを書き込む。
管理UIから一元的に監視・検索できるようにする。
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any
from google.cloud import firestore

logger = logging.getLogger("app.ops_logger")


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class EventType(str, Enum):
    # セッション系
    SESSION_CREATE = "SESSION_CREATE"
    SESSION_UPDATE = "SESSION_UPDATE"
    SESSION_DELETE = "SESSION_DELETE"

    # アップロード系
    UPLOAD_SIGNED_URL = "UPLOAD_SIGNED_URL"
    UPLOAD_CHECK = "UPLOAD_CHECK"

    # ジョブ系
    JOB_QUEUED = "JOB_QUEUED"
    JOB_STARTED = "JOB_STARTED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"

    # 外部API系
    STT_STARTED = "STT_STARTED"
    STT_COMPLETED = "STT_COMPLETED"
    STT_FAILED = "STT_FAILED"
    LLM_STARTED = "LLM_STARTED"
    LLM_COMPLETED = "LLM_COMPLETED"
    LLM_FAILED = "LLM_FAILED"

    # API/システム系
    API_ERROR = "API_ERROR"

    # 認証・課金・不正利用系
    AUTH_FAILED = "AUTH_FAILED"
    LIMIT_REACHED = "LIMIT_REACHED"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    ABUSE_DETECTED = "ABUSE_DETECTED"


class ErrorCode(str, Enum):
    # 500系
    SESSION_CREATE_500 = "SESSION_CREATE_500"
    UPLOAD_CHECK_500 = "UPLOAD_CHECK_500"
    JOB_WORKER_500 = "JOB_WORKER_500"

    # 前提不足
    TRANSCRIPT_MISSING = "TRANSCRIPT_MISSING"
    AUDIO_MISSING_IN_GCS = "AUDIO_MISSING_IN_GCS"
    AUDIO_TOO_LONG = "AUDIO_TOO_LONG"

    # 外部API
    STT_OPERATION_FAILED = "STT_OPERATION_FAILED"
    STT_QUOTA_EXCEEDED = "STT_QUOTA_EXCEEDED"
    VERTEX_QUOTA_EXCEEDED = "VERTEX_QUOTA_EXCEEDED"
    VERTEX_SCHEMA_PARSE_ERROR = "VERTEX_SCHEMA_PARSE_ERROR"

    # 権限・課金
    AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
    AUTH_EXPIRED_TOKEN = "AUTH_EXPIRED_TOKEN"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    FREE_LIMIT_REACHED = "FREE_LIMIT_REACHED"
    PRO_LIMIT_REACHED = "PRO_LIMIT_REACHED"

    # 疑わしい挙動
    RETRY_STORM = "RETRY_STORM"
    UPLOAD_SPAM = "UPLOAD_SPAM"


class OpsLogger:
    """運用イベントロガー"""

    _instance: Optional["OpsLogger"] = None
    _db: Optional[firestore.Client] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_db(self) -> firestore.Client:
        if self._db is None:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
            self._db = firestore.Client(project=project_id)
        return self._db

    def log(
        self,
        severity: Severity,
        event_type: EventType,
        *,
        uid: Optional[str] = None,
        server_session_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
        job_id: Optional[str] = None,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        status_code: Optional[int] = None,
        error_code: Optional[ErrorCode] = None,
        message: Optional[str] = None,
        debug: Optional[dict[str, Any]] = None,
        props: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        運用イベントを記録する。

        Returns:
            str: 生成されたイベントID
        """
        event_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"

        event_data = {
            "ts": datetime.now(timezone.utc),
            "severity": severity.value if isinstance(severity, Severity) else severity,
            "type": event_type.value if isinstance(event_type, EventType) else event_type,
        }

        # Optional fields (None は保存しない)
        if uid:
            event_data["uid"] = uid
        if server_session_id:
            event_data["serverSessionId"] = server_session_id
        if client_session_id:
            event_data["clientSessionId"] = client_session_id
        if job_id:
            event_data["jobId"] = job_id
        if request_id:
            event_data["requestId"] = request_id
        if trace_id:
            event_data["traceId"] = trace_id
        if endpoint:
            event_data["endpoint"] = endpoint
        if status_code is not None:
            event_data["statusCode"] = status_code
        if error_code:
            event_data["errorCode"] = error_code.value if isinstance(error_code, ErrorCode) else error_code
        if message:
            event_data["message"] = message
        if debug:
            event_data["debug"] = debug
        if props:
            # propsはトップレベルに展開するのではなく、propsフィールドに格納する (Firestoreのインデックス汚染を防ぐため)
            # 仕様書に合わせて props フィールドに格納
            event_data["props"] = props

        try:
            db = self._get_db()
            db.collection("ops_events").document(event_id).set(event_data)
            logger.debug(f"Logged ops event: {event_id} - {event_type}")
        except Exception as e:
            # ops_event の書き込み失敗は本体処理に影響させない
            logger.error(f"Failed to log ops event: {e}")

        return event_id

    def info(
        self,
        event_type: EventType,
        **kwargs,
    ) -> str:
        """INFO レベルのイベントを記録"""
        return self.log(Severity.INFO, event_type, **kwargs)

    def warn(
        self,
        event_type: EventType,
        **kwargs,
    ) -> str:
        """WARN レベルのイベントを記録"""
        return self.log(Severity.WARN, event_type, **kwargs)

    def error(
        self,
        event_type: EventType,
        **kwargs,
    ) -> str:
        """ERROR レベルのイベントを記録"""
        return self.log(Severity.ERROR, event_type, **kwargs)


# シングルトンインスタンス
ops_logger = OpsLogger()


# 便利関数
def log_session_create(
    uid: str,
    session_id: str,
    *,
    request_id: Optional[str] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> str:
    """セッション作成イベントを記録"""
    if success:
        return ops_logger.info(
            EventType.SESSION_CREATE,
            uid=uid,
            server_session_id=session_id,
            request_id=request_id,
            message="Session created successfully",
        )
    else:
        return ops_logger.error(
            EventType.SESSION_CREATE,
            uid=uid,
            server_session_id=session_id,
            request_id=request_id,
            error_code=ErrorCode.SESSION_CREATE_500,
            message=error_message or "Session creation failed",
        )


def log_job_transition(
    session_id: str,
    job_type: str,  # "summarize", "quiz", "transcribe", etc.
    status: str,  # "queued", "started", "completed", "failed"
    *,
    uid: Optional[str] = None,
    job_id: Optional[str] = None,
    request_id: Optional[str] = None,
    error_code: Optional[ErrorCode] = None,
    error_message: Optional[str] = None,
    debug: Optional[dict] = None,
) -> str:
    """ジョブ状態遷移イベントを記録"""
    event_type_map = {
        "queued": EventType.JOB_QUEUED,
        "started": EventType.JOB_STARTED,
        "completed": EventType.JOB_COMPLETED,
        "failed": EventType.JOB_FAILED,
    }
    event_type = event_type_map.get(status, EventType.JOB_STARTED)

    severity = Severity.ERROR if status == "failed" else Severity.INFO

    return ops_logger.log(
        severity,
        event_type,
        uid=uid,
        server_session_id=session_id,
        job_id=job_id,
        request_id=request_id,
        error_code=error_code,
        message=error_message or f"Job {job_type} {status}",
        debug={"jobType": job_type, **(debug or {})},
    )


def log_stt_event(
    session_id: str,
    status: str,  # "started", "completed", "failed"
    *,
    uid: Optional[str] = None,
    operation_id: Optional[str] = None,
    duration_sec: Optional[float] = None,
    error_code: Optional[ErrorCode] = None,
    error_message: Optional[str] = None,
) -> str:
    """STTイベントを記録"""
    event_type_map = {
        "started": EventType.STT_STARTED,
        "completed": EventType.STT_COMPLETED,
        "failed": EventType.STT_FAILED,
    }
    event_type = event_type_map.get(status, EventType.STT_STARTED)

    severity = Severity.ERROR if status == "failed" else Severity.INFO

    debug = {}
    if operation_id:
        debug["operationId"] = operation_id
    if duration_sec is not None:
        debug["durationSec"] = duration_sec

    return ops_logger.log(
        severity,
        event_type,
        uid=uid,
        server_session_id=session_id,
        error_code=error_code,
        message=error_message or f"STT {status}",
        debug=debug if debug else None,
    )


def log_llm_event(
    session_id: str,
    llm_type: str,  # "summary", "quiz", "explain", etc.
    status: str,  # "started", "completed", "failed"
    *,
    uid: Optional[str] = None,
    error_code: Optional[ErrorCode] = None,
    error_message: Optional[str] = None,
    model: Optional[str] = None,
    tokens_used: Optional[int] = None,
) -> str:
    """LLMイベントを記録"""
    event_type_map = {
        "started": EventType.LLM_STARTED,
        "completed": EventType.LLM_COMPLETED,
        "failed": EventType.LLM_FAILED,
    }
    event_type = event_type_map.get(status, EventType.LLM_STARTED)

    severity = Severity.ERROR if status == "failed" else Severity.INFO

    debug = {"llmType": llm_type}
    if model:
        debug["model"] = model
    if tokens_used is not None:
        debug["tokensUsed"] = tokens_used

    return ops_logger.log(
        severity,
        event_type,
        uid=uid,
        server_session_id=session_id,
        error_code=error_code,
        message=error_message or f"LLM {llm_type} {status}",
        debug=debug,
    )


def log_limit_reached(
    uid: str,
    limit_type: str,  # "free", "pro", "transcribe", etc.
    *,
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
    current_usage: Optional[dict] = None,
) -> str:
    """制限到達イベントを記録"""
    error_code = ErrorCode.FREE_LIMIT_REACHED if limit_type == "free" else ErrorCode.PRO_LIMIT_REACHED

    return ops_logger.warn(
        EventType.LIMIT_REACHED,
        uid=uid,
        server_session_id=session_id,
        request_id=request_id,
        error_code=error_code,
        message=f"Usage limit reached: {limit_type}",
        debug={"limitType": limit_type, "currentUsage": current_usage} if current_usage else {"limitType": limit_type},
    )
