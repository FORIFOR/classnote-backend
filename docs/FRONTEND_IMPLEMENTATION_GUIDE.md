# フロントエンド実装ガイドライン (Classnote API Integration Guide)

このドキュメントは、Classnote API (`ver 00414` 以降) を利用するフロントエンド実装者が、バックエンドの仕様変更や制約を正しく扱い、堅牢なアプリケーションを構築・保守するためのガイドラインです。

## 1. 共通: エラーハンドリングとステータスコード

バックエンドは特定のエラー状況において、HTTPステータスコードと詳細なJSONボディを返します。フロントエンドはこれらを適切にハンドリングし、ユーザーに正しいアクションを促す必要があります。

### 重要なステータスコード

| コード | 意味 | フロントエンドの対応 |
| :--- | :--- | :--- |
| **402** | **Payment Required** | **プラン制限到達**。ユーザーにアップグレード訴求（Paywall）を表示してください。 |
| **403** | **Forbidden** | 権限なし、または機能制限（例：Freeプランでクラウド機能を使おうとした）。メッセージや `code` を見て判断。 |
| **409** | **Conflict** | リソース競合。Jobの二重起動など。通常は無視するか、既存のJob状況を確認します。 |
| **429** | **Too Many Requests** | レート制限。ユーザーに「少し待ってから再試行してください」と表示。 |
| **500** | **Server Error** | **リトライ推奨**（指数バックオフ）。一時的な障害の可能性があります。 |

### エラーレスポンス形式
エラー時は以下の構造で詳細が返ることがあります。`detail.error.code` をプログラムで判定に使用してください。
```json
{
  "detail": {
    "error": {
      "code": "cloud_not_entitled",
      "feature": "summary",
      "message": "Free plan requires cloud transcription...",
      "meta": { "sessionId": "...", "plan": "free" }
    }
  }
}
```

---

## 2. セッション同期 (Offline-First)

アプリはオフラインファーストで動作するため、セッション作成 (`POST /sessions`) は以下のルールに従ってください。

### ルール: `clientSessionId` を必ず使用する
サーバーへのセッション作成リクエストには、必ずフロントエンで生成した UUID v4 を `clientSessionId` として含めてください。

```json
// POST /sessions
{
  "title": "New Session",
  "clientSessionId": "550e8400-e29b-41d4-a716-446655440000", // 必須！
  "createdAt": "2024-01-20T10:00:00Z"
}
```
*   **なぜ重要か**: ネットワーク不安定時にリトライが発生しても、サーバーはこのIDを見て「同じセッションの再送」だと判断し、重複作成を防ぎます（冪等性）。
*   **同期ロジック**: サーバーから `200 OK` が返るまでは、ローカルDB上の該当セッションを「未同期(dirty)」として扱い、バックグラウンドでリトライし続けてください。

---

## 3. 非同期ジョブ (Summary/Quiz)

重い処理（AI要約など）は「Job API」を使用します。

### インテグレーションフロー
1.  **Job作成**: `POST /sessions/{id}/jobs`
    *   Body: `{ "type": "summary" }` (など)
    *   Response: `200 OK` (Job Created or Existing Job Returned)
2.  **ポーリング**: レスポンスに含まれる `pollUrl` を使用してステータスを確認。
    *   推奨間隔: 2秒 -> 3秒 -> 5秒...
    *   完了条件: `status` が `completed` または `failed` になるまで。
3.  **UI反映**: `completed` になったら結果を表示。

### 注意事項: Calendar Sync
*   **変更点**: `Job API` (`POST /sessions/{id}/jobs` with `type="calendar_sync"`) は **廃止** されました。
*   **正しい方法**: 専用エンドポイント `POST /sessions/{id}/calendar:sync` を使用してください。

---

## 4. プラン制限と使用量の表示 (CostGuard)

ユーザーに「あと何回使えるか」を正しく表示するために、`GET /users/me` のレスポンスを活用してください。
特に `ver 00414` 以降、プラン構造が複雑化（Free/Basic/Premium）しているため、フロントエンドで独自の制限ロジックを持たず、APIの値を信頼してください。

### `GET /users/me` レスポンス（抜粋）
```json
{
  "plan": "free",
  
  // クラウド録音時間の制限状況
  "cloud": {
    "limitSeconds": 1800.0,    // 月間上限 (秒)
    "usedSeconds": 1200.0,     // 使用済み (秒)
    "remainingSeconds": 600.0, // 残り (秒)
    "canStart": true,          // 新規録音開始OKか？
    "reasonIfBlocked": null    // ブロック理由（"cloud_minutes_limit" など）
  },

  // 回数制限（Free/Basic向け）
  // ※Premiumは "llm_calls" として合算されるため、これらはnullの場合があります
  "freeSummaryCreditsRemaining": 2, // (Deprecatedだが互換性のため維持の可能性あり)
  
  // vNext推奨: UI側で「あと何回？」を出す場合
  // CostGuardの仕様上、以下の回数を超えると402が返ります
  // Free: Summary/Quiz 各3回
  // Basic: Summary 20回, Quiz 10回
}
```
**推奨実装**: 
録音開始ボタンやAI生成ボタンを押せるかどうかの判定には、`cloud.canStart` や、各機能実行時の `402` エラーレスポンスを正として利用してください。

---

## 5. WebSocket (Streaming STT)

リアルタイム文字起こし (`/ws/stream/{session_id}`) を利用する場合の注意点。

1.  **接続タイムアウト**:
    *   サーバー側の「無音タイムアウト」は20秒です。録音開始したらすぐに音声データを送信してください。
    *   **重要**: 同時接続ロックのタイムアウトが **5分** に短縮されました。アプリがクラッシュしても、5分後にはロックが外れます。
2.  **チケットの利用 (`cloudTicket`)**:
    *   Cloud STTを利用する場合、事前に `create_session` で発行された `cloudTicket` をWebSocketの `start` イベントメッセージに含める必要があります。
    *   これを忘れると `unauthorized_cloud_ticket` エラーで切断されます。
    
    ```json
    // WS: start message
    {
      "event": "start",
      "cloudTicket": "uuid-ticket-from-session-response", // 必須
      "config": { ... }
    }
    ```

---

## 6. 廃止・非推奨機能

*   **Ads (広告)**: 現在無効化されています。関連するUIコンポーネントは非表示、またはAPIレスポンス(`get_placement` returns null)に従って何も描画しないでください。
*   **Admin Bypass**: 開発用バックドアは削除されました。管理画面機能を使う場合は、Firebase Authで正しくAdmin権限 (`custom claim: admin`) を付与されたユーザーでログインする必要があります。

---

## チェックリスト

実装完了時、またはリリース前に以下を確認してください。

- [ ] セッション作成時、必ず `clientSessionId` を送信しているか？
- [ ] 402 エラーを受け取ったら Paywall (アップグレード画面) を表示しているか？
- [ ] Job API のポーリング間隔は適切か（1秒未満の連打をしていないか）？
- [ ] `calendar_sync` は専用エンドポイントを使っているか？
- [ ] WebSocket接続時に `cloudTicket` を送っているか？
- [ ] 広告枠は正しく非表示（またはAPI依存）になっているか？
