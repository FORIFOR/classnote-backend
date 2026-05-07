# Phase 2-B Desktop Automation — Backend 前提調査レポート

- **生成**: 2026-05-08 00:00 JST
- **対象**: classnote-api repo のみ (read-only 調査、実装変更なし)
- **目的**: Desktop Automation UX 着手前に backend 側 cron 基盤の現状と不足を可視化
- **想定後続**: `feat/desktop-scheduled-tasks-automation-ux` (deepnote-desktop) と、本リポでの不足分実装ブランチ

> **重要**: DeepNote 製品としての cron / 常駐通知は **必ず** backend `scheduled_tasks` + Cloud Scheduler + Cloud Tasks + `notification_events` polling のスタックで実装する。Claude Code の in-memory cron は使わない。

---

## 1. 結論サマリー

| 項目 | 状態 | 備考 |
|---|---|---|
| `POST /v1/scheduled-tasks` | ✅ 実装済 (auth 必須) | `app/routes/scheduled_tasks_routes.py:64` |
| `GET /v1/scheduled-tasks` | ✅ 実装済 (auth 必須) | 同 73 行目、`{tasks: [...]}` 形式 |
| `PATCH /v1/scheduled-tasks/{id}` | ✅ 実装済 | `id` パスパラメータ (`task_id`) |
| `DELETE /v1/scheduled-tasks/{id}` | ✅ 実装済 (204) | 同 |
| `POST /v1/scheduled-tasks/{id}:run` | ❌ **未実装** | Phase 2-B で追加必要 |
| `POST /internal/scheduler/tick` | ⚠ **実装はあるが本番で無防備** | env var `INTERNAL_SCHEDULER_SECRET` 未設定のため誰でも 200 返却 (後述 §3) |
| Cloud Scheduler の登録 | ❌ **未登録** | 既存ジョブは `youtube-health-check` と `audio-cleanup-daily` のみ。`/internal/scheduler/tick` を呼ぶ cron 不在 |
| Cloud Tasks 経由の dispatch | ❌ tick が同期 inline 実行 | スケール時に tick リクエストがタイムアウトし得る |
| `bot_audit` / `ops_events` 記録 | ❌ tick / dispatch に audit 計測なし | 監視・課題調査用に必須 |
| `notification_events` collection | ❌ 存在しない | Desktop polling 受信箱が無いため、Phase 2-B で新設要 |
| Firestore composite index `scheduled_tasks (enabled, nextRunAt)` | ⚠ `firestore.indexes.json` に未定義 | `find_due` が collection_group で `enabled==true && nextRunAt<=now` を引くので **本番で初回失敗の可能性** |

**Phase 2-B 着手前に最低限必要な backend 修正**:
1. `POST /v1/scheduled-tasks/{id}:run` を追加 (Desktop デバッグ + 即時テストに必須)
2. `INTERNAL_SCHEDULER_SECRET` を Cloud Run env に投入 + Cloud Scheduler ジョブを登録
3. `tick` / `_dispatch` に `bot_audit` または専用 `scheduled_task_run/*` ops_event を追加
4. `notification_events` collection と `GET /v1/notifications?unread=true` を新設 (Desktop 通知受信)
5. `firestore.indexes.json` に composite index を追記してデプロイ

これらは **本リポ単独で 1 PR** にまとめるのが妥当。Desktop UI は別リポ別 PR。

---

## 2. 既存 REST 公開 API (`/v1/scheduled-tasks`)

### 2.1 Router 登録
`app/main.py:330-332`:
```python
from app.routes import scheduled_tasks_routes as _st_routes
app.include_router(_st_routes.router)                            # public, in OpenAPI
app.include_router(_st_routes.internal_router, include_in_schema=False)  # internal
```

### 2.2 ルート一覧

