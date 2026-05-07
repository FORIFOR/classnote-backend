# Scheduled Tasks Backend Verification

## Summary

| 項目 | 判定 |
|---|---|
| **API availability (CRUD 4 ルート)** | ✅ PASS |
| **`:run` (manual run)** | ❌ MISSING |
| **`GET /v1/scheduled-tasks/{id}` single** | ❌ MISSING |
| **Scheduler tick endpoint** | 🟡 EXISTS but **silent fail** |
| **Notification API (`/v1/notifications*`)** | ❌ MISSING (collection 自体無し) |
| **Cloud Scheduler 接続** | ❌ MISSING (ジョブ未登録) |
| **`find_due` Firestore composite index** | ❌ MISSING(本番で `tick` が永久に `due=0`) |
| **多重実行防止 (lease/runningJobId/lastRunSlot)** | ❌ MISSING |
| **Disabled task が tick で fire されない** | ✅ PASS (index 無し副作用で tautology) |
| **権限ガード (auth required)** | ✅ PASS (PATCH/DELETE/list で 401) |
| **Result** | **❌ FAIL — Desktop Automation 着手不可** |

→ Phase 2-B Desktop Automation に進む前に、必須 Fix を `feat/backend-scheduler-hardening` 1 PR で潰す必要がある(後述 §Required Fixes)。

---

## Environment

