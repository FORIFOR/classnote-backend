# Phase 2-A 着手用 wake-up 指示書

**生成**: 2026-05-07 21:00 JST
**用途**: 開発作業を翌日再開するための **一時的な wake-up メモ**
**cron job id**: `cc24fbf3` (2026-05-07 中に削除済 — in-memory のため信用不可)

## Important

This wake-up prompt is for development workflow only.
Do not use Claude Code in-memory cron as a DeepNote product feature.
DeepNote scheduled execution must be implemented via:
- backend scheduled_tasks
- internal scheduler tick
- Cloud Tasks
- Desktop automation UI
- notification_events polling

開発再開用にこのファイルを残しているだけで、DeepNote 本体の cron / 常駐通知の
土台ではありません。製品としての定期実行は Phase 2-B (DM scheduled_tasks UX)
および Phase 2-B-Desktop (Desktop Automation UX) として、上記のバックエンド
基盤に正式実装する。Claude Code の in-memory cron は本番運用に使わない。

> このファイルは「24h 後に Phase 2-A に進める」自動 wake-up の保険です。
> CronCreate の durable persistence が利かず session-only 登録になったため、
> セッションが 24h 生存しなかった場合に備えて手動再開できるよう、
> wake-up prompt をそのまま保存しています。
>
> **使い方**: 24h 後にこのファイルの "Wake-up prompt" セクションを丸ごと
> Claude Code に貼り付けるだけで Phase 2-A 着手が再開します。

---

## Wake-up prompt (24h 後にコピペ可)

