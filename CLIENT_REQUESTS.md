# Client → Server Requests (ClassnoteX iOS)

Source of truth: iOS client (`ClassnoteX`), primarily `CloudAPIClient` and `AppModel`.

## Common
- Base URL: user editable, stored in `UserDefaults` (`AppModel.baseURLString`).
- Auth: `Authorization: Bearer <Firebase ID token>` on all HTTP requests.
- JSON bodies encoded with `JSONEncoder` (ISO8601 dates). All responses decoded with `JSONDecoder`.

## Endpoints

### Create session
- `POST /sessions`
- Body: `{ "mode": "lecture"|"meeting", "title": "<optional>", "userId": "<uid>" }`
- Called from `AppModel.startLiveSession*` and `createAndUploadSession`.

### Get signed upload URL
- `POST /upload-url`
- Body: `{ "sessionId": "<id>", "mode": "lecture"|"meeting", "contentType": "audio/wav" }`
- Returns: signed URL fields (`url`/`uploadUrl`/`upload_url`).
- Called from `AppModel.createAndUploadSession`.

### Upload audio file
- `PUT <signedUploadURL>`
- Headers: `Content-Type: audio/wav`
- Body: full recording file (16 kHz, 16-bit, mono WAV written by `FileRecordingModel`).
- Called from `CloudAPIClient.putSignedURL`.

### Start transcription (batch)
- `POST /sessions/{sessionId}/start_transcribe`
- Body: `{ "mode": "lecture"|"meeting" }`
- Triggered after upload in `AppModel.createAndUploadSession`.

### Refresh transcript
- `POST /sessions/{sessionId}/refresh_transcript`
- Body: `{}` (empty object)
- Returns: `{ "status": "...", "transcriptText": "..." }`
- Used in `AppModel.refreshTranscript/pollTranscript`.

### Summarize
- `POST /sessions/{sessionId}/summarize`
- Body: `{}` (empty object)
- Returns: `{ "summary": { overview, points[], keywords[] } }`
- Used in `AppModel.summarize` (auto-triggered after upload if未キャッシュ).

### Generate quiz
- `POST /sessions/{sessionId}/quiz?count=<N>`
- Body: none
- Returns: `{ "questions": [ { id, question, choices[], correctIndex, explanation? } ] }`
- Used in `AppModel.fetchQuiz` (auto 5問生成 after upload if未キャッシュ, and on demand in SessionDetailView).

### QA (lecture-based)
- `POST /sessions/{sessionId}/qa`
- Body: `{ "question": "<user input>" }`
- Returns: `{ "answer": "...", "citations": [...]? }`
- Used in `AppModel.askQuestion` (SessionDetailView QAセクション)。

## Realtime streaming (WebSocket)
- URL: `wss://<base-host>/ws/stream/{sessionId}?token=<idToken>` (scheme becomes `ws` if base is http).
- On connect client sends start config:
  ```json
  {"event":"start","config":{
    "languageCode":"ja-JP",
    "sampleRateHertz":16000,
    "enableSpeakerDiarization":true,
    "speakerCount":2,
    "model":"latest_long"
  }}
  ```
- Audio: binary PCM chunks (16 kHz, 16-bit, mono) pushed as `.data` messages.
- Stop: `{ "event": "stop" }`.
- Server responses: JSON with `event: "partial" | "final"` plus `transcript` and optional `words[]` (with `start`, `end`, `speakerTag`).
- Implemented in `RealtimeTranscriptionClient`, driven by `AppModel.startLiveSession*` and `FileRecordingModel.onChunk`.

## File/format notes
- Local recordings saved at 16 kHz / 16-bit / mono WAV under `Documents/Recordings/lecture_YYYYMMDD_HHMMSS.wav`.
- Streaming uses the same sample rate and channel layout.

## Relevant client files
- `ClassnoteX/CloudAPIClient.swift` — HTTP endpoints & auth
- `ClassnoteX/AppModel.swift` — request orchestration
- `ClassnoteX/FileRecordingModel.swift` — capture/encoding parameters
- `ClassnoteX/RealtimeTranscriptionClient.swift` — WS protocol
