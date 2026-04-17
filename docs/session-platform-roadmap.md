# Session Platform Roadmap

本書は classnote-api を **artifact 中心 / projection 中心 / realtime 対応の session
platform** に段階移行するための計画書です。Desktop / iOS との共通契約の source of
truth でもあります。

## 現在位置 (2026-04-17)

| フェーズ | 状態 |
|---|---|
| Phase 0 — Critical Security / Billing / Folder 修正 | ✅ 完了 (`fix/session-platform-critical-20260417`) |
| Phase 1 — Read Model / Projection BFF | ✅ 本 PR で完了 |
| Phase 2 — session doc vs derived/* の二重管理解消 | 🟡 一部前進 (reader 側を derived 優先に寄せた) / **writer 二重書き停止は次 PR** |
| Phase 3 — Realtime Event Layer 分散化 | 📝 設計のみ / 実装次 PR |
| Phase 4 — `sessions.py` / `tasks.py` 構造分割 | 📝 設計のみ / 実装次 PR |
| Phase 5 — Schema 固定化 + Evidence first-class | ✅ projection レイヤで完了 / canonical schema は次 PR |
| Phase 6 — permissions / error 統一表現 | ✅ projection レイヤで完了 |

---

## Phase 1: Projection / Read Model (完了)

### 提供エンドポイント

| Method | Path | 目的 |
|---|---|---|
| GET | `/v1/session-details/{id}` | 詳細画面用の集約 read model |
| GET | `/v1/session-details/{id}/overview` | SummaryV2 全文 payload |
| GET | `/v1/session-details/{id}/quiz` | Quiz 全問 |
| GET | `/v1/session-details/{id}/notes` | notes markdown |
| GET | `/v1/session-details/{id}/transcript?limit=200&cursor=N` | transcript chunks (pagination) |

### 決定事項

- **projection レスポンス shape v1** (`app/services/session_projection.py:build_session_detail`):
  `projectionVersion`, `revision`, `updatedAt`, `partials`, `header`, `context`,
  `audio`, `overview`, `transcript`, `notes`, `quiz`, `playlist`, `permissions`,
  `jobs`, `share`, `uiHints` の 16 トップレベルキー。
- **権限は `compute_permissions`** (同ファイル) に一本化。既存の `ensure_can_view`
  / `ensure_is_owner` は Phase 2 でこちらを呼ぶようリファクタ予定。
- **evidence は常に配列**で返す。legacy データは `[]` にフォールバック
  (`normalize_summary_payload` / `_normalize_evidence`)。
- **partial failure 許容**: あるセクションの Firestore read が失敗しても、その
  セクションを `status=failed` にしつつ他セクションは返す。
- **heavy payload は projection に含めない**。overview markdown, quiz 全問,
  transcript 全文は `/v1/session-details/{id}/*` で個別取得。

---

## Phase 2: session doc vs derived/* の二重管理解消

### 現状 (二重化)

```
sessions/{id}.summaryMarkdown          ← legacy (読まれている)
sessions/{id}.summaryJson              ← legacy
sessions/{id}.summaryStatus            ← legacy
sessions/{id}/derived/summary          ← canonical (書かれている)
```

### 本 PR までの前進

- **reader 側**: projection はすでに `derived/*` を source-of-truth として読む
  ようになった。`session.summaryMarkdown` / `summaryJson` には一切依存しない。
- **writer 側**: 既存の `tasks.py` が session doc と derived の両方を書いている
  状態は **未変更**（破壊リスク回避）。

### 次 PR でやること

1. `tasks.py:_handle_summarize_task_core` の `doc_ref.update(update_payload)` を
   軽量 status のみ (`status.summary = "completed"`) に縮退
2. 同じく `_handle_quiz_task_core` / `_handle_playlist_task_core`
3. `session.status` を `{ transcript, summary, quiz, audio }` オブジェクトに移行
   (互換のため legacy fields は両書きを 1 マイナーバージョン維持)
4. iOS / Desktop が `/v1/session-details/*` に完全移行したことを確認後、legacy
   フィールドを `deprecated` 明示 → 削除
5. Firestore の 1 MiB / doc 制限リスク低減（summaryMarkdown / transcriptText を
   session doc から除く）

### ガード

- Phase 2 実施前に iOS / Desktop 両方が `/v1/session-details/*` へ移行済みで
  あることを dashboard の API usage で確認する

---

## Phase 3: Realtime Event Layer

### 現状

- `app/services/session_event_bus.py` は **単一プロセス in-memory pub/sub**
- WebSocket `/ws/*` で `publish_session_event("assets.updated", ...)` を送るが、
  Cloud Run min=0 / max=10 だと他インスタンスに接続しているクライアントには届かない
- 詳細画面は polling で救えているが、UX 上 stale が長い

### 採用方針: **Firestore Snapshot Listener を一次チャネル**

クライアント (iOS / Desktop) が直接 Firestore を listen:

```
sessions/{sessionId}                         ← header / status / lifecycleState
sessions/{sessionId}/derived/summary         ← summary 完了
sessions/{sessionId}/derived/quiz            ← quiz 完了
sessions/{sessionId}/derived/playlist        ← playlist 完了
sessions/{sessionId}/derived/summary_progress  ← summary 進捗
sessions/{sessionId}/jobs                    ← job status 変化
```

### 実装タスク (次 PR)

1. **Firestore Security Rules** を listen 許可範囲に調整
   - `allow read: if request.auth != null && <ownerOrShared>` を `sessions/{id}`
     + `sessions/{id}/derived/{artifact}` + `sessions/{id}/jobs/{job}` に
2. `/ws/stream` は音声入出力専用に縮退（session event 配信は廃止 path）
3. session_event_bus は **削除 or 内部イベント専用** に
4. Desktop / iOS 側で Firestore SDK の snapshot listener を使う実装ガイドを追加

### 代替案 (採用しない)

- Google Cloud Pub/Sub + Redis Pub/Sub: インフラコスト増、Firestore 既存運用を
  活かせない

---

## Phase 4: `sessions.py` / `tasks.py` 構造分割

### 現状

- `app/routes/sessions.py` 6807 行 / 65 ルート / 41 URL グループ
- `app/routes/tasks.py` 2658 行 / 7 種のワーカーが同居

### 目標構造

```
app/routes/sessions/
  __init__.py           # router 集約 (main.py は無変更)
  core.py               # create / delete / list / batch / meta / notes / tags
  audio.py              # prepareUpload / commit / audio_url / retry
  share.py              # invite / members / participants / join / share-links
  artifacts.py          # artifact read (legacy; projection に寄せて将来縮退)
  jobs.py               # create_job / get_job_status
  images.py             # images prepare/commit/list
  transcript.py         # chunks / segments / replace / device_sync

app/routes/tasks/
  __init__.py
  summarize.py
  quiz.py
  transcribe.py
  playlist.py
  todo.py
  youtube.py
  maintenance.py        # cleanup / merge migration / daily aggregation
```

### 実装順序 (次 PR 群)

1. `sessions/` パッケージ化 (サブステップに分割)
   - Step 1: `sessions/__init__.py` + `core.py` 切り出し
   - Step 2: `audio.py` + `transcript.py`
   - Step 3: `share.py` + `jobs.py` + `images.py`
   - 各 step で regression (iOS smoke test) を必須にする
2. `tasks/` パッケージ化 (共通 helper が少ないので 1 PR でも可)
3. 共有ヘルパ `_resolve_session` / `_cascade_delete_session` / `ensure_*` を
   `app/services/session_access.py` に切り出し、循環 import を避ける

### 本 PR で **やらない** 理由

- sessions.py に他の未コミット修正が混在しており (ads.py / imports.py / ops.py
  等、別作業由来) 分割 diff と混ざると review 不能
- 6807 行を単一会話で分割する品質保証が困難

---

## Phase 5: Schema 固定化 (本 PR で完了)

### SummaryV2 契約 (freeze)

```ts
type SummaryV2 = {
  schemaVersion: 2
  type: "meeting" | "lecture"
  tldr: string[]
  bottomLine?: string
  outcomeStatus?: "decided" | "pending" | "blocked" | "n/a"
  keyPoints: Bullet[]
  decisions: Decision[]
  todos: Todo[]
  openQuestions: Bullet[]
  discussionPoints: Bullet[]
  sections: { heading: string; bullets: Bullet[] }[]
  terms: Term[]
  formulas: Formula[]
  keywords: string[]
  contextNotes: Bullet[]
  decisionLog: DecisionLog[]
  participants: string[]
  markdownDetail?: string
}

type EvidenceRef = {
  segmentId?: string
  startMs?: number
  endMs?: number
  quotePreview?: string     // ≤ 200 chars
}

type Bullet = {
  id: string
  text: string
  evidence: EvidenceRef[]   // ★ 必須配列 (空配列 OK)
}

type Decision = Bullet & { owner?: string|null; dueDate?: string|null; confidence?: number }
type Todo     = Bullet & { task: string; owner?: string|null; due?: string|null; confidence?: number }
type Term     = { term: string; definition: string; evidence: EvidenceRef[] }
type Formula  = { id: string; latex: string; description: string; evidence: EvidenceRef[] }
type DecisionLog = { id: string; topic: string; summary: string; outcome: string; evidence: EvidenceRef[] }
```

### QuizV1 契約

```ts
type QuizV1 = {
  schemaVersion: 1
  version: number           // 生成世代 (+1 on regenerate)
  questions: Question[]
  updatedAt: string
}

type Question = {
  id: string
  prompt: string
  questionType: "single" | "multi" | "tf"
  choices: { id: string; text: string }[]
  correctChoiceIds: string[]
  explanation?: string
  difficulty?: "easy" | "mid" | "hard"
  evidence: EvidenceRef[]   // ★ 必須配列
}
```

### バージョニング規約

- schemaVersion は **immutable**。フィールド削除・型変更は schemaVersion +1
- クライアントは知っている schemaVersion のみ解釈、未知は捨てる
- サーバは当面すべての過去 schemaVersion を返し続ける

---

## Phase 6: permissions / error 統一 (本 PR で完了)

### permissions オブジェクト (projection 経由で全レスポンスに同梱)

```ts
type Permissions = {
  role: "owner" | "editor" | "viewer" | "shared_viewer" | "none"
  canView: boolean
  canEditTitle: boolean
  canEditNotes: boolean
  canEditTags: boolean
  canMoveFolder: boolean
  canShare: boolean
  canDelete: boolean
  canLeaveShared: boolean
  canRegenerateSummary: boolean
  canRegenerateTranscript: boolean
  canGenerateQuiz: boolean
  canExport: boolean
}
```

### 統一エラー形式

```jsonc
{
  "error": {
    "code": "SESSION_NOT_FOUND" | "PERMISSION_DENIED" | "REVISION_MISMATCH"
          | "INSUFFICIENT_CREDITS" | "PROJECTION_ERROR" | "...",
    "message": "i18n-able or human readable",
    "retryable": false,
    "retryAfter": null,
    "details": {}
  }
}
```

- Phase 1 の `/v1/session-details/*` はこの形式で返す
- 次 PR で legacy `/sessions/*` を middleware で wrap 予定

---

## Desktop / iOS 実装契約 (再掲)

画面側は以下のみを守れば共通レイアウトが揃う:

1. 画面 init で `GET /v1/session-details/{id}` を **1 回だけ**叩く
2. 重いタブを開いた時のみ `/overview` / `/transcript` / `/quiz` / `/notes`
3. Firestore Snapshot Listener で以下を subscribe (Phase 3 で正式化):
   - `sessions/{id}` / `sessions/{id}/derived/*` / `sessions/{id}/jobs`
4. イベント受信 → projection を refetch (simple) もしくは差分 patch (optim)
5. mutation は `PATCH /v1/sessions/{id}` + `If-Match: <revision>` (Phase 2 完了後)
6. error response の `code` を i18n key として扱う
7. `partials.xxx === true` のセクションは「読み込みに失敗」UI を出す
8. `uiHints.primaryCta` に従って画面下部の primary ボタン出し分け

---

## マイグレーション checklist (Phase 2 実施前)

- [ ] iOS app (App Store / TestFlight) が `/v1/session-details/*` を叩いている
      バージョンで 95% 以上のユーザーに配布済み
- [ ] Desktop app (配布済み最新版) が `/v1/session-details/*` を叩いている
- [ ] `/sessions/{id}` レガシー経路の QPS が 10% 以下まで下がった
- [ ] Firestore audit: `sessions/{id}.summaryMarkdown` を読みに行く実装が
      backend / frontend のどこにも残っていない

確認できたら Phase 2 (writer 二重書き停止) を別 PR で実施する。

---

## Out of scope (本ロードマップでは扱わない)

- 全文検索 (transcript search) インデックス: 将来的に必要
- offline 同期の conflict resolution 戦略
- Multi-tenant / Enterprise プランの権限階層拡張
