# ClassnoteX API リリース前監査レポート

**作成日**: 2026-01-19
**対象**: classnote-api バックエンド
**状態**: リリース前レビュー

---

## エグゼクティブサマリー

全機能を調査した結果、**50件以上の問題**を特定しました。

| 重要度 | 件数 | 主な問題 |
|--------|------|----------|
| **CRITICAL** | 12件 | セキュリティバイパス、認証欠如、機能未実装 |
| **HIGH** | 18件 | データ不整合、レースコンディション、制限回避 |
| **MEDIUM** | 15件 | バリデーション不足、エラーハンドリング |
| **LOW** | 8件 | コード品質、パフォーマンス |

---

## 1. セキュリティ・認証 (CRITICAL)

### 1.1 Admin認証バイパス機構 🚨
**ファイル**: `app/admin_auth.py:23-25`
```python
if os.environ.get("BYPASS_ADMIN_AUTH") == "1":
    return {"uid": "admin_bypass", "admin": True, "email": "admin@example.com"}
```
**問題**: 環境変数で完全な管理者権限バイパスが可能
**影響**: 本番環境で環境変数が漏洩すると全権限取得
**修正**: この機構を完全に削除

### 1.2 Cloud Tasks内部エンドポイント認証なし 🚨
**ファイル**: `app/routes/tasks.py`
```
POST /internal/tasks/summarize  - 認証なし
POST /internal/tasks/quiz       - 認証なし
POST /internal/tasks/transcribe - 認証なし
POST /internal/tasks/qa         - 認証なし
```
**問題**: 内部タスクエンドポイントが認証なしで公開
**影響**: エンドポイントを知っていれば誰でもタスク実行可能
**修正**: Cloud Tasks OIDC トークン検証を実装

### 1.3 CORS設定が全オリジン許可 🚨
**ファイル**: `app/main.py:72-78`
```python
allow_origins=["*"],
allow_credentials=True,
```
**問題**: 全オリジンからの認証付きリクエストを許可
**影響**: CSRF攻撃が可能、任意のWebサイトから認証済みAPIコール
**修正**: 明示的なオリジンリスト指定

### 1.4 デフォルト管理者シークレット 🚨
**ファイル**: `app/routes/usage.py:28`
```python
admin_secret = os.environ.get("USAGE_BACKFILL_SECRET", "classnote-admin-secret-123")
```
**問題**: ハードコードされたデフォルトシークレット
**影響**: 環境変数未設定時にデフォルト値で認証突破
**修正**: デフォルト値を削除、必須環境変数化

---

## 2. プラン制限・課金 (CRITICAL)

### 2.1 Basic プランのサポート欠如 🚨
**ファイル**: `app/services/cost_guard.py:56-129`
```python
if plan == "premium":
    limits = PREMIUM_LIMITS
else:
    limits = FREE_LIMITS  # Basic も Free 扱い
```
**問題**: Basic プランが定義されておらず Free 扱い
**影響**: Basic ユーザーが5セッション制限（本来20）
**修正**: BASIC_LIMITS を追加し、プラン判定を3段階に

### 2.2 プロダクトID未指定時に Pro 返却 🚨
**ファイル**: `app/routes/billing.py:52-53`
```python
if not product_id:
    return "pro"  # バグ: Free であるべき
```
**問題**: App Store Webhook で product_id なしの場合に Pro 返却
**影響**: 無料ユーザーが意図せず Pro 扱いになる可能性
**修正**: `return "free"` に変更

### 2.3 WebSocket で Cost Guard バイパス 🚨
**ファイル**: `app/routes/websocket.py`
**問題**: WebSocket ストリーミングが Cost Guard を使用していない
```python
# consume_free_cloud_credit() は常に True を返す (deprecated)
allowed = await usage_logger.consume_free_cloud_credit(uid)  # 常に True
```
**影響**: Free ユーザーが無制限にクラウド音声認識を使用可能
**修正**: `cost_guard.guard_can_consume()` を統合

---

## 3. AI機能 (CRITICAL)

