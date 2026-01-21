# Plan Limits & Quotas (vNext)

**Status: DEFINITIVE (2026-01-17)**
**Reset Cycle:** Monthly, 1st of each month 00:00 JST (Asia/Tokyo).

## 1. Free Plan (¥0)
Designed for trial use with strict "Double Lock" (Count + Duration) to prevent ANY cost.

| Feature | Limit | Enforcement Mechanism |
| :--- | :--- | :--- |
| **Server Sessions** | **5 Sessions** | **Hard Stop**<br>Block POST /sessions if `serverSessionCount >= 5`. (Error: `server_session_limit`) |
| **Cloud Sessions** | **3 Sessions / Month** | **Hard Stop**<br>Block POST /sessions (cloud) if `cloudSessionsStarted >= 3`. (Error: `cloud_session_limit`) |
| **Cloud STT Duration** | **30 Mins / Month** (1,800s) | **Hard Stop**<br>Block if `cloudSecondsUsed >= 1800`. Disconnect active stream if reached. (Error: `cloud_minutes_limit`) |
| **AI Summary** | **3 Times / Month** | **Hard Stop**<br>Block if `summaryGenerated >= 3`. (Error: `summary_limit`) |
| **AI Quiz** | **3 Times / Month** | **Hard Stop**<br>Block if `quizGenerated >= 3`. (Error: `quiz_limit`) |
| **Session Duration** | **2 Hours** (Hard) | **Strict**<br>Stop recording/processing if > 7200s. |
| **On-Device** | **Unlimited** | Transcription unlimited (Server storage limit 5 applies). |

**Note**: "Daily Credits" are abolished. All limits are Monthly.

## 2. Standard Plan (¥500)
Guarantees "No Surprise Bills" via Triple Lock System.

| Feature | Limit | Enforcement Mechanism |
| :--- | :--- | :--- |
| **Session Creation** | **100 Sessions / Month** | **Hard Stop**<br>Block if `sessionsCreated >= 100`. (Error: `session_limit`) |
| **Cloud STT Duration** | **120 Mins / Month** (7,200s) | **Hard Stop**<br>Transactional Lock. Block if `cloudSecondsUsed >= 7200`. (Error: `cloud_minutes_limit`) |
| **Cloud Sessions** | **Unlimited** (within duration) | Bounded by Duration limit. |
| **Server Sessions** | **300 Sessions** | **Soft Limit + Cleanup**<br>If > 300, system allows creation but auto-deletes oldest/unpinned. |
| **LLM Calls** | **100 each / Month** | **Hard Stop**<br>Summary/Quiz each limited to 100. (Error: `summary_limit`, `quiz_limit`) |
| **Session Duration** | **2 Hours** (Hard) | **Strict** |



## 3. Backend Architecture (Cost Guard)

### Source of Truth
*   **Firestore**: `user_monthly_usage/{uid}_{monthKey}`
*   **Key Format**: `YYYY-MM` (JST based)
*   **Fields**:
    *   `cloudSecondsUsed` (float)
    *   `cloudSessionsStarted` (int)
    *   `summaryGenerated` (int)
    *   `quizGenerated` (int)
    *   `llmCalls` (int) - For Premium generic tracking

### Transactional Gates
All billable operations MUST reserve quota via `CostGuardService` (Transactional) **BEFORE** execution.

1.  **Start Recording**: Checks `serverSessionCount`, `cloudSessionsStarted`, `cloudSecondsUsed`.
2.  **Streaming**: Accumulates `cloudSecondsUsed` in real-time. Cuts connection on limit.
3.  **AI Generation**: Checks/Increments `summaryGenerated` or `quizGenerated`.

## 4. Error Codes (HTTP 409 Conflict)
*   `server_session_limit`
*   `cloud_session_limit`
*   `cloud_minutes_limit`
*   `summary_limit`
*   `quiz_limit`
*   `llm_monthly_limit`
*   `transcript_too_short`
*   `transcript_missing`

(Note: Use `402 Payment Required` if the intent is to upsell, otherwise `409` for limit reached).
