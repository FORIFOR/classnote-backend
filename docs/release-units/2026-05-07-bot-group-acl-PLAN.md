# Release Unit Plan — Bot Group ACL & Billing Separation (Phase 1)

- **Branch**: TBD (`fix/bot-group-acl-phase1` 想定)
- **Base**: `main` after current `fix/bot-prod-snapshot-and-summary-429` is merged
- **Date**: 2026-05-07 (planning)
- **Status**: Phase 0 mitigation shipped 2026-05-06 (paid group actions blocked outright). Phase 1 implementation pending design approval.

## Problem statement

LINE グループ / Slack channel の bot は現状、発言者 (`requester`) のリンクから直接 `account_id` を解決し、その人の DeepNote データを返し、その人のクレジットを消費する設計になっている。これだと:

1. **「グループの代表アカウント」を共有する運用が出来ない** — 例: 堀尾さんが自分の議事録をグループ全員に見せたいケース。発言者が誰でも自分のリンクが必要。
2. **代表アカウント方式に変えると今度は爆撃リスク** — グループ全員が代表のクレジットを消費できてしまう。
3. **他人の議事録を見るためのフォーマルな許可フローが無い** — 共有したい・見られたい両方の意思表示が現状コード上に存在しない。

## Design — 3-way separation

すべてのグループ実行を ``requester / data_owner / billing_owner`` の 3 軸で構築:

| ロール | 何 | 例 |
|---|---|---|
| ``requester`` | 実際に LINE/Slack で発言した人 | ``line_user_id="Uxxx"`` |
| ``data_owner`` | どの DeepNote アカウントのデータを見るか | グループ接続時に決まる代表アカウント |
| ``billing_owner`` | どの DeepNote アカウントのクレジットを消費するか | Phase 1 = ``data_owner`` と同一。Phase 3 で分離可 |

判定はすべて ``resolve_group_execution_context(event, intent)`` 1 関数を経由する。

## Storage — new Firestore collections

```
line_group_links/{line_group_id}
  groupName: string
  ownerDeepnoteUid: string                   # data_owner
  billingDeepnoteUid: string                 # Phase 1 = ownerDeepnoteUid
  createdByLineUserId: string
  mode: "owner_account"
  isActive: bool
  createdAt, updatedAt

line_group_acl/{line_group_id}/members/{line_user_id}
  role: "owner" | "admin" | "member"
  canRunPaidActions: bool
  deepnoteUid: string?                       # 個人リンクしている場合のみ
  addedAt, addedBy

line_group_usage/{line_group_id}/days/{YYYYMMDD-JST}
  runCount: int
  paidRunCount: int
  artifactCount: int
  perUser: {<line_user_id>: {runs:int, paid:int, artifacts:int}}

slack_workspace_links/{team_id}/channels/{channel_id}
  ... 同等構造
slack_channel_acl/...
slack_channel_usage/...

line_delegations/{line_group_id}/grants/{target_deepnote_uid}
  allowedRequesterLineUserIds: [string]
  allowedActions: ["latest_summary", "todos", "decisions", "credit_balance"]
  billingOwner: "target_user" | "requester" | "group_owner"
  isActive: bool
  grantedAt
```

## Action classification

```python
INTENT_TIER = {
    # tier=public  : 誰でも実行可、課金なし
    "help":           "public",
    "greeting":       "public",
    "latest":         "public",   # group-shared session を見るだけ
    "decisions":      "public",
    "assets":         "public",
    "pdf":            "public",   # 既存 export PDF link を返すだけ
    "docx":           "public",
    "pptx":           "public",
    # tier=private : 個人情報なので owner 本人 (DM)、group では reject
    "credit":         "private",
    "todos":          "private",
    # tier=paid    : クレジット消費、group では owner/admin のみ
    "assistant_qna":  "paid",
}
```

## `resolve_group_execution_context` flow

```python
def resolve(event, intent_tier):
    rl = parse_source(event)         # line_user_id, group_id
    glink = group_repo.get(rl.group_id)
    if not glink:
        return RequireGroupConnect()

    requester = user_link_repo.find_by_line_user_id(rl.line_user_id)
    acl = group_acl_repo.get(rl.group_id, rl.line_user_id)

    # paid action gate
    if intent_tier == "paid":
        if not acl or not acl.canRunPaidActions:
            return Denied("クレジット消費操作は管理者のみです")

    # usage gate
    if usage_limiter.exceeded(rl.group_id, rl.line_user_id, intent_tier):
        return Denied("本日のグループ利用上限に達しました")

    return ExecutionContext(
        requester_line_user_id=rl.line_user_id,
        requester_deepnote_uid=requester.deepnote_uid if requester else None,
        data_owner_deepnote_uid=glink.ownerDeepnoteUid,
        billing_owner_deepnote_uid=glink.billingDeepnoteUid,
        line_group_id=rl.group_id,
        is_owner=acl.role == "owner" if acl else False,
        is_admin=(acl.role in ("owner", "admin")) if acl else False,
    )
```

## New commands (group only)

