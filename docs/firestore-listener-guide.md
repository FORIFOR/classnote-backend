# Firestore Snapshot Listener Guide (Phase 3)

Desktop / iOS クライアントが realtime 更新を受ける際の**正式な一次チャネル**は
Firestore Snapshot Listener です。backend 側の `/ws/sessions` による集約
pub/sub は Cloud Run multi-instance 環境で壊れ得るため、**Listener を優先**
し WebSocket は fallback / 音声ストリーム専用に縮退します。

## 権限モデル

Firebase Auth ID Token の custom claim に **`accountId`** が埋め込まれます
(backend `app/dependencies.py:_set_account_id_claim`)。iOS / Desktop は
以下のどれでも session / artifact の read が成立します:

- `request.auth.token.accountId == session.ownerAccountId` (account-level owner)
- `request.auth.uid == session.ownerUserId` (legacy)
- `request.auth.token.accountId in session.sharedWithAccountIds`
- `request.auth.uid in session.sharedUserIds` / `sharedWithUserIds` / `participantUserIds`

admin は `request.auth.token.admin == true` の custom claim で全 read 可能。

## Listen 対象

| Path | 用途 | 更新頻度 |
|---|---|---|
| `sessions/{id}` | header / title / lifecycleState / status / tags | 中 |
| `sessions/{id}/derived/summary` | summary 完了・更新 | 数回 / セッション |
| `sessions/{id}/derived/quiz` | quiz 完了 | 数回 |
| `sessions/{id}/derived/playlist` | playlist 完了 | 数回 |
| `sessions/{id}/derived/summary_progress` | summary 進捗 (percent / phase) | 高 (数秒ごと) |
| `sessions/{id}/jobs` (collection) | 進行中ジョブ status=queued/running | 中 |
| `sessions/{id}/members` | 共有メンバー変化 | 低 |
| `sessions/{id}/conversations/{cid}` | AI Chat conversation メタ | 低 |
| `sessions/{id}/conversations/{cid}/messages` (Phase 7.3) | 各メッセージ append | 会話中に高 |
| `sessions/{id}/transcript_chunks` | 文字起こし追記 (Transcript タブ限定) | 録音中に高 |
| `accounts/{id}` | plan / memberUids の変化 | 低 |
| `orb_overrides/{uid}` | orb theme override | 極低 |

## 推奨サブスクリプション戦略

### 画面 init 時

1. `GET /v1/session-details/{id}` を 1 回だけ叩く（projection 取得）
2. 同時に `sessions/{id}` と `sessions/{id}/derived/{summary,quiz,playlist,summary_progress}` を listen 開始
3. 必要なら `sessions/{id}/jobs` を listen（active job 一覧が即時更新される）

### タブ切り替え時

- **Overview タブ開く** → `derived/summary` listen のまま。追加で `GET /v1/session-details/{id}/overview` で全文取得
- **Transcript タブ開く** → `transcript_chunks` の listen を追加。タブを閉じたら解除
- **Quiz タブ開く** → `derived/quiz` listen のまま。追加で `GET /v1/session-details/{id}/quiz`
- **Chat パネル開く** → `sessions/{id}/conversations/{cid}/messages` を listen (Phase 7.3 後)

### listener 生存期間

- 画面離脱時に必ず detach（iOS: `ListenerRegistration.remove()`, web: `unsubscribe()`）
- long-lived な session 一覧用 listener は無効化推奨（ドリフトで課金・read quota を圧迫）

## iOS 実装例 (Swift / Firestore iOS SDK)

```swift
final class SessionRealtimeSubscription {
    private var listeners: [ListenerRegistration] = []
    private let sessionId: String
    private let onHeader: (SessionHeader) -> Void
    private let onSummary: (DerivedSummary) -> Void
    private let onQuiz: (DerivedQuiz) -> Void
    private let onJobs: ([Job]) -> Void

    init(sessionId: String, onHeader: @escaping (SessionHeader) -> Void,
         onSummary: @escaping (DerivedSummary) -> Void,
         onQuiz: @escaping (DerivedQuiz) -> Void,
         onJobs: @escaping ([Job]) -> Void) {
        self.sessionId = sessionId
        self.onHeader = onHeader
        self.onSummary = onSummary
        self.onQuiz = onQuiz
        self.onJobs = onJobs
    }

    func start() {
        let db = Firestore.firestore()
        let sessionRef = db.collection("sessions").document(sessionId)

        listeners.append(sessionRef.addSnapshotListener { [weak self] snap, err in
            guard let self = self, let data = snap?.data() else { return }
            if let header = SessionHeader(firestore: data) {
                self.onHeader(header)
            }
        })

        listeners.append(sessionRef.collection("derived").document("summary")
            .addSnapshotListener { [weak self] snap, _ in
                guard let self = self, let data = snap?.data() else { return }
                self.onSummary(DerivedSummary(firestore: data))
            })

        listeners.append(sessionRef.collection("derived").document("quiz")
            .addSnapshotListener { [weak self] snap, _ in
                guard let self = self, let data = snap?.data() else { return }
                self.onQuiz(DerivedQuiz(firestore: data))
            })

        listeners.append(sessionRef.collection("jobs")
            .whereField("status", in: ["queued", "running", "pending"])
            .addSnapshotListener { [weak self] snap, _ in
                guard let self = self, let docs = snap?.documents else { return }
                let jobs = docs.map { Job(firestore: $0.data()) }
                self.onJobs(jobs)
            })
    }

    func stop() {
        listeners.forEach { $0.remove() }
        listeners.removeAll()
    }
}
```

