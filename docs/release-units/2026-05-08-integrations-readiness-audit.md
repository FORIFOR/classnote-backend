# DeepNote External Integrations — Backend Readiness Audit (V-041)

- **Date**: 2026-05-08
- **Production revision**: `deepnote-api-00416-loc` (V-038 + V-037 + V-040)
- **Scope**: Google Calendar / Gmail / Microsoft Calendar / Outlook Mail
- **Verdict**: **NOT READY** — OAuth secrets unconfigured + 5 endpoints missing vs spec
- **Goal**: turn this audit into 3 sequential release units that get DeepNote Clow's pre/post-meeting capabilities live

---

## TL;DR

| Layer | Status |
|---|---|
| OAuth start / callback routes (per provider) | ✅ implemented |
| Token store with encryption support | ✅ `app/services/integrations/store.py` |
| Calendar `list_events` (Google + Microsoft) | ✅ `google_client.py` / `microsoft_client.py` |
| Calendar `create_event` | ✅ both providers |
| Mail `send_*_message` | ✅ both providers |
| Mail `create_draft` | ❌ not implemented |
| Aggregated `GET /v1/integrations` | ❌ missing |
| `:test` readiness probe | ❌ missing |
| `:disconnect` | ❌ missing |
| `?capability=` selective scope on OAuth start | ❌ missing |
| `/v1/` prefix per V-037 contract | ❌ all routes are bare `/integrations/...` |
| **OAuth client_id / client_secret env vars** | **❌ ALL EMPTY in prod** |
| `TOKEN_ENCRYPTION_KEY` | **❌ EMPTY in prod** (tokens cannot be safely stored) |
| Master account integration docs in Firestore | 0 (never connected) |

---

## 1. What's already in place

### 1.1 Service modules (functions exist, code is sound)

`app/services/integrations/google_client.py`:
- `is_configured()`, `exchange_code()`, `fetch_userinfo()`, `refresh_access_token()`
- `_ensure_access_token(uid)` — auto-refresh on expiry
- `list_calendar_events(uid, …)`, `list_calendar_list(uid)`
- `list_gmail_messages(uid, …)`, `get_gmail_message(uid, …)`
- `send_gmail_message(uid, …)`
- `create_calendar_event(uid, …)`

`app/services/integrations/microsoft_client.py`:
- Symmetric set: `exchange_code` / `refresh_access_token` / `fetch_userinfo`
- `list_calendar_events`, `create_calendar_event`
- `list_mail_messages`, `get_mail_message`, `send_mail`

`app/services/integrations/store.py`:
- `save_tokens(uid, provider, …)`, `update_access_token(...)`, `load(uid, provider)`
- `get_decrypted_tokens(uid, provider)` — gated on `TOKEN_ENCRYPTION_KEY`
- `mark_error()`, `revoke()`
- `_migrate_legacy_google()` — back-compat shim for an older shape

### 1.2 Routes (both providers, registered, in OpenAPI)

`/integrations/google/oauth/start`, `/integrations/google/oauth/callback`,
`/integrations/google/status`,
`/integrations/google/calendar/events`, `/integrations/google/calendar/list`,
`/integrations/google/mail/messages`, `/integrations/google/mail/messages/{id}`

`/integrations/microsoft/oauth/start`, `/integrations/microsoft/oauth/callback`,
`/integrations/microsoft/status`,
`/integrations/microsoft/calendar/events`,
`/integrations/microsoft/mail/messages`, `/integrations/microsoft/mail/messages/{id}`

Plus auth aliases (`/auth/google/*`, `/auth/microsoft/*`) and the legacy `/google/oauth/callback`.

### 1.3 Production probe (without OAuth tokens)

```
GET  /integrations/google/status            → reachable (returns connected:false because no token)
GET  /integrations/google/oauth/start       → would 302 if OAuth secrets were set; currently 503
POST /sessions/{id}/calendar:sync           → exists (Google Calendar event creation flow)
```

---

## 2. Critical blockers (must fix before any further work)

### 2.1 OAuth secrets are entirely unset on Cloud Run

