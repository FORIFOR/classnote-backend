# AI Chat Implementation (Phase 7)

Session-first AI chat for SessionDetail right panel (Desktop) and sheet (iOS).

## TL;DR

- **New endpoint**: `POST /v1/chat` with explicit `scope: {type:"session"|"general", sessionId?}`
- Reuses all 6 existing layers (`chat_router`, `context_builder`, `anchor_resolver`, `gemini_chat`, `gemini_stream`, `ai_credits`) — no rewrite
- Adds the missing pieces: **Tool Runner lite (6 presets)**, **Citation in response**, **Per-session conversation persistence**
- Legacy `/v1/chat/send` and `/v1/chat/stream` untouched
- MVP = non-stream. SSE variant is planned for Phase 7.2.

## 7-layer design → existing code map

| Design layer | Existing module | Reused in `session_chat.py` |
|---|---|---|
| Intent Router | `app/services/chat_router.py:classify_route` | `chat_once` step 1 |
| Context Builder | `app/services/context_builder.py:build_session_context` / `build_turn_prompt` / `build_hybrid_prompt` | step 2-4 |
| Retrieval | `context_builder._extract_relevant_portions` + `transcript_chunks` read | step 3 |
| Tool Runner | — (new: `_PRESETS` dict) | step 4 (preset merge into message) |
| LLM Orchestrator | `gemini_chat.call_gemini_chat` / `call_gemini_search_hybrid` / `call_gemini_general_chat` / `call_gemini_general_with_search` | step 6 |
| Citation Builder | `anchor_resolver.find_best_segments` + `normalize_segments` | step 7 (`_build_citations`) |
| Response Streamer | `gemini_stream.stream_gemini_chat` | Phase 7.2 (not in MVP) |

## API contract

### `POST /v1/chat`

Request:
```jsonc
{
  "scope": { "type": "session", "sessionId": "sess_xxx" },
  "message": "この会議の決定事項を3つにまとめて",
  "preset": "summarize",          // optional, overrides / augments message
  "conversationId": "conv_xxx",   // optional, creates new if omitted
  "history": [                    // optional, client-supplied fallback; server conversation doc takes precedence
    { "role": "user", "text": "..." },
    { "role": "assistant", "text": "..." }
  ],
  "selectedContext": {            // optional: where the user clicked "ask AI"
    "tab": "overview",
    "evidenceId": "ev_1",
    "quote": "...",
    "segmentId": "seg_42",
    "startMs": 120000
  }
}
```

Response:
```jsonc
{
  "conversationId": "conv_xxx",
  "scope": { "type": "session", "sessionId": "sess_xxx" },
  "preset": "summarize",
  "mode": "session_grounded",
  "usedModel": "gemini-...",
  "answer": { "text": "・...\n・...\n・..." },
  "citations": [
    {
      "type": "transcript",
      "segmentId": "ch_42",
      "startMs": 532000,
      "endMs": 538000,
      "speaker": "山田",
      "quotePreview": "じゃあこれで決まり…",
      "score": 0.81
    }
  ],
  "creditCost": 1,
  "creditsRemaining": 399,
  "latencyMs": 1820,
  "suggestedActions": [
    { "id": "extract_todos",     "label": "TODOを抽出" },
    { "id": "extract_decisions", "label": "決定事項を抽出" },
    { "id": "next_agenda",       "label": "次回アジェンダ案" }
  ]
}
```

Error envelope (統一):
```jsonc
{
  "error": {
    "code": "SESSION_NOT_FOUND" | "PERMISSION_DENIED" | "INSUFFICIENT_CREDITS"
          | "SCOPE_INVALID" | "EMPTY_QUERY" | "CHAT_ERROR" | "INTERNAL_ERROR",
    "message": "...",
    "retryable": false,
    "details": {}
  }
}
```

### `GET /v1/chat/presets`

プリセットカタログ。クライアントはチップ表示用に使う。