| Method | Path | 認証 | 関数 |
|---|---|---|---|
| `POST` | `/v1/scheduled-tasks` | Firebase ID Token | `create_task` |
| `GET` | `/v1/scheduled-tasks` | Firebase ID Token | `list_tasks` |
| `PATCH` | `/v1/scheduled-tasks/{task_id}` | Firebase ID Token | `update_task` |
| `DELETE` | `/v1/scheduled-tasks/{task_id}` | Firebase ID Token (204) | `delete_task` |
| `POST` | `/v1/scheduled-tasks/{task_id}:run` | — | **未実装** |

### 2.3 Request 受理スキーマ (`ScheduledTaskRequest`)
```python
type: Optional[str] = "custom"
channel: Optional[str] = "slack"          # "slack" | "line"
destination: Optional[Dict[str, Any]]      # { workspaceId, channelId } | { lineUserId, ... }
rrule: str                                 # e.g. "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0"
timezone: Optional[str] = "Asia/Tokyo"
enabled: Optional[bool] = True
filters: Optional[Dict[str, Any]]          # { folderId?, sessionRange?, sessionId? }
output: Optional[Dict[str, Any]]           # { includeSummary, includeTodos, includeDecisions, attachPdf }
```

### 2.4 Phase 2-B での必要拡張
- **`channel: "desktop"`** を許容する (現状 enum 縛りはなく自由文字列だが、`_dispatch` は `slack/line` しかルーティングしない)
- **`destination: { channel: "desktop", target: "self" }`** 形を `_dispatch` で受けて `notification_events` に書き込む経路を追加
- **`POST :run` 手動実行 endpoint**(下記 §6.3 にコード雛形)

### 2.5 Production 動作確認 (実機)
```
GET /v1/scheduled-tasks (no auth)        → HTTP 401 ✓ (auth gate works)
POST /internal/scheduler/tick (no token) → HTTP 200 ⚠️ (secret 未設定で素通り)
POST /internal/scheduler/tick (bad tok)  → HTTP 200 ⚠️ (同上)
```

---

## 3. Internal scheduler tick (`POST /internal/scheduler/tick`)

### 3.1 実装場所
`app/routes/scheduled_tasks_routes.py:96` (`scheduler_tick`)

### 3.2 認証ガード — **実装はあるが本番で機能していない**
```python
expected = os.environ.get("INTERNAL_SCHEDULER_SECRET")
if expected and x_deepnote_internal_token != expected:
    raise HTTPException(status_code=401, detail="bad_internal_token")
```

`expected` が None (= env 未設定) の場合は **どんなトークンでも素通り** する fail-open 設計。本番で `INTERNAL_SCHEDULER_SECRET` が未設定のため、任意の外部から `/internal/scheduler/tick` を叩ける状態。

**修正案 (Phase 2-B 内)**:
- `INTERNAL_SCHEDULER_SECRET` を `gcloud run services update --update-env-vars` で投入
- かつ env 未設定時は **fail-closed** に変える(`if not expected: raise 503`)

### 3.3 Cloud Scheduler 接続想定 — **未接続**
```bash
$ gcloud scheduler jobs list --location asia-northeast1 --project classnote-x-dev
youtube-health-check          0 9 * * *    .../internal/tasks/youtube_health_check
classnote-audio-cleanup-daily 30 18 * * *  .../classnote-audio-cleanup:run
```
`/internal/scheduler/tick` を呼ぶジョブは **存在しない**。すなわち、現状 `scheduled_tasks` に書き込んでも自動 fire しない。Phase 2-B で:
```bash
gcloud scheduler jobs create http deepnote-scheduler-tick \
  --location asia-northeast1 \
  --schedule "*/5 * * * *" \
  --time-zone "Asia/Tokyo" \
  --uri "https://deepnote-api-mur5rvqgga-an.a.run.app/internal/scheduler/tick" \
  --http-method POST \
  --headers "X-DeepNote-Internal-Token=${INTERNAL_SCHEDULER_SECRET}" \
  --project classnote-x-dev
```

