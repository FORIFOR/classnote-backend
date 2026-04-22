# AI Chat Contract v2  (Phase 7.4 — implemented)

本書は実装済みの `/v1/chat*` 契約の最新版です。docs/ai-chat-implementation.md
(Phase 7 MVP) を**後方互換で上位互換した v2**の契約固定版。クライアント
(Desktop / iOS) は本書の shape で実装して問題ありません。

## TL;DR

| 項目 | v1 (MVP) | v2 (本書・Phase 7.4) |
|---|---|---|
| `request.message` | `string` | `{text: string}` OR `string`（両方受理） |
| `clientContext` | — | **追加** |
| `responseMode` | — | **追加** |
| `idempotencyKey` | — | **追加** |
| `answer` | `{text}` | `{text, blocks[]}` |
| `Citation` | transcript only | discriminated union (transcript / summary_evidence / note / web) |
| `Actions` | suggestedActions chips | **add** discriminated union (jump_to_transcript / save_as_note / create_todo / copy_answer / rewrite_answer) |
| `confidence` | — | `"high" | "medium" | "low"` |
| `intent` | — | rule-based label |
| SSE events | meta / token / done / error | + status / delta / citation / action / message |
| Conversation CRUD | — | `POST GET /v1/chat/conversations`, `GET /v1/chat/conversations/{id}` |
| `chat_traces` collection | — | 追加（診断・評価用） |

既存の v1 shape は全フィールドそのまま受理・返却されます。クライアントは
段階的に `blocks[]` / `actions[]` / `confidence` を使うよう移行してください。

---

## 1. Endpoints

```
POST  /v1/chat                                    — one-shot
POST  /v1/chat:stream                             — SSE
GET   /v1/chat/presets                            — preset catalog
POST  /v1/chat/conversations                      — create conversation
GET   /v1/chat/conversations?scope=&sessionId=    — list
GET   /v1/chat/conversations/{conversationId}     — metadata + messages page
GET   /v1/sessions/{sessionId}/chat/conversations/{conversationId}/messages
GET   /v1/chat/conversations/{conversationId}/messages
```

## 2. `POST /v1/chat` request

```jsonc
{
  "conversationId": "conv_xxx",            // optional, auto-minted if absent
  "scope": {
    "type": "session",                     // "session" | "general" | "multi_session" | "overlay_live"
    "sessionId": "sess_xxx",               // required for session scope
    "sessionIds": ["sess_a", "sess_b"]     // multi_session only
  },
  "message": { "text": "..." },            // v2 shape. Legacy plain string still accepted.
  "clientContext": {
    "surface": "desktop_session_detail",
    "activeTab": "overview",
    "selectedText": null,
    "selectedEvidenceId": null,
    "selectedSegmentId": null,
    "currentPlaybackMs": 512000
  },
  "responseMode": "default",               // "default" | "concise" | "structured" | "rewrite" | "coaching"
  "preset": "summarize",                   // optional preset chip
  "idempotencyKey": "chat-20260417-0001",
  "history": [                             // optional client fallback; server-side conversation doc wins
    { "role": "user", "text": "..." },
    { "role": "assistant", "text": "..." }
  ],
  "selectedContext": { "tab": "overview" } // legacy alias of clientContext (still accepted)
}
```

## 3. `POST /v1/chat` response

```jsonc
{
  "conversationId": "conv_xxx",
  "scope": { "type": "session", "sessionId": "sess_xxx" },
  "preset": "summarize",
  "mode": "session_grounded",              // session_grounded | session_plus_general | general_static | general_fresh
  "usedModel": "gemini-2.0-flash-lite",
  "intent": "session_qa",                  // rule-based; used for UI badges
  "confidence": "high",                    // "high" | "medium" | "low"

  "answer": {
    "text": "plaintext of the full answer",            // v1 clients still use this
    "blocks": [                                         // v2 clients render these
      { "type": "paragraph", "text": "..." },
      { "type": "bullet_list", "items": ["...","..."] }
    ]
  },

  "citations": [                            // always an array (empty for general scope)
    {
      "type": "transcript",
      "segmentId": "seg_42",
      "startMs": 532000,
      "endMs": 538000,
      "speaker": "山田",
      "quotePreview": "じゃあ火曜までに見積もりを…",
      "score": 0.81
    }
  ],

  "actions": [                              // payload-ed, discriminated union
    { "type": "jump_to_transcript", "targetMs": 532000, "segmentId": "seg_42" },
    { "type": "save_as_note", "payload": { "text": "..." } },
    { "type": "create_todo",   "payload": { "text": "見積もりを火曜までに提出する" } },
    { "type": "copy_answer" },
    { "type": "rewrite_answer", "mode": "slack" }
  ],

  "creditCost": 1,
  "creditsRemaining": 399,
  "latencyMs": 1820,

  "suggestedActions": [                     // v1-compat preset chips (complementary to actions)
    { "id": "extract_todos", "label": "TODOを抽出" }
  ]
}
```

