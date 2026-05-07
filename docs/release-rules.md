# Release Rules — DeepNote backend

This file is the **operational contract** for any change to this repo.
Coding Agents (and humans) MUST read this before opening any PR or running
any deploy.

The driving principle:
> **コードを前に進める** のではなく **本番で守るべき機能を壊さずに、正本仕様に沿って小さく前に進める**。

---

## 1. 1 変更 = 1 release unit

- 1 branch = 1 目的 = 1 PR = 1 deploy
- 違うテーマを 1 branch に混ぜない (`feat/google + folders + plan-fix` は禁止)
- branch 名は `<kind>/<release-unit-id>-<short-slug>` 形式
  - `kind`: `feat` / `fix` / `chore` / `refactor` / `hotfix`
  - 例: `fix/REG-BE-019-folders-regression`

## 2. Implementation Plan を実装より先に書く

実装に手をつける前に、以下を必ず作る (`docs/release-units/<id>.md`):

```markdown
# Release Unit
## 目的
## Scope
## Out of Scope
## Affected Specs
## Affected APIs
## Affected Data Models
## Affected Regression Pack
## Files allowed to change
## Files not allowed to change
## Compatibility Risks
## Tests to add/update
## Gate to run
## Rollback / Roll-forward Plan
```

`Out of Scope` と `Files not allowed to change` を空欄にしない。

## 3. deepnote-contracts を実装より先に更新する

仕様/規約変更が伴う場合の更新順:
1. `product/feature-map.md`
2. `api/api-id-registry.md`
3. `api/openapi.yaml`
4. `api/endpoints-map.md`
5. `api/legacy-endpoints.md`
6. `data/model-mapping.md`
7. `quality/regression-pack.md`
8. `quality/*-gate.md`
9. 実装

## 4. 本番で動いている機能は必ず Regression Pack に入れる

Critical (絶対に消してはいけない):
- auth, billing/plan, account merge/deletion
- session create/list/detail, recording finalize, summary job
- folders など既存ユーザーが触っているエンドポイント

High:
- export, chat, import, share, integrations

Medium:
- UI 補助, aliases, convenience endpoints

## 5. deploy 前 — 「消えたもの」を見る

新機能テストではなく **回帰** チェックを優先する。
deploy 前に必ず:
- 現行 production の route inventory と新 build の route inventory を比較
- 消えた endpoint / 404 になった endpoint / response schema 変更 / auth 要件変更を列挙
- production にあった route を消す deploy は **legacy-endpoints に sunset 済みでなければ BLOCK**

API 削除のライフサイクル:
```
stable → legacy → deprecated → removed
```
いきなり消してはいけない。

## 6. rollback 前 — Lost Fixes Review を必ず行う

rollback 候補 revision と現 revision の `git log <prev>..<current>` を取り、各 commit の機能と重要度を表にする:

| Commit | 機能 | 重要度 | rollback で失う影響 |
|---|---|---|---|
| ff1bce1c | plan entitlement | Critical | standard が free 表示に |

**rollback 禁止条件**:
- billing / plan の修正を失う
- auth / security の修正を失う
- data integrity / migration の修正を失う

該当した場合は rollback ではなく **roll-forward hotfix**:
> `現 revision + 復元したい修正` を新 revision として deploy

## 7. /version で build metadata を返す

本番の rev / commit / contracts commit を `/version` で読めるようにする。
`/version` を見て rollback 判断する。

## 8. Coding Agent と Production Manager Agent を分ける

| Coding Agent | Production Manager Agent |
|---|---|
| 実装、テスト、PR | Gate 検査、rev 監視、traffic、rollback/deploy 判断 |
| deploy しない | コードは書かない |

`rollback でどの修正を失うか` の判定は Production Manager Agent の責務。

## 9. Smoke ユーザを用意する

production smoke のたびに以下を確認:
- `smoke-free-user` → plan = free
- `smoke-standard-user` → plan = standard
- `smoke-folder-user` → folders が見える
- `smoke-session-user` → session list/detail が見える

`docs/production-smoke-checklist.md` 参照 (順次整備)。

## 10. 実装後の必須チェック

A. Spec Check — contracts に反映、OpenAPI、endpoints-map、Regression Pack
B. Diff Check — 関係ない変更が混ざっていない、本番 API が消えていない
C. Test Check — unit / integration / contract / golden flow / smoke
D. Release Check — Deploy Gate / Build Gate / Readiness / rollback plan

## 11. AI への実装依頼テンプレート

```
V-XXX <release-unit-id> を実装してください。
対象外: <out of scope を明記>。
変更してよいファイル: <list>
変更してはいけないファイル: <list>
対象 Feature ID: <FEAT-XXX-NNN>
対象 API ID: <API-XXX-NNN>
対象 Regression ID: <REG-XX-NNN>
```

---

## 直接防ぐべき 5 つ

今回の事故 (folders 復元で plan fix が巻き戻った) を直接防ぐには:

1. **Route inventory diff** (`tools/route_inventory.py`) を deploy 前に必ず実行
2. **Lost Fixes Review** を rollback 前に必ず実行
3. `docs/rollback-protected-fixes.md` で billing/auth/security/data fix を恒常マーク
4. `plan` / `folders` の production smoke
5. **Release Unit Template** (`docs/release-units/<id>.md`) を毎変更で必須化