### 3.4 Due task 検索 (`scheduled_tasks.find_due`)
- `db.collection_group("scheduled_tasks")` を `enabled==true AND nextRunAt<=now` で fetch
- account_id は doc reference path (`accounts/{accountId}/scheduled_tasks/{taskId}`) からパース
- limit=200
- **composite index 警告**: collection_group + 2-field where はインデックス必須。`firestore.indexes.json` に未登録なので、初回 fire で Firestore が `FAILED_PRECONDITION` を返す可能性 → tick の logger warning に出るが対外的には HTTP 200 返却される (silent failure)。

### 3.5 Dispatcher
`scheduled_tasks_routes._dispatch(account_id, task)` (line 127–200)
- `task_type == "pre_meeting_briefing"` → `assistant_briefing.deliver_pre_meeting`
- `task_type == "session_followup"` → `assistant_briefing.deliver_session_followup`
- それ以外 → `line_briefing` / `slack_briefing` で text を組み立てて **同期 push**
- `channel == "desktop"` の経路は無い

### 3.6 Cloud Tasks 連携 — **無し**
tick は dispatch を **同期 inline** で実行。due task が大量にある場合 Cloud Run のリクエストタイムアウト(1800s)を超えるリスク。Phase 2-B では:
```python
# pseudo-code (Phase 2-B 提案)
for account_id, task in rows:
    task_queue.create_task(  # → Cloud Tasks
        queue="scheduled-task-fanout",
        url="/internal/scheduler/run-one",
        body={"accountId": account_id, "taskId": task["taskId"]},
    )
```

### 3.7 bot_audit / ops_events — **無し**
- `bot_audit.record(...)` 呼び出しは `_dispatch` / `scheduler_tick` のどちらにも無い
- Phase 2-B で `provider="scheduler"`, `command="scheduled_task_run"`, `outcome` ∈ {`ok`,`failed`,`due_zero`} を記録すべき

---

## 4. 想定アーキテクチャ図 (target after Phase 2-B)

```
Desktop UI (Tauri)
  ↓ POST /v1/scheduled-tasks
  ↓ POST /v1/scheduled-tasks/{id}:run    ← 新設
classnote-api (Cloud Run, public)
  ↓ Firestore accounts/{aid}/scheduled_tasks/{tid}
Cloud Scheduler */5 * * * *               ← 新設
  ↓ X-DeepNote-Internal-Token Bearer
classnote-api: POST /internal/scheduler/tick
  ↓ find_due() collection_group query
  ↓ for each due → Cloud Tasks enqueue    ← 新設
Cloud Tasks Queue (scheduled-task-fanout)
  ↓
classnote-api: POST /internal/scheduler/run-one  ← 新設
  ↓ _dispatch(account_id, task)
  ↓ ┌── line_messaging.push (LINE DM)
  ↓ ├── slack_client.post_message (Slack DM)
  ↓ └── notification_events Firestore write   ← 新設 (desktop 用)
                ↑
Desktop polling: GET /v1/notifications?unread=true (1〜5 min)  ← 新設
  ↓ Tauri @tauri-apps/plugin-notification → OS 通知
```

---

## 5. Phase 2-B 着手前のバックエンド不足リスト

> 1 PR にまとめると以下のスコープになる目安。

### 5.1 必須
- [ ] `POST /v1/scheduled-tasks/{task_id}:run` の追加 (auth: ID Token、対象 task の owner 検証)
- [ ] `_dispatch` に `channel: "desktop"` ブランチを追加 → `notification_events/{auto_id}` に payload を書く
- [ ] `notification_events` collection 設計 + `GET /v1/notifications?unread=true` 新設
- [ ] `bot_audit` または専用 `ops_events` を tick / dispatch に注入
- [ ] `firestore.indexes.json` に collection_group composite index を追加
- [ ] `INTERNAL_SCHEDULER_SECRET` を `gcloud run services update` で投入
- [ ] `INTERNAL_SCHEDULER_SECRET` 未設定時は **fail-closed** にコード修正
- [ ] Cloud Scheduler ジョブ `deepnote-scheduler-tick` を `*/5 * * * *` で登録

