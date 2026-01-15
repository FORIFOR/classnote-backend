# バックグラウンド同期 検証設計書

本ドキュメントでは、Classnote API のバックグラウンド同期機能（録音アップロード、デバイス同期、非同期ジョブ）の正当性を検証するための戦略とテスト設計を定義します。

## 1. 検証戦略 (Testing Pyramid)

ユーザー定義のテストレベルに基づき、以下の階層で検証を行います。
テストコードは `tests/` ディレクトリに配置し、`pytest` で実行します。

| レベル | 目的 | 対象領域 | 手法 |
| :--- | :--- | :--- | :--- |
| **Level 1** | **Contract Test** <br> (API仕様整合) | 全エンドポイントのレスポンス | `openapi.yaml` と実際のレスポンスの型・構造の一致確認。 |
| **Level 2** | **State Machine** <br> (状態遷移) | 非同期処理 (Upload, DeviceSync) | ポーリングによる状態遷移の追跡 (`pending` -> `running` -> `completed`)。 |
| **Level 3** | **Idempotency** <br> (冪等性・リトライ) | Enqueue系, Upload, DeviceSync | 同一リクエストの連打・再送を行い、副作用（重複生成・エラー）がないことを確認。 |
| **Level 4** | **Golden Data** <br> (不変条件確認) | STT結果, 要約結果 | 固定入力に対する出力の構造的整合性（`start < end` 等）の確認。 |

---

## 2. 状態遷移表 (State Transition Tables)

バックグラウンド処理はステートマシンとしてモデル化し、以下の遷移を期待値とします。

### A. オーディオアップロード (Audio Upload Flow)

| イベント | 前状態 | 次状態 (AudioStatus) | 期待される副作用 |
| :--- | :--- | :--- | :--- |
| `POST audio:prepareUpload` | (null) / pending | `pending` | `uploadUrl` 発行, `audio.hasAudio=false` |
| `POST audio:commit` | pending | `available` (*) | `audio.hasAudio=true`, `sizeBytes` 確定 |
| `POST audio:uploaded` | available | `processing` | `Cloud Tasks` (Transcribe) エンキュー |
| `Task: Transcribe End` | processing | `ready` | `transcriptText` 保存, `segments` 保存 |
| `Task: Failed` | processing | `failed` | `audioStatus=failed` |

(*) Note: `commit` 時点で `available` とし、即時再生可能とするが、STTは `uploaded` トリガーで開始される。

### B. デバイス同期 (Device Sync Flow)

| イベント | 前状態 | 次状態 (PlaylistStatus) | 期待される副作用 |
| :--- | :--- | :--- | :--- |
| `POST device_sync` (needsPlaylist=T) | (null) / failed | `pending` | `Cloud Tasks` (Playlist) エンキュー |
| `Task: Start` | pending | `running` | - |
| `Task: Complete` | running | `completed` | `playlist` 保存, `tags` 保存 |
| `Task: Failed` | running | `failed` | `playlistError` 保存 |

---

## 3. 実装パターン: 冪等性 (Idempotency)

リトライ耐性を担保するため、以下のパターンを実装します。

### 3.1 Idempotency Key の仕様
- **Header**: `Idempotency-Key: <UUID>` (推奨)
- **Body**: `idempotencyKey: <UUID>` (Cloud Tasks ペイロード用)

### 3.2 処理ロジック (Pattern)
Firestore Transaction または Atomic Operation を使用して、同一キーの処理をガードします。

```python
# app/utils/idempotency.py (Pseudo Code)

async def ensure_idempotency(key: str, context: str):
    doc_ref = db.collection("idempotency_locks").document(key)
    if doc_ref.get().exists:
        raise ResourceAlreadyProcessed()
    
    # Lock creation
    doc_ref.set({
        "context": context,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "status": "processing"
    })
```

---

## 4. テスト実装テンプレート (Pytest)

### 4.1 準備 (`tests/conftest.py`)
- `AsyncClient` (httpx) のセットアップ
- テスト用 Firebase Authentication Token の生成（Mock または Emulator）
- テストデータのクリーンアップ

### 4.2 State Machine Test (`tests/test_background_sync.py`)

```python
async def test_device_sync_state_transition(client, auth_headers):
    # 1. Setup Session
    session = await create_session(client, auth_headers)
    
    # 2. Trigger Sync
    resp = await client.post(f"/sessions/{session['id']}/device_sync", json={...}, headers=auth_headers)
    assert resp.status_code == 202
    
    # 3. Poll for Completion
    async for status in poll_playlist_status(client, session['id']):
        if status == "completed":
            break
        assert status in ["pending", "running"]
    
    # 4. Verify Result
    final = await get_session(client, session['id'])
    assert len(final["playlist"]) > 0
```
