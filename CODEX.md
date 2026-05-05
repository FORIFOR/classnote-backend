# classnote-api — CODEX.md

Codex は [`AGENTS.md`](./AGENTS.md) の Hard Rules H1〜H8 を **必ず読み、従う**。

正本 = `~/Projects/deepnote-contracts/`。

特に:
- **deploy / rollback の前**に `quality/backend-deploy-checksheet.md` + `lost-fixes-review.md` + `rollback-protected-fixes.md` を必ず通す
- **1 branch = 1 release unit**（混在 branch 禁止）
- **production で動いている route を消す deploy は禁止**（route-inventory-diff で機械検出）
- **protected fix（Billing / Auth / Data Migration / Webhook）を失う rollback は禁止**

詳細は AGENTS.md と `~/Projects/deepnote-contracts/quality/*` を参照。