```jsonc
[
  { "id": "summarize",         "label": "要点を要約" },
  { "id": "extract_todos",     "label": "TODOを抽出" },
  { "id": "extract_decisions", "label": "決定事項を抽出" },
  { "id": "next_agenda",       "label": "次回アジェンダ案" },
  { "id": "short_share",       "label": "Slack用に短く" },
  { "id": "quiz_questions",    "label": "理解度チェックを作る" }
]
```

## Firestore schema

### Conversation docs (Phase 7.3 — sub-collection form)

```
sessions/{sessionId}/conversations/{conversationId}
  conversationId  : string
  scope           : { type, sessionId }
  ownerAccountId  : string
  schemaVersion   : 2
  createdAt, updatedAt, lastMessageAt  : serverTimestamp
  messageCount    : number (Firestore.Increment)
  # NO `messages` array. Each message is its own doc below.

sessions/{sessionId}/conversations/{conversationId}/messages/{messageId}
  messageId       : string (= Firestore doc id)
  conversationId  : string
  role            : "user" | "assistant"
  text            : string
  createdAt       : serverTimestamp
  clientSortKey   : number (monotonic ms — stable sort before serverTs resolves)
  authorUid?      : string        # user messages only
  authorAccountId?: string        # user messages only
  citations?      : [...]         # assistant messages only
  mode?           : string        # assistant messages only
  usedModel?      : string        # assistant messages only

accounts/{accountId}/conversations/{conversationId}       # general scope
accounts/{accountId}/conversations/{conversationId}/messages/{messageId}
  (same shape, scope.type = "general", no scope.sessionId)
```

**Why sub-collection**: each message is an independent Firestore doc, so
concurrent appends (multi-tab / multi-device / server-side retry) are
conflict-free. The previous MVP stored the whole array under one doc and
used read-modify-write, which lost updates under concurrency.

**Writes are batched**: `chat_once` / `chat_stream` commit *two* message
docs (user + assistant) plus the parent metadata update in a single
`db.batch()` so a crash between them can't leave a half-written turn.

**Sort order**: LLM context load does
`messages.order_by(createdAt DESC).limit(12)` then reverses to
chronological. If two docs land with identical `serverTimestamp`,
`clientSortKey` (wall-clock ms at write time, +1 for the assistant reply)
gives a stable tie-breaker.

**Listener pattern**: clients may subscribe directly to
`sessions/{id}/conversations/{cid}/messages` (rules: Phase 3) and append
UI rows as messages arrive. This is how the Desktop / iOS chat view stays
live without polling.

### Retrieval endpoints (Phase 7.3)

```
GET /v1/sessions/{sessionId}/chat/conversations/{conversationId}/messages
  ?limit=50&before=<clientSortKey>
    → { conversationId, scope, messages: [...], nextCursor }

GET /v1/chat/conversations/{conversationId}/messages   # general scope
  ?limit=50&before=<clientSortKey>
    → same shape
```

- Messages returned in **reverse-chronological order**.
- `nextCursor` = last message's `clientSortKey` when a full page was
  returned; pass it as `before=` for the next older page.
- `limit` capped at 200.
- Permission: session scope honors `compute_permissions` owner / shared;
  general scope requires the caller's accountId to own the parent path.

## Credits policy

`session_chat.chat_once` calls `ai_credits.consume` **once** per turn, with
`mode` derived from routing:

| scope | needs_web | mode | cost |
|---|---|---|---|
| session | false | `session_grounded` | 1 |
| session | true  | `session_plus_general` | 2 |
| general | false | `general_static` | 2 |
| general | true  | `general_fresh` | 5 |

Failure path refunds: on LLM exception, `ai_credits.refund` is called before
raising `ChatError`. `CreditLimitError` maps to HTTP 429.

## Permissions

