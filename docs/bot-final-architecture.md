# DeepNote Clow — 最終アーキテクチャ (Phase 0–8)

> **Release units shipped:**
> 0. `feat/integrations-google-microsoft-oauth-20260501` (LINE Phase 0)
> 1. `feat/slack-bot-1to1-mvp`
> 2. `feat/asset-delivery-phase2`
> 3. `feat/scheduled-digests`
> 4. `feat/bot-audit-trail`
> 5. `feat/bot-frontend-fallback` (本ファイル含む)

## 1. 最終ゴール対応

| ゴール | 実装 |
|---|---|
| Slack / LINE から DeepNote 連携 | LINE: `/integrations/line/{webhook,connect,login,link-tokens/...}` / Slack: `/integrations/slack/{events,oauth/start,oauth/callback,connect,login,link-tokens/...}` |
| 個人チャットで本人データ | LINE 1:1 `source.type=user` / Slack DM `channel_type=im` のみで credit / latest / todos / decisions / 資料 |
| グループで本人または共有データのみ | Phase 7 `group_shared_briefing` — `sessions.sharedToWorkspaceTeams` 配列に `slack:{teamId}` / `line:{groupId}` を含む session のみ。credit / TODO は groups で必ず拒否 |
| PDF / DOCX / PPTX を URL / Web で開ける | bot 返答に `${BASE}/sessions/{id}/export?format=...` を含める。backend 自身が GET でその bridge HTML を提供 (Phase 6) |
| cron 自動配信 | Phase 3 `/internal/tasks/run_morning_digests` + Cloud Scheduler |
| 企業・複数ユーザー安全運用 | bot_audit_logs 監査 + workspace 単位 token 暗号化 + opt-out 自助 UI |
| 他人の個人データを返さない | 全分岐でテスト assertion (`"あなたのDeepNoteアカウント" not in text` 等) |

## 2. backend 内 self-host で frontend なしでも完結

| 機能 | エンドポイント | 説明 |
|---|---|---|
| LINE/Slack 連携完了画面 | `GET /integrations/{line,slack}/login?token=...` | Firebase Web SDK で Google ログイン → consume API を JS から叩く |
| Export bridge | `GET /sessions/{id}/export?format=pdf` | Firebase login → `POST /sessions/{id}/export` (既存) → signed URL |
| ユーザー設定 | `GET /integrations/me/settings` | digest opt-out, link 一覧, 利用履歴, 連携解除 |
| 自分の link 一覧 | `GET /integrations/me/links` | (Bearer) |
| digest opt-out | `POST /integrations/me/digest` | `{enabled: bool}` (Bearer) |
| 自分の audit | `GET /integrations/me/audit` | (Bearer) 直近 20 件 |
| LINE 連携解除 | `DELETE /integrations/me/links/line` | (Bearer) |
| Slack 連携解除 | `DELETE /integrations/me/links/slack/{teamId}/{slackUserId}` | (Bearer) |

## 3. 環境変数まとめ