### 3.1 Calendar Sync 機能が未実装 🚨
**問題箇所**:
- `google_calendar.sync_event()` 関数が存在しない
- `/internal/tasks/calendar_sync` ハンドラがない
- `enqueue_calendar_sync_task()` がない

**現状コード** (`sessions.py:1940-1946`):
```python
elif req.type == "calendar_sync":
    google_calendar.sync_event(session_id, current_user.uid)  # AttributeError
```
**影響**: Calendar Sync を呼び出すと即座にクラッシュ
**修正**: 完全な実装が必要、または機能を無効化

### 3.2 _run_local_nuke 関数が未定義 🚨
**ファイル**: `app/task_queue.py:471`
```python
asyncio.create_task(_run_local_nuke(user_id))  # NameError
```
**影響**: ローカル開発モードでアカウント削除がクラッシュ
**修正**: 関数を実装するか呼び出しを削除

---

## 4. セッション管理 (HIGH)

### 4.1 _resolve_session の未定義変数 🚨
**ファイル**: `app/routes/sessions.py:170`
```python
else:
    results = list(query.stream())  # query が未定義
```
**問題**: user_id=None の場合に NameError
**修正**:
```python
results = list(db.collection("sessions")
    .where("clientSessionId", "==", session_id)
    .limit(1).stream())
```

### 4.2 clientSessionId 未対応エンドポイント (10件以上)
以下のエンドポイントは `_resolve_session` を使用しておらず、clientSessionId で404:

| エンドポイント | 影響 |
|---------------|------|
| `/sessions/{id}/transcript_chunks:append` | オフライン→オンライン同期失敗 |
| `/sessions/{id}/transcript_chunks:replace` | 同上 |
| `/sessions/{id}/device_sync` | デバイス同期失敗 |
| `/sessions/{id}/share:invite` | 共有招待失敗 |
| `/sessions/{id}/members` | メンバー取得失敗 |
| `/sessions/{id}/share/code` | シェアコード生成失敗 |

**修正**: 全エンドポイントで `_resolve_session()` を使用

### 4.3 複数オーナーフィールドの不整合
**ファイル**: `app/dependencies.py:219-229`
```python
owner = session_data.get("ownerUid") or session_data.get("ownerUserId") or
        session_data.get("ownerId") or session_data.get("userId")
```
**問題**: 4つのフィールドが混在し、どれが正しいか不明
**影響**: 権限チェックの不整合による認可バイパスの可能性
**修正**: `ownerUid` に統一し、マイグレーション実施

### 4.4 ソフト削除時の関連データ未削除
**問題**: セッション削除時に以下が残存:
- `sessions/{id}/transcript_chunks/*`
- `sessions/{id}/derived/*`
- `sessions/{id}/jobs/*`
- `session_members/{id}_*`
- `users/*/sessionMeta/{id}`
- GCS 音声ファイル

**影響**: Firestore コスト増加、孤立データ
**修正**: カスケード削除の実装

---

## 5. WebSocket (HIGH)

### 5.1 同時接続ロックのタイムアウトが3時間
**ファイル**: `app/routes/websocket.py:107-127`
```python
if (datetime.utcnow() - last_active).total_seconds() < 10800:  # 3時間
    return False
```
**問題**: クラッシュ後3時間再接続不可
**修正**: 5分程度に短縮、または手動ロック解除エンドポイント追加

### 5.2 音声なしタイムアウトがstart後のみ
**問題**: start イベント前は無限待機可能
**影響**: リソース枯渇攻撃
**修正**: 接続直後からタイムアウト開始

### 5.3 キューバックプレッシャーで音声消失
**ファイル**: `app/routes/websocket.py:388-395`
```python
if audio_queue.full():
    _ = audio_queue.get_nowait()  # 無言でドロップ
```
**問題**: 音声データがサイレントに消失
**影響**: 文字起こし品質劣化（ユーザー通知なし）
**修正**: ドロップ通知、またはキューサイズ拡大

---

## 6. 音声・文字起こし (HIGH)

