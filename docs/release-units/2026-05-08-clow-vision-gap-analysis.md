# DeepNote Clow Vision — 現状実装 vs 構想ギャップ監査

- **生成**: 2026-05-08 00:30 JST
- **対象**: classnote-api repo (read-only audit、コード変更なし)
- **背景**: ユーザー提示の「DeepNote v1 vision」(25 章) と、production revision `deepnote-api-00400-jeg` (Phase 1.5 完了時点) の実装乖離を可視化

> **重要**: 本書は **設計判断のスナップショット**。Phase 2-A 着手前の意思決定材料として残す。実装は本書で挙げた優先順に沿って **1 release unit = 1 PR** で進める。

---

## TL;DR

- 完成度ざっくり **35–40%**
- 強い: 基盤レイヤー(Cloud Run / Firestore / Cloud Tasks / STT / summary_v2 bundle 1-shot / chat-bot 全体像 / OAuth integrations / scheduled_tasks 基盤)
- 弱い: 会議後 UX レイヤー(`projectId` / Project Memory / `next_actions` 絞り込み / `share_drafts` 本文生成 / Tool 系 intent / inputDigest cache / Frontend ViewModel)
- 短期最重要 = **next_actions Artifact** + **share_drafts MVP**(LLM 追加コストなしで体感が変わる)
- 中期 = **projectId bridge** → **Project Memory foundation**

---

## セクション別ステータス

凡例: ✅ 完備 / 🟡 部分実装 / ❌ 未着手 / 🔵 別アプローチで実現

