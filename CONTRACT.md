# classnote-api — CONTRACT.md

このリポジトリは DeepNote backend の実装。**正本仕様 = `~/Projects/deepnote-contracts`**。

## 参照する正本

- `../deepnote-contracts/api/openapi.yaml`
- `../deepnote-contracts/api/endpoints-map.md`
- `../deepnote-contracts/api/api-id-registry.md`
- `../deepnote-contracts/api/legacy-endpoints.md`
- `../deepnote-contracts/data/firestore-schema.md`
- `../deepnote-contracts/data/model-mapping.md`
- `../deepnote-contracts/quality/regression-pack.md`
- `../deepnote-contracts/quality/backend-deploy-checksheet.md`
- `../deepnote-contracts/quality/rollback-protected-fixes.md`
- `../deepnote-contracts/quality/lost-fixes-review.md`
- `../deepnote-contracts/quality/route-inventory-diff.md`
- `../deepnote-contracts/quality/production-smoke-checklist.md`
- `../deepnote-contracts/quality/release-unit-template.md`
- `../deepnote-contracts/reports/canonicalization-plan.md`

## Backend が所有する責務

- API 実装 (FastAPI)
- Firestore スキーマ
- Cloud Tasks ジョブ
- Artifact 生成
- Usage / billing 計測
- 認証 / 権限制御
- 外部連携 webhook

## Backend がやってはいけないこと

- canonical の response field を削除
- enum 値の意味を変える
- production で 200 を返している path を `legacy-endpoints.md` 経由なしに削除
- protected fix (Billing / Auth / Data Migration / Webhook) を失う rollback
- 1 branch に複数の release unit を混在
- `git reset --hard` / `git clean` / dirty deploy

## Mandatory Gates

- **Deploy Gate**: `AGENTS.md` H1 + `quality/backend-deploy-checksheet.md`
- **Rollback Gate**: `AGENTS.md` H4 + `quality/rollback-protected-fixes.md` + `quality/lost-fixes-review.md`
- **Block Recovery**: `AGENTS.md` H6
- **Production Smoke**: deploy 後必ず `production-smoke-checklist.md` の SMOKE-* 実行

## Implementation map

- `app/main.py` … router 登録、middleware、`/health`, `/version`
- `app/routes/*` … endpoint 実装
- `app/services/*` … domain 層
- `app/services/integrations/*` … OAuth クライアント
- `app/task_queue.py` … Cloud Tasks enqueue
- `app/dependencies.py` … 認証
- `app/admin_auth.py` … 管理者認証
- `tools/deploy.sh` … deploy script (CLAUDE.md memory の cpu-throttling 等を含む)

## 既知の Critical (`canonicalization-plan.md`)

- V-000 merge_migration 二重定義 (Phase 1 done)
- V-026 main hygiene (PR #22 で main merge 済)
- V-029 folders regression (PR #22 deploy で解消予定)
- V-033 /version endpoint (PR #23 で実装、deploy 待ち)
