# Classnote API 現行仕様（サマリー）

本書は 2025-12-23 時点のバックエンド実装に基づく API サマリーです。  
Base URL 例: `https://classnote-api-<project>.run.app`

## 共通
- 認証: `Authorization: Bearer <Firebase ID Token>`
- フォーマット: JSON
- タイムスタンプ: Firestore Timestamp を ISO8601 で返却（必要箇所）

## Users
- `GET /users/me`  
  プロファイル取得。`uid, displayName, email?, photoUrl?, providers, provider, shareCode?, allowSearch, isShareable, plan, createdAt` を返す。

- `PATCH /users/me`  
  displayName / email / allowSearch / isShareable を部分更新。

- `GET /users/me/profile`  
  displayName / shareCode / isShareable を返す。

- `PATCH /users/me/profile`  
  displayName / isShareable を更新（存在しない場合は作成）。

- `POST /users/me/share_code`  
  6 桁の `shareCode` を発行（既存あればそれを返す）。`shareCodeSearchEnabled` を true に初期化。

- `POST /users/share_lookup`  
  body: `{ "code": "a1B2c3" }`  
  共有コードからユーザを検索（`isShareable`/`shareCodeSearchEnabled` が true のみ）。`found/targetUserId/displayName` を返す。

- `GET /users/search_by_share_code?code=xxxxxx`  
  共有コード検索（クエリ版）。6 文字必須。

- `GET /users/search?q=...`  
  Email/DisplayName を検索（検索許可オフのユーザは除外）。

## Sessions
- `POST /sessions`  
  body: `{title, mode, userId?, tags?, status?}`  
  セッション作成。`userId` は省略可（認証トークンの UID が使用される）。  
  ステータスは `予定/未録音/録音中/録音済み/要約済み/テスト生成/テスト完了` を許容（未指定は録音中）。

- `GET /sessions`  
  クエリ: `userId`（必須で自分の owner/shared を取得）、`kind`, `limit`, `from_date`, `to_date`  
  `sharedWith` を含めてマージした一覧を返す。
  **Note**: `isPinned`, `isArchived`, `lastOpenedAt` はユーザーごとのメタデータを結合して返す。

- `GET /sessions/{id}`  
  セッション詳細。`sharedWith` は `sharedUserIds/sharedWithCount` として返却。  
  `playlist/playlistStatus`、summary/quiz ステータス、tags 等も含む。
  **Note**: `isPinned` 等のメタデータも含まれる。

- `PATCH /sessions/{id}/meta`  
  body: `{ "isPinned": true, "isArchived": false, "lastOpenedAt": "..." }`  
  ユーザーごとのセッション設定（ピン留め・アーカイブ・既読）を更新する。コピーなし共有設計に対応。

- `POST /sessions/{id}/transcript`  
  端末側の transcript / segments をアップロード。`status=transcribed`。

- `POST /sessions/{id}/device_sync`  
  body: `{audioPath, transcriptText?, segments?, notes?, durationSec?, needsPlaylist?}`  
  端末側同期。`needsPlaylist` が true の場合、summary+playlist+tags 生成をキック。  
  `durationSec` は PlaybackCard 表示用。`segments` の `speakerId` は Optional (話者分離OFF時は nil 可)。

- `PATCH /sessions/{id}`  
  タイトル/タグ更新。

- `POST /sessions/{id}/summarize`  
  非同期で Gemini（要約+再生リスト+タグ）を生成。即 202 を返す (バックグラウンド実行)。  
  結果は `GET /sessions/{id}` の `summaryMarkdown` 等で確認する。

- `POST /sessions/{id}/quiz?count=5`  
  クイズ生成（非同期キューイング）。202 Accepted を返す。  
  クライアントは `GET /sessions/{id}` をポーリングして `quizStatus=completed` を待つ。

- `POST /sessions/{id}/qa`  
  body: `{ "question": "..." }`  
  Gemini でトランスクリプトを元に QA 回答（即時処理）。

- `POST /sessions/{id}/highlights` / `GET /sessions/{id}/highlights`  
  ハイライト生成トリガーと取得。

- `GET /sessions/{id}/diarization/status`  
  話者分離のステータス（pending/running/completed/failed）・話者一覧・セグメントを返す。

- `POST /sessions/{id}/chapters`  
  チャプター（再生リスト）生成をトリガー。202 Accepted を返す。

