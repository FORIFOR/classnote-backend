# DeepNote Clow Phase 1.5 Release

- **Date**: 2026-05-07
- **Production revision**: `deepnote-api-00400-jeg`
- **Stable tag**: `stable-2026-05-07-bot-clow-phase1.5`
- **PR**: [#29](https://github.com/FORIFOR/classnote-backend/pull/29)
- **Feature branch**: `feat/bot-clow-phase1.5-connect-and-session-picker`
- **Feature commit**: `9defeea4`
- **Merge commit on main**: `80b80ddd`

## Summary

LINE group Smart Share Bot の安全性と会議選択 UX を改善。Phase 1 の "implicit owner promotion at `DeepNote 接続`" と "auto-pick-latest on no shared data" の 2 つの暗黙挙動を、明示的な Flex confirm / picker 経由の **2-step consent** に変更しました。

## Changes

- LINE group connect confirmation card
  - `DeepNote 接続` → 即時 binding は廃止
  - 「セッション参照・クレジット消費・owner 権限付与」を card 本文で明記
  - postback 押下者 == 元の発言者 を `u=<line_user_id>` 検証
- Pending connect confirmation via postback
  - `action=group_connect_confirm` のみ `create_group_link`
  - confirm 時に DM-link / already-connected を再検証(race protection)
- Session picker for recent meetings
  - `group_shared_briefing.get_recent_any_sessions(limit=5)` で 3-5 件候補
  - Flex bubble、各行 `share_confirm` postback
  - `isDeleted` / `deletedAt` 付き session は picker から除外
- Additional `share_confirm` guards (owner re-verification at execution)
  - `session_not_found`
  - `session_deleted`
  - `ownership_mismatch`
  - `not_on_group_acl`
- Bot audit events for connect / picker / share confirm
  - `group_connect_request/{card_shown,requester_not_linked,already_connected}`
  - `group_connect/{ok,cancelled,confirm_user_mismatch,requester_not_linked_at_confirm,already_connected_at_confirm}`
  - `session_picker/shown_N`
  - `share_confirm/{ok,session_not_found,session_deleted,ownership_mismatch,not_on_group_acl,cancelled,requester_not_linked}`

## Validation

- **Master Readiness**: PASS 12/12 (`tools/master_pre_deploy_smoke.py`)
- **Route diff prod vs dev**: 0 (zero loss)
- **Tests**: 99 pass / 2 skip / 0 fail
  - +12 new in `tests/test_bot_phase1_5_connect_and_picker.py`
  - 0 regression in `tests/test_bot_*` / `tests/test_*_webhook.py` / `tests/contract`

## Files

- `app/services/group_shared_briefing.py` — `get_recent_any_sessions(limit=5)` 追加
- `app/services/line_messaging.py` — `flex_message` helper 追加
- `app/routes/integrations_line.py` — connect confirm card / session picker / postback handler / share owner re-verification
- `tests/test_bot_phase1_5_connect_and_picker.py` — 12 unit tests

## Scope NOT included (deferred to Phase 2)

- ❌ Transformation commands (mail / Slack post / agenda / reminder)
- ❌ Admin add request → owner approve flow
- ❌ Per-group Q&A permission settings
- ❌ DM scheduled task UX

## Rollback

```bash
gcloud run services update-traffic deepnote-api \
  --region asia-northeast1 \
  --to-revisions deepnote-api-00397-quw=100 \
  --project classnote-x-dev
```

`stable-2026-05-07-p0-compat-layer` (revision `00397-quw`) is the immediately-prior stable.
