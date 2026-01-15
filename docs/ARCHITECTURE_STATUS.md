# ClassnoteX アーキテクチャ実装状況

**更新日**: 2025-12-11  
**ステータス**: ✅ 提案された最適化構成の大部分は実装済み

---

## 1. 実装状況サマリ

| 項目 | ステータス | 備考 |
|------|----------|------|
| `/summarize` 非同期化 (202 + Cloud Tasks) | ✅ 完了 | Transaction 付き冪等性ガードも実装済み |
| `/transcript` で segments 受信 | ✅ 完了 | iOS オンデバイス STT 対応 |
| Cloud Tasks キュー設定 | ✅ 完了 | `summarize-queue` |
| LLM ワーカー (要約/クイズ/ハイライト) | ✅ 完了 | `/internal/tasks/*` |
| Playlist 生成 (非同期) | ✅ 完了 | Cloud Tasks 経由 |
| クライアントポーリング (`refresh_playlist`) | ✅ 完了 | - |
| Firestore AsyncIO | ⏳ 未着手 | 将来課題 |

---

## 2. 現在のアーキテクチャ

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         iOS Client (オンデバイス完結)                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐          │
│  │ Whisper STT     │→ │ 話者分離        │→ │ ローカル保存    │          │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘          │
│           ↓ 録音完了時                                                   │
│  POST /sessions/{id}/transcript                                          │
│    { transcriptText, segments[], source: "device" }                      │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                        Cloud Run (classnote-api)                         │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ POST /sessions/{id}/summarize                                      │ │
│  │   1. Transaction でステータス確認                                  │ │
│  │   2. completed なら即座にキャッシュ返却                            │ │
│  │   3. running/queued なら 202 (何もしない)                          │ │
│  │   4. それ以外 → Cloud Tasks に enqueue → 202 Accepted              │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                    │                                     │
│                                    ▼                                     │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Cloud Tasks (summarize-queue)                                      │ │
│  │   dispatch_deadline: 1800s (30分)                                  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                    │                                     │
│                                    ▼                                     │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ POST /internal/tasks/summarize                                     │ │
│  │   1. Firestore から transcriptText 取得                           │ │
│  │   2. Vertex AI Gemini で要約生成                                   │ │
│  │   3. summaryStatus = "completed" + summaryMarkdown 保存            │ │
│  │   4. 失敗時: summaryStatus = "failed" + summaryError               │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. コスト最適化の実現状況

### iOS セッション (STT/diar コスト = $0)

| フェーズ | 実行場所 | 実装状況 |
|---------|---------|---------|
| 音声録音 | iOS | ✅ |
| 文字起こし (Whisper) | iOS | ✅ クライアント側 |
| 話者分離 | iOS | ✅ クライアント側 |
| transcript + segments アップロード | Cloud | ✅ 実装済み |
| 要約生成 (Gemini) | Cloud | ✅ Cloud Tasks 非同期 |

**結果**: iOS 録音 1 本あたりの課金は **Gemini のみ** (~$0.01/本)

### Web セッション (クラウド STT 使用)

| フェーズ | 実行場所 | 実装状況 |
|---------|---------|---------|
| WebSocket STT | Cloud (Speech API) | ✅ `/ws/stream/{id}` |
| 話者分離 | Cloud | ⏳ スタブ実装 |
| 要約生成 | Cloud | ✅ Cloud Tasks 非同期 |

---

## 4. API エンドポイント一覧

### 4.1 Transcript (iOS オンデバイス対応)

```http
POST /sessions/{session_id}/transcript
Content-Type: application/json

{
  "transcriptText": "会議の全文...",
  "segments": [
    { "startSec": 0.0, "endSec": 5.2, "speakerId": "A", "text": "今日の議題は..." },
    { "startSec": 5.3, "endSec": 10.1, "speakerId": "B", "text": "了解です" }
  ],
  "source": "device"
}
```

**レスポンス**:
```json
{ "sessionId": "xxx", "status": "transcribed", "source": "device" }
```

### 4.2 Summarize (非同期)

```http
POST /sessions/{session_id}/summarize
```

**レスポンス (即座に返却)**:
```json
// まだ要約がない場合
{ "sessionId": "xxx", "status": "running" }  // HTTP 202

// 既に完了している場合
{ "sessionId": "xxx", "status": "completed", "summary": "..." }  // HTTP 200
```

### 4.3 Polling (クライアント側)

```http
GET /sessions/{session_id}
```

**レスポンスに含まれるフィールド**:
- `summaryStatus`: `"pending"` | `"running"` | `"completed"` | `"failed"`
- `summaryMarkdown`: 完了時の要約テキスト
- `summaryError`: 失敗時のエラーメッセージ

---

## 5. iOS クライアント実装例

### 5.1 録音完了後の同期

```swift
func finalizeSession(sessionId: String,
                     transcript: String,
                     segments: [DiarizedSegment]) async throws {
    let request = TranscriptUploadRequest(
        transcriptText: transcript,
        segments: segments,
        source: "device"
    )
    
    try await api.uploadTranscript(sessionId: sessionId, request: request)
    print("[STT] ✅ Transcript uploaded from device")
}
```

### 5.2 要約リクエスト + ポーリング

```swift
func requestSummary(sessionId: String) async throws -> String {
    // 1. 要約リクエスト (すぐ返る)
    let result = try await api.requestSummary(sessionId: sessionId)
    
    if result.status == "completed", let summary = result.summary {
        return summary
    }
    
    // 2. ポーリング (1秒間隔)
    while true {
        try await Task.sleep(nanoseconds: 1_000_000_000)
        let session = try await api.getSession(sessionId: sessionId)
        
        switch session.summaryStatus {
        case "completed":
            return session.summaryMarkdown ?? ""
        case "failed":
            throw SummaryError.failed(session.summaryError ?? "Unknown error")
        default:
            continue  // pending / running
        }
    }
}
```

---

## 6. 残課題

| 優先度 | 項目 | 説明 |
|--------|------|------|
| 中 | Firestore AsyncIO | 高負荷時のイベントループブロック軽減 |
| 低 | Web 話者分離の実装 | 現在スタブ。pyannote.audio 等を検討 |
| 低 | Whisper コンテナ (Web用) | Speech API の代替でコスト削減 |

---

## 7. コスト比較

| 構成 | 1,000 時間/月 | 備考 |
|------|-------------|------|
| **現状 (iOS オンデバイス)** | **~$200** | Gemini のみ |
| 従来 (クラウド STT + diar) | ~$4,500 | Speech API + Gemini |
| Web 用 Whisper 構成 | ~$300 | Cloud Run Jobs + Gemini |

**結論**: iOS オンデバイス STT により、STT/話者分離コストは **ゼロ** になりました。