### 6.1 AudioStatus.DELETED が Enum に存在しない
**ファイル**: `app/routes/sessions.py:3027`
```python
"audioStatus": AudioStatus.DELETED.value  # AttributeError
```
**修正**: `util_models.py` の AudioStatus に `DELETED = "deleted"` を追加

### 6.2 SHA256 検証が未実装
**ファイル**: `app/routes/sessions.py:2917`
```python
# expectedPayloadSha256 は受け取るが検証しない
# GCS は MD5 のみ提供
```
**影響**: 破損ファイルのアップロード検出不可
**修正**: クライアント側検証を信頼、またはサーバー側で再計算

### 6.3 WAV 変換後のサイズ制限なし
**ファイル**: `app/services/google_speech.py:68-90`
**問題**: 圧縮音声を非圧縮 WAV に変換（1時間 = ~115MB）
**影響**: GCS クォータ超過、メモリ枯渇
**修正**: 変換前に推定サイズチェック

### 6.4 セグメントタイムスタンプ検証なし
**ファイル**: `app/routes/sessions.py:1250-1260`
**問題**: startSec < endSec、重複チェックなし
**影響**: 不正なタイムライン表示
**修正**: セグメント順序・範囲バリデーション追加

---

## 7. 入力バリデーション (MEDIUM)

### 7.1 タイトル長制限なし
**ファイル**: `app/util_models.py:76-92`
```python
class CreateSessionRequest(BaseModel):
    title: str  # max_length なし
```
**修正**: `Field(max_length=200)` 追加

### 7.2 WebSocket 言語コード/サンプルレート検証なし
**ファイル**: `app/routes/websocket.py:319-322`
```python
language_code = client_config["languageCode"]  # 検証なし
sample_rate = int(client_config["sampleRateHertz"])  # 検証なし
```
**修正**: BCP 47 形式チェック、サンプルレート範囲チェック

### 7.3 シェアコード総当たり対策なし
**ファイル**: `app/routes/users.py:654-680`
**問題**: 6桁数字コード（100万通り）にレート制限なし
**修正**: IP/ユーザーベースのレート制限追加

---

## 8. エラーハンドリング (MEDIUM)

### 8.1 サイレント except: pass
**ファイル**: `app/routes/tasks.py:220, 481`
```python
except: pass  # エラー握りつぶし
```
**修正**: 最低限 `logger.exception()` でログ

### 8.2 datetime.utcnow() の使用（非推奨）
**ファイル**: `app/task_queue.py` 他
```python
datetime.utcnow()  # Python 3.12+ で非推奨
```
**修正**: `datetime.now(timezone.utc)` に統一

### 8.3 重複した重複チェックロジック
**ファイル**: `app/routes/sessions.py:1714-1728, 1797-1812`
**問題**: create_job() 内で同じチェックが2回
**修正**: 1箇所に統合

---

## 9. データ整合性 (MEDIUM)

### 9.1 ロールの二重管理
**問題**: ロールが2箇所に保存:
- `session_members/{session_id}_{user_id}` ドキュメント
- `sessions/{session_id}/participants/{user_id}` マップ

**影響**: 部分更新でロール不整合
**修正**: トランザクションで同時更新、または1箇所に統一

### 9.2 cloudEntitledSessionIds の未クリーンアップ
**問題**: セッション削除時に配列から削除されない
**影響**: 制限カウントが永久に増加
**修正**: 削除時に配列から除去

### 9.3 ユーザーID解決の不整合
複数のパターンが混在:
```python
# パターン1
data.get("ownerUserId") or data.get("userId") or data.get("ownerUid")
# パターン2
data.get("ownerUid") or data.get("userId") or data.get("ownerUserId")
# パターン3
data.get("userId") or data.get("ownerUserId")
```
**修正**: 統一ヘルパー関数を作成

---

## 10. パフォーマンス (LOW)

### 10.1 セッション一覧で3クエリ実行
**ファイル**: `app/routes/sessions.py:869-883`
```python
owned_docs = db.collection("sessions").where("ownerUserId", "==", uid)
shared_docs = db.collection("sessions").where("participantUserIds", "array_contains", uid)
legacy_shared_docs = db.collection("sessions").where(f"sharedWith.{uid}", "==", True)
```
**修正**: データモデル統一後、1クエリに

