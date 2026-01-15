# ClassnoteX Backend API 仕様書

**Version**: 0.1.0  
**Last Updated**: 2025-12-12  
**Base URL**: `https://classnote-api-900324644592.asia-northeast1.run.app`  

---

## 目次

1. [概要](#概要)
2. [認証](#認証)
3. [セッション (Sessions)](#セッション-sessions)
4. [録音・同期 (Transcription & Sync)](#録音同期-transcription--sync)
5. [AI 生成 (Summary, Quiz, Playlist)](#ai-生成-summary-quiz-playlist)
6. [メディア (Audio & Images)](#メディア-audio--images)
7. [共有 (Sharing)](#共有-sharing)
8. [データモデル](#データモデル)

---

## 概要

ClassnoteX (GlassnoteX) のバックエンド API 仕様書です。  
クライアント（iOS/Web）からのデータ同期、AI 生成タスクの管理、およびユーザー間共有機能を提供します。

### ステータス管理
セッションは以下のライフサイクルステータスを持ちます：
- `予定` / `未録音` / `録音中` / `録音済み`
- `要約済み` / `テスト生成` / `テスト完了`

---

## 認証

Firebase Authentication を使用します。すべてのリクエストヘッダーに ID トークンを含めてください。

```http
Authorization: Bearer <FIREBASE_ID_TOKEN>
```

---

## セッション (Sessions)

### `GET /sessions`
ユーザーのセッション一覧を取得します。自分が所有するセッションと、共有されたセッションが含まれます。

**Query Parameters:**
- `userId` (Required): ユーザーID（通常は自分のUID）
- `mode`: `lecture` | `meeting` (Optional)

### `POST /sessions`
新規セッションを作成します。

**Request Body:**
- `title` (string, required)
- `mode` (string, required): `lecture` | `meeting`
- `userId` (string, optional): 指定しない場合、トークンの UID が使用されます。
- `tags` (string[], optional)

### `GET /sessions/{id}`
セッションの詳細を取得します。AI 生成結果（要約、クイズ、プレイリスト）もこのレスポンスに含まれます。

**Response (Session Object):**
- `summaryStatus`, `quizStatus`, `playlistStatus`: 各タスクの実行状態 (`pending`, `running`, `completed`, `failed`)
- `summaryMarkdown`: 生成された要約 Markdown
- `quizMarkdown`: 生成されたクイズ Markdown
- `playlist`: 再生リストアイテムの配列
- `transcriptText`: 文字起こしテキスト
- `diarizedSegments`: 話者分離セグメント

---

## 録音・同期 (Transcription & Sync)

### `POST /sessions/{id}/device_sync`
iOS デバイス等のオンデバイス処理結果をサーバーに同期します。

**Request Body:**
- `audioPath` (string, required): GCS 上の音声ファイルパス
- `durationSec` (number): 音声の長さ（秒）
- `transcriptText` (string): 全文テキスト
- `segments` (Segment[]): セグメント情報。`speakerId` は話者分離 OFF 時は省略可。
- `needsPlaylist` (boolean): `true` の場合、サーバー側で要約・プレイリスト生成を自動トリガーします。

**Response:**
- `202 Accepted`

### `POST /upload-url`
音声ファイルアップロード用の署名付き URL を取得します。

**Request Body:**
- `sessionId` (string)
- `contentType` (string)

**Response:**
- `uploadUrl`: PUT 用の署名付き URL
- `storagePath`: 保存先パス

---

## AI 生成 (Summary, Quiz, Playlist)

### `POST /sessions/{id}/summarize`
要約・プレイリスト・タグ生成を手動でトリガーします。
（通常は `device_sync` 時に自動実行されます）

**Response:**
- `202 Accepted` (非同期実行)

### `POST /sessions/{id}/quiz?count=5`
クイズ生成をトリガーします。

**Response:**
- `202 Accepted`

**確認方法:**
クライアントは `GET /sessions/{id}` をポーリングし、`quizStatus` が `completed` になるのを待ちます。

### `POST /sessions/{id}/qa`
文字起こし内容に基づいて質問に回答します（RAG 的アプローチ）。

**Request Body:**
- `question` (string)

**Response:**
- `answer` (string): 回答
- `citations`: 根拠となるテキストとその理由

---

## メディア (Audio & Images)

### `GET /sessions/{id}/audio_url`
音声再生用の署名付き URL (GET) を取得します。

### `POST /sessions/{id}/image_notes/upload_url`
画像ノート（板書など）のアップロード用 URL を取得します。

**Response:**
- `uploadUrl`: PUT 用 URL
- `imageId`: 生成された画像 ID
- `storagePath`: 保存パス

### `GET /sessions/{id}/image_notes`
セッションに紐付く画像ノートの一覧（署名付き URL 含む）を取得します。

---

## Imports

### `POST /imports/youtube`
YouTube の字幕/文字起こしを取得してセッションを作成し、要約/テスト生成をキューに入れます。

**Request Body:**
- `url` (string): YouTube URL
- `mode` (string, optional): `lecture` | `meeting` (default `lecture`)
- `title` (string, optional): セッションタイトル
- `language` (string, optional): 優先言語 (default `ja`)

**Response:**
- `sessionId`
- `transcriptStatus` (`ready`)
- `summaryStatus` (`pending`)
- `quizStatus` (`pending`)

**Notes:**
- 字幕が取得できない動画は 422 で失敗します（音声ダウンロードはしません）。

---

## 共有 (Sharing)

共有コード（6桁の英数字）を使用してセッションを他のユーザーと共有します。

### `POST /users/me/share_code`
自分の共有コードを発行します。

### `POST /sessions/{id}/share`
他者の共有コードを指定して、そのユーザーを共有メンバーに追加します。

**Request Body:**
- `targetShareCode` (string)

---

## データモデル

### Session
```json
{
  "id": "lecture-xxx",
  "title": "...",
  "status": "transcribed",
  "summaryStatus": "completed",
  "summaryMarkdown": "## 要約...",
  "quizStatus": "completed",
  "quizMarkdown": "...",
  "quizJson": "...",
  "playlist": [
    { "id": "c1", "startSec": 0, "endSec": 60, "title": "導入" }
  ],
  "imageNotes": [
    { "id": "img_1", "storagePath": "..." }
  ]
}
```

### Segment
```json
{
  "startSec": 0.0,
  "endSec": 10.5,
  "text": "...",
  "speakerId": "spk_0"  // Optional
}
```

### PlaylistItem
```json
{
  "id": "c1",
  "startSec": 10.0,
  "endSec": 120.0,
  "title": "章のタイトル",
  "summary": "詳細な説明",
  "confidence": 0.95
}
```
