# Integrations OAuth — Deployment Guide

Phase 1 (2026-05-01): Google Calendar/Gmail + Microsoft Outlook Calendar/Mail (read-only).

## 1. Required Secrets (Google Secret Manager)

Set these in `classnote-x-dev` (and later in production project):

| Secret name                      | Source                         | Notes |
|----------------------------------|--------------------------------|-------|
| `google-oauth-client-id`         | GCP OAuth Web client ID        | |
| `google-oauth-client-secret`     | GCP OAuth Web client secret    | |
| `google-oauth-state-secret`      | `openssl rand -base64 32`      | Used to HMAC-sign `state` |
| `microsoft-oauth-client-id`      | Azure App Registration         | |
| `microsoft-oauth-client-secret`  | Azure → Certificates & secrets | "Value", not the ID |
| `microsoft-oauth-state-secret`   | `openssl rand -base64 32`      | |
| `token-encryption-key`           | `openssl rand -base64 32`      | Fernet key (32 bytes) |

## 2. Cloud Run env / secrets mapping

Add to `gcloud run deploy deepnote-api ...`:

```
--set-env-vars \
GOOGLE_OAUTH_REDIRECT_URI=https://dev---deepnote-api-mur5rvqgga-an.a.run.app/google/oauth/callback,\
MICROSOFT_OAUTH_REDIRECT_URI=https://dev---deepnote-api-mur5rvqgga-an.a.run.app/auth/microsoft/callback,\
MICROSOFT_OAUTH_TENANT=common \
--update-secrets \
GOOGLE_OAUTH_CLIENT_ID=google-oauth-client-id:latest,\
GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest,\
GOOGLE_OAUTH_STATE_SECRET=google-oauth-state-secret:latest,\
MICROSOFT_OAUTH_CLIENT_ID=microsoft-oauth-client-id:latest,\
MICROSOFT_OAUTH_CLIENT_SECRET=microsoft-oauth-client-secret:latest,\
MICROSOFT_OAUTH_STATE_SECRET=microsoft-oauth-state-secret:latest,\
TOKEN_ENCRYPTION_KEY=token-encryption-key:latest
```

Optional:
- `GOOGLE_OAUTH_SCOPES` to override the default scope set
- `MICROSOFT_OAUTH_SCOPES` to override Microsoft scopes
- `OAUTH_STATE_TTL_SECONDS` (default 600)
- `TOKEN_ENCRYPTION_KEY_PREVIOUS` while rotating

## 3. Provider console setup

### Google Cloud Console
1. **OAuth consent screen → Data access**: add scopes
   - `https://www.googleapis.com/auth/calendar.events.readonly`
   - `https://www.googleapis.com/auth/gmail.readonly`
2. **OAuth client → Authorized redirect URIs**: add
   - `https://dev---deepnote-api-mur5rvqgga-an.a.run.app/google/oauth/callback`
3. **Audience → Test users**: add owner email while in Testing mode

### Microsoft Entra ID (Azure portal)
1. **API permissions → Microsoft Graph → Delegated**:
   - `User.Read`, `Calendars.Read`, `offline_access`, `Mail.Read`
   (admin consent if your tenant requires it)
2. **Authentication → Web → Redirect URIs**: add
   - `https://dev---deepnote-api-mur5rvqgga-an.a.run.app/auth/microsoft/callback`

## 4. Endpoints

After deploy:

```
GET  /integrations/google/oauth/start            # → redirects to Google
GET  /google/oauth/callback                      # provider returns here
GET  /integrations/google/status
DELETE /integrations/google
GET  /integrations/google/calendar/events?timeMin=...&timeMax=...
GET  /integrations/google/calendar/list
GET  /integrations/google/mail/messages?q=...
GET  /integrations/google/mail/messages/{id}

GET  /integrations/microsoft/oauth/start
GET  /auth/microsoft/callback
GET  /integrations/microsoft/status
DELETE /integrations/microsoft
GET  /integrations/microsoft/calendar/events?startDateTime=...&endDateTime=...
GET  /integrations/microsoft/mail/messages?top=25&search=...
GET  /integrations/microsoft/mail/messages/{id}
```

If `TOKEN_ENCRYPTION_KEY` or provider client secrets are missing, the
endpoints return **503 token_crypto_not_configured / google_oauth_not_configured /
microsoft_oauth_not_configured**.

## 5. Firestore data model

```
users/{uid}/integrations/google
users/{uid}/integrations/microsoft
  - status: "connected" | "revoked"
  - scope, accessTokenCipher, refreshTokenCipher (Fernet)
  - tokenType, expiresAt, accountEmail, accountId
  - createdAt, updatedAt, lastError, lastErrorAt

oauth_state/{nonce}
  - uid, provider, returnTo, scopeHash, expiresAt, consumedAt
```

State documents are single-use (consumed in a Firestore transaction); call
`app.services.oauth_state_store.cleanup_expired()` from a periodic job to
sweep stale records.

## 6. Key rotation

1. Generate new Fernet key, set as `TOKEN_ENCRYPTION_KEY` (deploy).
2. Set previous key as `TOKEN_ENCRYPTION_KEY_PREVIOUS` so existing
   ciphertexts can still be decrypted.
3. Run a one-off migration that calls `token_crypto.rotate(cipher)` for every
   `users/{uid}/integrations/{provider}.{accessTokenCipher,refreshTokenCipher}`.
4. After verification, drop `TOKEN_ENCRYPTION_KEY_PREVIOUS`.

## 7. Disabling the legacy `/google/*` routes

The legacy implementation in `app/routes/google.py` (which used
`app/google_calendar.py`) is no longer registered in `app/main.py`. Once
clients are migrated to `/integrations/google/*`, both files can be deleted
in a follow-up cleanup PR.
