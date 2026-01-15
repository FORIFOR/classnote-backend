# Deployment Guide

Classnote API のデプロイ手順書です。

## 前提条件

- Google Cloud SDK (`gcloud`) がインストール・認証済みであること。
- Docker が動作している必要はありません（Cloud Build を使用するため）。

## デプロイ手順

プロジェクトルートで以下のコマンドを実行するだけです。

```bash
./tools/deploy.sh
```

このスクリプトは以下の処理を行います：
1. `gcloud builds submit`: ソースコードを Cloud Build に送信し、コンテナイメージをビルドして Container Registry (GCR) に保存します。
2. `gcloud run deploy`: 新しいイメージを使って Cloud Run サービスを更新します。

## 環境変数の設定

`tools/deploy.sh` 内で環境変数を設定しています。必要に応じて編集してください。

| 変数名 | 説明 |
|---|---|
| `SUMMARIZE_QUEUE` | Cloud Tasks のキュー名 (例: `summarize-queue`) |
| `TASKS_LOCATION` | Cloud Tasks のリージョン (例: `asia-northeast1`) |
| `CLOUD_RUN_SERVICE_URL` | 自分自身の公開URL（Cloud Tasks からのコールバック用） |
| `USE_MOCK_DB` | 本番環境では `0` に設定 |

## 初回セットアップ時のみ必要な作業

もし新しいプロジェクトにデプロイする場合は、Cloud Tasks のキューを作成する必要があります。詳細は `docs/CLOUD_TASKS_SETUP.md` を参照してください。

```bash
gcloud tasks queues create summarize-queue --location=asia-northeast1
```

## 音声の自動削除（30日）ジョブ

音声の期限切れ削除は Cloud Run Jobs + Cloud Scheduler で実行します。

```bash
# Cloud Run Job を作成（初回のみ）
gcloud run jobs create classnote-audio-cleanup \
  --project=classnote-x-dev \
  --region=asia-northeast1 \
  --image=asia-northeast1-docker.pkg.dev/classnote-x-dev/classnote-repo/classnote-api \
  --command=python \
  --args=-m,app.jobs.cleanup_audio \
  --set-env-vars=GOOGLE_CLOUD_PROJECT=classnote-x-dev,AUDIO_CLEANUP_LIMIT=200

# Cloud Scheduler（毎日 3:30 JST）
gcloud scheduler jobs create http classnote-audio-cleanup-daily \
  --project=classnote-x-dev \
  --location=asia-northeast1 \
  --schedule="30 18 * * *" \
  --time-zone="Asia/Tokyo" \
  --http-method=POST \
  --uri="https://asia-northeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/classnote-x-dev/jobs/classnote-audio-cleanup:run" \
  --oauth-service-account-email=900324644592-compute@developer.gserviceaccount.com
```

既存データで `audio.deleteAfterAt` が無い場合は削除対象になりません。必要なら別途バックフィルが必要です。

## deleteAfterAt バックフィル（既存データ向け）

既存セッションで `audio.deleteAfterAt` が無い場合、下記ジョブを一度だけ実行してください。

```bash
# Cloud Run Job を作成（初回のみ）
gcloud run jobs create classnote-audio-backfill \
  --project=classnote-x-dev \
  --region=asia-northeast1 \
  --image=asia-northeast1-docker.pkg.dev/classnote-x-dev/classnote-repo/classnote-api \
  --command=python \
  --args=-m,app.jobs.backfill_audio_delete_after \
  --set-env-vars=GOOGLE_CLOUD_PROJECT=classnote-x-dev,AUDIO_BACKFILL_LIMIT=200,AUDIO_DELETE_TTL_DAYS=30

# 手動実行（必要なタイミングで1回）
gcloud run jobs execute classnote-audio-backfill \
  --project=classnote-x-dev \
  --region=asia-northeast1
```
