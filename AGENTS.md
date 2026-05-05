# classnote-api — AGENTS.md

正本 = `~/Projects/deepnote-contracts/`。各 Hard Rule の詳細は contracts repo の該当 doc を **必ず読み、従う**。

# Hard Rules (例外なし)

## H1. No Deploy Proposal Before Gate

deploy / `gcloud run deploy` / traffic 切替 / Cloud Run revision 作成 / tag 更新を提案・実行する前に必ず:

1. `~/Projects/deepnote-contracts/quality/backend-deploy-checksheet.md` を全部埋める
2. `bash ~/Projects/deepnote-contracts/tools/run_audit.sh`
3. `python3 ~/Projects/deepnote-contracts/tools/check_release_readiness.py --project backend --strict warning` (Sprint 1) / `critical` (Sprint 2+)
4. **`~/Projects/deepnote-contracts/quality/master-user-pre-deploy-test-instructions.md` の Step 1〜15 を全 PASS**
   - master uid: `cfdXMsjPXfea8OsidGQtXrSZOfP2`
   - accountId: `Jwb9VwA4kkfOLQh7PVZ9`
   - email: `horio.shuhei98@gmail.com`
   - 必須確認: Firestore folders / `/v1/users/me` plan / `/v1/folders` / legacy `/folders` / `/v1/sessions` / `/v1/config/client` / audit Critical 増加なし
5. Release Readiness Report が **PASS** であること

PASS でない限り「deploy してよいですか?」と聞いてはいけない。
**Master User Pre-Deploy Test が PASS でない限り production deploy は禁止**。 PARTIAL は staging / dev tag まで。 FAIL は deploy 禁止。

## H2. 1 branch = 1 release unit

実装着手前に `~/Projects/deepnote-contracts/quality/release-unit-template.md` を埋め、`reports/release-readiness/<date>-<short>-PLAN.md` に保存する。

- branch 名は目的を表す: `fix/backend-folders-regression` ✅ / `feat/integrations-google-microsoft-oauth-...` (混在) ❌
- Files allowed / NOT allowed を明示
- out-of-scope ファイルが変更に混ざったら即停止

## H3. Production-existing functions are protected

production で 200 を返している API / 機能は、仕様書未登録でも **守るべき機能**。

deploy 前に必ず `~/Projects/deepnote-contracts/quality/route-inventory-diff.md` の手順:
1. 現行 production OpenAPI と新 build の path 集合を diff
2. 消えた route が `legacy-endpoints.md` に sunset 済みでなければ deploy 禁止

今回の `/folders` regression は本手順で機械的に検出できた。

## H4. No rollback before lost-fixes review

rollback (`update-traffic --to-revisions=<old>=100` 等) を提案する前に必ず:

1. `~/Projects/deepnote-contracts/quality/rollback-protected-fixes.md` を読む
2. `~/Projects/deepnote-contracts/quality/lost-fixes-review.md` テンプレを埋める
3. lost fixes に protected category (Billing / Auth / Data Migration / Webhook) があれば **rollback 禁止 → roll-forward hotfix**

protected fix を失う rollback は禁止。今回の `00266-98l → 00232-jux` rollback で plan fix `ff1bce1c` を失い standard ユーザが free 表示された事故が該当。

## H5. Production smoke after every deploy / rollback

deploy / rollback / hotfix 後に必ず `~/Projects/deepnote-contracts/quality/production-smoke-checklist.md` の SMOKE-* を 5 分後 / 30 分後 の 2 タイミング実行。

特に必須:
- **SMOKE-003** (`GET /users/me/entitlement` で plan 確認)
- **SMOKE-006** (`GET /folders`)
- **SMOKE-009** (`POST /v1/chat/send`)

## H6. Block Recovery Routine

Gate が `BLOCKED` / `FAIL` で終了せず、以下を実施:

1. BLOCK 理由を `BLOCK-CAT-001..009` で分類
2. Intended release unit を 1 文で記述
3. Contaminating changes を列挙
4. 復旧案 3 つ + 推奨案
5. 安全コマンド計画 (`git stash` / `git switch -c` / `git apply` 等。**`git reset --hard` / `git clean` / dirty deploy 禁止**)
6. clean branch + 再 Gate
7. PASS してから初めて deploy 提案

## H7. Coding Agent vs Production Manager Agent 分離

| | Coding Agent | Production Manager Agent |
|---|---|---|
| 役割 | コードを書く / テスト / PR 作成 | Gate / revision / traffic / rollback / deploy 判断 |
| やってよい | branch / commit / push / PR | `update-traffic` / `gcloud run deploy` / rollback judgement |
| やってはいけない | deploy / rollback / traffic 切替 | コードを書く |

緊急対応 (rollback / hotfix 判断) は Production Manager Agent。コード変更が必要なら Coding Agent に依頼を分ける。

## H8. /version endpoint で revision 同定

deploy 後は `GET /version` で `gitCommit` / `cloudRunRevision` / `contractsCommit` を必ず確認。rollback 前にこの値を `rollback-protected-fixes.md` と照合する。

# 必読 (deepnote-contracts/quality/)

| ファイル | 用途 |
|---|---|
| `regression-pack.md` | REG-BE-* 一覧 (REG-BE-017 plan, REG-BE-019 folders, REG-BE-023 master user smoke は **Critical**) |
| **`master-user-pre-deploy-test-instructions.md`** | **production deploy 前必須**。master uid `cfdXMsjPXfea8OsidGQtXrSZOfP2` で全 15 step PASS が条件 |
| `backend-deploy-checksheet.md` | deploy 前必須 9 章 |
| `rollback-protected-fixes.md` | rollback で失ってはいけない fix の正本 |
| `lost-fixes-review.md` | rollback 前テンプレ |
| `route-inventory-diff.md` | deploy 前 route diff 手順 |
| `production-smoke-checklist.md` | SMOKE-001..014 |
| `release-unit-template.md` | 実装着手前テンプレ |
| `frontend-build-gate.md` | (該当なし) |
| `implementation-done-checksheet.md` | PR ready 時 |

# 禁止コマンド (明示承認なし)

- `git reset --hard`
- `git clean`
- `git push --force` (main / 出荷 branch)
- `gcloud run deploy --source .` を dirty worktree で
- `update-traffic` を `lost-fixes-review.md` 未実施で
- 未確認の他人 commit を勝手に commit
- `compat_aliases.py` の sunset 期日チェックなし削除

# 開発ルーティン (推奨)

```
1. release-unit-template.md を埋める          (← まずここ)
2. deepnote-contracts (openapi / endpoints-map / etc) を更新
3. 実装 (Files allowed のみ)
4. AST + ruff + pytest + audit
5. PR 作成 + checksheet 貼付
6. Production Manager Agent に deploy 判断を依頼
```
