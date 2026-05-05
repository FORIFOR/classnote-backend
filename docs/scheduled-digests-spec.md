# DeepNote Clow — Scheduled Digests Phase 3 仕様

> **Release unit**: `feat/scheduled-digests`
> **依存**: Phase 1 (LINE 1:1 link), Phase 1 Slack DM link
> **本番反映**: dev タグ → ユーザー承認後に traffic 切替 + Cloud Scheduler ジョブ作成

## 1. ルート
| Method | Path | 認証 | 公開 |
|---|---|---|---|
| POST | `/internal/tasks/run_morning_digests` | `Authorization: Bearer ${DIGEST_INTERNAL_TOKEN}` | hidden |

## 2. ペイロード
- 入力: なし（呼び出すだけ）
- 出力: `{"status": "ok", "line": <count>, "slack": <count>, "failed": <count>}`

## 3. 動作
- `line_user_links` `slack_user_links` を全件 stream
- 各リンクで `digestDisabled: true` の場合スキップ
- それ以外 → 朝のダイジェストを構成して push (LINE) / chat.postMessage (Slack DM)
- 各ユーザー push の例外は握り潰して残りを継続（best effort）

## 4. ダイジェスト本文
```
おはようございます。DeepNote の朝のダイジェストです。

クレジット: 50 / 100

最新の会議: 〇〇定例

未完了TODO (上位3件):
・タスクA（期限: 2026-05-07）
・タスクB
・タスクC
```

## 5. 環境変数
| 変数 | 必須 | 用途 |
|---|---|---|
| `DIGEST_INTERNAL_TOKEN` | ✅ | Cloud Scheduler が presents するシェアード bearer |

## 6. Cloud Scheduler 設定例 (本番反映時にユーザーが実行)
```bash
gcloud scheduler jobs create http morning-digest \
  --location asia-northeast1 \
  --schedule "0 8 * * *" \
  --time-zone "Asia/Tokyo" \
  --uri "https://deepnote-api-mur5rvqgga-an.a.run.app/internal/tasks/run_morning_digests" \
  --http-method POST \
  --headers "Authorization=Bearer ${DIGEST_INTERNAL_TOKEN}" \
  --project classnote-x-dev
```

## 7. 失敗時
- token 未設定: 503
- token 不一致: 401
- 個別 push 失敗: HTTP 200 + `failed` カウント増加 (alert は ops 側で監視)

## 8. オプトアウト
ユーザー側で digest を停止したい場合、Firestore の link doc に
`digestDisabled: true` をセットする（Phase 7 で UI を提供）。