| § | 構想要素 | 状態 | 根拠 / 該当ファイル |
|---|---|---|---|
| **1〜3** 全体パイプライン / レイヤー構造 | 🟡 60% | Cloud Run + Firestore + Cloud Tasks 基盤完備。Workflow Layer の job 系も `app/task_queue.py` で完成。Intelligence Layer は `summary_v2` 単一バンドルで部分実現。 |
| **4.1** Session に `projectId` | ❌ | `app/util_models.py` の `CreateSessionRequest` / 既存 session model に `projectId` フィールド無し。プロジェクト束ね不可。 |
| **4.2** Canonical Transcript versioning | ✅ | `transcriptVersion` (int) が session doc に存在 (`sessions.py:2466,2485,2526`)。version inc + return も実装済。 |
| **4.3** Artifact `versions/{version}` サブコレクション | 🔵 | 構想は `artifacts/{type}/versions/{version}` のネスト。実装は `sessions/{id}/artifacts/{type}` 単層 + `version: int` フィールド (`SummaryV2.version=1` 等)。世代保持は無く、最新を上書き。 |
| **5** Meeting Understanding Engine 単独レイヤー | 🔵 | 単独 engine ではなく `summary_v2` が **bundle として** {summary, decisions, todos, openQuestions, topicSummary} を 1 LLM 呼出で返す形で部分実現 (`app/services/llm.py:736-762`)。 |
| **6** TODO 3 段階分類 (explicit / preventive / strategic) | ❌ | `todos[]` はフラット配列のみ。`type` フィールド無し。reason / confidence / riskReduced / expectedImpact 等の根拠フィールドも無い。 |
| **7** Action Candidate Engine | ❌ | `action_candidates` Artifact 不在。todo 候補を多めに出して後で絞る設計になっておらず、LLM が一発で `todos` を最終形として返す。 |
| **8** Minimal Action Planner (1〜3 件絞り込み + scoring) | ❌ | スコアリング (importance/urgency/effort/userBurden) 無し、`hiddenCandidatesCount` 無し、上限制御も `topFocus: ["decisions","todos"]` という弱いヒントのみ。 |
| **9** UI 「決まったこと/次にやること/確認が必要なこと」レイヤー | ❌ | バックエンドは `decisions[]` `todos[]` `openQuestions[]` を素のまま返却。"次にやること" として絞った 1〜3 件 view は無い。 |
| **10.1** `POST /v1/sessions/{id}/artifacts:generate` (汎用) | 🔵 | 形式が異なる: `POST /sessions/{id}/jobs` (`type=summary_v2` 等) で既に同等動作。`/artifacts/{type}:generate` 形は `summary_v2:generate` に存在。 |
| **10.2** `GET /v1/sessions/{id}/artifacts?type=...&version=latest` | 🔵 | `GET /sessions/{id}/artifacts/{type}` 個別エンドポイント (`summary` / `summary_quick` / `summary_v2` / `playlist` / `quiz` / `transcript` / `highlights` の 7 種) で部分実現。汎用 query param 形は無し。 |
| **10.3** `GET /v1/sessions/{id}/action-candidates` | ❌ | endpoint 存在せず、バックエンドモデルも無い。 |
| **10.4** `POST /v1/sessions/{id}/actions/{id}:create-task` | ❌ | 内部 task / Google Tasks / Notion 等への外部作成 endpoint 無し。`scheduled_tasks` は cron 用で別物。 |
| **10.5** `POST /v1/sessions/{id}/share-drafts:generate` (本文生成) | ❌ | 既存 `/sessions/{id}/share_link` は **URL を返すだけ** で、Slack 用 / LINE 用 / メール用の整形済み本文を生成する API は無い。 |
| **10.6** `POST /v1/share-drafts/{id}:send` (確認後送信) | ❌ | `requiresReview` フラグ + 確認 → 送信フロー無し。Smart Share の Lv3 confirm card は **session 単位の共有** であって、タスク化された share_draft オブジェクトを review してから送る仕組みではない。 |
| **11** Jobs API 統一 (root collection `jobs/{jobId}`) | 🟡 70% | `POST /sessions/{id}/jobs` + `GET /sessions/{id}/jobs/{jobId}` は実装済 (`sessions.py:2985, 3389`)、`JobResponse` model あり。ただし root `/v1/jobs/{jobId}` は無し、idempotencyKey はあるが汎用 `inputDigest` 無し。 |
| **12.1** inputDigest キャッシュ | ❌ | `inputDigest` / `prompt_cache_key` / `content_hash` 一切無し。同じ session で 2 回 generate すると毎回 LLM 呼ばれる(idempotency は重複防止のみで再利用ではない)。 |
| **12.2** Bundle 生成 (1 LLM 呼で複数 Artifact) | ✅ | `summary_v2` がまさにこれ。{summary, decisions, todos, openQuestions, topicSummary, highlights, ui_hints} を 1 呼出で生成 (`llm.py:736-762,964-1010`)。 |
| **12.3** 軽量 / 重いモデル使い分け | 🟡 30% | `GEMINI_MODEL_NAME` (flash-lite) と `gemini-2.0-flash` の 2 候補があるが、**選択ロジックが入力長や重要度ベースではなく fallback 用** (`llm.py:258`)。Map-Reduce hierarchical は実装済。 |
| **12.4** ルール処理優先 | 🟡 50% | YouTube import retry / 重複削除 / 期限文字列パース等は ルール側に寄っている。一方 TODO 整形 / 重複排除 / 上限制御は LLM 内部に任せている。 |
| **13** Project Memory (`projects/{id}/memory/...`) | ❌ | コレクション不在。`projectId` 自体が session model に無いので前提が立たない。 |
| **14** `GET /v1/projects/{id}/briefing` | ❌ | endpoint 無し。pre_meeting_briefing は scheduled_task 内部処理として `assistant_briefing.deliver_pre_meeting` がカレンダー連携で実装済だが、**API として外に出ていない**。 |
| **15** Slack / LINE 共有 3 段階 | 🟡 40% | Phase 1 (URL のみ): ✅ `/sessions/{id}/share_link`。Phase 2 (ワンタップ送信): 🔵 Smart Share Lv3 confirm card + postback で実現済。Phase 3 (ルール付き自動共有): ❌(`bot_smart_share` は Lv1/Lv2 の DM digest のみ、ルール条件付き auto-share は未実装で Lv4 として明示的に retired)。 |
| **16** 共有前レビュー / 機密検出 / 宛先確認 / 取消不可表示 | 🟡 30% | Lv3 confirm card で「共有しますか?」の最低限はある。「外部共有警告」「機密情報検出」「宛先と件数の明示」「取消不可文言」**全て未実装**。 |
| **17** Personal Context 設定 (`personalization.{usePastMeetings,…,retentionDays}`) | ❌ | Firestore 上にもモデル無し。ON/OFF UI も無し。 |
| **18** Frontend ViewModel 分離 | ❌(クライアント側) | バックエンドは Artifact をそのまま返す形。`SessionInsightViewModel` 概念は iOS / Desktop 側で実装する必要があるがバックエンド側のサポートは無い (overview-only エンドポイント等)。 |
| **19** Chat Intent Router + Tool 実行 | 🟡 40% | `assistant_hub.handle_message` + `assistant_qna._classify_command` で intent 判定はあり (`ask_session_freeform` / `help` / `unknown`)。ただし intents の **種類が QnA に偏り** で、`generate_pdf` / `send_to_slack` / `create_task` / `compare_with_previous` の Tool 系 intent は未実装。 |
| **20** Speech to Speech | ❌ | streaming_stt_v2 (`app/streaming_stt_v2.py`) で **入力**側の realtime STT はあるが、Realtime TTS / 会議終了後 30 秒音声アシスト は未実装。 |
| **21** Cron / 常駐化 | 🟡 50% | `scheduled_tasks` 基盤実装済、しかし `docs/release-units/2026-05-08-phase2b-desktop-automation-prep.md` の audit にある通り **本番で fire していない**(Cloud Scheduler 未登録、`INTERNAL_SCHEDULER_SECRET` 未投入、`:run` 未実装)。 |
| **22 Phase 0** 土台整理 (transcript/artifact versioning, jobs API 統一, inputDigest, ViewModel) | 🟡 50% | transcriptVersion ✅、jobs ✅(統一は不完全)、artifact versioning 🔵、inputDigest ❌、ViewModel ❌。 |
| **22 Phase 1** 会議後アクション生成 | 🟡 30% | summary_v2 bundle ✅。ただし「next_actions 1〜3 件」「決まったこと/次にやること/確認」UI 用 view と "preventive/strategic" 分類は ❌。 |
| **22 Phase 2** 共有体験 | 🟡 25% | URL リンク共有 ✅、Smart Share Lv3 confirm ✅。share_draft 文生成・送信ログ・PDF/Word 出力は ❌。 |
| **22 Phase 3** タスク・カレンダー連携 | 🟡 40% | Google Calendar / Microsoft Calendar の OAuth + send/create は実装済 (`integrations/google_client.py`, `microsoft_client.py`)。Gmail / Outlook Mail 送信も実装済。しかし「会議後アクション → カレンダー登録 / メール下書き」へのワンタップ動線は未実装。 |
| **22 Phase 4** Project Memory | ❌ |  |
| **22 Phase 5** Chat / Voice Agent | 🟡 20% | LINE/Slack DM の chat 形式 QnA は完備 (`assistant_hub`)。Voice agent は ❌。 |
| **24 MVP** (会議後 5 項目) | 🟡 60% | 1.決まったこと ✅(`decisions[]`)、2.次にやること max3 ❌(絞り込みなし)、3.確認が必要 ✅(`openQuestions[]`)、4.共有文 Slack/LINE/メール用 ❌、5.PDF 出力 ✅(`bot_export_bridge.py` 経由)。 |

