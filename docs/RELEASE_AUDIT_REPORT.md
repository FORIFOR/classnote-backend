# Release Readiness Audit Report (2026-01-19)

## 概要
リリースに向けたバックエンド機能の網羅的チェック結果。
各機能の実装詳細を確認し、潜在的なバグ、仕様不整合、リスクを記録しました。

## 凡例
- 🔴 **CRITICAL**: データ消失、課金回避、セキュリティホールなど、リリース不可レベルの問題。
- 🟡 **WARNING**: UX低下、エッジケースでのエラー、運用負荷増などの懸念事項。
- 🔵 **INFO**: 将来的な改善点、仕様確認事項。

---

## 1. セッション管理 & 同期 (Session Sync)

### 🔴 Session Duplication / Data Split [FIXED]
- **Status**: ✅ **FIXED (v00412)**
- **Issue**: `create_session` が `clientSessionId` を正しく解決できず、同一クライアントセッションに対して複数のサーバーセッションID（UUID）を発行してしまう問題があった。
- **Fix**: クリエイト時に `clientSessionId` クエリを実行し、既存セッションがあればそれを返すように修正済み。
- **Verification**: コードレビューにより、`clientSessionId` による事前ルックアップとID統一ロジックを確認。

---

## 2. 広告機能 (Ads)

### 🔵 Feature Disabled [FROZEN]
- **Status**: ✅ **DISABLED (v00413)**
- **Detail**: ユーザーリクエストにより、広告機能は無効化されました。
- **Implementation**: `app/routes/ads.py` の `get_placement` エンドポイントが常に `ad: None` を返すように変更済み。コード自体は残存しているが、機能しない状態。

---

## 3. 課金ガード & プラン制限 (Cost Guard)

### 🔵 Premium "Soft Limit" for Server Sessions
- **Status**: ✅ **Spec Compliant**
- **Detail**: Premiumプランの `server_session` 上限 (300件) は、`CostGuard` でブロックされず (`pass`)、保存自体は成功します。
- **Risk**: サーバーの一時的なストレージ肥大化。ただし、次回同期時などにクリーンアップされる前提の設計。

---

## 4. 非同期タスク (Task Queue)

### 🟡 Inflight Decrement Risk (Edge Case)
- **Status**: ⚠️ **Code Smell**
- **Detail**: `app/routes/tasks.py` の各タスクハンドラにおいて、リクエストJSONのパースに失敗した場合や、ペイロード内に `userId` が含まれていない場合、インフライトカウンタ（処理中のタスク数）のデクリメントが行われない可能性があります。
- **Mitigation**: 現状のエンキュー側 (`task_queue.py`) は正しく `userId` を付与しているため、実害は低いですが、将来的にAPI経由などで不正なタスクリクエストが投げられた場合、そのユーザーのインフライト枠が「埋まったまま」になるリスクがあります。

---

## 5. 推奨アクション

1. **ゾンビセッションの監視**: 同期修正を入れましたが、修正前に作られた「二重セッション」が残っている可能性があります。これらは自動では消えません。
2. **モニタリング**: Cloud Run のログで `[CostGuard] BLOCKED` が多発していないか確認してください。
