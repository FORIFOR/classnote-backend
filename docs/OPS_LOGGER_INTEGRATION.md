# ops_logger 組み込みガイド

## 概要

`app/services/ops_logger.py` を使用して、運用イベントを `ops_events` コレクションに記録します。
これにより管理コンソールでエラーの追跡・分析が可能になります。

## インポート

```python
from app.services.ops_logger import (
    ops_logger,
    log_session_create,
    log_job_transition,
    log_stt_event,
    log_llm_event,
    log_limit_reached,
    Severity,
    EventType,
    ErrorCode,
)
```

## 主要な組み込みポイント

### 1. セッション作成 (`app/routes/sessions.py`)

```python
# create_session 関数内

# 成功時
log_session_create(
    uid=current_user.uid,
    session_id=session_id,
    request_id=x_idempotency_key,
    success=True
)

# 失敗時（例外ハンドラ内）
log_session_create(
    uid=current_user.uid,
    session_id=session_id,
    request_id=x_idempotency_key,
    success=False,
    error_message=str(e)
)
```

### 2. 制限到達時

```python
# _check_session_creation_limits 内で制限に達した場合
log_limit_reached(
    uid=user_id,
    limit_type="free",  # or "pro"
    session_id=session_id,
    current_usage={"daily_sessions": count}
)
```

### 3. ジョブ状態遷移 (`app/routes/tasks.py`)

```python
# ジョブ開始時
log_job_transition(
    session_id=session_id,
    job_type="summarize",
    status="started",
    uid=user_id,
    job_id=job_id
)

# ジョブ完了時
log_job_transition(
    session_id=session_id,
    job_type="summarize",
    status="completed",
    uid=user_id,
    job_id=job_id
)

# ジョブ失敗時
log_job_transition(
    session_id=session_id,
    job_type="summarize",
    status="failed",
    uid=user_id,
    job_id=job_id,
    error_code=ErrorCode.JOB_WORKER_500,
    error_message=str(e)
)
```

### 4. STT 呼び出し (`app/services/google_speech.py`)

```python
# STT 開始時
log_stt_event(
    session_id=session_id,
    status="started",
    uid=user_id,
    operation_id=operation_name
)

# STT 完了時
log_stt_event(
    session_id=session_id,
    status="completed",
    uid=user_id,
    operation_id=operation_name,
    duration_sec=duration
)

# STT 失敗時
log_stt_event(
    session_id=session_id,
    status="failed",
    uid=user_id,
    operation_id=operation_name,
    error_code=ErrorCode.STT_OPERATION_FAILED,
    error_message=str(e)
)
```

### 5. LLM 呼び出し (`app/services/llm.py`)

```python
# LLM 呼び出し完了時
log_llm_event(
    session_id=session_id,
    llm_type="summary",  # "quiz", "qa", etc.
    status="completed",
    uid=user_id,
    model="gemini-1.5-flash"
)

# LLM 呼び出し失敗時
log_llm_event(
    session_id=session_id,
    llm_type="summary",
    status="failed",
    uid=user_id,
    error_code=ErrorCode.VERTEX_SCHEMA_PARSE_ERROR,
    error_message=str(e)
)
```

## 直接呼び出し（カスタムイベント）

```python
# INFO レベル
ops_logger.info(
    EventType.UPLOAD_CHECK,
    uid=user_id,
    server_session_id=session_id,
    message="Upload check completed",
    debug={"file_size": size_bytes}
)

# ERROR レベル
ops_logger.error(
    EventType.UPLOAD_CHECK,
    uid=user_id,
    server_session_id=session_id,
    error_code=ErrorCode.AUDIO_MISSING_IN_GCS,
    message="Audio file not found in GCS",
    debug={"expected_path": gcs_path}
)
```

## 重要な注意点

1. **パフォーマンス**: ops_logger の呼び出しは非同期ではありません。高頻度の呼び出しが必要な場合はバックグラウンドタスクを検討してください。

2. **エラーハンドリング**: ops_logger 内部でエラーが発生しても本体処理には影響しません（例外を握りつぶします）。

3. **PII の取り扱い**: ユーザーのメールアドレスなど個人情報は `debug` フィールドに含めないでください。

4. **requestId の統一**: iOS から送られる `X-Request-Id` または `X-Idempotency-Key` を必ず `request_id` として渡してください。

## 優先的に組み込むべき場所

1. `app/routes/sessions.py` - セッション作成/更新/削除
2. `app/routes/tasks.py` - 全ワーカーエンドポイント
3. `app/services/google_speech.py` - STT 呼び出し
4. `app/services/llm.py` - LLM 呼び出し
5. `app/routes/billing.py` - 課金関連エラー