---

## 強い部分(構想に対して既に追いついている領域)

1. **音声 → Canonical Transcript** までの取り込みパス: STT (cloud + on-device + import) / `transcriptVersion` 管理 / Cloud Tasks による idempotent 再実行 / batch STT idempotency guard / Webshare YouTube proxy retry
2. **summary_v2 bundle 1-shot 生成** (まさに §12.2 の理想形): {summary, decisions, todos, openQuestions, topicSummary, highlights, ui_hints} を 1 LLM 呼で吐き、Map-Reduce hierarchical も完備
3. **chat-bot 全体像**: LINE/Slack DM + Phase 1 group ACL + Phase 1.5 connect confirm + session picker + paid/private/public tier ゲート + audit
4. **Cloud Tasks / async job 基盤**: 13 種の internal task type、retry policy、cost guard、reservation 系
5. **OAuth integration**: Google Calendar/Drive/Gmail + Microsoft Calendar/Outlook Mail の connect+send 実装済

## 弱い部分(構想までのギャップが大きい領域)

1. **`projectId` と Project Memory 全般** — §13 の前提 `projectId` フィールドが Session に無い時点で、§14 `briefing` も §6.3 strategic TODO も成立しない
2. **TODO の type 分類と絞り込み** — `explicit / preventive / strategic` の概念がコードに存在せず、scoring も上限制御 (1〜3 件) も無い
3. **`share_drafts` レイヤー** — Slack/LINE/メール用の整形済み本文を作って → review → send する API が完全に未実装。現状は session 全体の URL を渡すか summary_v2 を chat に貼るかの 2 択
4. **Tool 系 intent** — `generate_pdf`, `send_to_slack`, `create_task`, `compare_with_previous` 等の Chat → Tool 動線が未実装。今ある intent は QnA 寄り
5. **`inputDigest` キャッシュ** — 同じ入力で再生成時に LLM を skip する仕組みが無い
6. **Frontend ViewModel** — Server 側で Overview 専用エンドポイントを出す動きが無く、クライアントは Artifact を素のまま消費

---

## 実装判断

本監査により、DeepNote v1 Vision に対して現在の classnote-api は基盤レイヤーが強く、会議後 UX レイヤーが弱いことが分かった。

短期では Project Memory よりも、ユーザー体感に直結する以下を優先する。

1. **`next_actions` artifact**
   - summary_v2 の `todos / decisions / openQuestions` から **LLM 追加呼び出しなし** で 1〜3 件へ絞る
   - 会議後 Overview UI の中核にする
2. **`share_drafts` MVP**
   - Slack / LINE / email / markdown 用の共有文を生成
   - `requiresReview: true` を基本にする
   - 既存 bot / integration 送信機能へ接続する