```
GOOGLE_OAUTH_CLIENT_ID:        empty
GOOGLE_OAUTH_CLIENT_SECRET:    empty
GOOGLE_OAUTH_STATE_SECRET:     empty
MICROSOFT_OAUTH_CLIENT_ID:     empty
MICROSOFT_OAUTH_CLIENT_SECRET: empty
MICROSOFT_OAUTH_STATE_SECRET:  empty
TOKEN_ENCRYPTION_KEY:          empty
```

Until these are set, `is_configured()` returns False for both providers and `oauth/start` will refuse to redirect. Even if a callback came back with a code, `store.save_tokens` cannot encrypt without `TOKEN_ENCRYPTION_KEY`.

#### Action items (operator)

1. **Google OAuth client**: Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client (Web app). Authorized redirect URI = `https://deepnote-api-mur5rvqgga-an.a.run.app/integrations/google/oauth/callback` (already pinned in `GOOGLE_OAUTH_REDIRECT_URI`).
2. **Microsoft OAuth client**: Azure AD → App registrations → New registration. Redirect URI = `https://deepnote-api-mur5rvqgga-an.a.run.app/integrations/microsoft/oauth/callback`. Tenant = whichever multi-tenant pattern fits (`common`, `consumers`, or specific tenant id).
3. **State secrets**: `openssl rand -hex 32` × 2 (Google / Microsoft).
4. **Token encryption key**: `openssl rand -base64 32` (256-bit AES key).
5. **Inject** via either:
   - direct: `gcloud run services update deepnote-api --region asia-northeast1 --update-env-vars "GOOGLE_OAUTH_CLIENT_ID=…,GOOGLE_OAUTH_CLIENT_SECRET=…,…"` (visible in env)
   - **recommended**: Secret Manager + `--update-secrets` (zero leakage on `gcloud run services describe`)

### 2.2 Master account has zero integrations connected

`accounts/{master}/integrations/{google,microsoft}` is empty. Any "live" readiness probe (Calendar list / Mail draft) requires:
- step 1 above (secrets configured)
- master to walk through the OAuth consent screen on iOS / Desktop
- token to land in Firestore

Until then, all readiness tests fall back to "code-path probes" (route registered, schema valid).

---

## 3. Gaps vs V-041 product spec

The user's spec asks for the following surface area; current state:

| Spec endpoint | Current | Gap |
|---|---|---|
| `GET /v1/integrations` (unified status across providers) | ❌ | implement — aggregate `google.status` + `microsoft.status` |
| `GET /v1/integrations/google/oauth/start?capability=calendar\|gmail\|calendar_mail` | ⚠ exists at `/integrations/google/oauth/start`, no `/v1/` prefix, no capability param | add `/v1/` alias + `capability` param that selects scopes server-side |
| `GET /v1/integrations/microsoft/oauth/start?capability=calendar\|mail\|calendar_mail` | ⚠ same | same |
| `GET /v1/integrations/calendar/events?from=&to=` (unified, both providers) | ❌ | new aggregator that fans out to Google + Microsoft (per connected provider) and merges |
| `POST /v1/integrations/mail/drafts` (unified, draft only — no send) | ❌ | new endpoint. **`send_gmail_message` exists, but not `create_draft`. Must add `create_gmail_draft` and `create_outlook_draft` to the service modules** |
| `POST /v1/integrations/{provider}:disconnect` | ❌ | wrap `store.revoke()` |
| `POST /v1/integrations/{provider}:test` (readiness probe per spec §7) | ❌ | new — runs `_ensure_access_token` + 1 lightweight API call (Calendar `events.list?maxResults=1` or Gmail `users.getProfile`), returns PASS/FAIL with reason |

### 3.1 Service module additions required

| File | Function to add |
|---|---|
| `google_client.py` | `create_gmail_draft(uid, *, to, subject, body, attach_pdf=False)` — Gmail `users.drafts.create` |
| `microsoft_client.py` | `create_outlook_draft(uid, *, to, subject, body)` — Graph `/me/messages` POST (creates a draft) |
| `store.py` (optional) | `record_test_run(uid, provider, *, ok, reason)` — log to `lastHealthCheckAt` / `lastError` |

---

