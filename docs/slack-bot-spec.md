# DeepNote Clow — Slack 連携 Phase 1 仕様

> **Release unit**: `feat/slack-bot-1to1-mvp`
> **対象**: Slack DM (channel_type=im) のみ。Channel / Group は未対応
> **本番反映**: dev タグで Verify → ユーザー承認後に traffic 切替

## 1. 全体フロー
```
Slack DM ──► POST /integrations/slack/events
              ├─ X-Slack-Signature 検証 (V0)
              ├─ url_verification → challenge を返す
              ├─ 連携済? → DeepNote データ返答 (chat.postMessage)
              └─ 未連携? → link token + connect URL を DM 返答

ユーザーが connect URL を開く
   ├─ Slack mobile in-app browser (UA "Slack/") → URL コピー HTML
   ├─ それ以外 + SLACK_CONNECT_FRONTEND_URL 設定済 → 302
   └─ 未設定 → fallback HTML

frontend で Firebase login →
POST /integrations/slack/link-tokens/{token}:consume  Authorization: Bearer
  └─ slack_user_links/{teamId}:{slackUserId} を保存

Workspace install:
GET /integrations/slack/oauth/start  → slack.com 同意画面
GET /integrations/slack/oauth/callback → bot token を Fernet 暗号化保存
```

## 2. ルート一覧
| Method | Path | 認証 | 公開 |
|---|---|---|---|
| POST | `/integrations/slack/events` | X-Slack-Signature V0 | hidden |
| GET  | `/integrations/slack/oauth/start` | (state HMAC) | hidden |
| GET  | `/integrations/slack/oauth/callback` | state | hidden |
| GET  | `/integrations/slack/link-tokens/{token}` | (token) | **公開** |
| POST | `/integrations/slack/link-tokens/{token}:consume` | Firebase Bearer | **公開** |
| GET  | `/integrations/slack/connect?token=...` | なし | hidden |

## 3. メッセージ → 応答対応表
| 入力キーワード | 応答 |
|---|---|
| ヘルプ / help / ? | コマンド一覧 |
| クレジット / 残量 / credit | プラン + 残量 / 月限度 / 購入分 |
| 最新 / 会議 / 要約 | 最新会議タイトル + 要約 (≤500) |
| todo / タスク / やること | 未完了 TODO 上位 3件 |
| 決定 / decision | 最新会議の `summary_v2.decisions` 上位 3件 |
| その他 | 認識不可 + ヘルプ誘導 |

## 4. データモデル (Firestore 新規)
- `slack_workspaces/{teamId}` — `accessTokenCipher / botUserId / scope / installedAt / installedByUid`
- `slack_link_tokens/{token}` — `teamId / slackUserId / slackChannelId / expiresAt(10min) / usedAt / linkedUid / linkedAccountId`
- `slack_user_links/{teamId}:{slackUserId}` — `deepnoteUid / accountId / linkedAt`
- `slack_oauth_state/{nonce}` — workspace install state (single-use, 10min TTL)

## 5. 環境変数
| 変数 | 必須 | 用途 |
|---|---|---|
| `SLACK_CLIENT_ID` | ✅ | OAuth |
| `SLACK_CLIENT_SECRET` | ✅ | OAuth |
| `SLACK_SIGNING_SECRET` | ✅ | Events 署名 V0 |
| `SLACK_OAUTH_REDIRECT_URI` | ✅ | OAuth callback |
| `SLACK_OAUTH_STATE_SECRET` | ✅ | state HMAC |
| `SLACK_PUBLIC_BASE_URL` | 任意 | connect URL ホスト (fallback あり) |
| `SLACK_CONNECT_FRONTEND_URL` | 任意 | 設定時、外部ブラウザを frontend へ 302 |

## 6. セキュリティ・プライバシー
- **個人情報の漏洩防止**: `channel_type != "im"` の場合は `M.GROUP_NOT_SUPPORTED` のみ。クレジット / 会議 / TODO / 決定事項は一切返さない。
- **Bot loop 防止**: `event.bot_id` または `subtype == "bot_message"` は一切応答せず無視。
- **token 単一使用**: `consume` は Firestore transaction で `usedAt` をアトミックにセット。
- **resolve は最小公開**: `GET /link-tokens/{token}` は `teamId` / `slackUserId` のみ。`deepnoteUid` `accountId` は返さない。
- **bot token 暗号化**: `slack_workspaces.accessTokenCipher` は Fernet 暗号化 (token_crypto)。
- **secret ログ非出力**: handler ログは `team_id / event.type / channel_type / user` のみ。`text` / `access_token` / `signing_secret` はログに出さない。

## 7. 失敗時の挙動
| 状況 | 応答 |
|---|---|
| `SLACK_*` env 未設定 | `events` → 503 / `oauth/start` → 503 |
| 署名不一致 | 401 |
| body JSON 不正 | 400 |
| `url_verification` | 200 + `challenge` (text/plain) |
| token 不明 | 404 / connect HTML 400 |
| token 期限切れ | 410 |
| token 既使用 | 409 |
| consume 認証なし | 401 |

## 8. 検証手順 (dev タグ)
1. dev タグへ deploy:
   ```bash
   gcloud run deploy deepnote-api --source . \
     --region asia-northeast1 --project classnote-x-dev \
     --no-traffic --tag dev \
     --concurrency 15 --min-instances 0 --max-instances 10 \
     --memory 2Gi --cpu 1 --timeout 3600 --cpu-throttling
   ```
2. Slack App 設定 (api.slack.com → your app):
   - Event Subscriptions → Request URL: `https://dev---deepnote-api-...run.app/integrations/slack/events`
     → "Verified" になることを確認
   - Subscribe to bot events: `message.im`, `app_mention`
   - OAuth & Permissions → Redirect URLs: `https://dev---deepnote-api-...run.app/integrations/slack/oauth/callback`
   - Scopes: `chat:write`, `im:history`, `im:read`, `im:write`, `app_mentions:read`, `users:read`
3. `https://dev---.../integrations/slack/oauth/start` をブラウザで開いて workspace に install
4. Slack で bot に DM:
   - `ヘルプ` → コマンド一覧
   - 未連携時 → connect URL → frontend ログイン後に DM で再度「クレジット」と送ると残量が返る
5. **public channel で bot に @mention** → "未対応" のみ返り、個人情報が出ないこと
6. Route inventory diff: 直前 dev (00338-mob) と新 dev で Critical 欠落なしを確認

## 9. 後続 release unit
- `feat/slack-channel-support` — channel / group 対応 (発言者本人 / 共有済みデータの権限設計)
- `feat/slack-asset-delivery` — PDF / DOCX / PPTX 配信
- `feat/slack-slash-command` — `/deepnote ...` slash command
- `feat/slack-block-kit-rich` — Block Kit リッチ UI
- `feat/scheduled-digests` — cron 配信 (LINE と統一)
