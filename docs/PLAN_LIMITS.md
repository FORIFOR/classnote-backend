# Plan Limits & Quotas (vNext)

**Status: DEFINITIVE (2026-01-17)**
**Reset Cycle:** Monthly, 1st of each month 00:00 JST (Asia/Tokyo).

## 1. Free Plan (¥0)
Designed for trial use with strict "Double Lock" (Count + Duration) to prevent ANY cost.

| Feature | Limit | Enforcement Mechanism |
| :--- | :--- | :--- |
| **Server Sessions** | **5 Sessions** | **Hard Stop** (User doc `serverSessionCount`) |
| **Cloud Sessions** | **10 Sessions / Month** | **Hard Stop** (`cloud_sessions_started`) |
| **Cloud STT Duration** | **30 Mins / Month** | **Hard Stop** (`cloud_stt_sec`) |
| **AI Summary** | **3 Times / Month** | **Hard Stop** (`summary_generated`) |
| **AI Quiz** | **3 Times / Month** | **Hard Stop** (`quiz_generated`) |
| **Session Duration** | **2 Hours** (Hard) | **Strict** |

## 2. Standard Plan (¥500)
Guarantees "No Surprise Bills" via Triple Lock System.

| Feature | Limit | Enforcement Mechanism |
| :--- | :--- | :--- |
| **Session Creation** | **100 Sessions / Month** | **Hard Stop** (`sessions_created`) |
| **Cloud STT Duration** | **120 Mins / Month** | **Hard Stop** (`cloud_stt_sec`) |
| **Cloud Sessions** | **100 Sessions / Month** | **Hard Stop** (`cloud_sessions_started`) |
| **Server Sessions** | **300 Sessions** | **Soft Limit + Cleanup** |
| **AI Summary** | **100 Times / Month** | **Hard Stop** (`summary_generated`) |
| **AI Quiz** | **100 Times / Month** | **Hard Stop** (`quiz_generated`) |
| **Session Duration** | **2 Hours** (Hard) | **Strict** |



## 3. Backend Architecture (Cost Guard)

### Source of Truth (Unified Account)
*   **Firestore**: `accounts/{accountId}/monthly_usage/{monthKey}`
*   **Aggregation**: 同一の電話番号をリンクした全てのUIDで残枠を共有します。
*   **Key Format**: `YYYY-MM` (JST based)
*   **Fields**:
    - `cloud_stt_sec` (float)
    - `cloud_sessions_started` (int)
    - `summary_generated` (int)
    - `quiz_generated` (int)
    - `llm_calls` (int)
    - `server_session` (int) - アカウント全体でのスロット数

### Transactional Gates
All billable operations MUST reserve quota via `CostGuardService` (Transactional) **BEFORE** execution.

1.  **Start Recording**: Checks `server_session`, `cloud_sessions_started`, `cloud_stt_sec`.
2.  **Streaming**: Accumulates `cloud_stt_sec` in real-time. Cuts connection on limit.
3.  **AI Generation**: Checks/Increments `summary_generated` or `quiz_generated`.

## 4. Error Codes (HTTP 402/403/409)
*   `server_session_limit` (409)
*   `cloud_session_limit` (402)
*   `cloud_minutes_limit` (402)
*   `summary_limit` (402)
*   `quiz_limit` (402)
*   `llm_monthly_limit` (402)
*   `transcript_too_short`
*   `transcript_missing`

(Note: Use `402 Payment Required` if the intent is to upsell, otherwise `409` for limit reached).