`session_chat._load_session` calls `session_projection.compute_permissions`
(from PR #2) for authorization. `canView=False` → 403. This is the same
single source of truth used by `/v1/session-details/*`. No duplicate ACL
logic.

## Citation construction

For session scope:
1. Load all `transcript_chunks` (capped 500 chunks).
2. Wrap as `EvidenceRef`-shaped segments (`anchor_resolver.normalize_segments`).
3. `find_best_segments(answer, segments, top_k=5)` — char-bigram scoring.
4. Drop scores < 0.15.
5. Return `citations: []` (empty array, never null) for general scope.

**Phase 7.4 (planned)**: replace char-bigram with embedding similarity via
Vertex AI `text-embedding-*`. Current implementation is deterministic and
fast; good enough for MVP.

## Client integration

### Desktop (deepnote-desktop)

```ts
// Right-panel in SessionDetailScreen
const { data } = useQuery(
  ['chat-presets'],
  () => api.get('/v1/chat/presets'),
  { staleTime: Infinity }
)

async function sendChat(message: string, preset?: string) {
  return await api.post('/v1/chat', {
    scope: { type: 'session', sessionId },
    message,
    preset,
    conversationId: conversation.id ?? undefined,
  })
}
```

Store `response.conversationId` in local state on first turn; send it back
on subsequent turns.

### iOS (ClassnoteX)

```swift
// SessionDetail sheet
struct ChatResponse: Decodable {
    let conversationId: String
    let answer: Answer
    let citations: [Citation]
    let creditsRemaining: Int?
    let suggestedActions: [SuggestedAction]
}
```

For SummaryV2 / Quiz jump-to: tap citation → navigate to Transcript tab and
seek to `startMs`. The `segmentId` matches `transcript_chunks` subcollection
keys returned by `/v1/session-details/{id}/transcript`.

## Out of scope for MVP (planned)

- ~~**Phase 7.2**: SSE variant `POST /v1/chat:stream` (reuse `gemini_stream`)~~ ✅ 完了
  - Event order: `meta` → `token*` → `done`  (error 時は途中で `error` event)
  - Client should append `token.data.text` until `done` arrives.
  - `done.data.citations` は stream 終了後に非同期で構築された結果。
  - LLM 失敗時は credits 自動 refund + `event:error`。
- ~~**Phase 7.3**: conversation as sub-collection (solve concurrent append)~~ ✅ 完了
  - Messages は `conversations/{cid}/messages/{messageId}` の独立 doc
  - batched write で user + assistant + parent metadata を atomic に更新
  - `GET /v1/sessions/{sid}/chat/conversations/{cid}/messages` と
    `GET /v1/chat/conversations/{cid}/messages` が pagination 対応
  - 並列 append 時の lost update 問題は解消済み
  - clients は listener で messages sub-collection を直接 subscribe 可能 (rules は Phase 3 で許可済み)
- **Phase 7.4**: embedding-based retrieval via Vertex AI
- **Phase 7.5**: explicit Tool Runner with function-calling (jump_to_timestamp,
  insert_into_notes, generate_quiz_from_session as server-side tools)
- **Phase 7.6**: cross-session retrieval (ask over last 10 sessions)
- **Phase 7.7**: overlay-specific presets (quick stream during recording)

## Testing checklist

- [ ] `POST /v1/chat` with `scope.type="session"` and no `sessionId` → 422 SCOPE_INVALID
- [ ] `POST /v1/chat` with empty message and no preset → 422 EMPTY_QUERY
- [ ] `POST /v1/chat` with another user's sessionId → 403 PERMISSION_DENIED
- [ ] `POST /v1/chat` with `preset="summarize"` on a real session → answer
      contains bullets, citations.length ≥ 1, `mode="session_grounded"`
- [ ] Credit-exhausted user → 429 INSUFFICIENT_CREDITS
- [ ] LLM 500 on Vertex → credits auto-refunded, 500 CHAT_ERROR returned
- [ ] Conversation continuity: 2nd turn with same `conversationId` → history
      is reloaded from Firestore, LLM sees previous turn
- [ ] General scope (`scope.type="general"`) → `citations: []`, conversation
      saved under `accounts/{accountId}/conversations/{id}`
- [ ] `GET /v1/chat/presets` → 6 preset items in stable order