### 5.2 推奨
- [ ] tick 内のループを Cloud Tasks enqueue に切り替え (`task_queue.create_task` 呼出)
- [ ] `POST /internal/scheduler/run-one` worker 新設 (Cloud Tasks 受け側)
- [ ] notification 既読フラグ (`PATCH /v1/notifications/{id}` で `readAt` セット)
- [ ] notification 一覧の `since=` パラメータ + cursor pagination

### 5.3 Phase 2-B-Desktop (deepnote-desktop 側、別リポ別 PR)
- Settings > Automation 画面
- Session Detail > Follow-up タブ
- Tauri OS 通知 plugin 組込み
- backend client + polling loop

---

## 6. バックエンド単体での cron 動作テスト手順

> Phase 2-B 着手者が `:run` endpoint と Cloud Scheduler 連携を **本物のスケジューラ無しで** 検証するための手順。dev tag deploy を前提とする。

### 6.1 前提
- master ID Token 取得手段: `python tools/master_pre_deploy_smoke.py` 内の `_mint_id_token()` を流用 (Master uid `cfdXMsjPXfea8OsidGQtXrSZOfP2`, accountId `Jwb9VwA4kkfOLQh7PVZ9`)
- `INTERNAL_SCHEDULER_SECRET` を dev 環境で適当な乱数文字列に設定:
  ```bash
  SECRET=$(openssl rand -hex 32)
  gcloud run services update deepnote-api \
    --region asia-northeast1 \
    --update-env-vars INTERNAL_SCHEDULER_SECRET="$SECRET" \
    --no-traffic --tag dev \
    --project classnote-x-dev
  ```

### 6.2 Step 1 — task 作成 (現状で可能)

```bash
ID_TOKEN="$(python -c '
import sys; sys.path.insert(0, "tools")
import master_pre_deploy_smoke as m
print(m._mint_id_token())
')"

DEV=https://dev---deepnote-api-mur5rvqgga-an.a.run.app

curl -sS -X POST "$DEV/v1/scheduled-tasks" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "daily_open_todos",
    "channel": "line",
    "destination": {"lineUserId": "U_master_dummy"},
    "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    "timezone": "Asia/Tokyo",
    "enabled": true,
    "output": {"includeSummary": true, "includeTodos": true}
  }' | jq
# → { taskId: "st_…", nextRunAt: "2026-05-09T00:00:00Z", ... }
```

### 6.3 Step 2 — 一覧取得 (現状で可能)
```bash
curl -sS "$DEV/v1/scheduled-tasks" \
  -H "Authorization: Bearer $ID_TOKEN" | jq '.tasks | length'
```

### 6.4 Step 3 — 手動 fire (`:run` が新設されたら)
```bash
TASK_ID="st_xxxxxxxxxxxxxxxx"
curl -sS -X POST "$DEV/v1/scheduled-tasks/$TASK_ID:run" \
  -H "Authorization: Bearer $ID_TOKEN" | jq
# 期待: { fired: true, dispatchOutcome: "ok", lastRunAt: "...", notificationEventId: "..." }
```

`:run` が無い間の代替 — `nextRunAt` を強制的に過去に倒して tick で拾わせる:
```bash
curl -sS -X PATCH "$DEV/v1/scheduled-tasks/$TASK_ID" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# → 直接 Firestore で nextRunAt を 1 hour ago に書き換える (firebase_admin SDK)
python - <<'PY'
import sys; sys.path.insert(0,'tools')
import master_pre_deploy_smoke as m
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timedelta, timezone
firebase_admin.initialize_app(credentials.Certificate('classnote-api-key.json'))
db = firestore.client()
TASK_ID = "st_xxxxxxxxxxxxxxxx"
ref = (db.collection('accounts').document(m.MASTER_ACCOUNT_ID)
         .collection('scheduled_tasks').document(TASK_ID))
ref.update({'nextRunAt': datetime.now(timezone.utc) - timedelta(hours=1)})
print('nextRunAt rewound')
PY
```

