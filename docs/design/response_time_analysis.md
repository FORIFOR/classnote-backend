# バックグラウンド処理 応答時間ボトルネック分析

## 概要
ClassnoteX API のバックグラウンド処理で応答時間が遅くなる可能性のある箇所を調査しました。

---

## 🔴 クリティカル（ユーザー体験に直接影響）

### 1. 同期 LLM 呼び出し（`/qa`, `/translate`）

| エンドポイント | 問題 | 推定遅延 |
|--------------|------|---------|
| `POST /sessions/{id}/qa` | `await llm.answer_question()` を直接呼び出し | **3〜15秒** |
| `POST /sessions/{id}/transcript:translate` | `await llm.translate_text()` を直接呼び出し | **5〜20秒** |

**問題点:**
- クライアントは HTTP レスポンスを待ち続ける（タイムアウトリスク）
- Vertex AI の応答時間は不安定（ピーク時にさらに遅延）

**推奨対策:**
- Cloud Tasks にオフロードし、202 Accepted を即座に返す
- ポーリング or WebSocket で結果を通知

---

### 2. GCS `blob.exists()` 呼び出し

| 箇所 | 問題 | 推定遅延 |
|------|------|---------|
| `GET /sessions/{id}/audio_url` (L1540) | 毎回 GCS にメタデータ取得リクエスト | **50〜200ms** |
| `DELETE /sessions/{id}/audio` (L1685) | 削除前の存在確認 | **50〜200ms** |

**問題点:**
- `blob.exists()` は GCS への HTTP リクエストを発行する同期処理
- 頻繁なアクセスで累積遅延が大きくなる

**推奨対策:**
- `audioStatus` を Firestore で管理し、GCS 確認は非同期ジョブで行う
- 署名付き URL のキャッシュを活用（既に一部実装済み）

---

## 🟡 中程度（最適化余地あり）

### 3. Firestore 複数読み取り

| 箇所 | 問題 |
|------|------|
| `GET /sessions` (list_sessions) | `db.get_all()` で sessionMeta を一括取得しているが、クエリ2回 + get_all |
| `GET /sessions/{id}` | Session + SessionMeta の2回読み取り |

**推奨対策:**
- SessionMeta を Session ドキュメントにインライン化（書き込みトレードオフあり）
- Firestore クエリのインデックス最適化

### 4. 署名付き URL 生成

| 箇所 | 問題 | 推定遅延 |
|------|------|---------|
| `list_image_notes` (L2448) | 画像ごとに `generate_signed_url()` | **画像3枚で 300〜600ms** |

**推奨対策:**
- 署名付き URL をFirestore にキャッシュ（1時間 TTL）

---

## 🟢 問題なし（適切に非同期化済み）

| 処理 | 状態 |
|------|------|
| 要約生成 (`/summarize`) | ✅ Cloud Tasks にオフロード |
| クイズ生成 (`/quiz`) | ✅ Cloud Tasks にオフロード |
| プレイリスト生成 | ✅ Cloud Tasks にオフロード |
| 音声アップロード通知 | ✅ 202 Accepted で即応答 |
| デバイス同期 (`/device_sync`) | ✅ 202 Accepted で即応答 |

---

## 優先度順の改善アクション

| 優先度 | 対象 | アクション |
|--------|------|-----------|
| **P0** | `/qa` | Cloud Tasks 化 + ポーリング/WebSocket |
| **P0** | `/translate` | Cloud Tasks 化 + ポーリング/WebSocket |
| **P1** | `audio_url` | GCS exists() 呼び出しを削除、キャッシュ優先 |
| **P2** | `list_image_notes` | 署名付き URL キャッシュ |
| **P3** | `list_sessions` | クエリ最適化 |
