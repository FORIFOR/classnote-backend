# YouTube Import Frontend Implementation Guide

This guide details how to implement the YouTube video import feature on the client side (iOS/Web).

## 1. Overview
The feature allows users to import a YouTube video, extract its subtitles (if available), and create a session for summarization and quiz generation.

### Core Architecture
- **Pre-check**: Verify if the video has valid subtitles before creating a session.
- **Import**: Create a session and queue the processing task.
- **Async Processing**: The backend handles transcript fetching, summarization, and quiz generation asynchronously.

---

## 2. API Endpoints

### A. Check Availability (Pre-flight)
**ENDPOINT**: `POST /imports/youtube/check`

Called immediately after the user pastes a URL to validate compatibility.

**Request:**
```json
{
  "url": "https://www.youtube.com/watch?v=..."
}
```

**Response (Success):**
```json
{
  "videoId": "dQw4w9WgXcQ",
  "available": true,
  "tracks": [
    {
      "language": "Japanese",
      "language_code": "ja",
      "is_generated": false,
      "is_translatable": true
    },
    {
      "language": "English",
      "language_code": "en",
      "is_generated": true,
      "is_translatable": true
    }
  ],
  "reason": null
}
```

**Response (Unavailable):**
```json
{
  "videoId": "...",
  "available": false,
  "tracks": [],
  "reason": "no_transcript" // "transcripts_disabled", "video_unavailable", etc.
}
```

### B. Import Video
**ENDPOINT**: `POST /imports/youtube`

Called when the user confirms the import.

**Request:**
```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "mode": "lecture",       // or "meeting"
  "title": "My Video Title", // Optional (User input or fetched title)
  "language": "ja"         // Preferred language code from the check response
}
```

**Response:**
```json
{
  "sessionId": "lecture-1700000000-abcdef",
  "transcriptStatus": "processing",
  "sourceUrl": "..."
}
```

---

## 3. UI/UX Flow

### Step 1: Input & Validation
1.  **User Interface**: Provide a text field for the YouTube URL.
2.  **Action**: Identify valid YouTube URLs (regex or creating a `URL` object).
3.  **Trigger**: Call `POST /imports/youtube/check` upon valid input (debounce or "Check" button).

### Step 2: Handling Check Results
-   **If `available: true`**:
    -   Show a **"Subtitle Found"** indicator.
    -   (Optional) Display a dropdown to select the preferred language from `tracks`.
        -   Default to "ja" if available, else "en".
    -   Enable the **"Import"** button.
-   **If `available: false`**:
    -   Show an error message based on `reason`:
        -   `no_transcript`: "No subtitles found for this video."
        -   `transcripts_disabled`: "Subtitles are disabled for this video."
        -   `video_unavailable`: "Video is unavailable or private."
    -   Disable the **"Import"** button.

### Step 3: Execution
1.  **User Action**: User clicks "Import".
2.  **API Call**: `POST /imports/youtube`.
    -   Send the selected `language` (e.g., "ja").
3.  **Transition**:
    -   On success, navigate immediately to the **Session Detail Screen**.
    -   The session state will likely be `processing` or `recording_finished`.
    -   Show a loading spinner or status indicator in the session detail view while `summaryStatus` / `quizStatus` are `pending`.

---

## 4. Error Handling & Limits

### Plan Limits (Triple Lock)
The backend enforces monthly limits on imports.

-   **HTTP 409 Conflict**:
    -   Occurs if the user has exceeded their monthly limit (Free: 30 sessions/mo, Premium: 120 mins/mo).
    -   **Action**: Show a "Limit Reached" alert and prompt to upgrade (if Free) or wait for reset.
    -   Error response body will contain `code: "server_session_limit"` or `code: "monthly_limit"`.

### Application Errors
-   **HTTP 503**: Backend issue or YouTube API rate limit. Retry processing.
-   **HTTP 400**: Invalid URL.

---

## 5. Implementation Checklist
- [ ] Create `YouTubeClient` or add methods to `APIClient`.
- [ ] Implement `YouTubeImportView` with URL input and status feedback.
- [ ] Integreate `POST /check` for real-time validation.
- [ ] Handle `available=false` states gracefully.
- [ ] Handle 409 Plan Limits errors responsibly.