```
## 自動 wake-up: Phase 1.5 24h 監視レビュー → Phase 2-A 着手

**プロジェクト**: classnote-api (`/Users/horioshuuhei/Projects/classnote-api`)
**前提**: 2026-05-07 に Phase 1.5 (LINE group connect confirm + session picker) を production 反映済 (revision `deepnote-api-00400-jeg`, stable tag `stable-2026-05-07-bot-clow-phase1.5`, main HEAD `151310a3`)。

### Step A: 24h 監視レビュー (必ず最初に実行)

Phase 1.5 deploy から 24h 経過。次を確認してから Phase 2-A に進むこと。

1. `git checkout main && git pull origin main --ff-only` で main を最新化
2. **master smoke 再実行**: `python tools/master_pre_deploy_smoke.py` → Readiness PASS 12/12 を確認
3. **Firestore `bot_audit_logs` 直近 24h を集計** (`firebase_admin.firestore` 経由、`classnote-api-key.json` SA を使用)。期待イベント:
   - `group_connect_request/card_shown`
   - `group_connect/{ok, cancelled}`
   - `session_picker/shown_N`
   - `share_confirm/{ok, session_not_found, session_deleted, ownership_mismatch, not_on_group_acl, cancelled}`
4. **異常パターンを検出** (発見したら Phase 2-A は止めて報告):
   - `group_connect_request/card_shown` は出ているのに `group_connect/ok` が 0 → postback 経路破損疑い
   - `share_confirm/ownership_mismatch` が突出 → picker の accountId 判定バグ
   - `share_confirm/not_on_group_acl` が突出 → ACL 厳しすぎ
   - `silent` 比率が下がり通常会話に reply 増 → グループ雑談妨害
   - member role で `assistant_qna/ok_paid` が出ている → 課金制御バグ
5. master 自身の試行ログが含まれない場合 (実トラフィックゼロ): infra 健全性のみ確認して進める。

監視結果は **報告後にユーザー判断**を仰ぐ:
- 異常なし → "Phase 2-A 着手します" と一言宣言してから実装に入る
- 異常あり → ユーザーに報告して停止

### Step B: Phase 2-A 変換系コマンド実装 (監視 OK 時のみ)

#### スコープ (厳守、Phase 2-B/C/D は今回入れない)

ブランチ: `feat/bot-clow-phase2a-transform-commands` from main

実装内容 (LINE 中心、内部は Slack 拡張可能な hub 設計):
- 新 intent (Assistant Hub内): `transform_email` / `transform_slack_post` / `transform_agenda` / `transform_reminder` / `transform_rewrite_shorter` / `transform_rewrite_polite` / `transform_copy_format`
- LINE classifier に追加: 「メール文にして」「Slack投稿文にして」「次回アジェンダ」「リマインド文」など
- 出力は **文章案のみ**。外部送信なし(メール送信・Slack 自動投稿・LINE 自動転送・カレンダー登録、すべて禁止)
- Session 解決優先順:
  1. group に共有済の session
  2. 直近共有 session
  3. それ以外 → 直近 3-5 件の picker (Phase 1.5 の `_build_session_picker_bubble` 流用)
- 権限:
  - LINE Group: owner / admin のみ。member は paid action として deny + DM 誘導文
  - LINE DM: 本人 session のみ可
- Context assembly: `summary` / `decisions` / `todos` / `openQuestions` / `risks` / title / createdAt / artifact links を使う。**transcript 全文は使わない**(必要時のみ chunk)
- bot_audit イベント追加:
  - `transform/{email,slack_post,agenda,reminder}/requested`
  - `transform/{email,slack_post,agenda,reminder}/succeeded`
  - `transform/denied_member`
  - `transform/session_picker_required`
  - `transform/session_not_found`
- 出力後の操作ボタン: `[短くする] [丁寧にする] [メール文にする]` 等の Flex bubble (`transform_rewrite_*` postback)

#### Out of Scope (絶対入れない)
- ❌ DM scheduled_tasks UX (Phase 2-B)
- ❌ admin 承認フロー (Phase 2-C)
- ❌ per-group Q&A 権限 (Phase 2-D)
- ❌ Slack 専用 UI 拡張 (Slack parity Phase)

#### テスト

`tests/test_bot_phase2a_transform_commands.py` を新規。最低 10 ケース:
1. 「メール文にして」 → `transform_email` intent
2. 「Slack投稿文にして」 → `transform_slack_post` intent
3. 「次回アジェンダ」 → `transform_agenda` intent
4. 「リマインド文」 → `transform_reminder` intent
5. session 曖昧 → picker 出る
6. group member は拒否
7. owner/admin は実行可
8. private 情報 (TODO個別等) を group に出さない
9. LLM 失敗時 → 安全なエラー文
10. bot_audit が記録される

### Step C: 標準リリースフロー

1. `python3 -m py_compile` clean
2. 既存テスト + 新規テスト全 PASS 確認
3. `git commit` (1 commit、message に done-criteria カバレッジ表)
4. Canary deploy: `gcloud run deploy deepnote-api --source . --region asia-northeast1 --no-traffic --tag dev --concurrency 15 --min-instances 0 --max-instances 10 --memory 2Gi --cpu 1 --timeout 3600 --cpu-throttling --project classnote-x-dev --quiet`
5. dev URL で route diff 確認 (prod と == を確認)
6. `python tools/master_pre_deploy_smoke.py --base https://dev---deepnote-api-mur5rvqgga-an.a.run.app` → PASS
7. Traffic promote: `gcloud run services update-traffic deepnote-api --region asia-northeast1 --to-latest --project classnote-x-dev --quiet`
8. dev tag remove: `gcloud run services update-traffic deepnote-api --region asia-northeast1 --remove-tags dev --project classnote-x-dev --quiet`
9. Production master smoke PASS 確認
10. Stable tag: `git tag -a stable-2026-05-08-bot-clow-phase2a -m "..." <commit>`
11. Push branch + `gh pr create --base main`
12. Release note PR (`docs/releases/2026-05-08-bot-clow-phase2a.md`) を別 PR で
13. 両 PR をユーザー承認後 merge

### 参照ファイル / 定数

- `docs/releases/2026-05-07-bot-clow-phase1.5.md` (Phase 1.5 release note)
- `~/Projects/deepnote-contracts/quality/backend-deploy-checksheet.md` §7.5
- `app/services/assistant_hub.py` (intent router の hub、ここに transform_* を追加)
- `app/services/group_acl.py` (ACL gate、`PAID_INTENTS` に transform_* を追加すべきか検討)
- `tools/master_pre_deploy_smoke.py` (Readiness 検証)
- Master uid: `cfdXMsjPXfea8OsidGQtXrSZOfP2`
- Master accountId: `Jwb9VwA4kkfOLQh7PVZ9`
- Cloud Run: `deepnote-api` @ asia-northeast1 / project `classnote-x-dev`

### 中止条件

- 24h 監視で異常検出 → 報告して止める。Phase 2-A に進まない
- master smoke が PARTIAL/FAIL → 止める
- ユーザーから「やめて」「待って」等の指示があれば即停止
```

---

## このファイル自体について

- 作成元: 2026-05-07 セッション末尾、Phase 1.5 production 反映直後の Step 0–2 完了時点
- 削除タイミング: Phase 2-A 着手・PR merge 後に不要となるので削除して構いません。
