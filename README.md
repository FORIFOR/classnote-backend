# ClassnoteX Backend API

ClassnoteX (Glassnote-X) のバックエンド API サーバーです。
FastAPI をベースに構築されており、音声のアップロード、文字起こし、要約、クイズ生成などの機能を提供します。

## 主な機能

- **セッション管理**: 講義や会議のセッションを作成・管理
- **音声アップロード**: Cloud Storage へのセキュアなアップロード (Signed URL)
- **AI 機能 (Gemini)**:
    - 講義/会議の要約生成
    - 理解度確認クイズの生成
    - QA (質問応答)
- **文字起こし**: Cloud Speech-to-Text (Batch) との連携 (※一部スタブ)
- **話者分離**: 話者の識別と発言の分離 (※一部スタブ)

## 動作環境

- Python 3.11+
- Google Cloud Platform (Firestore, Cloud Storage, Vertex AI, etc.)

## セットアップ (ローカル開発)

### 1. 簡易セットアップ

付属のセットアップスクリプトを使用すると、仮想環境の作成から依存パッケージのインストールまでを自動で行えます。

```bash
chmod +x setup.sh
./setup.sh
```

### 2. 手動セットアップ

手動でセットアップする場合の手順です。

```bash
# 仮想環境の作成
python3 -m venv .venv

# 仮想環境の有効化
source .venv/bin/activate

# 依存パッケージのインストール
pip install -r requirements.txt
```

## サーバーの起動

### モックモード (推奨)

GCP の認証情報が手元になくても動作確認ができるモードです。
Firestore や Gemini の呼び出しがモック化され、ローカルだけで完結して動きます。

```bash
# setup.sh を実行済みの場合、仮想環境に入ってから実行してください
source .venv/bin/activate

export USE_MOCK_DB=1
export ENABLE_STREAMING_STT=0
uvicorn app.main:app --reload --port 8000
```

### 本番/開発モード (GCP接続)

実際に GCP のサービスに接続して動作させる場合です。
`GOOGLE_APPLICATION_CREDENTIALS` などの認証設定が必要です。

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
export AUDIO_BUCKET=gs://your-audio-bucket
uvicorn app.main:app --reload --port 8000
```

## API 仕様

API の詳細な仕様は `docs/API_SPECIFICATION.md` または `openapi.yaml` を参照してください。

サーバー起動後、以下の URL で Swagger UI にアクセスできます。
- [http://localhost:8000/docs](http://localhost:8000/docs)

## ディレクトリ構成

- `app/`: アプリケーションコード
    - `main.py`: エントリーポイントと API 定義
    - `models.py`: Pydantic モデル定義
- `scripts/`: ユーティリティスクリプト
    - `validate_openapi.py`: OpenAPI 定義と実装の整合性チェック
- `docs/`: ドキュメント
- `openapi.yaml`: OpenAPI 3.0 定義ファイル
