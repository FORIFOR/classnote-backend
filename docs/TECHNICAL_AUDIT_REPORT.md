# ClassnoteX Backend 技術監査レポート

作成日: 2025-12-11  
対象: `classnote-api` (FastAPI + Cloud Run)

---

## 1. システム全体の構成サマリ

### アーキテクチャ概観

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              iOS / Web Client                               │
└───────────────────┬─────────────────────────────────┬───────────────────────┘
                    │ HTTPS (REST)                    │ WebSocket
                    ▼                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Cloud Run (classnote-api)                            │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                 │
│  │ sessions.py    │  │ websocket.py   │  │ auth.py        │                 │
│  │ (/sessions)    │  │ (/ws/stream)   │  │ (/auth/line)   │                 │
│  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘                 │
│          │                   │                   │                          │
│  ┌───────┴───────────────────┴───────────────────┴─────────┐                │
│  │                    firebase.py                          │                │
│  │  ・Firestore Client (db)                                │                │
│  │  ・Storage Client (storage_client)                      │                │
│  │  ・Firebase Admin (Auth)                                │                │
│  └─────────────────────────────────────────────────────────┘                │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐              │
│  │ task_queue.py   │──│ Cloud Tasks     │──│ tasks.py        │              │
│  │ (Enqueue)       │  │ (summarize-q)   │  │ (/internal/...) │              │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘              │
│          │                                         │                        │
│          └─────────────────────┬───────────────────┘                        │
│                                ▼                                            │
│  ┌─────────────────────────────────────────────────────────┐                │
│  │ services/llm.py  +  gemini_client.py                    │                │
│  │  ・Vertex AI Gemini (summarize, quiz, highlights)       │                │
│  └─────────────────────────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────────────────┘
                    │                │                │
                    ▼                ▼                ▼
          ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐
          │  Firestore  │  │ GCS Audio   │  │ Vertex AI/      │
          │  (sessions) │  │ (buckets)   │  │ Gemini 1.5      │
          └─────────────┘  └─────────────┘  └─────────────────┘
```

### データフロー: 録音〜要約生成

```
1. クライアント POST /sessions → Firestore に session 作成
2. クライアント /ws/stream/{id} 接続 → リアルタイム STT (Google Speech)
   └→ 音声チャンクを /tmp に保存 → 終了時 GCS にアップロード
3. クライアント POST /sessions/{id}/transcript → transcriptText を保存
4. クライアント POST /sessions/{id}/summarize
   ├→ 即座に 202 Accepted を返却
   └→ Cloud Tasks に enqueue → /internal/tasks/summarize
       └→ Vertex AI Gemini 呼び出し → Firestore に summaryMarkdown 保存
5. クライアント GET /sessions/{id} でポーリング → summaryStatus == "completed"
```

---

## 2. エンドポイントごとのボトルネック一覧

| エンドポイント | sync/async | 外部API | ボトルネック/問題点 |
|---------------|-----------|---------|-------------------|
| `POST /sessions` | async | Firestore (W) | **sync Firestore write** in async handler |
| `GET /sessions` | async | Firestore (R) | **sync `list(query.stream())`** - N+1 ではないが同期ブロック |
| `GET /sessions/{id}` | async | Firestore (R) | **sync `doc.get()`** |
| `PATCH /sessions/{id}` | async | Firestore (R+W) | **sync read + write** 2回 |
| `POST /upload-url` | async | GCS (signed URL) | 署名生成は軽量だが同期呼び出し |
| `POST /.../summarize` | async | Firestore + Cloud Tasks | ✅ 非同期化済み（202返却 + Cloud Tasks） |
| `POST /.../quiz` | async | Firestore + Cloud Tasks | ✅ 非同期化済み |
| `POST /.../qa` | async | (モック) | 将来 LLM 呼び出し時に要対応 |
| `POST /auth/line` | **sync** | LINE API + Firebase Auth | ⚠️ **sync def** + ネットワークI/O |
| `/ws/stream/{id}` | async | Speech API + Firestore + GCS | STT は async、GCS upload は **sync** |
| `/internal/tasks/summarize` | async | Firestore + Vertex AI | 内部 worker - OK |

### 主要な問題点

#### 🔴 Critical: `/auth/line` が sync def + ネットワーク I/O

```python
# auth.py:12
def auth_line(req: LineAuthRequest):  # ← sync def
    verify_resp = requests.post(...)   # ← blocking I/O
```

- FastAPI の sync ハンドラはスレッドプールで実行されるが、Cloud Run の同時リクエスト数に影響
- LINE API タイムアウト (5秒設定) が発生するとスレッドを長時間占有

#### 🟡 Warning: Firestore の同期操作

`google-cloud-firestore` のデフォルトクライアントは同期 API。  
`async def` ハンドラ内で `doc_ref.get()`, `doc_ref.update()` を呼ぶとイベントループをブロック。

```python
# sessions.py:129
async def get_session(session_id: str):
    doc = _session_doc_ref(session_id).get()  # ← 同期 I/O
