# DeepNote Clow — LINE 連携 Phase 1 仕様

> **Release unit**: `feat/line-webhook-and-link-token`
> **対象**: LINE 1:1 個人チャットのみ。グループ/ルームは未対応
> **本番反映**: dev タグで検証 → ユーザー承認後に traffic 切替

---

## 1. 全体フロー

```
LINE ユーザー
    │  メッセージ送信
    ▼
LINE Platform ──► POST /integrations/line/webhook
                    ├── X-Line-Signature 検証
                    ├── 連携済? → DeepNote データ返答
                    └── 未連携? → link token 発行 + connect URL を返答

ユーザーが connect URL を開く
    │
    ▼
GET /integrations/line/connect?token=<TOKEN>
    ├── User-Agent に "Line" 含む  → URLコピー案内 HTML
    ├── LINE_CONNECT_FRONTEND_URL 設定済 → 302 redirect (?lineToken=...)
    └── 未設定                       → fallback HTML

frontend (Safari 等) で Firebase Google ログイン
    │
    ▼
POST /integrations/line/link-tokens/{token}:consume
  Authorization: Bearer <Firebase ID token>
    └── line_user_id ↔ deepnote_uid を line_user_links に保存

以後の LINE メッセージは連携済みアカウント情報を返答
```

---

## 2. エンドポイント一覧

| Method | Path | 認証 | 公開 |
|---|---|---|---|
| POST | `/integrations/line/webhook` | X-Line-Signature (HMAC-SHA256) | ❌ (`include_in_schema=False`) |
| POST | `/integrations/line/link-tokens` | `X-Internal-Token` (env `LINE_INTERNAL_TOKEN` 設定時) | ❌ |
| GET  | `/integrations/line/link-tokens/{token}` | なし (token そのものが認可代わり) | ✅ |
| POST | `/integrations/line/link-tokens/{token}:consume` | Firebase Bearer | ✅ |
| GET  | `/integrations/line/connect?token=...` | なし | ❌ (HTML) |

---

## 3. webhook イベント分岐

| event.type | source.type | 動作 |
|---|---|---|
| `message` (text) | `user` | 連携済→DeepNote データ応答 / 未連携→connect URL |
| `message` | `group` / `room` | "未対応" 文面のみ返信。**個人情報は一切返さない** |
| `follow` | `user` | 連携済→ヘルプ / 未連携→connect URL |
| `join` / `memberJoined` | (group) | "未対応" 文面 |
| `unfollow` / `leave` / `postback` 他 | - | ログのみ |

---

## 4. 1:1 メッセージ → 応答の対応表

| ユーザー入力 (含むキーワード) | 応答 |
|---|---|
| `ヘルプ` `help` `使い方` `?` `？` | コマンド一覧 |
| `クレジット` `残量` `credit` | プラン名 + 残量 / 月限度 / 購入分 |
| `最新` `会議` `summary` `要約` | 最新会議のタイトル + 要約 (≤500 chars) |
| `todo` `タスク` `やること` | 未完了 TODO 上位 3 件 |
| `決定` `decision` | 最新会議の `summary_v2.decisions` 上位 3 件 |
| その他 | "認識できませんでした" + ヘルプ誘導 |

---

## 5. データモデル

### `line_link_tokens/{token}` (Firestore)
| field | type | note |
|---|---|---|
| lineUserId | str | LINE Messaging API source.userId |
| lineGroupId | str \| null | Phase 1 では常に null |
| lineSourceType | "user" \| "group" \| "room" | Phase 1 は "user" のみ受け付け |
| expiresAt | datetime | 発行から 10 分 |
| usedAt | datetime \| null | consume 後にセット |
| createdAt | datetime | |
| linkedUid | str \| null | consume 後にセット |
| linkedAccountId | str \| null | consume 後にセット |

トークン値: `secrets.token_urlsafe(32)` (43 chars, url-safe)

### `line_user_links/{lineUserId}` (Firestore)
| field | type |
|---|---|
| deepnoteUid | str |
| accountId | str |
| linkedAt | datetime |
| lineSourceType | str |

---

## 6. 環境変数