### AnswerBlock shape

```ts
type AnswerBlock =
  | { type: "paragraph"; text: string }
  | { type: "bullet_list"; items: string[] }
  | { type: "numbered_list"; items: string[] }
  | { type: "section"; title: string; body: string }
  | { type: "warning"; text: string }
```

Blocks は backend 側で plaintext から heuristic に構築します:
- `・`, `-`, `•`, `*` で始まる連続行 → `bullet_list`
- `1. `, `2. ` で始まる連続行 → `numbered_list`
- `## ` / `### ` → `section`
- それ以外の空行区切り → `paragraph`

LLM から JSON 直出力に切り替える時期 (Phase 7.5+) でも shape は変わりません。

### Citation union

```ts
type Citation =
  | TranscriptCitation
  | SummaryEvidenceCitation      // Phase 7.6+
  | NoteCitation                 // Phase 7.6+
  | WebCitation                  // Phase 7.7+

type TranscriptCitation = {
  type: "transcript";
  segmentId?: string;
  startMs?: number; endMs?: number;
  speaker?: string;
  quotePreview?: string;
  score?: number;
}
```

MVP 実装では `type: "transcript"` のみ発行。契約上は上記全 kind が来るとして
クライアントを書いてください（未知 kind は捨てる）。

### Actions union

```ts
type ChatAction =
  | { type: "jump_to_transcript"; targetMs: number; segmentId?: string }
  | { type: "save_as_note"; payload: { text: string } }
  | { type: "create_todo"; payload: { text: string; owner?: string; due?: string } }
  | { type: "copy_answer" }
  | { type: "rewrite_answer"; mode: "slack" | "email" | "summary" }
```

`POST /v1/chat/actions` (Phase 7.5) で execute できます。

## 4. `POST /v1/chat:stream` — SSE events

v2 contract では以下の順で events を emit します:

```
event: status            { "phase": "routing" }
event: status            { "phase": "retrieving" }   (session scope のみ)
event: status            { "phase": "generating" }
event: meta              { conversationId, messageId, scope, preset, intent, mode, usedModel,
                           creditCost, creditsRemaining }
event: delta             { "text": "chunk" }        (v2)
event: token             { "text": "chunk" }        (v1-compat、同時 emit)
event: citation          { "citation": { ... } }    (stream 末尾で per-citation)
event: action            { "action": { ... } }
event: message           { "message": { messageId, answer:{text,blocks},
                                         citations, actions, confidence } }
event: done              { conversationId, messageId, answer, citations, actions,
                           confidence, intent, creditCost, creditsRemaining,
                           latencyMs, suggestedActions }
event: error             { code, message, details? }   (mid-stream failure)
```

Pre-stream の 401 / 403 / 404 / 422 / 429 は通常の HTTP エラー応答です。
mid-stream の LLM 失敗は `event: error` + credit 自動 refund。

## 5. Conversation CRUD

### `POST /v1/chat/conversations`

```jsonc
// request
{
  "scope": { "type": "session", "sessionId": "sess_xxx" },
  "surface": "desktop_session_detail",
  "title": "4月定例のAIチャット"
}
// response
{
  "conversation": {
    "conversationId": "conv_xxx",
    "scope": { "type": "session", "sessionId": "sess_xxx" },
    "ownerAccountId": "acct_xxx",
    "surface": "desktop_session_detail",
    "title": "4月定例のAIチャット",
    "messageCount": 0,
    "archived": false,
    "createdAt": null,                // serverTimestamp; resolve after refetch
    "updatedAt": null
  }
}
```

### `GET /v1/chat/conversations?scope=session&sessionId=sess_xxx`

```jsonc
{
  "conversations": [
    { "conversationId": "conv_xxx", "scope": {...}, "messageCount": 4, ... },
    ...
  ],
  "nextCursor": null
}
```

### `GET /v1/chat/conversations/{conversationId}?sessionId=sess_xxx&limit=50&before=<clientSortKey>`

```jsonc
{
  "conversation": { ...ConversationSummary... },
  "messages": [
    {
      "messageId": "msg_xxx",
      "role": "user",
      "text": "...",
      "citations": [], "actions": [],
      "createdAt": "2026-04-17T10:01:00Z",
      "clientSortKey": 1744880460123
    },
    {
      "messageId": "msg_xxx",
      "role": "assistant",
      "text": "...",
      "citations": [...], "actions": [...],
      "mode": "session_grounded",
      "usedModel": "gemini-2.0-flash-lite",
      "createdAt": "2026-04-17T10:01:02Z",
      "clientSortKey": 1744880462456
    }
  ],
  "nextCursor": 1744880460123       // clientSortKey of oldest message in this page
}
```

`sessionId` 未指定のときは account-scope (general / multi_session / overlay_live)
の conversation を取得します。