## 4. Recommended release units (sequential, each ≤ 1 day of work)

### V-041-A: OAuth secrets injection (operator-only, no code change) ★ blocker

- Generate Google + Microsoft OAuth credentials per §2.1
- Inject via Secret Manager (recommended):
  ```bash
  gcloud secrets create google-oauth-client-secret --data-file=- <<<"$GOOGLE_OAUTH_CLIENT_SECRET"
  gcloud run services update deepnote-api --region asia-northeast1 \
    --update-secrets "GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest" \
    --update-env-vars "GOOGLE_OAUTH_CLIENT_ID=…,GOOGLE_OAUTH_STATE_SECRET=…,TOKEN_ENCRYPTION_KEY=…" \
    --project classnote-x-dev
  ```
- After deploy: `curl -i .../integrations/google/oauth/start` should now 302 to Google consent.
- **No PR**, only env config change.

### V-041-B: Backend `:test` + `:disconnect` + draft create + aggregated status (1 PR)

`feat/integrations-readiness-and-drafts`
- New service functions: `create_gmail_draft`, `create_outlook_draft`
- New routes (V-037 contract style under `/v1/integrations/*`):
  - `GET /v1/integrations`
  - `POST /v1/integrations/google:test` / `:disconnect`
  - `POST /v1/integrations/microsoft:test` / `:disconnect`
  - `POST /v1/integrations/mail/drafts` (unified, picks provider per connected status)
  - `GET /v1/integrations/calendar/events` (unified, fans out to providers)
- Legacy routes (no `/v1/` prefix) remain for back-compat — add-only contract change
- Unit tests + dev E2E (master must connect Google/Microsoft first)
- Master smoke + canary + traffic promote per the standard playbook

### V-041-C: `?capability=` selective scope on OAuth start (1 PR)

`feat/integrations-capability-scopes`
- `GET /v1/integrations/{provider}/oauth/start?capability=calendar|gmail|mail|calendar_mail`
- Server picks the scope subset based on capability. Ensures users get a minimal consent screen instead of "give us everything".
- Capability matrix:

| capability | Google scopes | Microsoft scopes |
|---|---|---|
| `calendar` | calendar.readonly | Calendars.Read |
| `calendar_write` | calendar.events | Calendars.ReadWrite |
| `gmail` (Google only) | gmail.compose | — |
| `mail` (Microsoft only) | — | Mail.ReadWrite |
| `calendar_mail` | calendar.readonly + gmail.compose | Calendars.Read + Mail.ReadWrite |

### V-041-D: Calendar event ↔ session linking + briefing (1 PR)

`feat/integrations-session-linking-and-briefing`
- `external_calendar_events` cache collection
- `sessions/{sid}/external_links` subcollection writes (the `_dispatch` for `pre_meeting_briefing` already exists, this PR ties it to actual Calendar reads)
- Time-overlap-and-title heuristic for auto-link
- `GET /v1/calendar-briefings` aggregator endpoint

### V-041-E: Mail draft generation pipeline (1 PR, follows Phase 2-A share_drafts)

`feat/integrations-meeting-followup-mail`
- `share_draft_service.generate_share_draft(channel="email")` already required by Phase 2-A Hard Rule
- Wire that draft into Gmail / Outlook draft creation via V-041-B `mail/drafts` endpoint
- UI exposed in Session Detail "メール下書きを作成"

---

## 5. Backend-only readiness checklist (run after V-041-A)

These probes run **without a UI**, only with `python` / `curl` + a master ID Token + the operator-provided OAuth client config.

### 5.1 Google Calendar readiness

```bash
# 1. OAuth start emits 302 to Google
curl -i -X GET "$BASE/integrations/google/oauth/start?capability=calendar" \
  -H "Authorization: Bearer $ID_TOKEN"
# expect: 302 Location: https://accounts.google.com/o/oauth2/v2/auth?...

# 2. Walk through OAuth consent on a browser (manual one-time)
# 3. After callback, master's accounts/{aid}/integrations/google should exist
python -c "..."  # verify Firestore doc

# 4. Status endpoint
curl -i "$BASE/integrations/google/status" -H "Authorization: Bearer $ID_TOKEN"
# expect: {"connected": true, "email": "...", "scopes": [...]}

# 5. Calendar list
curl -i "$BASE/integrations/google/calendar/events?from=2026-05-08T00:00Z&to=2026-05-15T00:00Z" \
  -H "Authorization: Bearer $ID_TOKEN"
# expect: 200, items[]
```