- `PATCH /sessions/{id}/notes` / `PATCH /sessions/{id}/tags`  
  メモ更新 / タグ更新。

- `GET /sessions/{id}/audio_url`  
  音声の署名付き URL を返す。

- `DELETE /sessions/{id}/audio`  
  音声ファイルを手動削除する。文字起こしや要約は保持される。

- `POST /upload-url`  
  body: `{sessionId, contentType}`  
  アップロード用署名付き URL を返し、`storagePath` を含む。

- `DELETE /sessions/{id}` / `POST /sessions/batch_delete`  
  セッション削除・一括削除。

## Sharing（6 桁コード）
- `POST /sessions/{id}/share`  
  body: `{ "targetShareCode": "a1B2c3" }`  
  コードでユーザを引き当て、`sharedWith` に追加。自己共有や許可オフは拒否。

- `DELETE /sessions/{id}/share/{target_uid}`  
  共有解除。

## Images
- `POST /sessions/{id}/image_notes/upload_url`  
  body: `{contentType}`  
  画像アップロード URL を取得し、`imageNotes` に追記。
  Res: `{imageId, uploadUrl, storagePath}`

- `GET /sessions/{id}/image_notes`  
  画像ノートの署名付き URL 一覧を返す。
  Res: `[{id, url, createdAt, description?}]`

## Imports
- `POST /imports/youtube`  
  body: `{ url, mode?, title?, language? }`  
  YouTube の字幕/文字起こしを取得してセッション化し、要約/テスト生成をキューに入れる。  
  字幕が取得できない動画は 422。

## Calendar
- `POST /sessions/{id}/calendar:sync`  
  body: `{userId, calendarId?}`  
  Google カレンダー（またはデバイスカレンダー）にセッションを同期する。
  
- `GET /sessions/{id}/calendar:status`  
  現在のカレンダー同期状況を取得する。
  Res: `{status: synced/failed/none, eventId?, updatedAt}`

## Auth
- `POST /auth/line`  
  LINE の idToken/accessToken を検証し、Firebase カスタムトークンを返す。

## Internal Tasks（Cloud Tasks 用）
- `POST /internal/tasks/summarize`  
  Gemini で要約 + 再生リスト + タグ生成。`summaryStatus/playlistStatus` を更新。
- `POST /internal/tasks/quiz`  
  クイズ生成。`quizStatus/quizMarkdown` を更新。
- `POST /internal/tasks/highlights`  
  ハイライト・タグ生成。
- `POST /internal/tasks/playlist`  
  プレイリスト生成（単独で呼ぶ場合）。
- `POST /internal/tasks/audio-cleanup`  
  [Cloud Scheduler] 期限切れ音声ファイルの削除ジョブ。
- `POST /internal/tasks/daily-usage-aggregation`  
  [Cloud Scheduler] 日次利用統計の集計ジョブ。

## 主なフィールド
- `sessions`  
  - `ownerUid` / `userId`（後方互換）  
  - `sharedWith`: {uid: true}  
  - `sharedUserIds/sharedWithUserIds`: 共有先（配列も保持）  
  - `durationSec`: 音声ファイルの長さ (PlaybackCardで使用)  
  - `summaryStatus`: `pending/running/completed/failed`  
  - `summaryMarkdown`: 要約テキスト  
  - `quizStatus`: `pending/running/completed/failed`  
  - `quizMarkdown`: クイズテキスト  
  - `playlistStatus`: `pending/running/completed/failed`  
  - `playlist`: `[{id, startSec, endSec, title, summary, label?}]`  
  - `tags`: 最大 4 件  
  - `transcriptText`  
  - `diarizedSegments/segments`: `[{startSec, endSec, text, speakerId?}]`  
  - `audioPath`
  - `imageNotes`: `[{id, storagePath, createdAt}]`
  - `WebSocket`: Web 版は `/ws/stream/{id}` でリアルタイム同期

- `users`  
  - `displayName`（アプリ表示名）  
  - `email?`, `photoUrl?`, `provider`, `providers[]`  
  - `shareCode`, `shareCodeSearchEnabled`, `isShareable`, `allowSearch`

## 注意点
- 共有コード検索は `shareCode` + `shareCodeSearchEnabled` + `isShareable/allowSearch` を満たすユーザのみヒットします。
- 権限チェックは owner または `sharedWith`/`sharedUserIds` に含まれる場合に閲覧可。共有は owner のみ操作可。
