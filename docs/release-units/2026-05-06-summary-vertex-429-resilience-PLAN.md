# Release Unit Plan — Summary / Quiz Vertex AI 429 Resilience

- **Branch**: `fix/summary-vertex-429-resilience`
- **Base**: `main` (`10241c7d`)
- **Date**: 2026-05-06
- **Driver**: 多数ユーザー (master 含む) のセッションが `summaryStatus="running"` で固まる本番障害

## Scope

Vertex AI Gemini API の `429 ResourceExhausted` (asia-northeast1 quota burst)
で要約 / クイズの Cloud Tasks タスクが失敗 → Cloud Tasks リトライ枯渇後も
Firestore のステータスが `running` のまま残り、UI が「処理中」永久ローディング
になる事象を解消する。

## Files allowed

- `app/services/llm.py` — `_generate_with_retry` 追加 + `_timed_llm_call` 経由化
- `app/routes/tasks.py` — `_is_final_cloud_task_attempt` ヘルパ + summarize / quiz / quick_summary の transient 失敗パスを最終 attempt で `failed` 確定に変更
- `tools/recover_stuck_summaries.py` — 既存スタックセッションのバックフィルツール (新規)
- `docs/release-units/2026-05-06-summary-vertex-429-resilience-PLAN.md` — 本書

## Files NOT allowed (out of scope)

- `app/routes/sessions.py` — 別 hotfix (auto-create / auto-summary / tombstone) は別 release unit
- `app/routes/compat_aliases.py`, `app/routes/ops.py` — 別件
- bot 関連 (`feat/bot-frontend-fallback` の Phase 1〜8) — 既に別 branch にコミット済み

## Changes

### A. `_generate_with_retry` (llm.py)

- `[2.0, 6.0, 18.0, 45.0, 90.0]` 秒のバックオフ + ±25% jitter
- 計 6 attempts (初回 + 5 retries)、累積最大 ~161 秒
- 例外クラス (google.api_core 由来) と文字列パターン両方で transient 判定
- 既存の `_timed_llm_call` 全 path を `_generate_with_retry` 経由に変更

### B. `_is_final_cloud_task_attempt` + 失敗確定 (tasks.py)

- Cloud Tasks の `X-CloudTasks-TaskRetryCount` を読む。`maxAttempts=3` の最終
  attempt 判定 (`>= 2`)。
- summarize / quiz / summarize_quick の except パス:
  - 非最終 attempt + transient → 503 raise (Cloud Tasks リトライ継続)
  - 最終 attempt OR 非 transient → `summaryStatus="failed"` / `quizStatus="failed"` を
    Firestore に確定書き込み + 200 を返して Cloud Tasks 完了扱い
- `derived/{summary,quiz}.finalAttempt: bool` を追加し原因分析容易化

### C. `tools/recover_stuck_summaries.py`

- `summaryStatus IN ("queued","running")` かつ
  `summaryUpdatedAt <= now - {age-minutes}` のセッションを抽出
- `--dry-run` で件数確認、再実行時に `enqueue_summarize_task` で再投入
- `--throttle-seconds` で Vertex AI quota 配慮の流量制御 (default 0.5s)
- バックフィル時は `summaryRecoveredAt` を Firestore に書く

## Acceptance criteria

1. `python -m py_compile app/services/llm.py app/routes/tasks.py tools/recover_stuck_summaries.py` 通過 ✅
2. Branch diff が Files allowed の範囲内 (route inventory diff で消えた route 無し)
3. dev tag deploy → 30 分間の `[LLM] gave up` ログ件数を計測
4. 同じ 30 分窓で `summaryStatus="running"` 滞留セッション数が単調減少
5. master-user pre-deploy test (Step 1–15) PASS
6. Production smoke (`SMOKE-003`, `SMOKE-006`, `SMOKE-009`) は本変更で影響しないが念のため deploy 後 5 分後 / 30 分後の 2 回実施

## Risks / mitigations

| リスク | 対応 |
|---|---|
| 1 件あたり最大 161 秒の retry 待機で Cloud Run timeout (3600s) を逼迫 | summarize / quiz の他処理は既に短時間。複数 attempt が同一インスタンスで重なっても 5 並列上限内 |
| `finalAttempt` フィールドがクライアント側で未定義扱い | derived ドキュメント拡張は許容。iOS は未参照 |
| バックフィル実行で再度 quota burst | `--throttle-seconds 0.5` + `--limit` で段階実行 |

## Out-of-scope follow-ups

- D. Vertex AI 永続 quota 増額申請 — GCP コンソールから別途
- 既存 uncommitted hotfix (sessions.py auto-create / tasks.py title promote) の
  別 release unit としての整理