3. **`projectId` bridge**
   - Session に `projectId` を追加
   - Project Memory / briefing / strategic TODO の土台にする

実装順は、ユーザー体感を優先して **`next_actions` → `share_drafts` → `projectId`** とする。
ただし、DB schema 変更を先にまとめたい場合は `projectId` を先行してもよい。

---

## 後続 release unit のスケッチ

> 各 unit は **1 branch / 1 PR / 1 release unit** で進める。LLM 呼出の追加コストを最小に保つ。

### Release Unit 1: `feat/next-actions-artifact` (最優先)

- 目的: `summary_v2` から派生する `next_actions` Artifact を新設、最大 3 件・hiddenCandidatesCount 付き
- 配置: `sessions/{sessionId}/artifacts/next_actions` (既存 単層 Artifact 流儀に揃える)
- 入力: `summary_v2` Artifact のみ。LLM は呼ばない (rules_v1)
- 型分類: `todos[]` 由来 → `explicit` / `openQuestions[]` 由来 → `preventive` / `decisions[]` 由来で方針的なもの → `strategic`
- 絞り込み: 重複タイトル除外 + max 3 + 候補ゼロ時の fallback action
- API: `POST /sessions/{id}/artifacts/next_actions:generate`、`GET /sessions/{id}/artifacts/next_actions`
- 簡易 inputDigest: `sha256(summary_v2.updatedAt + transcriptVersion + generatorVersion)`

### Release Unit 2: `feat/share-drafts-mvp`

- 目的: 会議内容から **チャンネル別の共有文** を生成。**送信は別 API で確認後**
- 配置: `sessions/{sessionId}/share_drafts/{draftId}` サブコレクション(複数下書き許容)
- 中核: `app/services/share_draft_service.py::generate_share_draft(session, summary_v2, next_actions, channel, audience, format, include)` pure service
  - **Phase 2-A 変換系コマンドはこの service を呼ぶ形で実装する**(Hard Rule、`docs/release-units/2026-05-08-phase2a-wakeup-prompt.md` 参照)
- 初期は LLM 呼ばず **テンプレートベース**。「丁寧に / 短く / 顧客向けに」等のリクエスト時のみ LLM 呼出
- API: `POST /v1/sessions/{id}/share-drafts:generate` (生成、`requiresReview: true`)、`POST /v1/share-drafts/{id}:send` (確認後送信)

### Release Unit 3: `feat/sessions-projectid-bridge`

- 目的: Session を Project に紐付ける最小 bridge。Project Memory 自体は本 unit で実装しない
- データ追加: Session に `projectId: Optional[str]`、`projects/{projectId}` collection 最小フィールド (id/accountId/name/archived/createdAt/updatedAt)
- API: `POST /v1/projects` / `GET /v1/projects` / `PATCH /v1/sessions/{id}` (projectId 更新) / `GET /v1/sessions?projectId=...`
- 既存 session は `projectId: null` のまま動く(自動分類しない)

### 後続 (Release Unit 4 以降)

- `feat/project-memory-foundation` — decisions memory / open tasks / risks / project briefing API
- `feat/intent-router-tool-expansion` — `generate_pdf` / `generate_share_draft` / `create_task` / `send_to_slack` / `compare_with_previous` の Tool 系 intent 追加
- `feat/personal-context-settings` — `personalization.{usePastMeetings, retentionDays}` 設定 API + UI
- `feat/inputdigest-cache-summary-v2` — summary_v2 全体への inputDigest 適用(影響範囲が大きいので慎重に)

---

## やってはいけない実装パターン

1. ❌ `assistant_hub.py` の中に Slack 用文面生成を直書き
2. ❌ LINE handler の中に LINE 用文面生成を直書き
3. ❌ Desktop 用に別の共有文生成を書く
4. ❌ summary_v2 prompt に next_actions まで全部押し込む(LLM コスト増+責務肥大)
5. ❌ `projectId` と Project Memory を同時に大きく作る

正しい構造:

```
summary_v2 (LLM bundle)
   ↓
next_actions_service (rules)
   ↓
share_draft_service (templates + opt-in LLM)
   ↓
REST / LINE / Slack / Desktop / iOS が共通利用
```

---

## 本書の sunset

Release Unit 1〜3 がすべて merged され、それぞれの release note が `docs/releases/` に積まれた段階で本書は削除して構わない(その時点で「ギャップ」ではなく「未着手のロードマップ」になっており、別文書に役割が移る)。

それまでは Phase 2-A 着手者・Phase 2-B Desktop Automation 着手者・後続 release unit 担当者が前提を共有するための **唯一の単一ソース**として残す。