### オフライン復帰

- Firestore SDK は自動で差分を再同期するため、追加コード不要
- オフライン中の write は不可（クライアントから書かないルールなので問題なし）
- catch-up 取得したい場合のみ `GET /v1/session-details/{id}` を再取得

## Desktop 実装例 (TypeScript / Firebase Web SDK)

```ts
import { onSnapshot, doc, collection, query, where } from 'firebase/firestore'

export function subscribeSessionDetail(
  sessionId: string,
  handlers: {
    onHeader: (data: unknown) => void
    onSummary: (data: unknown) => void
    onQuiz: (data: unknown) => void
    onJobs: (data: unknown[]) => void
  },
): () => void {
  const sessionRef = doc(db, 'sessions', sessionId)
  const unsubSession = onSnapshot(sessionRef, (snap) => {
    if (snap.exists()) handlers.onHeader(snap.data())
  })
  const unsubSummary = onSnapshot(
    doc(db, 'sessions', sessionId, 'derived', 'summary'),
    (snap) => snap.exists() && handlers.onSummary(snap.data()),
  )
  const unsubQuiz = onSnapshot(
    doc(db, 'sessions', sessionId, 'derived', 'quiz'),
    (snap) => snap.exists() && handlers.onQuiz(snap.data()),
  )
  const jobsQ = query(
    collection(db, 'sessions', sessionId, 'jobs'),
    where('status', 'in', ['queued', 'running', 'pending']),
  )
  const unsubJobs = onSnapshot(jobsQ, (snap) =>
    handlers.onJobs(snap.docs.map((d) => d.data())),
  )
  return () => {
    unsubSession()
    unsubSummary()
    unsubQuiz()
    unsubJobs()
  }
}
```

## Realtime + projection のハイブリッド運用

- **projection API (`/v1/session-details/*`)** を真実の source とする（権限/shape 確定版）
- **Snapshot Listener** は「変化があったことの検知」に使う
- 変化検知 → projection を refetch、または snap.data() を patch マージ

### シンプル戦略（推奨）

```
on snapshot change:
  const projection = await api.get(`/v1/session-details/${id}`)
  store.updateProjection(projection)
```

### 高度戦略（差分 patch）

- snap.data() の `summaryStatus`/`quizStatus` 等だけを projection にマージ
- evidence / citations の shape は projection 経由でしか正しく正規化されないので、**heavy change 時は refetch が安全**

## Backend 側の対応

### 既存 `/ws/sessions` の位置づけ（変更）

- **推奨ではない**。新規クライアントは実装しない
- 既存接続は互換のため維持するが、将来の feature は listener 側に追加
- `app/services/session_event_bus.py` は WebSocket 内の event 配信専用にスコープダウン

### `_set_account_id_claim` を通しておくこと

- backend はすでに Firebase custom claim に `accountId` を set している
  (`app/dependencies.py:_set_account_id_claim`)
- クライアントは **ログイン後に token refresh が必要**
  - iOS: `try await Auth.auth().currentUser?.getIDTokenResult(forcingRefresh: true)`
  - web: `await user.getIdToken(true)`

account merge 直後 (Desktop が LINE からログイン → 既存 Apple アカウントに
合流) は、custom claim が反映された最新 token を取得するまで listener の
`accountId` ベースの rule が通らない。以下のどちらかで対応:

1. ログイン直後 / `/auth/canonicalize` 直後に token 強制 refresh
2. rules は legacy uid fallback も許可してあるので、account 未反映でも自分の uid の session は読める（primary uid の間だけ）

## テストチェックリスト

- [ ] Firestore Rules Playground (Firebase Console) で以下を通す:
  - owner (account match) が `sessions/{id}` を read → allow
  - shared account が `sessions/{id}/derived/summary` を read → allow
  - 無関係 account の uid が `sessions/{id}` を read → deny
  - admin custom claim で `ops_events` を read → allow、無ければ deny
  - client が `sessions/{id}` に write → deny
  - 自分の accountId の `accounts/{accountId}` を read → allow
  - 他人の `accounts/{accountId}` を read → deny
- [ ] iOS / Desktop どちらでも snapshot listener が動く（account-merge 済み uid でも両方見える）
- [ ] summary 生成中 → `derived/summary_progress` が毎秒更新、client が進捗表示できる
- [ ] Chat の conversation messages が append されるたびに UI が伸びる
- [ ] 画面離脱で全 listener が detach（Firestore read quota が膨らまない）

## 既知の制約 / 将来の課題

- **`get()` を使った rule 評価**: `sessions/{id}/derived/*` は親 session doc を `get()` で引いて権限判定する。これは 1 read を消費する（親の doc 自体は cache される）。大量の rapid snapshot では read quota に注意。
- **orb / app_config の client read**: rules で `/config/*` は authenticated なら read 可能にした。sensitive でないメタ設定のみ置く運用を守る。
- **conversation message の write**: 現状 messages は server-only。client 側から直接 Firestore に書き込む運用は**していない**。Phase 7.3 で sub-collection 化しても同じ (append は backend 経由)。
- **Admin custom claim の付与経路**: `ADMIN_UIDS` env に含まれる uid に対し backend で `auth.set_custom_user_claims(uid, {admin: true})` を呼ぶフローが必要。自動同期ジョブは別 PR で追加予定。
