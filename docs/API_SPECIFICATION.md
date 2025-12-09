# ClassnoteX Backend API 仕様書

**Base URL**: `https://classnote-api-900324644592.asia-northeast1.run.app`

**Last Updated**: 2025-12-09

---

## 目次

1. [認証](#認証)
2. [セッション管理](#セッション管理)
   - [セッション作成](#post-sessions)
   - [セッション一覧取得](#get-sessions)
   - [セッション詳細取得](#get-sessionssession_id)
   - [セッション削除](#delete-sessionssession_id)
   - [セッション一括削除](#post-sessionsbatch_delete)
3. [録音・文字起こし](#録音文字起こし)
   - [トランスクリプトアップロード](#post-sessionssession_idtranscript)
   - [メモ更新](#patch-sessionssession_idnotes)
   - [音声URL取得](#get-sessionssession_idaudio_url)
   - [WebSocket音声ストリーム](#websocket-wsstreamession_id)
4. [AI機能](#ai機能)
   - [要約生成](#post-sessionssession_idsummarize)
   - [クイズ生成](#post-sessionssession_idquiz)
   - [話者分離](#post-sessionssession_iddiarize)
5. [ヘルスチェック](#get-health)
6. [データ型定義](#データ型定義)
7. [エラーハンドリング](#エラーハンドリング)

---

## 認証

現在は認証なし（`allow_unauthenticated`）で動作しています。
将来的には Firebase Auth の ID Token を `Authorization: Bearer <token>` ヘッダーで送信する予定です。

---

## セッション管理

### POST /sessions

録音開始時に新しいセッションを作成します。

#### Request

```http
POST /sessions
Content-Type: application/json
```

```json
{
  "title": "講義タイトル",
  "mode": "lecture",
  "userId": "firebase-user-uid"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | ✅ | セッションのタイトル |
| `mode` | string | ❌ | `"lecture"` or `"meeting"` (default: `"lecture"`) |
| `userId` | string | ✅ | Firebase Auth の UID |

#### Response (201 Created)

```json
{
  "id": "lecture-1765180222933-e9187c",
  "title": "講義タイトル",
  "mode": "lecture",
  "userId": "firebase-user-uid",
  "status": "recording",
  "createdAt": "2025-12-08T12:30:00.000Z"
}
```



---

### GET /sessions

セッション一覧を取得します。カレンダー画面やホーム画面で使用。

#### Request

```http
GET /sessions?user_id=xxx&kind=lecture&limit=20&from_date=2025-12-01&to_date=2025-12-31
```

| Query Param | Type | Required | Description |
|-------------|------|----------|-------------|
| `user_id` | string | ❌ | ユーザーIDでフィルタ |
| `kind` | string | ❌ | `"lecture"`, `"meeting"`, or `"all"` |
| `limit` | int | ❌ | 最大取得件数 (default: 20) |
| `from_date` | string | ❌ | 開始日 (ISO format: `2025-12-01`) |
| `to_date` | string | ❌ | 終了日 (ISO format: `2025-12-31`) |

#### Response

```json
[
  {
    "id": "lecture-1765180222933-e9187c",
    "title": "講義タイトル",
    "mode": "lecture",
    "userId": "firebase-user-uid",
    "status": "transcribed",
    "createdAt": "2025-12-08T12:30:00.000Z",
    "startedAt": "2025-12-08T12:30:00.000Z",
    "endedAt": "2025-12-08T13:30:00.000Z",
    "durationSec": 3600.5,
    "hasSummary": true,
    "hasQuiz": true,
    "speakers": [
      { "id": "spk_0", "label": "A", "displayName": "話者A" },
      { "id": "spk_1", "label": "B", "displayName": "話者B" }
    ]
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `hasSummary` | boolean | 要約が生成済みかどうか |
| `hasQuiz` | boolean | クイズが生成済みかどうか |
| `durationSec` | number | 録音時間（秒） |

---

### GET /sessions/{session_id}

特定のセッションの詳細を取得します。

#### Request

```http
GET /sessions/lecture-1765180222933-e9187c
```

#### Response

```json
{
  "id": "lecture-1765180222933-e9187c",
  "title": "講義タイトル",
  "mode": "lecture",
  "userId": "firebase-user-uid",
  "status": "transcribed",
  "createdAt": "2025-12-08T12:30:00.000Z",
  "startedAt": "2025-12-08T12:30:00.000Z",
  "endedAt": "2025-12-08T13:30:00.000Z",
  "durationSec": 3600.5,
  "transcriptText": "今日は人工知能について学びます...",
  "notes": "重要ポイント: AI、ディープラーニング",
  "summaryMarkdown": "## 講義ノート\n...",
  "quizMarkdown": "### 問題1\n...",
  "audioPath": "gs://classnote-x-audio/sessions/xxx/audio.raw",
  "speakers": [
    { "id": "spk_0", "label": "A", "displayName": "話者A" },
    { "id": "spk_1", "label": "B", "displayName": "話者B" }
  ],
  "diarizedSegments": [
    {
      "id": "seg_001",
      "start": 0.0,
      "end": 3.1,
      "speakerId": "spk_0",
      "text": "本日は、AIエージェントの基本について解説します。"
    }
  ]
}
```

| Status Value | Description |
|--------------|-------------|
| `recording` | 録音中 |
| `recorded` | 録音完了（音声アップロード済み） |
| `transcribed` | 文字起こし完了 |

---

### DELETE /sessions/{session_id}

セッションを削除します。

#### Request

```http
DELETE /sessions/lecture-1765180222933-e9187c
```

#### Response

```json
{
  "ok": true,
  "deleted": "lecture-1765180222933-e9187c"
}
```

---

### POST /sessions/batch_delete

複数のセッションを一括削除します。

#### Request

```http
POST /sessions/batch_delete
Content-Type: application/json
```

```json
{
  "ids": [
    "lecture-1765180222933-e9187c",
    "lecture-1765180223447-f9b0d9"
  ]
}
```

#### Response

```json
{
  "ok": true,
  "deleted": 2
}
```

---

## 録音・文字起こし

### POST /sessions/{session_id}/transcript

iOS の On-device STT で生成した文字起こしをアップロードします。

#### Request

```http
POST /sessions/lecture-1765180222933-e9187c/transcript
Content-Type: application/json
```

```json
{
  "transcriptText": "今日は人工知能について学びます。AIは人間の知能を模倣するシステムです..."
}
```

#### Response

```json
{
  "sessionId": "lecture-1765180222933-e9187c",
  "status": "transcribed"
}
```

> **Note**: このエンドポイントを呼ぶと `status` が `transcribed` に変わり、`endedAt` と `durationSec` が自動計算されます。

---

### PATCH /sessions/{session_id}/notes

録音中のメモを保存・更新します。

#### Request

```http
PATCH /sessions/lecture-1765180222933-e9187c/notes
Content-Type: application/json
```

```json
{
  "notes": "重要ポイント:\n- AI\n- ディープラーニング\n- ニューラルネットワーク"
}
```

#### Response

```json
{
  "sessionId": "lecture-1765180222933-e9187c",
  "ok": true
}
```

> **Note**: メモは要約・クイズ生成時に文字起こしと結合され、LLM に渡されます。

---

### GET /sessions/{session_id}/audio_url

GCS 上の音声ファイルへの署名付き URL (Signed URL) を取得します。

#### Request

```http
GET /sessions/lecture-1765180222933-e9187c/audio_url
```

#### Response

```json
{
  "audioUrl": "https://storage.googleapis.com/classnote-x-audio/sessions/xxx/audio.raw?X-Goog-Algorithm=..."
}
```

> **Note**: URL は 1時間有効です。
> **Audio Format**: iOS からのアップロードは `.m4a` (AAC) が推奨されます。バックエンドは `.m4a` および `.wav` (16kHz) をサポートします。

---

### WebSocket /ws/stream/{session_id}

録音中の音声データをリアルタイムでサーバーにストリーミングします。

#### 接続

```
wss://classnote-api-900324644592.asia-northeast1.run.app/ws/stream/{session_id}
```

#### 認証

クエリパラメータ `token` に Firebase ID Token を付与してください。
UID がセッションの `userId` と一致しない場合、接続は切断されます (Code 4403)。

```
wss://classnote-api-900324644592.asia-northeast1.run.app/ws/stream/{session_id}?token=FIREBASE_ID_TOKEN
```

#### プロトコル

1. **接続後、`start` イベントを送信**:
   ```json
   {"event": "start", "config": {}}
   ```

2. **サーバーから `connected` を受信**:
   ```json
   {"event": "connected"}
   ```

3. **音声データをバイナリで送信** (繰り返し)

4. **接続を閉じる** → サーバーが音声を GCS にアップロード

---

## AI機能

### POST /sessions/{session_id}/summarize

文字起こしから要約を生成します（Gemini 使用）。

#### Request

```http
POST /sessions/lecture-1765180222933-e9187c/summarize
```

#### Response

```json
{
  "sessionId": "lecture-1765180222933-e9187c",
  "summary": "## 講義ノート: 人工知能入門\n\n### 概要\n- AIは人間の知能を模倣するシステム\n- 機械学習はデータから学習する手法\n..."
}
```

| Field | Description |
|-------|-------------|
| `summary` | Markdown 形式の要約テキスト |

> **Note**: 要約は Firestore の `summaryMarkdown` フィールドにも保存されます。

---

### POST /sessions/{session_id}/quiz

文字起こしから小テストを生成します（Gemini 使用）。

#### Request

```http
POST /sessions/lecture-1765180222933-e9187c/quiz?count=5
```

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `count` | int | 5 | 生成する問題数 |

#### Response

```json
{
  "sessionId": "lecture-1765180222933-e9187c",
  "quizMarkdown": "---BEGIN QUIZ---\n### 問題1\nAIとは何の略ですか？\n\n- A. Artificial Intelligence\n- B. Advanced Internet\n- C. Automated Input\n- D. Application Interface\n\n**正解:** A\n**解説:** AIは Artificial Intelligence（人工知能）の略です。\n...\n---END QUIZ---"
}
```

#### クイズ Markdown フォーマット

```markdown
---BEGIN QUIZ---
### 問題1
問題文がここに入ります

- A. 選択肢A
- B. 選択肢B
- C. 選択肢C
- D. 選択肢D

**正解:** A
**解説:** 解説文がここに入ります
---END QUIZ---
```

---

### POST /sessions/{session_id}/diarize

セッションの音声に対して話者分離を実行します（ReazonSpeech + OnlineDiarizer）。

#### Request

```http
POST /sessions/lecture-1765180222933-e9187c/diarize
Content-Type: application/json
```

```json
{
  "force": false  // 既に完了していても再実行する場合は true
}
```

#### Response

```json
{
  "sessionId": "lecture-xxx",
  "status": "done",
  "speakers": [
    {
      "id": "spk_0",
      "label": "A",
      "displayName": "話者A",
      "colorHex": "#FFADAD"
    },
    {
      "id": "spk_1",
      "label": "B",
      "displayName": "話者B",
      "colorHex": "#A0C4FF"
    }
  ],
  "segments": [
    {
      "id": "seg_001",
      "start": 0.0,
      "end": 3.1,
      "speakerId": "spk_0",
      "text": "こんにちは。"
    },
    {
      "id": "seg_002",
      "start": 3.5,
      "end": 6.8,
      "speakerId": "spk_1",
      "text": "はい、よろしく。"
    }
  ],
  "speakerStats": {
    "spk_0": { "totalSec": 12.5, "turns": 5 },
    "spk_1": { "totalSec": 8.2, "turns": 4 }
  }
}
```

> **Note**: 現在はスタブ実装で動作していますが、パラメータやレスポンス形式は最終的なモデルに合わせてあります。

---

## GET /health

ヘルスチェック用エンドポイント。

#### Request

```http
GET /health
```

#### Response

```json
{
  "status": "ok"
}
```

---

## データ型定義

### Session オブジェクト

```typescript
interface Session {
  id: string;
  title: string;
  mode: "lecture" | "meeting";
  userId: string;
  ownerId: string;
  status: "recording" | "recorded" | "transcribed";
  
  // タイムスタンプ
  createdAt: string;    // ISO 8601
  startedAt: string;    // ISO 8601
  endedAt?: string;     // ISO 8601
  durationSec?: number;
  
  // コンテンツ
  transcriptText?: string;
  notes?: string;
  summaryMarkdown?: string;
  quizMarkdown?: string;
  
  // メディア
  audioPath?: string;   // GCS URI (gs://...)
  
  // フラグ（一覧取得時のみ）
  hasSummary?: boolean;
  hasQuiz?: boolean;
}

interface Speaker {
  id: string;
  label: string;
  displayName: string;
  colorHex?: string;
}

interface DiarizedSegment {
  id: string;
  start: number;
  end: number;
  speakerId: string;
  text: string;
}

```

### Request/Response の型

```typescript
// セッション作成
interface CreateSessionRequest {
  title: string;
  mode?: "lecture" | "meeting";
  userId: string;
}

// トランスクリプトアップロード
interface TranscriptUpdateRequest {
  transcriptText: string;
}

// メモ更新
interface NotesUpdateRequest {
  notes: string;
}

// 一括削除
interface BatchDeleteRequest {
  ids: string[];
}

// 話者分離
interface DiarizationRequest {
  force?: boolean;
}

```

---

## エラーハンドリング

### エラーレスポンス形式

```json
{
  "detail": "エラーメッセージ"
}
```

### HTTP ステータスコード

| Code | Description |
|------|-------------|
| 200 | 成功 |
| 400 | リクエスト不正（例: transcript が空） |
| 404 | セッションが見つからない |
| 500 | サーバーエラー（例: Gemini API エラー） |

### よくあるエラー

| Error | Cause | Solution |
|-------|-------|----------|
| `Session not found` | 存在しないセッションID | 正しいセッションIDを使用 |
| `transcriptText is empty` | 文字起こしがアップロードされていない | 先に `/transcript` を呼ぶ |
| `summarizer_error:...` | Gemini API エラー | リトライまたはログ確認 |

---

## レート制限

現在の構成:
- **Cloud Run**: 80 同時リクエスト × 20 インスタンス = 最大 1,600 並列
- **Gemini API**: プロジェクトのクォータに依存（RPM/TPM）

推奨:
- 要約/クイズ生成は **5-10 同時リクエスト** まで安全に処理可能
- レスポンスタイムは Gemini の生成時間に依存（平均 ~9 秒）

---

## iOS 統合例

### Swift での使用例

```swift
// セッション作成
func createSession(title: String, mode: String, userId: String) async throws -> Session {
    let url = URL(string: "\(baseURL)/sessions")!
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    
    let body = ["title": title, "mode": mode, "userId": userId]
    request.httpBody = try JSONEncoder().encode(body)
    
    let (data, _) = try await URLSession.shared.data(for: request)
    return try JSONDecoder().decode(Session.self, from: data)
}

// セッション一覧取得
func fetchSessions(userId: String, kind: String = "all") async throws -> [Session] {
    let url = URL(string: "\(baseURL)/sessions?user_id=\(userId)&kind=\(kind)&limit=50")!
    let (data, _) = try await URLSession.shared.data(from: url)
    return try JSONDecoder().decode([Session].self, from: data)
}

// 要約生成
func generateSummary(sessionId: String) async throws -> String {
    let url = URL(string: "\(baseURL)/sessions/\(sessionId)/summarize")!
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    
    let (data, _) = try await URLSession.shared.data(for: request)
    let response = try JSONDecoder().decode(SummaryResponse.self, from: data)
    return response.summary
}
```