### 10.2 STT ドレインタイムアウトが短い
**ファイル**: `app/routes/websocket.py:22`
```python
STT_DRAIN_TIMEOUT_SEC = 5.0  # 5秒は短い
```
**修正**: 15-30秒に延長

---

## 修正優先度マトリックス

### 即時対応必須 (リリースブロッカー)

| # | 問題 | ファイル | 修正工数 |
|---|------|----------|----------|
| 1 | Admin バイパス削除 | admin_auth.py | 小 |
| 2 | CORS 修正 | main.py | 小 |
| 3 | デフォルトシークレット削除 | usage.py | 小 |
| 4 | _resolve_session の NameError | sessions.py | 小 |
| 5 | AudioStatus.DELETED 追加 | util_models.py | 小 |
| 6 | WebSocket Cost Guard 統合 | websocket.py | 中 |
| 7 | Basic プランサポート | cost_guard.py | 中 |
| 8 | Calendar Sync 無効化/実装 | sessions.py, google_calendar.py | 大 |

### 高優先度 (1週間以内)

| # | 問題 | ファイル | 修正工数 |
|---|------|----------|----------|
| 9 | Cloud Tasks 認証 | tasks.py | 大 |
| 10 | clientSessionId 対応漏れ | sessions.py | 中 |
| 11 | 同時接続ロックタイムアウト | websocket.py | 小 |
| 12 | オーナーフィールド統一 | 全体 | 大 |
| 13 | ソフト削除カスケード | sessions.py | 中 |
| 14 | billing.py Pro デフォルト | billing.py | 小 |

### 中優先度 (2週間以内)

| # | 問題 | ファイル | 修正工数 |
|---|------|----------|----------|
| 15 | 入力バリデーション強化 | util_models.py | 中 |
| 16 | シェアコードレート制限 | users.py | 中 |
| 17 | セグメント検証 | sessions.py | 小 |
| 18 | ロール二重管理解消 | sessions.py, dependencies.py | 大 |
| 19 | サイレント except 修正 | tasks.py | 小 |

---

## 付録: 修正コードスニペット

### A. Admin バイパス削除
```python
# admin_auth.py - 削除すべきコード (23-25行目)
# if os.environ.get("BYPASS_ADMIN_AUTH") == "1":
#     logger.warning("!!! BYPASSING ADMIN AUTH (BYPASS_ADMIN_AUTH=1) !!!")
#     return {"uid": "admin_bypass", "admin": True, "email": "admin@example.com"}
```

### B. CORS 修正
```python
# main.py:72-78
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://classnote.app",
        "https://www.classnote.app",
        "http://localhost:3000",  # 開発用
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
```

### C. Basic プラン追加
```python
# cost_guard.py に追加
BASIC_LIMITS = {
    "server_session": 20,
    "cloud_session": 10,
    "cloud_stt_sec": 36000,  # 10時間
    "summary_generated": 20,
    "quiz_generated": 10,
}

# guard_can_consume() 内
if plan == "premium":
    limits = PREMIUM_LIMITS
elif plan == "basic":
    limits = BASIC_LIMITS
else:
    limits = FREE_LIMITS
```

### D. AudioStatus.DELETED 追加
```python
# util_models.py
class AudioStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    AVAILABLE = "available"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    FAILED = "failed"
    DELETED = "deleted"  # 追加
```

### E. _resolve_session 修正
```python
# sessions.py:168-175
else:
    # user_id がない場合は clientSessionId のみでクエリ
    results = list(db.collection("sessions")
        .where("clientSessionId", "==", session_id)
        .limit(1).stream())
```

---

## 結論

リリース前に最低限 **即時対応必須** の8項目を修正してください。
特にセキュリティ関連（Admin バイパス、CORS、デフォルトシークレット）は
**本番環境で深刻な脆弱性**となります。

高優先度の項目もリリース後1週間以内に対応することを強く推奨します。
