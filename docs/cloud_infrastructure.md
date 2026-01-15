# Cloud Infrastructure Configuration

This document outlines the cloud infrastructure components used by the ClassnoteX API, including Cloud Run services, Cloud Tasks queues, Cloud Scheduler jobs, Firestore collections, and Cloud Storage buckets.

## 1. Cloud Run
The API backend is deployed on Cloud Run.

- **Service Name**: `classnote-backend` (implied)
- **Region**: `asia-northeast1` (recommended)
- **Environment Variables**:
  - `GCP_PROJECT`: Google Cloud Project ID
  - `TASKS_LOCATION`: Cloud Tasks queue location (e.g., `asia-northeast1`)
  - `SUMMARIZE_QUEUE`: Name of the Cloud Tasks queue (default: `summarize-queue`)
  - `CLOUD_RUN_SERVICE_URL`: URL of the deployed Service (e.g., `https://api-xyz.a.run.app`)
  - `AUDIO_BUCKET_NAME`: GCS bucket for raw audio
  - `MEDIA_BUCKET_NAME`: GCS bucket for processed media (images, etc.)

## 2. Cloud Tasks
Asynchronous background processing is handled via Cloud Tasks.

### Queue Configuration
- **Queue Name**: `summarize-queue`
- **Location**: `asia-northeast1`
- **Rate Limits**: 
  - Max dispatches: 10/sec
  - Max concurrent: 50

### Task Types & Worker Endpoints
All workers are implemented as HTTP POST endpoints within the main API service.

| Task Type | Worker Endpoint | Purpose | Triggered By |
|-----------|-----------------|---------|--------------|
| **Summarize** | `/internal/tasks/summarize` | Generate summary, tags, playlist | `POST /sessions/{id}/summarize` |
| **Quiz** | `/internal/tasks/quiz` | Generate quizzes | `POST /sessions/{id}/quizzes` |
| **Explain** | `/internal/tasks/explain` | Generate detailed explanation | `POST /sessions/{id}/explain` |
| **Highlights** | `/internal/tasks/highlights` | Generate highlights | `POST /sessions/{id}/highlights` |
| **Playlist** | `/internal/tasks/playlist` | Generate playlist timeline | Device Sync / `POST /sessions/{id}/events` |
| **QA** | `/internal/tasks/qa` | Answer user question (LLM) | `POST /sessions/{id}/qa` |
| **Translate** | `/internal/tasks/translate` | Translate transcript | `POST /sessions/{id}/translate` |

## 3. Cloud Scheduler
Recurring jobs are managed by Cloud Scheduler, triggering internal API endpoints.

| Job Name | Schedule (JST) | Target Endpoint | Purpose |
|----------|----------------|-----------------|---------|
| `audio-cleanup-daily` | Daily 03:00 | `POST /internal/tasks/audio-cleanup` | Delete expired audio files based on `deleteAfterAt` |
| `usage-aggregation-daily` | Daily 01:00 | `POST /internal/tasks/daily-usage-aggregation` | Aggregate daily usage stats to `system_stats` |

## 4. Firestore
Database schema overview.

### Top-Level Collections
- **`sessions`**: Main document for class sessions.
- **`users`**: User profiles and settings.
- **`usage_logs`**: Detailed logs of API usage / token consumption.
- **`system_stats`**: Aggregated system-wide statistics (e.g., daily active users).
- **`translations`**: Async translation results (`sessionId` as document ID).

### Sub-Collections (under `sessions/{id}`)
- **`derived`**: Stores async generation results (summary, quiz, etc.) to keep the main document light.
- **`qa_results`**: Stores async QA results.
- **`calendar_sync`**: Stores per-user calendar sync status.

## 5. Cloud Storage (GCS)
- **Audio Bucket** (`classnote-x-audio`): Stores raw uploaded audio files (`.m4a`, `.raw`).
  - Lifecycle: Should correspond to the application's expiration logic (e.g., 90 days).
- **Media Bucket** (`classnote-x-media`): Stores image notes and other media assets.

## Setup Script
A convenience script is available to provision the Cloud Tasks queue and Scheduler jobs:
`docs/setup_cloud_infrastructure.sh`

```bash
# Usage
chmod +x docs/setup_cloud_infrastructure.sh
./docs/setup_cloud_infrastructure.sh
```