| コマンド | 役割 | 権限 |
|---|---|---|
| `DeepNote 接続` | 発言者を owner として ``line_group_links`` 作成 | 任意 (発言者が DM でリンク済み必須) |
| `DeepNote 状態` | 現在の link / role / 上限残量を表示 | 全員 |
| `DeepNote メンバー追加 @x` | x に admin role 付与 | owner |
| `DeepNote 権限解除 @x` | x の admin/member entry 削除 | owner |
| `DeepNote 切断` | ``line_group_links.isActive=false`` | owner |

## Default usage limits (env-overridable)

```
GROUP_MAX_RUNS_PER_DAY=30
GROUP_MAX_PAID_RUNS_PER_DAY=10
GROUP_MAX_ARTIFACTS_PER_DAY=5
GROUP_MAX_RUNS_PER_USER_PER_DAY=10
```

## Files to add / change

### New
- `app/services/group_acl.py` — context resolution + helpers
- `app/services/group_usage_limiter.py` — counter + reset logic
- `app/services/group_link_store.py` — Firestore CRUD for new collections
- `app/util_models.py` — `LineGroupLink`, `GroupAclMember`, `GroupExecutionContext` models
- `tools/init_group_links_from_existing_links.py` — backfill script

### Modified
- `app/routes/integrations_line.py` — group handler の最初に `resolve_group_execution_context` を呼び、`paid` tier をブロック (Phase 0 の置換)
- `app/routes/integrations_slack.py` — 同上
- `docs/release-units/2026-05-07-bot-group-acl-PLAN.md` — 本書

### Out of scope (Phase 2 / 3)
- 個人連携経由で発言者本人の data を返す flow (Phase 2)
- `line_delegations` の代理参照 (Phase 3)
- Workspace / Team 共有領域モデル (Phase 3)
- Slack interactive ACL UI (Block Kit modal) — Phase 2

## Acceptance criteria

1. New Firestore collections created with proper indexes (composite: `line_group_id + line_user_id` on `line_group_acl/.../members`)
2. Greeting / latest / decisions / pdf-link in groups continue to work without ACL changes (public tier)
3. Group `assistant_qna` (paid tier):
   - owner / admin → executes, charges billing_owner
   - member → "クレジット消費操作は管理者のみです" message
   - non-linked requester → connect URL
4. Daily usage cap enforced: 30 runs / 10 paid / 5 artifacts / 10 per-user runs
5. Owner can promote / demote admins via commands
6. Master pre-deploy test (Step 1–15) PASS for non-group routes (group routes need their own test plan)
7. Cloud Tasks (`/internal/tasks/*`) flow continues to use `requester.deepnote_uid` (or `billing_owner` when paid) for cost_guard

## Risks / mitigations

| Risk | Mitigation |
|---|---|
| 既存リンク済 group の挙動変更で互換性破壊 | 既存リンク済 line_user_id を auto-migrate して owner として `line_group_links` に投入する backfill script を実行 |
| ACL 設定漏れの group が無反応に | first-user-to-issue-command が auto-promote owner になる soft fallback (env flag で off 可) |
| 利用上限の固定値が thin | 環境変数 + Firestore override 両方で調整可にする |
| Slack channel は per-channel か per-team か | 設計上は per-channel (channel_id ごとに ACL)。team 横断 ACL は不要 |
| Cloud Tasks worker が account_id をどう解決するか | enqueue 時に payload に `billing_owner_uid` を明示的に詰める。worker 側はその値を `cost_guard` に渡す |

## Phase 0 mitigation (already shipped 2026-05-06)

`integrations_line.py` / `integrations_slack.py` の group ハンドラで `cmd == "assistant_qna"` を即時 deny。クレジット爆撃ゼロ確定。

```python
PAID_GROUP_ACTIONS = {"assistant_qna"}
if cmd in PAID_GROUP_ACTIONS:
    reply("クレジット消費操作はグループ未対応。DM へ。")
    return
```

Phase 1 が deploy される時にこの簡易ガードは ACL ベースに置き換える。

## Test plan (Phase 1)

| 項目 | 期待値 |
|---|---|
| 新グループに `DeepNote 接続` | owner として登録、Firestore に `line_group_links` doc 作成 |
| owner が `最新` | 共有 session があれば返す、無ければ Lv3 確認カード |
| member が `assistant_qna` (`?`) | "クレジット消費操作は管理者のみ" message |
| member が `最新` | public tier、通常通り |
| owner が連投 (上限超え) | "本日の上限に達しました" message |
| 連携解除後 | "DeepNote 接続" を再要求 |
| 既存 link 済 group (auto-migrate) | 既存 `line_link_tokens` の link が group の owner に自動昇格 |

## Estimated effort

- Phase 1 設計確定: 0.5 日 (本書レビュー)
- 実装: 1.5–2 日
- master pre-deploy test + canary deploy: 0.5 日
- 合計: **2.5–3 日**

Phase 2 (個人連携 + 共有領域) は別 release unit、+1.5–2 日想定。
