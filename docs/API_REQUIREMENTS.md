# ClassnoteX API 要件書

**Version**: 0.3.0  
**Last Updated**: 2026-01-15  
**Source of Truth**: `openapi.yaml`

---

## 1. 目的
ClassnoteX のクライアント（iOS/Web）から、録音・文字起こし・AI生成・共有・課金確認を安全に処理するための API 要件を定義する。

---

## 2. 対象ユーザーと権限
- **一般ユーザー**: 自分のセッション作成/更新/取得、AI生成の起動、共有の参照。
- **共有ユーザー**: 共有されたセッションの閲覧。
- **内部ワーカー**: Cloud Tasks からの内部エンドポイント（公開 API ではない）。

権限制御は **セッション所有者** と **共有メンバー** によって行う。

---

## 3. 認証・認可
- **公開 API**: Firebase ID Token（Bearer）
- **内部ワーカー**: Cloud Run IAM もしくは内部シークレットで保護（Firebase Token 前提ではない）
- ヘルスチェック等、限定されたエンドポイントを除き認証必須

例:
```
Authorization: Bearer <FIREBASE_ID_TOKEN>
```

---

## 4. 共通仕様

### 4.1 時刻と形式
- すべて **UTC**
- JSON レスポンス
- 文字起こしセグメントは **startSec/endSec（秒）** を基本とする

### 4.2 ステータスモデル
- **Job/Artifact 共通**: `pending` / `running` / `completed` / `failed`
- `canceled` は将来拡張用に予約（現行 API は未使用）

### 4.3 冪等性
以下の **副作用を伴う POST** は冪等性キーの指定を推奨する。
- `POST /sessions/{id}/jobs`
- `POST /sessions/{id}/device_sync`
- `POST /sessions/{id}/audio:prepareUpload`
- `POST /sessions/{id}/audio:commit`
- `POST /billing/ios/confirm`

指定方法:
- **ヘッダー**: `Idempotency-Key`（推奨）
- **ボディ**: `idempotencyKey`（`/sessions/{id}/jobs` などで利用）

同一キーの再送時は「同じ結果を返す」ことを優先する（409 で失敗させない）。

### 4.4 リクエスト追跡
- クライアントから `X-Request-Id` を送信可能
- `/billing/ios/confirm` は **レスポンスヘッダー `X-Request-Id`** と **ボディ `requestId`** を必ず返却
- 他のエンドポイントでも段階的に導入予定（ログと相関できることが目的）

### 4.5 エラーハンドリング
- 400: パラメータ不正
- 401: 未認証
- 403: 権限不足
- 404: リソース不存在
- 409: 状態競合
- 500: サーバ内部エラー
- 503: 外部サービス未設定/一時停止

---

## 5. データモデル（概要）

### 5.1 Session
- `id`, `ownerUid`, `title`, `mode`, `tags`, `createdAt`, `updatedAt`
- `acl`: `visibility` / `sharedUids`
- `audio`: `gcsPath`, `durationSec`, `codec`, `container`, `sizeBytes`, `sha256`, `status`

### 5.2 Job
- `jobId`, `sessionId`, `type`, `status`
- `createdAt`, `startedAt`, `finishedAt`
- `errorReason`, `idempotencyKey`, `progress`

### 5.3 Artifact（成果物）
- `type` = `transcript` / `playlist` / `summary` / `quiz` / `explain` / `highlights`
- `status`, `updatedAt`, `jobId`, `result`, `modelInfo`

重い成果物（全文 transcript など）は **GCS 参照** とし、Firestore には参照パスのみ保存する。

---

## 6. 主要ユースケース（最低要件）

### 6.1 セッション作成
- `POST /sessions`
- 入力: `title` / `mode` / `tags`（任意）
- 出力: `sessionId` を返却

### 6.2 音声アップロード
- `POST /sessions/{id}/audio:prepareUpload`
  - 署名付き URL 発行
- `POST /sessions/{id}/audio:commit`
  - `expectedSizeBytes` / `expectedPayloadSha256` を使い整合性チェック

### 6.3 文字起こし（クラウド）
- `POST /sessions/{id}/jobs` で `type=transcribe`
- 完了後 `transcriptText` と `segments` を保存
- `GET /sessions/{id}/artifacts/transcript` で状態取得

### 6.4 端末同期
- `POST /sessions/{id}/device_sync`
- 入力: `transcriptText`, `segments`, `audioMeta`
- 必要に応じて playlist/summary を自動トリガー

### 6.5 プレイリスト（チャプター）生成
- `POST /sessions/{id}/jobs` で `type=playlist`
- 取得の正ルートは `GET /sessions/{id}/artifacts/playlist`
  - `status` と `items` を返し、ポーリングはここに統一する
  - `GET /sessions/{id}` の `playlist` は互換目的（非推奨）

### 6.6 アセット解決（署名 URL）
- `POST /sessions/{id}/assets/resolve` は **audio / transcript / summary / quiz** のみ対応
- playlist は `GET /sessions/{id}/artifacts/playlist` を使用する

### 6.7 要約・クイズ・QA
- `POST /sessions/{id}/jobs` で `type=summary/quiz/explain`
- `POST /sessions/{id}/qa` で QA

### 6.7 iOS 課金確認
- `POST /billing/ios/confirm`
- 署名検証 → decode → DB 更新 → `requestId` 返却

---

## 7. コスト制御要件
**usage_logs** に以下の実績を残し、プラン制限と連動する。
- `aiTokensIn` / `aiTokensOut`
- `audioSeconds` / `sttSeconds`
- `artifactWrites`
- `gcsEgressBytes`

プラン別に以下の制限を設ける。
- 1日あたりの jobs 上限
- 1セッションあたり最大音声長
- 生成物再実行のクールダウン

---

## 8. 仕様変更時の運用
- API 変更は必ず `openapi.yaml` を更新し、関連ドキュメントを同期する
- 互換性が崩れる変更はバージョン更新が必要