| 変数 | 必須? | 用途 |
|---|---|---|
| `LINE_MESSAGING_CHANNEL_SECRET` | ✅ | webhook 署名検証 |
| `LINE_MESSAGING_CHANNEL_ACCESS_TOKEN` | ✅ | reply / push |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` / `SLACK_SIGNING_SECRET` | ✅ | Slack OAuth + Events |
| `SLACK_OAUTH_REDIRECT_URI` / `SLACK_OAUTH_STATE_SECRET` | ✅ | install flow |
| `TOKEN_ENCRYPTION_KEY` | ✅ | bot token / link tokens 暗号化 |
| `FIREBASE_WEB_API_KEY` / `FIREBASE_WEB_AUTH_DOMAIN` / `FIREBASE_WEB_PROJECT_ID` | ✅ if frontend なし運用 | self-host login & settings ページ |
| `DIGEST_INTERNAL_TOKEN` | ✅ for cron | `/internal/tasks/run_morning_digests` の bearer |
| `DEEPNOTE_APP_BASE_URL` | 任意 | frontend がある場合の export ページ host (未設定時は backend 自身) |
| `LINE_CONNECT_FRONTEND_URL` / `SLACK_CONNECT_FRONTEND_URL` | 任意 | frontend のログイン画面に redirect (未設定時は backend self-host login へ) |
| `LINE_PUBLIC_BASE_URL` / `SLACK_PUBLIC_BASE_URL` | 任意 | connect URL の host fallback |
| `LINE_INTERNAL_TOKEN` | 任意 (推奨) | `/integrations/line/link-tokens` POST 保護 |

## 4. データモデル (Firestore)

| collection | キー | フィールド要点 |
|---|---|---|
| `line_link_tokens/{token}` | url-safe 32 byte | `lineUserId, expiresAt(10min), usedAt, linkedUid, linkedAccountId` |
| `line_user_links/{lineUserId}` | LINE userId | `deepnoteUid, accountId, linkedAt, digestDisabled?` |
| `slack_workspaces/{teamId}` | Slack team id | `accessTokenCipher (Fernet), botUserId, scope, installedAt` |
| `slack_link_tokens/{token}` | url-safe 32 byte | `teamId, slackUserId, slackChannelId, expiresAt, usedAt, linkedUid` |
| `slack_user_links/{teamId:slackUserId}` | composite | `deepnoteUid, accountId, linkedAt, digestDisabled?` |
| `slack_oauth_state/{nonce}` | random | `expiresAt, consumedAt` |
| `bot_audit_logs/{auto}` | auto | `provider, sourceType, sourceUserId, teamId?, accountId?, deepnoteUid?, command, outcome, at` |
| `sessions/{id}.sharedToWorkspaceTeams` | string array | `["slack:T123", "line:G456"]` (Phase 7 group共有 opt-in) |

## 5. テスト

```
$ pytest tests/test_line_link_tokens.py tests/test_line_webhook.py \
         tests/test_slack_link_tokens.py tests/test_slack_webhook.py \
         tests/test_asset_delivery.py tests/test_digests.py \
         tests/test_bot_audit.py tests/test_bot_login_and_bridge.py \
         tests/test_group_shared.py
71 passed
```

## 6. 残タスク（ユーザー側）

1. **環境変数の dev 投入**:
   - `FIREBASE_WEB_API_KEY` / `FIREBASE_WEB_AUTH_DOMAIN` / `FIREBASE_WEB_PROJECT_ID`
   - `DIGEST_INTERNAL_TOKEN`
   - 既存 LINE / Slack secrets はすでに本番に設定済み
2. **LINE Developers Console** webhook URL を `https://dev---.../integrations/line/webhook` に設定 → Verify
3. **Slack App Console** Event URL / OAuth Redirect URL を dev URL に設定
4. **dev タグで E2E smoke** (`docs/line-webhook-spec.md` `docs/slack-bot-spec.md` の検証手順)
5. **本番 traffic 切替** (ユーザー承認後):
   ```bash
   gcloud run services update-traffic deepnote-api \
     --region asia-northeast1 --project classnote-x-dev \
     --to-revisions=<NEW_REVISION>=100
   ```
6. **Cloud Scheduler ジョブ作成** (digest 利用時):
   ```bash
   gcloud scheduler jobs create http morning-digest \
     --location asia-northeast1 --schedule "0 8 * * *" --time-zone "Asia/Tokyo" \
     --uri "https://deepnote-api-mur5rvqgga-an.a.run.app/internal/tasks/run_morning_digests" \
     --http-method POST \
     --headers "Authorization=Bearer ${DIGEST_INTERNAL_TOKEN}" \
     --project classnote-x-dev
   ```

## 7. 後続 (任意拡張)

- `feat/server-side-asset-rendering` — bot 上で即時 PDF を返す（cost guard 付き）
- `feat/natural-language-router` — Gemini で自由文意図解釈
- `feat/audit-export-and-monitoring` — 監査ログのダッシュボード
- `feat/sharing-ui-in-deepnote-app` — `sessions.sharedToWorkspaceTeams` を編集する UI
