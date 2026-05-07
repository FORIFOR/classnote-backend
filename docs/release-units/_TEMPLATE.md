# Release Unit — V-XXX

Copy this file to `docs/release-units/<id>-<slug>.md` and fill before any
implementation work starts. PRs without a populated release-unit doc are
to be rejected.

## 目的
<!-- 例: folders API regression を修正する -->

## Scope
<!-- 何を変更するかを列挙 (route, service, schema, etc.) -->

## Out of Scope
<!-- 関係しない領域を必ず列挙する。例: plan, OAuth, Slack, YouTube -->

## Affected Specs
<!-- 例: deepnote-contracts/api/openapi.yaml, endpoints-map.md ... -->

## Affected APIs
<!-- API-* の ID を列挙 -->

## Affected Data Models
<!-- model-mapping.md で記述された entity 名 -->

## Affected Regression Pack
<!-- REG-* の ID を列挙 -->

## Files allowed to change
<!-- 触ってよいファイルを明示 (glob でも可) -->

## Files NOT allowed to change
<!-- 触ってはいけないファイル。空欄禁止 -->

## Compatibility Risks
<!-- 既存 API / DB / クライアント挙動への影響 -->

## Tests to add/update
<!-- unit / integration / contract / golden flow / smoke -->

## Gate to run
<!-- 実装後に通すべき Gate (Build / Deploy / Readiness など) -->

## Rollback / Roll-forward Plan
<!-- 失敗時に rollback できるか、roll-forward が必要か。
     billing/plan/auth/security/data に触れる場合は roll-forward 前提で書く -->