```

**影響**: 高負荷時にイベントループがブロックされ、他のリクエスト処理が遅延。

---

## 3. summarize / transcription のスケール設計レビュー

### 現状の設計 ✅ 良好

`/sessions/{id}/summarize` は既に適切に非同期化されています：

```python
# sessions.py:212-247
async def summarize_session(session_id: str, background_tasks: BackgroundTasks):
    # 1. Firestore からセッション取得
    # 2. 既に running/queued なら早期リターン (202)
    # 3. 既に completed なら結果を返却
    # 4. summaryStatus = "running" に更新
    # 5. Cloud Tasks に enqueue
    # 6. 202 Accepted を返却
```

この設計は以下の点で優れています：

1. **即座にレスポンス**: クライアントは待たされない
2. **二重実行ガード**: `summaryStatus in ("running", "queued")` でチェック
3. **キャッシュ利用**: `completed` なら既存結果を返す
4. **Cloud Tasks による分離**: LLM 処理を別リクエストに分離

### 改善余地

#### 3.1 冪等キーの不在

同一ユーザーが短時間に連続クリックした場合：

```
T0: POST /summarize → status = "running" → Cloud Tasks enqueue (Task A)
T1: Client timeout/retry → POST /summarize → status is "running" → 202 返却 (OK✅)
T2: POST /summarize (別セッション) → ...
```

現状は `summaryStatus` チェックで対応できているが、Firestore の transaction を使っていないため、極端な高頻度リクエストでは race condition の可能性あり。

#### 3.2 リトライ戦略

```python
# tasks.py:72-75
# Cloud Tasks にリトライさせるために 500 を返す手もあるが、
# ここでは無限ループ防止のため 200 OK (failed status) で完了とする運用もアリ。
```

現状は 200 OK で失敗を返しているため、Cloud Tasks のリトライが発動しない。  
→ LLM のレート制限エラー (429) などで失敗した場合、自動復旧しない。

### 改善提案

#### プチ改善: Transaction を使った二重実行防止

```python
@router.post("/sessions/{session_id}/summarize")
async def summarize_session(session_id: str):
    doc_ref = _session_doc_ref(session_id)
    
    @firestore.transactional
    def update_status_if_idle(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise HTTPException(404, "Session not found")
        
        data = snapshot.to_dict()
        status = data.get("summaryStatus")
        
        if status in ("running", "queued"):
            return {"already_running": True, "status": status}
        if status == "completed" and data.get("summaryMarkdown"):
            return {"completed": True, "summary": data.get("summaryMarkdown")}
        
        transaction.update(doc_ref, {
            "summaryStatus": "running",
            "summaryUpdatedAt": firestore.SERVER_TIMESTAMP
        })
        return {"needs_enqueue": True}
    
    # Run transaction
    transaction = db.transaction()
    result = update_status_if_idle(transaction, doc_ref)
    
    if result.get("already_running"):
        return JSONResponse(202, {"status": result["status"]})
    if result.get("completed"):
        return {"status": "completed", "summary": result["summary"]}
    
    enqueue_summarize_task(session_id)
    return JSONResponse(202, {"status": "running"})
```

#### 本格改善: Cloud Tasks のリトライ設定

```python
# task_queue.py
task = {
    "http_request": {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": url,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload).encode(),
    },
    "dispatch_deadline": {"seconds": 600},  # 10分のタイムアウト
}
```

Worker 側では LLM エラー時に 5xx を返してリトライ：

```python
# tasks.py
except google.api_core.exceptions.ResourceExhausted as e:
    # Gemini レート制限 → リトライさせる
    raise HTTPException(503, detail="LLM rate limited")
```

---

## 4. コスト・無駄 API コールの分析

### 推定 API コール数 (1セッションあたり)

| API | 呼び出し回数 | 単価目安 | 備考 |
|-----|------------|---------|------|
| Firestore Read | 5-10回 | $0.06/100K | セッション作成、取得、更新 |
| Firestore Write | 3-5回 | $0.18/100K | 作成、transcript更新、summary更新 |
| GCS Write | 1回 | $0.02/10K | 音声アップロード |
| GCS Signed URL | 2-5回 | 無料 | 音声取得、アップロードURL |
| Speech API | 5-60分相当 | $0.024/分 | リアルタイムSTT |
| Vertex AI Gemini | 1-2回 | $0.00075/1K tokens | 要約 + クイズ |

### 無駄・改善可能な呼び出し

#### 4.1 `list_sessions` のオーバーフェッチ

```python
# sessions.py:98-105
for doc in docs:
    data = doc.to_dict()
    if user_id and data.get("userId") != user_id:
        continue  # ← Firestore でフィルタ済みなのに再チェック
```

Firestore クエリで `where("userId", "==", user_id)` しているのに、Python 側で再フィルタ。  
→ 冗長だが害は少ない

#### 4.2 `batch_delete_sessions` の N+1 読み込み

```python
# sessions.py:283-288
for sid in body.ids:
    ref = db.collection("sessions").document(sid)
    if ref.get().exists:  # ← 1件ずつ read
        batch_write.delete(ref)
```

10件削除で10回 read → 1回の batch.commit()。  
→ 存在確認なしで delete しても Firestore はエラーにならないので read 不要。

#### 4.3 LLM 呼び出しの最適化

現状は transcript 全文を LLM に送信。長い会議 (1万文字以上) では：
- Token 消費が増加
- レスポンス遅延

**改善案**: 要約の段階的生成

```python
# 5000文字ごとにチャンク分割して中間要約
# 最後に中間要約をマージして最終要約
```

### Zenn 記事参考: Step Functions 的なコスト最適化

現状の Cloud Run + Cloud Tasks 構成は既に良い設計。  
さらにコスト削減するなら：

1. **Firestore → BigQuery**: ログ・分析用データを BigQuery に移行  
   (読み込みコスト削減)

2. **Batch Speech API**: リアルタイムでなく録音後処理なら  
   `speech.recognize()` (バッチ) は Streaming より安い場合あり

3. **Cloud Functions (Gen2) への分離**:  
   - `/internal/tasks/*` を Cloud Functions に切り出し
   - Cloud Run のスケーリング設定と分離
   - CPU/Memory 制限を細かく設定

---

## 5. 実装アンチパターンの列挙

### 🔴 Critical

#### 5.1 sync def + ネットワーク I/O

**ファイル**: `app/routes/auth.py:12-62`

```python
def auth_line(req: LineAuthRequest):  # sync def
    verify_resp = requests.post(...)   # blocking
```

**修正案**:
```python
import httpx

@router.post("/auth/line", response_model=LineAuthResponse)
async def auth_line(req: LineAuthRequest):
    async with httpx.AsyncClient() as client:
        verify_resp = await client.post(
            "https://api.line.me/oauth2/v2.1/verify",
            data={"id_token": req.idToken, "client_id": LINE_CLIENT_ID},
            timeout=5.0
        )
```

### 🟡 Warning

#### 5.2 async def 内での同期 Firestore API

**影響箇所**: `sessions.py` 全体

```python
async def get_session(session_id: str):
    doc = _session_doc_ref(session_id).get()  # sync I/O in async context
```

**改善案** (将来的):
- `google-cloud-firestore` の AsyncIO 対応版を使う
- または `run_in_executor` でラップ

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=4)

async def get_session(session_id: str):
    loop = asyncio.get_event_loop()
    doc = await loop.run_in_executor(
        executor, 
        _session_doc_ref(session_id).get
    )
```

#### 5.3 WebSocket 終了時の同期 GCS アップロード

**ファイル**: `app/routes/websocket.py:123-136`

```python
finally:
    if tmp_file.exists():
        blob.upload_from_filename(str(tmp_file))  # sync upload
```

WebSocket 切断後に同期アップロードを行うと、次の接続処理に影響する可能性。

**改善案**: BackgroundTasks or Cloud Tasks で非同期化

#### 5.4 冪等性ガードなし (軽微)

`POST /sessions` は毎回新規 ID を生成するので問題なし。  
`POST /.../summarize` は `summaryStatus` チェックで対応済み。  
ただし transaction ではないので極端な並列リクエストで race 可能性。

### 🟢 Good Patterns (参考)

- **グローバルクライアント初期化**: `firebase.py` で `db`, `storage_client` を一度だけ初期化 ✅
- **Cloud Tasks による非同期化**: `/summarize`, `/quiz` は即座に 202 返却 ✅
- **エラー時のログ出力**: `logging.exception()` でスタックトレース ✅

---

## 6. 改善コード例

### 6.1 auth.py の async 化

```python
# app/routes/auth.py (AFTER)
from fastapi import APIRouter, HTTPException
import httpx
import os
import logging
from firebase_admin import auth as fb_auth

from app.util_models import LineAuthRequest, LineAuthResponse

router = APIRouter()
logger = logging.getLogger("app.auth")

@router.post("/auth/line", response_model=LineAuthResponse)
async def auth_line(req: LineAuthRequest):
    LINE_CLIENT_ID = os.environ.get("LINE_CHANNEL_ID")
    if not LINE_CLIENT_ID:
        logger.warning("LINE_CHANNEL_ID is not set in environment")
    
    logger.info(f"[/auth/line] Verifying LINE token")
    
    async with httpx.AsyncClient() as client:
        try:
            verify_resp = await client.post(
                "https://api.line.me/oauth2/v2.1/verify",
                data={
                    "id_token": req.idToken,
                    "client_id": LINE_CLIENT_ID,
                },
                timeout=5.0
            )
        except httpx.TimeoutException:
            logger.error("LINE token verification timed out")
            raise HTTPException(503, "LINE server timeout")
    
    if verify_resp.status_code != 200:
        logger.error(f"LINE verify failed: {verify_resp.status_code}")
        raise HTTPException(401, "Invalid LINE token")

    payload = verify_resp.json()
    line_user_id = payload.get("sub")
    name = payload.get("name")
    picture = payload.get("picture")
    
    if not line_user_id:
        raise HTTPException(401, "No sub in LINE token")

    firebase_uid = f"line:{line_user_id}"
    
    try:
        custom_token_bytes = fb_auth.create_custom_token(
            firebase_uid,
            {"provider": "line", "name": name, "picture": picture}
        )
        custom_token = custom_token_bytes.decode("utf-8")
    except Exception:
        logger.exception(f"Failed to create custom token for uid={firebase_uid}")
        raise HTTPException(500, "Failed to create custom token")

    return LineAuthResponse(firebaseCustomToken=custom_token)
```

### 6.2 batch_delete の最適化

```python
# sessions.py (AFTER)
@router.post("/sessions/batch_delete")
async def batch_delete_sessions(body: BatchDeleteRequest):
    if not body.ids:
        return {"ok": True, "deleted": 0}
    
    batch_write = db.batch()
    for sid in body.ids:
        ref = db.collection("sessions").document(sid)
        batch_write.delete(ref)  # 存在確認なしで削除（Firestoreは存在しなくてもエラーにならない）
    
    batch_write.commit()
    return {"ok": True, "deleted": len(body.ids)}
```

### 6.3 requirements.txt に httpx 追加

```
# requirements.txt
httpx>=0.27.0
```

---

## 7. 優先順位付き TODO リスト

### S: 今すぐ直したい (1-2日)

| ID | 項目 | 理由 | 工数 |
|----|------|------|------|
| S1 | `auth.py` を async 化 | sync def + ネットワーク I/O はアンチパターン | 30分 |
| S2 | `requirements.txt` に `httpx` 追加 | S1 の前提 | 1分 |
| S3 | `batch_delete` の N+1 read 削除 | 無駄な Firestore read | 5分 |

### A: 余裕ができたら (1週間以内)

| ID | 項目 | 理由 | 工数 |
|----|------|------|------|
| A1 | summarize の transaction 化 | Race condition 対策 | 1時間 |
| A2 | Cloud Tasks リトライ有効化 | LLM 429 エラーからの復旧 | 30分 |
| A3 | WebSocket 終了時 GCS アップロードを BackgroundTasks 化 | 次接続への影響除去 | 1時間 |
| A4 | Firestore read を `run_in_executor` でラップ | イベントループブロック軽減 | 2時間 |

### B: 将来のリファクタ候補 (1ヶ月〜)

| ID | 項目 | 理由 | 工数 |
|----|------|------|------|
| B1 | Firestore AsyncIO クライアントへの移行 | 本質的な非同期化 | 1週間 |
| B2 | `/internal/tasks/*` を Cloud Functions に分離 | スケーリング最適化 | 3日 |
| B3 | LLMチャンク分割処理 | 長文書処理の効率化 | 2日 |
| B4 | pyannote.audio による話者分離実装 | スタブからの脱却 | 1週間 |
| B5 | Batch Speech API への切り替え検討 | リアルタイム不要ならコスト削減 | 3日 |

---

## まとめ

### 現状の評価

| 観点 | 評価 | コメント |
|------|------|----------|
| リアルタイム性 | ⭐⭐⭐⭐ | WebSocket + Streaming STT は適切 |
| スケール | ⭐⭐⭐ | Cloud Tasks で非同期化済みだが、sync Firestore がボトルネック |
| コスト | ⭐⭐⭐⭐ | 概ね最適化済み、LLM チャンク化で改善余地 |
| 保守性 | ⭐⭐⭐⭐ | コード構造は整理されている |

### 最重要アクション

1. **S1: auth.py の async 化** - ログイン時のユーザー体験に直結
2. **A1: summarize の transaction 化** - 本番運用での安定性向上
3. **B1: Firestore AsyncIO 移行** - 長期的なスケーラビリティ確保

現在の設計は全体として良好であり、Cloud Tasks による非同期化パターンは正しく実装されています。  
上記の優先順位に従って段階的に改善することで、さらに堅牢なシステムになります。
