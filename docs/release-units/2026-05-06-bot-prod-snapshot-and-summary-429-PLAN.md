# Release Unit Plan — Bot prod snapshot + Summary 429 + DTO alignment

- **Branch**: `fix/bot-prod-snapshot-and-summary-429`
- **Base**: `feat/bot-frontend-fallback` (`904501f9`)
- **Tip**: `a28cd9cc`
- **Date**: 2026-05-06
- **Driver**:
  1. Vertex AI 429 で要約 / クイズが Cloud Tasks 最終リトライ後も "running" のまま固まる本番障害
  2. iOS の `POST /users/bootstrap` / `GET /system/status` が DTO 不一致で decode 失敗
     ("key 'plan' / 'mode' not found")
  3. 本番 deploy 済みの uncommitted hotfix を branch にも commit して
     `git history = production` に揃える

## Scope

3 commits:

1. `65f7ebb7` chore(release): production hotfix state (00342-xiq snapshot)
   - 既に本番動作している `compat_aliases.py /system/* + /users/bootstrap stub`、
     `sessions.py auto-create / auto-summary / tombstone`、`tasks.py title
     promote`、`ops.py instrumentation`、`llm.py 旧 retry (3 attempts /
     1.5+4.0s)` を branch に commit
   - **新しい挙動を入れる commit ではなく**、production = branch を
     一致させるためのスナップショット
2. `ca9b2d3d` fix(routes): align /users/bootstrap and /system/status with iOS DTO contract
   - `compat_aliases.py` の両 stub を iOS `BootstrapResponse` /
     `SystemStatus` 互換 schema に書き換え
   - `/system/status` は `SYSTEM_STATUS_MODE` 等の env-var を読む
3. `a28cd9cc` fix(summary,quiz): survive Vertex AI 429 bursts + persist terminal failure
   - `_generate_with_retry` を強化版 ([2,6,18,45,90]s + ±25% jitter, 6 attempts)
   - `_is_final_cloud_task_attempt` ヘルパ + summarize / quiz / quick の
     最終 attempt で `summaryStatus="failed"` 確定
   - `tools/recover_stuck_summaries.py` (バックフィル CLI)

## Files allowed

- `app/routes/compat_aliases.py` — DTO 修正
- `app/services/llm.py` — retry 強化 (旧版置き換え)
- `app/routes/tasks.py` — 最終 attempt failed 確定
- `tools/recover_stuck_summaries.py` — 新規バックフィル CLI
- `app/routes/sessions.py`, `app/routes/ops.py` — production snapshot
  commit のみで触れている (新規変更なし)
- `docs/release-units/2026-05-06-*-PLAN.md` × 2 (snapshot + 本書)

## Files NOT allowed (out of scope)

- `app/routes/users.py` — bootstrap 重複登録回避のため新規 handler 追加せず
- `app/main.py` — system route 別 register せず (compat_aliases に集約)
- `feat/bot-frontend-fallback` の bot Phase 1-8 commits — 既に上流に存在、変更なし

## Acceptance criteria

1. `python3 -m py_compile` 全ファイル ✅
2. route-inventory diff: production routes 消失 0 (`/health`, `/version`,
   `/sessions/{id}/share_link` の 3 件は AST scanner の false negative
   で `@app.get` / `@router.api_route` を拾わないだけ — 実コードに健在)
3. master-user pre-deploy test (`master-user-pre-deploy-test-instructions.md`)
   Step 1〜15 を全 PASS で完了
4. `curl POST /users/bootstrap` で iOS DTO 必須キー (`plan`, `featureGates`,
   `needsPhoneVerification`, `canonicalized`, `claimsRefreshRequired`,
   ...) が全部入った 200 JSON を返す
5. `curl GET /system/status` で `mode` キー込みの 200 JSON を返す
6. dev tag deploy → 30 分窓で `[LLM] gave up` 件数測定 + iOS 起動時の
   `[AppConfigStore] Primary system/status failed` / `[AuthCoordinator]
   keyNotFound 'plan'` 解消を確認
7. production smoke `SMOKE-003` / `SMOKE-006` / `SMOKE-009` 5 分後 / 30 分後

## Risks / mitigations

| リスク | 対応 |
|---|---|
| `compat_aliases` の `/users/bootstrap` 旧 shape を期待する別クライアント (Watch / Desktop) | iOS のみ `postBootstrap` を呼ぶことを APIClient grep で確認済 (Desktop は Tauri invoke、Watch は別 endpoint) |
| `SYSTEM_STATUS_MODE` 未設定 → `normal` fallback | デフォルト動作をコード内で明示。env unset = normal モードで安全 |
| Vertex AI retry 強化で Cloud Run timeout 逼迫 | 1 リクエスト最大 ~161s、Cloud Run timeout 3600s に対し十分余裕 |
| 30分以上前の "running" セッションを `recover_stuck_summaries.py` で再 enqueue した瞬間に再度 quota burst | `--throttle-seconds 0.5` で分散 + `--limit N` で段階実行 |

## Out-of-scope follow-ups

- D. Vertex AI 永続 quota 増額申請 (GCP コンソール)
- bot-frontend-fallback branch 自体が Phase 1-8 混在で H2 違反の状態。今回の
  release unit はそれを改善しない (既存の罪)。次の整理 release unit で扱う
- `fix/summary-vertex-429-resilience` (`aeb44416`) と
  `fix/missing-bootstrap-system-status-routes` (`ab9e01bc`) の旧 branch は
  本 branch で置き換えられたため、参照のみで deploy には使わない
