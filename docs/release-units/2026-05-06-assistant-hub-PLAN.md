# Release Unit Plan — DeepNote Assistant Hub (Phase A)

- **Branch**: `fix/bot-prod-snapshot-and-summary-429` (continuation)
- **Goal**: Move from per-channel command parsers to a central
  Assistant Hub that exposes ``POST /v1/assistant/messages`` and is
  consumed by iOS / Desktop / Slack / LINE through one contract.
- **Why**: each surface (LINE, Slack, iOS, Desktop) currently re-implements
  intent classification, response formatting, and access checks. As we
  add scheduled tasks, confirmation-card share, and Q&A, the duplication
  becomes a correctness hazard (e.g. each surface has its own
  ``_classify_command``; commands drift between LINE/Slack today).
  Centralising also lets us govern LLM cost in one place and audit every
  user-driven action through one log.

## Target architecture

```
 iOS / Desktop / Slack / LINE / Email
                │
                ▼
        ┌─────────────────────┐
        │  Assistant Hub      │  POST /v1/assistant/messages
        │   - Intent Router   │  POST /v1/assistant/actions
        │   - Tool Executor   │  GET  /v1/assistant/conversations/{id}
        └─────────────────────┘
                │
                ▼
   ┌─────────────────────────────────────┐
   │ DeepNote Core APIs                  │
   │  sessions / artifacts / jobs /      │
   │  exports / shares / scheduled-tasks │
   └─────────────────────────────────────┘
```

**Routing strategy** (cost-aware):
1. **Slash / explicit verbs first** (``/pdf``, ``/todo``, ``decision``)
   - rules-only, zero LLM cost
2. **Structured-data lookup** for known questions
   - 「決定事項」 → ``decisions`` array directly
   - 「TODO」 → ``todos`` collection directly
3. **Lightweight extraction** (Gemini Flash Lite) only for free-form
   questions like "田中さんが担当のTODOは？" with structured data as
   context, never the full transcript by default
4. **Full transcript LLM** only when the question is unanswerable from
   summary + decisions + todos AND the cost-guard reservation succeeds

## Scope (Phase A — this commit)

### Files allowed
- `app/services/assistant_hub.py` (new) — intent router + tool executor
- `app/services/assistant_qna.py` (new) — Q&A engine with grounded answers
- `app/routes/assistant.py` (new) — REST surface
- `app/util_models.py` — request / response Pydantic models
- `app/main.py` — register assistant router
- `docs/release-units/2026-05-06-assistant-hub-PLAN.md` — this file

### Files NOT allowed (out of scope for Phase A)
- `app/routes/integrations_line.py`, `integrations_slack.py` — bot
  surfaces will adopt the hub in **Phase B**, not this commit.
  Existing classifier/handlers stay as-is.
- iOS / Desktop client code
- Scheduled task / confirmation card / Slack Block Kit

## API contract — Phase A

### `POST /v1/assistant/messages`
Single Q&A request. Idempotent on ``idempotencyKey``.

```json
Request:
{
  "sessionId": "lecture-...",          // optional; defaults to latest
  "question": "この会議で田中さん担当のTODOは？",
  "mode": "session" | "general",       // default "session"
  "channel": "ios" | "desktop" | "slack" | "line",  // for audit
  "idempotencyKey": "..."              // optional, dedupe across retries
}

Response:
{
  "messageId": "msg_...",
  "intent": "ask_session_todo",
  "answer": "田中さんの担当TODOは2件です。\n1. 見積書の再提出 (期限: 5/10)\n2. 顧客資料の修正 (期限: 未設定)",
  "citations": [
    {"type": "todo", "id": "todo_abc", "snippet": "見積書の再提出"},
    {"type": "todo", "id": "todo_def", "snippet": "顧客資料の修正"}
  ],
  "sessionId": "lecture-...",
  "tokenUsage": {"prompt": 0, "completion": 0},  // 0 when rules-only
  "createdAt": "2026-05-06T05:00:00Z"
}
```

**Failure modes**:
- 401 / 403: not the session owner / no view access
- 404: session not found and no fallback latest
- 422: question empty
- 503: LLM transient (retry with backoff via existing `_generate_with_retry`)

### `GET /v1/assistant/conversations/{id}` (Phase A: stub)
Returns prior messages in the conversation. Phase A: returns 200 +
empty list so iOS code can call it without 404; full conversation
history persisted in Phase B.

### `POST /v1/assistant/actions` (Phase B)
Executes a hub action (export PDF, share to channel, schedule task,
etc.). Phase A returns 501 Not Implemented.

## Storage

```
assistant_conversations/{conversationId}                 (Phase B)
  messages/{messageId}                                   (Phase B)
    accountId, channel, source, text, intent,
    sessionId, status, createdAt

assistant_messages/{messageId}                           (Phase A)
  accountId, sessionId, question, answer, intent,
  citations, channel, idempotencyKey, createdAt
```

Phase A flat collection ``assistant_messages`` is enough for audit /
cost-tracking. Phase B will add the conversation tree.

## Intent taxonomy (Phase A)

| Intent | Routing | LLM? |
|---|---|---|
| ``help`` | rules → static text | no |
| ``ask_session_decision`` | direct read of ``decisions[]`` | no |
| ``ask_session_todo`` | direct read of ``todos`` collection | no |
| ``ask_session_summary`` | direct return of ``summaryMarkdown`` | no |
| ``ask_session_freeform`` | Gemini Flash Lite + summary/decisions/todos context | yes (small) |
| ``ask_general`` | Gemini Flash Lite, no session context | yes (small) |
| ``unknown`` | Echo "I'm not sure" + suggest help | no |

For Phase A we ship: ``help / ask_session_decision / ask_session_todo /
ask_session_summary / ask_session_freeform``. ``ask_general`` lives
behind an env-var flag (off by default).

## Acceptance criteria

1. `python -m py_compile` for new files passes
2. `POST /v1/assistant/messages` with master token + a valid sessionId
   answers "決定事項は？" with the actual ``decisions[]`` array, no LLM call
3. Same endpoint, "TODO は？" returns the session's ``todos`` rows
4. Same endpoint, "要約して" returns ``summaryMarkdown``
5. Free-form question hits Gemini ONCE (verified via existing profiler
   ``llm_request`` phase) and the answer mentions a citation source
6. Cost guard: free-form requests are gated by existing
   ``cost_guard.guard_can_consume("summary_generated", 0)`` (zero cost
   probe — falls back gracefully if quota gone)
7. Master pre-deploy test (Step 4-14) passes after deploy

## Risks / mitigations

| Risk | Mitigation |
|---|---|
| LLM cost spike from chatty users | Rules-first router; freeform LLM only fires when structured data can't satisfy; existing 429 retry + cost_guard apply |
| Cross-account leakage | Endpoint runs through ``ensure_can_view``; sessions are scoped to ``current_user.uid`` |
| Citation hallucination | Phase A: only structured-data citations (no transcript line numbers yet). Phase B will add transcript span citations |
| iOS / Desktop existing endpoints break | New routes only, no existing route shape changes |

## Out-of-scope (separate release units)

- **Phase B**: LINE / Slack handlers adopt the hub; conversation tree;
  scheduled tasks; confirmation-card share; Slack Block Kit / Slack PDF
  attachment via files.uploadV2
- **Phase C**: Calendar / Email / Outlook / Teams integration
- **Phase D**: General-mode answers, ML-based intent classifier