## 6. Firestore schema

### chat_traces (root-level)

`POST /v1/chat` および `/v1/chat:stream` の assistant ターンごとに 1 doc 書かれる
診断ログ。admin dashboard からクロスセッションで集計する想定:

```jsonc
/chat_traces/{traceId}
  id, conversationId, sessionId?, userId, accountId,
  routing: {
    scopeType, intent, modeLabel,
    usedWeb, usedSummary,
    preset, responseMode
  },
  clientContext: { surface, activeTab, selected*, currentPlaybackMs } | null,
  citations: [ { kind, segmentId, startMs, endMs, score } ],   // capped at 10
  citationCount: int,
  usedModel: string,
  latencyMs: { total: int },
  createdAt: serverTimestamp
```

### Conversation (Phase 7.3 sub-collection)

```
sessions/{sid}/conversations/{cid}
sessions/{sid}/conversations/{cid}/messages/{messageId}

accounts/{aid}/conversations/{cid}           (general / multi_session / overlay_live)
accounts/{aid}/conversations/{cid}/messages/{messageId}
```

Conversation doc fields:
```jsonc
conversationId, scope, ownerAccountId, schemaVersion=2,
surface?, title?, messageCount (Increment), archived=false,
createdAt, updatedAt, lastMessageAt
```

Message doc fields:
```jsonc
messageId, conversationId, role, text, createdAt, clientSortKey,
authorUid?, authorAccountId?,    # user
citations[]?, mode?, usedModel?  # assistant
```

## 7. Error envelope

すべてのエラー応答は以下の shape で返します:

```jsonc
{
  "error": {
    "code": "SESSION_NOT_FOUND" | "PERMISSION_DENIED" | "INSUFFICIENT_CREDITS"
          | "SCOPE_INVALID" | "EMPTY_QUERY" | "CONVERSATION_NOT_FOUND"
          | "CHAT_ERROR" | "INTERNAL_ERROR",
    "message": "human / i18n-able",
    "retryable": false,
    "retryAfter": null,
    "details": { ... }
  }
}
```

## 8. Credits policy

```
session + !web  →  session_grounded      (cost 1)
session +  web  →  session_plus_general  (cost 2)
general + !web  →  general_static        (cost 2)
general +  web  →  general_fresh         (cost 5)
multi_session   →  session_grounded × N  (Phase 7.7 で単独計算になる予定)
overlay_live    →  session_grounded      (Phase 7.7)
```

LLM 失敗時は `ai_credits.refund` が自動で呼ばれてから `error` が返ります。

## 9. Phase 対応表

| Phase | 範囲 | 状態 |
|---|---|---|
| 7 (MVP) | /v1/chat non-stream, /v1/chat/presets, sub-collection 書き込み | ✅ |
| 7.2 | /v1/chat:stream SSE | ✅ |
| 7.3 | conversation sub-collection (concurrent-safe) | ✅ |
| **7.4** | **v2 contract: blocks / actions union / clientContext / conversations CRUD / chat_traces** | ✅ 本書 |
| 7.5 | `POST /v1/chat/actions` — save_as_note / create_todo / copy_answer / rewrite_answer | 🟡 次 PR |
| 7.6 | Hybrid retrieval (keyword + vector + reranker), transcript_embeddings, summary_evidence_index | 🟡 |
| 7.7 | multi_session / overlay_live / web grounded (native) | 🟡 |

## 10. Desktop / iOS クライアント契約 (最重要)

1. `message` は **`{text}` で送る**。既存の `string` もサポートしているが、v2 以降は wrapped 形式を推奨。
2. `response.answer.blocks[]` をレンダリング主体にする（`response.answer.text` はフォールバック専用）。
3. `response.citations[]` は常に配列。`type` で分岐してチップ表示。
4. `response.actions[]` を **ボタン列**として描画。各ボタンは tap 時に:
   - `jump_to_transcript` → Transcript タブへ seek(`targetMs`)
   - `save_as_note` → `POST /v1/chat/actions` に forward (Phase 7.5)
   - `create_todo` → 同上
   - `copy_answer` → clipboard
   - `rewrite_answer` → 次の `/v1/chat` を `responseMode=rewrite` + `preset=short_share` で叩く
5. `confidence` で UI にバッジ:
   - `high` → 強調なし
   - `medium` → 「要確認」バッジ
   - `low` → 「推測に近い」バッジ + citation タップを促す
6. Streaming 時は `status` / `meta` / `delta` / `message` 4 種で十分 UI を構成できる（`citation` / `action` は optional; 最終的に `message` / `done` に含まれる）。
7. `clientContext.currentPlaybackMs` は毎リクエスト送る（retrieval の時系列 filter に使う、Phase 7.6 で活用予定）。
8. `idempotencyKey` は **ボタン連打対策**として client 側で UUID を付与して送る。