| 変数 | 必須 | 用途 |
|---|---|---|
| `LINE_MESSAGING_CHANNEL_SECRET` | ✅ | webhook 署名検証 (HMAC) |
| `LINE_MESSAGING_CHANNEL_ACCESS_TOKEN` | ✅ | reply / push の Bearer |
| `LINE_PUBLIC_BASE_URL` | 任意 | connect URL のホスト。未設定なら `CLOUD_RUN_SERVICE_URL` フォールバック |
| `LINE_CONNECT_FRONTEND_URL` | 任意 | 設定時、in-app browser **以外** から開いたユーザーをここへ 302。未設定なら backend 完結 HTML |
| `LINE_INTERNAL_TOKEN` | 任意 (推奨) | `/link-tokens` POST の保護。未設定なら警告のみで通す (Phase 1 暫定) |

---

## 7. 失敗時の挙動 (Phase 1)

| 状況 | 応答 |
|---|---|
| `LINE_MESSAGING_CHANNEL_SECRET` / `_ACCESS_TOKEN` 未設定 | `webhook` → 503 `line_messaging_not_configured` |
| 署名不一致 | 401 `invalid_signature` |
| body が JSON 不正 | 400 `malformed_body` |
| token 不明 | 404 `token_unknown` (連携 API) / 400 HTML (connect) |
| token 期限切れ | 410 `token_expired` |
| token 既使用 | 409 `token_already_used` |
| consume 時に Firebase 認証なし | 401 |
| イベント個別ハンドラの例外 | logger.exception + LINE には 200 を返す (リトライ抑止) |

---

## 8. セキュリティ・プライバシー

- **個人情報の漏洩防止**: `source.type != "user"` (group / room) の場合は `M.GROUP_NOT_SUPPORTED` のみ返す。クレジット残量・会議タイトル・TODO・決定事項は一切返さない。
- **token 単一使用**: `consume` は Firestore transaction でアトミックに `usedAt` をセット (oauth_state_store と同じパターン)。同時 consume は 1 つだけ成功。
- **token 漏れ対策**: TTL 10 分、reuse window 60 秒以内のみ token を使い回す → bot スパム時の発行量を抑制。
- **resolve は最小公開**: `GET /link-tokens/{token}` は `lineUserId` と `lineSourceType` のみ。`deepnoteUid` `accountId` は返さない。
- **secret ログ非出力**: webhook ハンドラは `event.type / source.type / source.userId` のみログ。本文 (event.message.text) や access_token はログに出さない。

---

## 9. 後続 release unit (Phase 1 では実装しない)

- `feat/line-group-support` — グループ/ルーム対応 (発言者本人 vs グループ共有の権限設計)
- `feat/line-asset-delivery` — PDF / DOCX / PPTX の出力 URL ラッパ
- `feat/line-liff-integration` — LIFF 上で資料一覧 / 操作画面
- `fix/line-internal-token-mandatory` — `LINE_INTERNAL_TOKEN` 必須化
- `feat/line-natural-language` — Gemini で意図解釈

---

## 10. 検証手順 (dev タグ)

1. dev タグへ deploy:
   ```bash
   gcloud run deploy deepnote-api --source . \
     --region asia-northeast1 --project classnote-x-dev \
     --no-traffic --tag dev \
     --concurrency 15 --min-instances 0 --max-instances 10 \
     --memory 2Gi --cpu 1 --timeout 3600 --cpu-throttling
   ```
2. LINE Developers Console で webhook URL に `https://dev---deepnote-api-...run.app/integrations/line/webhook` を設定 → Verify 押下。署名検証が通れば 200 + `{"status":"ok"}` が返る。
3. LINE bot を 1:1 で友だち追加 → 自動で `follow` イベントが飛び、connect URL が返ることを確認。
4. connect URL を **LINE 内ブラウザ**で開く → URLコピー画面が出ること。
5. URL を Safari に貼り付け → ログイン画面 (frontend が無ければ fallback HTML) が出ること。
6. ログイン後 `POST /integrations/line/link-tokens/{token}:consume` が成功し、`line_user_links/{lineUserId}` が作られること。
7. 再度 LINE で「クレジット」と送信 → 自分の DeepNote 残量が返ること。
8. **グループに招待**して「クレジット」と送信 → "未対応" のみ返り、個人情報が出ないこと。
9. Route inventory diff: `/v1/folders` `/v1/sessions/{id}:move` 等 Critical route が **消えていないこと** を確認 (本 release unit は純粋追加なので欠落するはずがないが確認)。