### 6.5 Step 4 — tick を直接叩く (現状で可能だが認証強化前)
```bash
SECRET="<value set in 6.1>"
curl -sS -X POST "$DEV/internal/scheduler/tick" \
  -H "X-DeepNote-Internal-Token: $SECRET" \
  -w '\nHTTP %{http_code}\n'
# 期待: { due: 1, fired: 1, failures: 0 }
# 注: SECRET 未設定の現状は誰でも 200 で叩ける (§3.2 参照)
```

### 6.6 Step 5 — 結果確認

#### 6.6.1 Firestore で `lastRunAt` / `nextRunAt` を確認
```python
ref.get().to_dict()
# {'lastRunAt': <datetime>, 'lastRunOutcome': 'ok', 'nextRunAt': <next>, ...}
```

#### 6.6.2 LINE 側到達確認 (channel=line の場合)
master の LINE DM に push 済かどうかを LINE Developer Console の Messaging API ログで確認 (本番は実トラフィックなのでテスト時は dummy lineUserId に倒すか、push をモックする)。

#### 6.6.3 Cloud Logging
```bash
gcloud logging read 'resource.labels.service_name="deepnote-api" "scheduler.tick"' \
  --project classnote-x-dev --limit 5 \
  --format='value(timestamp,textPayload)'
```

### 6.7 Step 6 — Cloud Scheduler の本物との連携テスト
```bash
gcloud scheduler jobs create http deepnote-scheduler-tick-dev \
  --location asia-northeast1 \
  --schedule "*/5 * * * *" \
  --time-zone "Asia/Tokyo" \
  --uri "$DEV/internal/scheduler/tick" \
  --http-method POST \
  --headers "X-DeepNote-Internal-Token=$SECRET" \
  --project classnote-x-dev

# fire を強制
gcloud scheduler jobs run deepnote-scheduler-tick-dev \
  --location asia-northeast1 \
  --project classnote-x-dev

# Cloud Logging で受信を確認
gcloud logging read 'resource.labels.service_name="deepnote-api" httpRequest.requestUrl=~"scheduler/tick"' \
  --project classnote-x-dev --limit 3
```

テスト終了後、dev ジョブは削除:
```bash
gcloud scheduler jobs delete deepnote-scheduler-tick-dev \
  --location asia-northeast1 \
  --project classnote-x-dev --quiet
```

### 6.8 Step 7 — Cleanup
```bash
# task を削除
curl -sS -X DELETE "$DEV/v1/scheduled-tasks/$TASK_ID" \
  -H "Authorization: Bearer $ID_TOKEN" -w '\nHTTP %{http_code}\n'
# 期待: HTTP 204

# dev tag deploy を撤去
gcloud run services update-traffic deepnote-api \
  --region asia-northeast1 \
  --remove-tags dev \
  --project classnote-x-dev --quiet
```

---

## 7. 参照

- `app/routes/scheduled_tasks_routes.py` — REST + tick 実装
- `app/services/scheduled_tasks.py` — RRULE パーサ + `find_due` + `mark_run`
- `app/services/assistant_briefing.py` — `pre_meeting_briefing` / `session_followup` の dispatcher
- `docs/scheduled-digests-spec.md` — 旧 Phase B-spec、Cloud Scheduler 例 (この doc とは別系統で morning-digest 実装、現状 `/internal/tasks/run_morning_digests` は **未実装**)
- `tools/master_pre_deploy_smoke.py` — master ID Token 発行のリファレンス
- `~/Projects/deepnote-contracts/quality/backend-deploy-checksheet.md` §7.5 — master pre-deploy

## 8. 本レポートの sunset

Phase 2-B Desktop Automation 実装 PR が merge され、本レポートで挙げた「必須」項目がすべて消化されたら本ファイルは削除して構いません。
