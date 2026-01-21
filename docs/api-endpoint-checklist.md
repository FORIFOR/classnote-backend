# API Endpoint チェックリスト（OpenAPI 自動生成）

このファイルは Cloud Run 本番の OpenAPI から生成されます。手編集しないでください。
Source: https://classnote-api-900324644592.asia-northeast1.run.app/openapi.json
Generated: 2026-01-19T16:39:37.317706+00:00
Paths: 92, Operations: 102

## Admin
- [ ] GET /admin/events
- [ ] GET /admin/sessions/{session_id}
- [ ] GET /admin/stats/dashboard
- [ ] GET /admin/users/{uid}
- [ ] POST /admin/users/{uid}/actions

## Ads
- [ ] POST /ads/events
- [ ] GET /ads/placement

## Assets
- [ ] GET /sessions/{session_id}/assets
- [ ] POST /sessions/{session_id}/assets/resolve
- [ ] POST /sessions/{session_id}/assets/{asset_type}/ensure

## Authentication
- [ ] POST /auth/line

## Billing
- [ ] POST /billing/apple/notifications
- [ ] POST /billing/ios/confirm

## Google
- [ ] GET /google/oauth/callback
- [ ] GET /google/oauth/start

## Imports
- [ ] POST /imports/youtube
- [ ] POST /imports/youtube/check

## Quiz Analytics
- [ ] GET /analytics/quiz
- [ ] GET /analytics/quiz/sessions
- [ ] POST /sessions/{session_id}/quiz_attempts

## Reactions
- [ ] GET /sessions/{session_id}/reaction
- [ ] PUT /sessions/{session_id}/reaction

## Search
- [ ] GET /search/decisions
- [ ] GET /search/sessions
- [ ] GET /search/tasks

## Sessions
- [ ] GET /sessions
- [ ] POST /sessions
- [ ] POST /sessions/batch_delete
- [ ] POST /sessions/share/join
- [ ] GET /sessions/{session_id}
- [ ] PATCH /sessions/{session_id}
- [ ] DELETE /sessions/{session_id}
- [ ] GET /sessions/{session_id}/artifacts/highlights
- [ ] GET /sessions/{session_id}/artifacts/playlist
- [ ] GET /sessions/{session_id}/artifacts/quiz
- [ ] GET /sessions/{session_id}/artifacts/summary
- [ ] GET /sessions/{session_id}/artifacts/transcript
- [ ] POST /sessions/{session_id}/artifacts/transcript
- [ ] DELETE /sessions/{session_id}/audio
- [ ] POST /sessions/{session_id}/audio:commit
- [ ] POST /sessions/{session_id}/audio:prepareUpload
- [ ] GET /sessions/{session_id}/audio_url
- [ ] GET /sessions/{session_id}/calendar:status
- [ ] POST /sessions/{session_id}/calendar:sync
- [ ] POST /sessions/{session_id}/device_sync
- [ ] GET /sessions/{session_id}/image_notes
- [ ] DELETE /sessions/{session_id}/images/{image_id}
- [ ] POST /sessions/{session_id}/images:commit
- [ ] POST /sessions/{session_id}/images:prepare
- [ ] POST /sessions/{session_id}/jobs
- [ ] GET /sessions/{session_id}/jobs/{job_id}
- [ ] GET /sessions/{session_id}/jobs/{job_type}
- [ ] GET /sessions/{session_id}/members
- [ ] PATCH /sessions/{session_id}/members/{user_id}
- [ ] DELETE /sessions/{session_id}/members/{user_id}
- [ ] PATCH /sessions/{session_id}/meta
- [ ] PATCH /sessions/{session_id}/notes
- [ ] GET /sessions/{session_id}/participants_users
- [ ] GET /sessions/{session_id}/qa/{qa_id}
- [ ] POST /sessions/{session_id}/quizzes/{quiz_id}/answers
- [ ] POST /sessions/{session_id}/share/code
- [ ] POST /sessions/{session_id}/share:invite
- [ ] GET /sessions/{session_id}/shared_with_users
- [ ] PATCH /sessions/{session_id}/tags
- [ ] POST /sessions/{session_id}/transcript
- [ ] POST /sessions/{session_id}/transcript_chunks:append
- [ ] POST /sessions/{session_id}/transcript_chunks:replace
- [ ] POST /sessions/{session_id}/transcription/retry
- [ ] POST /sessions/{session_id}/transcription:run

## Share
- [ ] GET /s/{token}
- [ ] POST /sessions/{session_id}/share
- [ ] DELETE /sessions/{session_id}/share/{target_uid}
- [ ] GET /sessions/{session_id}/share_link
- [ ] POST /sessions/{session_id}/share_link
- [ ] GET /share-code/{code}
- [ ] GET /share/{token}
- [ ] GET /share/{token}/info
- [ ] POST /share/{token}/join
- [ ] GET /shares/{token}

## Usage
- [ ] POST /usage/admin/backfill
- [ ] GET /usage/admin/users/{user_id}/summary
- [ ] GET /usage/analytics/me/timeline
- [ ] GET /usage/me/summary

## Users
- [ ] POST /users/claim-username
- [ ] GET /users/lookup
- [ ] GET /users/me
- [ ] PATCH /users/me
- [ ] DELETE /users/me
- [ ] GET /users/me/capabilities
- [ ] POST /users/me/consents
- [ ] GET /users/me/entitlement
- [ ] GET /users/me/profile
- [ ] PATCH /users/me/profile
- [ ] POST /users/me/share-code
- [ ] POST /users/me/subscription
- [ ] POST /users/me/subscription/ios
- [ ] GET /users/me/usage
- [ ] GET /users/search
- [ ] GET /users/search_by_share_code
- [ ] POST /users/share_lookup

## Untagged
- [ ] GET /health
