# ops_events コレクション設計

## 概要

運用イベント（成功/失敗/警告）を構造化して保存し、管理UIから一元的に監視・検索できるようにする。

## コレクション構造

```
ops_events/{eventId}
```

## フィールド定義

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `ts` | timestamp | ✓ | イベント発生時刻 |
| `severity` | string | ✓ | `INFO` / `WARN` / `ERROR` |
| `type` | string | ✓ | イベント種別（下記参照） |
| `uid` | string | | ユーザーID |
| `serverSessionId` | string | | セッションID（Firestore doc id） |
| `clientSessionId` | string | | クライアント側UUID |
| `jobId` | string | | ジョブID |
| `requestId` | string | | X-Request-Id / Idempotency-Key |
| `endpoint` | string | | APIエンドポイント |
| `statusCode` | number | | HTTPステータスコード |
| `errorCode` | string | | 分類用エラーコード（下記参照） |
| `message` | string | | 短い説明メッセージ |
| `debug` | map | | 追加デバッグ情報（operationId等） |

## イベント種別 (type)

### セッション系
- `SESSION_CREATE` - セッション作成
- `SESSION_UPDATE` - セッション更新
- `SESSION_DELETE` - セッション削除

### アップロード系
- `UPLOAD_SIGNED_URL` - 署名付きURL発行
- `UPLOAD_CHECK` - アップロード確認

### ジョブ系
- `JOB_QUEUED` - ジョブキュー投入
- `JOB_STARTED` - ジョブ開始
- `JOB_COMPLETED` - ジョブ完了
- `JOB_FAILED` - ジョブ失敗

### 外部API系
- `STT_STARTED` - STT開始
- `STT_COMPLETED` - STT完了
- `STT_FAILED` - STT失敗
- `LLM_STARTED` - LLM呼び出し開始
- `LLM_COMPLETED` - LLM呼び出し完了
- `LLM_FAILED` - LLM呼び出し失敗

### 認証・課金系
- `AUTH_FAILED` - 認証失敗
- `LIMIT_REACHED` - 制限到達
- `PAYMENT_REQUIRED` - 課金必要

## エラーコード (errorCode)

### 500系
- `SESSION_CREATE_500`
- `UPLOAD_CHECK_500`
- `JOB_WORKER_500`

### 前提不足
- `TRANSCRIPT_MISSING` - トランスクリプトが空
- `AUDIO_MISSING_IN_GCS` - GCSに音声ファイルがない
- `AUDIO_TOO_LONG` - 音声が長すぎる（>120min）

### 外部API
- `STT_OPERATION_FAILED`
- `STT_QUOTA_EXCEEDED`
- `VERTEX_QUOTA_EXCEEDED`
- `VERTEX_SCHEMA_PARSE_ERROR`

### 権限・課金
- `AUTH_INVALID_TOKEN`
- `AUTH_EXPIRED_TOKEN`
- `PAYMENT_REQUIRED`
- `FREE_LIMIT_REACHED`
- `PRO_LIMIT_REACHED`

### 疑わしい挙動
- `RETRY_STORM` - 短時間に同一endpoint連打
- `UPLOAD_SPAM` - 大容量連投

## Firestoreインデックス

### 複合インデックス（必須）

```
Collection: ops_events
Fields:
1. severity ASC, ts DESC
2. type ASC, ts DESC
3. uid ASC, ts DESC
4. serverSessionId ASC, ts DESC
5. errorCode ASC, ts DESC
6. ts DESC (単一フィールド、既定で作成)
```

### クエリ例

```python
# 直近24時間のERROR
db.collection("ops_events")
  .where("severity", "==", "ERROR")
  .where("ts", ">=", yesterday)
  .order_by("ts", direction=firestore.Query.DESCENDING)
  .limit(100)

# 特定セッションのタイムライン
db.collection("ops_events")
  .where("serverSessionId", "==", session_id)
  .order_by("ts")

# 特定ユーザーの問題
db.collection("ops_events")
  .where("uid", "==", user_id)
  .where("severity", "==", "ERROR")
  .order_by("ts", direction=firestore.Query.DESCENDING)
```

## 保持期間

- デフォルト: 30日
- Cloud Scheduler で日次削除ジョブを実行

## サイズ見積もり

- 1イベント: 約500bytes
- 1日あたり: 10,000イベント想定 = 5MB/日
- 30日保持: 150MB