- **BASE_URL**: `https://deepnote-api-mur5rvqgga-an.a.run.app` (production)
- **Cloud Run revision**: `deepnote-api-00400-jeg`
- **Git commit (main)**: `bb08e20c` (PR #35 merge)
- **Test account**: master (`uid=cfdXMsjPXfea8OsidGQtXrSZOfP2`, `accountId=Jwb9VwA4kkfOLQh7PVZ9`)
- **Date**: 2026-05-08
- **Test task id (created + cleaned up)**: `st_365606414a4f4ba4`

---

## API Contract

| Endpoint | Status | Notes |
|---|---|---|
| `POST /v1/scheduled-tasks` | ✅ implemented (HTTP 200, `taskId` returned) | `nextRunAt` precomputed, `enabled=true` default |
| `GET /v1/scheduled-tasks` | ✅ implemented (HTTP 200, `{tasks:[…]}`) | per-account filter ✓ |
| `GET /v1/scheduled-tasks/{taskId}` | ❌ missing (HTTP 405) | canonical spec planned |
| `PATCH /v1/scheduled-tasks/{taskId}` | ✅ implemented (HTTP 200) | `enabled` toggle 動作確認済 |
| `DELETE /v1/scheduled-tasks/{taskId}` | ✅ implemented (HTTP 204) |  |
| `POST /v1/scheduled-tasks/{taskId}:run` | ❌ missing (HTTP 405) | canonical spec planned |
| `POST /internal/scheduler/tick` | 🟡 implemented but **silent fail** + auth fail-open | 詳細 §Findings |
| `GET /v1/notifications` | ❌ missing (HTTP 404) | canonical spec planned |
| `POST /v1/notifications/{id}:markRead` | ❌ missing (HTTP 404) | canonical spec planned |
| `POST /v1/notifications:markAllRead` | ❌ missing (HTTP 404) | canonical spec planned |

Canonical spec のうち `stable` 印は CRUD 4 ルートのみ。`planned` 印 (`:run`, `GET single`, notifications 3, scheduler/tick の hardening) が未実装。

---

## Test Results

| Test | Result | Evidence |
|---|---|---|
| Create task | ✅ PASS | HTTP 200, `taskId=st_365606414a4f4ba4`, `nextRunAt=2026-05-08T00:00:00+00:00` |
| List task | ✅ PASS | 12 件返却 (master account 限定)、test task 含有確認 |
| Disable task (PATCH) | ✅ PASS | `enabled: false` 反映 |
| Re-enable task (PATCH) | ✅ PASS | `enabled: true` 反映 |
| Manual run (`:run`) | ❌ FAIL | endpoint 不在 (HTTP 405) |
| Force `nextRunAt` past | ✅ PASS | Firestore 直接書込で確認 |
| Tick fires due task | ❌ FAIL | tick が `due=0, fired=0` を返す。原因 = composite index 不足の silent failure |
| `nextRunAt` advance | ❌ FAIL | tick が無 op のため進まない |
| `lastRunAt` 記録 | ❌ FAIL | 同上 |
| notification_events 作成 | ❌ FAIL | collection 自体存在せず |
| Double-tick idempotency | 🟡 INCONCLUSIVE | tick が常に `fired=0` のため二重実行発生せず。**実際に発火する状態にすれば lease/lastRunSlot 無しで二重実行する設計** |
| Disabled task ignored | ✅ PASS (tautology) | tick 全体が動かないので disabled かどうかに関わらず安全 |
| 別ユーザー (unauth) アクセス | ✅ PASS | PATCH/DELETE/list に 401。GET single は 405 (route 自体無いため) |
| 不正入力 — 空 rrule | ✅ PASS | HTTP 400 |
| 不正入力 — 壊れた rrule | ⚠ WEAK | HTTP 200 で受理、`nextRunAt=null` で保存される(rrule parser が緩い) |

---

## Findings

### 🚨 F1. `find_due` composite index 不足で本番 tick が silent fail (CRITICAL)

`app/services/scheduled_tasks.py::find_due` の collection_group クエリ:
```python
db.collection_group("scheduled_tasks")
  .where("enabled", "==", True)
  .where("nextRunAt", "<=", now)
```

これは Firestore composite index が必須。本番で同等クエリを直接実行するとエラー:
```
400 The query requires an index. You can create it here:
https://console.firebase.google.com/v1/r/project/classnote-x-dev/firestore/indexes?create_composite=...
```

`find_due` は例外を `try/except` で握りつぶして空リストを返す:
```python
except Exception as e:
    logger.warning("[scheduled_tasks.find_due] failed: %s", e)
return out  # 空リスト
```

→ `tick` は HTTP 200 + `due=0, fired=0` を返却。**Cloud Logging 警告 ログを誰も見ていなければ気づかない silent failure**。production deploy 後に Cloud Scheduler を仮に登録しても **永久に何も発火しない**。

### 🚨 F2. `INTERNAL_SCHEDULER_SECRET` env 未設定で tick 認証 fail-open

```python
expected = os.environ.get("INTERNAL_SCHEDULER_SECRET")
if expected and x_deepnote_internal_token != expected:
    raise HTTPException(status_code=401, detail="bad_internal_token")
```
`expected` が None の場合は **どんなトークンでも素通り**。本番で env 未設定 → 任意の外部から `/internal/scheduler/tick` を叩ける(現状 due=0 なので副作用は出ていないが、F1 を直した瞬間に問題化)。

### 🚨 F3. `notification_events` collection が存在しない (Desktop polling 不可)

Firestore に `notification_events` collection が無く、`/v1/notifications*` 3 ルートも未実装。Desktop Automation の主要要件 (polling 受信箱) が未着手。

### 🚨 F4. dispatcher に `channel = "desktop"` 分岐が無い

`scheduled_tasks_routes._dispatch` は `channel == "line"` / `channel == "slack"` のみ分岐:
```python
if channel == "line":
    line_messaging.push(...)
if channel == "slack":
    slack_client.post_message(...)
```
`channel = "desktop"` は **どの分岐にも入らず関数末尾に到達 → no-op**。Desktop 向け通知は dispatcher を拡張するまで発射不能。

### 🟡 F5. 多重実行防止 (lease / runningJobId / lastRunSlot) 完全に無い

`scheduled_tasks_routes.scheduler_tick` は `find_due` → ループで同期 dispatch。**lease 取得 / runningJobId 記録 / lastRunSlot による idempotency** いずれも実装無し。本番で複数 Cloud Scheduler instance や手動再叩きが起きると **同じ task を多重実行** する。F1 が直って tick が機能し始めた瞬間にこれが問題化する。

### 🟡 F6. `:run` (manual run) endpoint 不在

Desktop UI の "今すぐ実行" ボタン用、テスト時の即時 fire 用、両方で必要。canonical spec planned だが実装無し。

### 🟡 F7. Cloud Scheduler ジョブ未登録

```bash
$ gcloud scheduler jobs list --location asia-northeast1 --project classnote-x-dev
youtube-health-check          0 9 * * *    .../internal/tasks/youtube_health_check
classnote-audio-cleanup-daily 30 18 * * *  .../classnote-audio-cleanup:run
```
`/internal/scheduler/tick` を呼ぶジョブが無く、自動 fire は決して起きない。

### 🟡 F8. `bot_audit` / `ops_events` 計測無し

tick / dispatch どちらにも `bot_audit.record(...)` 等の計測が無い。本番で何が動いているか追えない。

### ⚠ F9. RRULE parser が緩く invalid 文字列を受け入れる

`POST /v1/scheduled-tasks` に `rrule: "COMPLETELY_INVALID"` を送ると HTTP 200 で task 作成 + `nextRunAt=null` で保存される。silent な無効 task が貯まり得る。期待は HTTP 400。

### ⚠ F10. `smart_share_prompt` task type の dispatcher 分岐無し

canonical spec の `ScheduledTask.type` enum に `smart_share_prompt` あり、dispatcher には対応分岐無し。fire してもスキップ。

---

## Blockers

Phase 2-B Desktop Automation に進む前に **必ず潰すべき** 順:

1. **F1** Firestore composite index `(enabled, nextRunAt)` を `firestore.indexes.json` に追加 + デプロイ
2. **F2** `INTERNAL_SCHEDULER_SECRET` env を Cloud Run に投入し、env 未設定時は **fail-closed** にコード修正
3. **F4** dispatcher に `channel = "desktop"` 分岐 + `notification_events` 書込
4. **F3** `/v1/notifications*` 3 ルート(list / markRead / markAllRead)新設
5. **F6** `POST /v1/scheduled-tasks/{id}:run` 新設 (Desktop "Run now" 用)
6. **F7** Cloud Scheduler ジョブ `deepnote-scheduler-tick` を `*/5 * * * *` で登録
7. **F5** lease / runningJobId / lastRunSlot 実装で多重実行防止
8. **F8** tick / dispatch に `ops_events` または `bot_audit` 計測追加
9. **F9** RRULE parser を strict 化(空 + 文法不一致は 400)
10. **F10** `smart_share_prompt` 分岐(または unknown type を明示エラー)

---

## Required Fixes Before Desktop Automation

**最低でも F1〜F4 + F6 が無ければ Desktop Automation UX は機能しない**(F1 = tick 発火不能、F3 = polling 受信箱不在、F4 = desktop へ届かない、F6 = "Run now" ボタンが押せない、F2 = security の前提)。

F5 / F7 / F8 は Phase 2-B 着手と同時並行でも可だが、本番投入前に揃える。

---

## Recommended Next PRs

### `feat/backend-scheduler-hardening` (Phase 2-B 前提、必須)

スコープ:
- `firestore.indexes.json` に composite index 追加 + 適用
- `find_due` を例外 → 例外伝播 (silent failure 防止)
- `INTERNAL_SCHEDULER_SECRET` env 投入 + コード fail-closed 化
- `_dispatch` に `channel = "desktop"` 分岐 + `notification_events` 書込
- `POST /v1/scheduled-tasks/{id}:run` 新設 (auth + owner 検証 + Cloud Tasks enqueue)
- `GET /v1/scheduled-tasks/{id}` 新設 (planned → stable)
- `GET /v1/notifications`, `POST :markRead`, `POST :markAllRead` 3 ルート新設
- lease (`leaseUntil`, `runningJobId`, `lastRunSlot`) 実装 + tick で取得 → 解放
- `ops_events` 計測注入(`scheduled_task_run_*` 6 種)
- RRULE parser strict 化
- Cloud Scheduler ジョブ登録手順を `tools/deploy.sh` または独立スクリプトに

スコープ外:
- Desktop UI(別 release unit `feat/desktop-scheduled-tasks-automation-ux` で deepnote-desktop 側)
- Phase 2-A 変換系コマンド(別 unit、本前提とは独立)
- `pre_meeting_briefing` の Calendar 連携強化(既存実装で OK)

### `feat/desktop-scheduled-tasks-automation-ux` (deepnote-desktop)

スコープ:
- Settings > Automation 画面
- scheduled task 一覧 / 作成ウィザード
- notification polling (1〜5 分間隔)
- Tauri OS 通知

`backend-scheduler-hardening` の merge 後に着手。

---

## Test Reproduction (再実行手順)

このレポートを再現するには:
```bash
git checkout test/verify-scheduled-tasks-backend
VENV_SP=.venv/lib/python3.13/site-packages PYTHONPATH="$VENV_SP:." \
  /opt/homebrew/bin/python3.13 - <<'PY'
import sys; sys.path.insert(0,'tools')
import master_pre_deploy_smoke as m
import requests, firebase_admin
from firebase_admin import credentials, firestore
firebase_admin.initialize_app(credentials.Certificate('classnote-api-key.json'))
# ... (本検証で実行したのと同じ手順)
PY
```

または `tools/master_pre_deploy_smoke.py` を流用して同等の token mint 経由で各 endpoint を叩く。

すべての書込 (test task 作成 / disable / enable / nextRunAt rewind) は **cleanup で削除済**(末尾 `DELETE /v1/scheduled-tasks/{id}` HTTP 204 確認、`master` アカウントの task 数 12→11→11 で観測)。

---

## Note: 関連既存ドキュメント

- `docs/release-units/2026-05-08-phase2b-desktop-automation-prep.md` — Phase 2-B 着手前 read-only 監査(本検証はその実機テスト版)
- `docs/release-units/2026-05-08-clow-vision-gap-analysis.md` — DeepNote v1 全体ギャップ分析
- `~/Projects/deepnote-contracts/api/openapi.yaml` (uncommitted) — canonical spec に scheduled_tasks / notifications / scheduler/tick 既登録済(stable / planned 混在)
