# クラウド利用・コスト発生タイミング一覧

**Version**: 0.2.0  
**Last Updated**: 2026-01-15

---

## 目的
ClassnoteX で利用するクラウドサービスについて、**使われるタイミング**・**課金が発生するタイミング**・**使いすぎを防ぎたいポイント**を整理する。
運用でのコスト増大を防ぐため、計測すべき項目も明記する。

---

## 0. 共通の計測ルール（usage_logs）
**最低限の記録項目**
- `requestId`, `uid`, `sessionId`, `jobId`, `endpoint`
- `audioSeconds`, `sttSeconds`
- `aiTokensIn`, `aiTokensOut`, `model`
- `artifactWrites`, `firestoreReads`, `firestoreWrites`
- `gcsStoredBytes`, `gcsEgressBytes`

**目的**
- どの操作がコストを押し上げたかを追跡できる状態にする

---

## 1. Cloud Run
**用途**: API サーバ（HTTP/WebSocket）

**使用タイミング**
- すべての API リクエスト
- `/ws/stream/{id}` によるストリーミング STT

**コスト発生タイミング**
- リクエスト数
- CPU/メモリ使用時間
- WebSocket 長時間接続

**計測項目**
- `request_count`
- `cpu_sec`, `mem_gb_sec`
- `ws_connection_sec`

**使いすぎ防止ポイント**
- WebSocket のアイドル接続を長時間放置しない
- 重い処理は Cloud Tasks へ委譲し、API は短時間処理に限定

---

## 2. Cloud Build / Artifact Registry
**用途**: デプロイ用ビルドとコンテナイメージ保管

**使用タイミング**
- `./tools/deploy.sh` 実行時

**コスト発生タイミング**
- ビルド時間（分単位）
- コンテナイメージの保存容量

**計測項目**
- `build_minutes`
- `image_storage_gb`

**使いすぎ防止ポイント**
- 不要な頻繁デプロイを避ける
- 古いイメージの定期クリーンアップ方針を決める

---

## 3. Firestore
**用途**: セッション・ユーザー・サブスク・通知の永続保存

**使用タイミング**
- ほぼ全 API の読み書き
- ジョブ状態更新

**コスト発生タイミング**
- ドキュメント Read / Write / Delete 回数
- 大量の一覧取得やポーリング

**計測項目**
- `firestore_reads`, `firestore_writes`, `firestore_deletes`
- 画面や API ごとの read 回数

**使いすぎ防止ポイント**
- ポーリング間隔を短くしすぎない
- `GET /sessions/{id}` などの高頻度呼び出しを避ける

---

## 4. Cloud Storage (GCS)
**用途**: 音声・画像・生成物ファイルの保存

**使用タイミング**
- `/sessions/{id}/audio:prepareUpload` → PUT
- `/sessions/{id}/audio_url` の署名 URL 配布

**コスト発生タイミング**
- 保存容量
- 外向き転送（ダウンロード/ストリーミング）

**計測項目**
- `gcs_stored_gb`
- `gcs_egress_gb`
- `signed_url_issued_count`

**使いすぎ防止ポイント**
- 不要な音声ファイルの削除/期限管理
- 署名 URL の乱発を避ける（短時間での再発行を制限）

---

## 5. Cloud Tasks
**用途**: 非同期ジョブ（transcribe / summary / playlist / quiz / translate など）

**使用タイミング**
- `/sessions/{id}/jobs` から enqueue
- audio commit 後の自動トリガー

**コスト発生タイミング**
- タスク作成数
- 失敗によるリトライ回数

**計測項目**
- `task_created_count`
- `task_retry_count`

**使いすぎ防止ポイント**
- `Idempotency-Key` で重複起動を防止
- 永続失敗は retry しない（失敗分類を実装）

---

## 6. Google Speech-to-Text (Batch)
**用途**: クラウド文字起こし

**使用タイミング**
- `type=transcribe` ジョブ
- 音声アップロード後の自動トリガー

**コスト発生タイミング**
- 音声長に応じた課金（秒単位）

**計測項目**
- `sttSeconds`
- `language`, `diarization_enabled`

**使いすぎ防止ポイント**
- 既に transcript がある場合は再実行しない
- 失敗時の再試行回数を制限

---

## 7. Vertex AI (Gemini)
**用途**: 要約 / クイズ / プレイリスト / QA / 翻訳 / ハイライト

**使用タイミング**
- `/sessions/{id}/jobs`
- `/sessions/{id}/qa`
- `/internal/tasks/*`

**コスト発生タイミング**
- 入力/出力トークン数

**計測項目**
- `aiTokensIn`, `aiTokensOut`, `model`

**使いすぎ防止ポイント**
- 結果キャッシュで再生成を抑制
- プラン別の回数制限を必ず実装

---

## 8. Firebase Authentication
**用途**: API 認証

**使用タイミング**
- すべての認証付き API

**コスト発生タイミング**
- 通常は無料枠だが、過剰リクエストで制限がかかる可能性あり

**使いすぎ防止ポイント**
- 無駄な再ログインを避ける

---

## 9. App Store Server API / Notifications
**用途**: iOS 課金検証・通知

**使用タイミング**
- `/billing/ios/confirm`
- `/billing/apple/notifications`

**コスト発生タイミング**
- 直接課金は無いが、API 制限に注意

**計測項目**
- `appstore_verify_count`
- `appstore_transaction_info_count`

**使いすぎ防止ポイント**
- Transaction Info 照合はデバッグ時のみ
- 本番環境では必要時に限定する

---

## 10. Cloud Logging
**用途**: ログ集約

**使用タイミング**
- すべての API

**コスト発生タイミング**
- ログ量（大量の request/response ログ）

**計測項目**
- `log_bytes_ingested`

**使いすぎ防止ポイント**
- JWS 全体など機密/巨大ログを避ける
- requestId + 最小限の構造化ログに絞る
