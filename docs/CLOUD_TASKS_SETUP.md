# Cloud Tasks Setup Guide

ClassnoteX の非同期要約機能を使用するためには、Google Cloud Tasks のキューを作成する必要があります。

## 1. Cloud Tasks API の有効化

```bash
gcloud services enable cloudtasks.googleapis.com
```

## 2. キューの作成

以下のコマンドでキューを作成します。
リージョンは Cloud Run と同じ場所（例: `asia-northeast1`）を推奨します。

```bash
gcloud tasks queues create summarize-queue \
    --location=asia-northeast1
```

## 3. 環境変数の設定

Cloud Run のデプロイ時に以下の環境変数を設定してください。

| 変数名 | 値の例 | 説明 |
|--------|--------|------|
| `SUMMARIZE_QUEUE` | `summarize-queue` | 作成したキューの名前 |
| `TASKS_LOCATION` | `asia-northeast1` | キューの場所 |
| `CLOUD_RUN_SERVICE_URL` | `https://classnote-api-xxx.run.app` | 自身のサービスURL（Worker呼び出し用） |
| `GCP_PROJECT` | `classnote-x-dev` | プロジェクトID |

## 4. 権限設定（IAM）

Cloud Run のサービスアカウントが Cloud Tasks を作成できるように権限を付与します。

```bash
# サービスアカウントの特定（デフォルトの場合）
SERVICE_ACCOUNT=$(gcloud list compute-service-accounts --format="value(email)" | head -n 1)

# Cloud Tasks Enqueuer ロールを付与
gcloud projects add-iam-policy-binding $GCP_PROJECT \
    --member=serviceAccount:${SERVICE_ACCOUNT} \
    --role=roles/cloudtasks.enqueuer
```

## ローカル開発時

ローカル環境（`localhost`）では Cloud Tasks に接続できないため、`app/task_queue.py` 内で自動的に `BackgroundTasks`（FastAPI 標準機能）にフォールバック、またはログ出力のみを行うスタブ動作になります。

`USE_LOCAL_TASKS=1` を環境変数に設定すると、明示的に Cloud Tasks をスキップできます。