### 5.2 Gmail draft readiness (after V-041-B)

```bash
# Create draft
curl -i -X POST "$BASE/v1/integrations/mail/drafts" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider":"google","to":["test@example.com"],"subject":"V-041 readiness","body":"draft test"}'
# expect: 201, draftId, externalDraftId, openUrl

# Verify draft exists (Gmail API)
# expect: confirm via Gmail web UI "Drafts" folder
```

### 5.3 Microsoft Calendar / Outlook Mail

Symmetric to Google. `Microsoft Graph` `/me/calendarView` and `/me/messages` endpoints used by the existing client.

### 5.4 Token refresh / revocation

```bash
# Force token refresh
python -c "
from app.services.integrations import store, google_client
tok = google_client._ensure_access_token('$MASTER_UID')
print('refresh ok, new expiry:', store.load('$MASTER_UID', 'google').get('expiresAt'))
"

# Disconnect
curl -i -X POST "$BASE/v1/integrations/google:disconnect" -H "Authorization: Bearer $ID_TOKEN"
# expect: 204; subsequent /status returns connected:false

# After revoke, calling list_calendar_events should raise GoogleAuthError → routes return 401 needsReconnect
```

### 5.5 Verdict matrix template

| Integration | OAuth | Token refresh | Read test | Write/draft test | Cleanup | Verdict |
|---|---|---|---|---|---|---|
| Google Calendar | — | — | — | n/a | n/a | NOT TESTED (V-041-A pending) |
| Gmail | — | — | — | — | — | NOT TESTED (V-041-A + V-041-B pending) |
| Microsoft Calendar | — | — | — | n/a | n/a | NOT TESTED (V-041-A pending) |
| Outlook Mail | — | — | — | — | — | NOT TESTED (V-041-A + V-041-B pending) |

This matrix becomes the body of `docs/releases/2026-05-xx-integrations-readiness.md` per release unit completion.

---

## 6. Minimal "今すぐ動く" pathway (≤ 30 min after V-041-A lands)

If the operator only wants Google Calendar **read** (the smallest useful slice for pre-meeting briefing):

1. V-041-A: set just `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_STATE_SECRET`, `TOKEN_ENCRYPTION_KEY`
2. Master walks `/integrations/google/oauth/start` → consents → callback writes token
3. Run `GET /integrations/google/calendar/events?from=…&to=…`
4. → DeepNote Clow can pull master's calendar events → assistant_briefing.deliver_pre_meeting (already in scheduled_tasks dispatcher) actually has data to work with

Microsoft + draft creation can wait for V-041-B.

---

## 7. Hard Rules (carried into all V-041-* release units)

- **Auto-send forbidden** in V-041 phase. Only `create_draft` + `openUrl` returning to the user. The user opens Gmail / Outlook and presses Send themselves.
- **Auto-share forbidden**. The Phase 2-A Smart Share Lv3 confirm card pattern applies — never push externally without an explicit user tap.
- **Token encryption required**. If `TOKEN_ENCRYPTION_KEY` is missing, `store.save_tokens` must refuse to save (fail-closed) rather than store plaintext.
- **State / nonce** required on OAuth start. Single-use, ≤10 min TTL.
- **Scope minimization**. Default to read-only / draft-only. Write / send capabilities require explicit user opt-in (V-041-C `capability=…` selector).
- **Rate-limit graceful**. `_api_get` / `_api_post` already raise `GoogleApiError` / `MicrosoftApiError`; routes must translate 429 / 403 to user-friendly responses without crashing the client.

---

## 8. Sunset

This audit doc is sunset-able when V-041-A through V-041-D have all merged and the verdict matrix in §5.5 is fully `READY` for at least Google Calendar + Gmail. Microsoft can lag if user demand is lower.

Until then, this is the single source of truth for "what's missing on integrations".
