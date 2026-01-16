# Classnote API Backend Detailed Design Specification
**Version**: 1.1 (Current Production State)
**Date**: 2026-01-16
**Status**: Live

## 1. System Architecture
The Classnote API is built as a stateless, containerized microservice running on **Google Cloud Run**, designed for horizontal scalability and high availability.

- **Compute**: Google Cloud Run (Python 3.11 / FastAPI / Uvicorn)
- **Database**: Google Cloud Firestore (NoSQL)
- **Storage**: Google Cloud Storage (Audio inputs, Transcript JSONs)
- **Authentication**: Firebase Authentication (Bearer Token)
- **AI Services**:
    - **STT**: Google Cloud Speech-to-Text V2 (Batch API / Long Model)
    - **LLM**: Google Vertex AI (Gemini 1.5 Pro/Flash) for Summary/Quiz
    - **Translation**: Google Cloud Translation API

## 2. Core Concepts & Data Models

### 2.1 User & Subscription
Users are managed via Firebase Auth. Subscription status (`plan`) is trusted from the `users` Firestore collection, synced mostly via Stripe webhooks (external to this API) or generic fallbacks.

- **Plans**: `free`, `basic`, `premium`
- **Credit System** (Managed by `app.services.usage.UsageLogger`):
    - **Free**: 1 Cloud Session (Atomic `consume_free_cloud_credit` check).
    - **Daily Limits**: Tracked in `users/{uid}/daily_usage/{YYYY-MM-DD}`.

### 2.2 Sessions (`sessions` collection)
A session represents a single class recording or meeting.
- **Idempotency**: Clients must provide `clientSessionId`. The server guarantees creation idempotency using a composite check on `ownerUid` + `clientSessionId`.
- **Modes** (`transcriptionMode`):
    - `cloud_google`: Server-side processing.
    - `device_sherpa`: On-device processing (server only syncs text).
- **Security**:
    - **Ticket System**: `cloudTicket` issued at creation for authorization context.
    - **Limits**: Max Duration 120 min.

### 2.3 Jobs (`sessions/{id}/jobs` sub-collection)
Asynchronous tasks for heavy processing.
- **Types**: `transcribe`, `summary`, `quiz`, `explain`, `translate`
- **State Machine**: `queued` -> `processing` -> `completed` / `failed` (w/ `error` field)
- **Worker**: Background tasks run within the generic API instance (via `app.task_queue`), utilizing Cloud Run's CPU allocation for async IO waiting, but offloading heavy compute (STT/LLM) to GCP APIs.

## 3. Key Workflows

### 3.1 Cloud Session Creation (`POST /sessions`)
1.  **Auth**: Verify Firebase Token.
2.  **Idempotency**: Check if `clientSessionId` exists for user. If so, return existing session (200 OK).
3.  **Limits Check** (`_check_session_creation_limits`):
    - **Free Plan**: Check if `HasConsumedFreeCloudCredit`. If No, atomically consume key. If Yes, block unless it's a device sync.
    - **Active Limit**: Block if user has too many active sessions (Soft Delete awareness).
4.  **DB Write**: Create Session Document.
5.  **Response**: Return Session ID + `cloudTicket` + Upload Signed URL (for Audio).

### 3.2 Cloud Transcription (`POST /sessions/{id}/upload_check` -> Async Task)
*(Note: Upload is direct to GCS via Signed URL)*
1.  **Trigger**: Client calls endpoint after upload, or background worker detects file.
2.  **Validation**: Verify file existence and size.
3.  **STT Execution** (`google_speech.py`):
    - **V2 Batch API**: Input M4A -> Convert to WAV (16kHz) -> GCS -> BatchRecognize.
    - **Config**: `auto_decoding_config` enabled for reliable WAV detection.
    - **Output**: JSON stored in `transcripts/{uuid}/`.
4.  **Completion**:
    - Parse JSON results.
    - Update Firestore Session (`transcriptText` + `segments`).
    - **Auto-Trigger**: If successful, enqueue `summary` job automatically.

### 3.3 Summary & Quiz Generation (`POST /sessions/{id}/jobs`)
1.  **Prerequisite Check**: Validate `transcriptText` or `segments` exist.
    - **Guard**: If transcript missing -> `400 Bad Request` ("文字起こしが完了していません").
2.  **Credit Check**:
    - **Free**: 1-time atomic consumption for Summary/Quiz (separate from STT, but strongly correlated).
    - **Premium**: Unlimited (within fair use).
3.  **Ticket Check**: (Removed in recent fix) Persistent jobs allowed anytime.
4.  **LLM Check**:
    - Fetch Transcript.
    - Call Vertex AI (Gemini) with specific System Prompt for Classnote.
    - Parse JSON response (strict schema).
5.  **Save**: Update Session with `summary` or save `generated_quiz` in sub-collection.

## 4. Security & Robustness Mechanisms

- **Rate Limiting**: Per-user limits on endpoints (e.g., 5 jobs/min).
- **Empty Guard**: Prevents costly LLM calls on empty transcripts.
- **UnboundLocalError Guard**: (Code Quality) Strict linting/variable casing to prevent shadowing global services.
- **Idempotency**: Prevents duplicate billing/processing on network retries.
- **Heartbeat/Timeout**: Only for WebSocket streams (legacy/real-time), enforced to kill stale connections.

## 5. Directory Structure
- `app/routes/`: API Endpoints
    - `sessions.py`: Core CRUD + Job Triggers.
    - `tasks.py`: Background Worker Logic.
- `app/services/`:
    - `google_speech.py`: Wrapper for STT V2.
    - `usage.py`: Credit logging & Plan enforcement.
    - `llm/`: Prompts & Gemini Wrappers.
- `app/util_models.py`: Pydantic Schemas sharing.
