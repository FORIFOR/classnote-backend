#!/bin/bash

# エラーハンドリング: エラーがあれば即停止
set -e

# プロジェクト設定（適宜変更してください）
PROJECT_ID=${GCP_PROJECT:-"classnote-x-dev"}
export CLOUDSDK_CORE_PROJECT=$PROJECT_ID  # 確実にプロジェクトを切り替える

REGION=${GCP_REGION:-"asia-northeast1"}
SERVICE_NAME=${CLOUD_RUN_SERVICE:-"classnote-api"}
# Image name for Artifact Registry
IMAGE_NAME="$REGION-docker.pkg.dev/$PROJECT_ID/classnote-repo/$SERVICE_NAME"

echo "========================================"
echo "🚀 Deploying to Cloud Run"
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"
echo "Service: $SERVICE_NAME"
echo "========================================"

# 1. Build
echo "Building container image..."
gcloud builds submit --tag $IMAGE_NAME --project $PROJECT_ID .

# 2. Deploy
echo "Deploying service..."

# 環境変数は必要に応じて --update-env-vars で追加してください
# 例: --update-env-vars SUMMARIZE_QUEUE=summarize-queue
gcloud run deploy $SERVICE_NAME \
    --image $IMAGE_NAME \
    --project $PROJECT_ID \
    --region $REGION \
    --platform managed \
    --memory 2Gi \
    --cpu 1 \
    --allow-unauthenticated \
    --timeout 3600 \
    --update-env-vars SUMMARIZE_QUEUE=summarize-queue,TASKS_LOCATION=$REGION,USE_MOCK_DB=0,CLOUD_RUN_SERVICE_URL="https://classnote-api-mur5rvqgga-an.a.run.app",GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GCP_PROJECT=$PROJECT_ID,VERTEX_REGION=us-central1,GEMINI_MODEL_NAME=gemini-2.0-flash-lite,SIGNING_SA_EMAIL=classnote-api-sa@classnote-x-dev.iam.gserviceaccount.com,LINE_CHANNEL_ID=2008667999,USE_LOCAL_TASKS=0

echo "✅ Deployment completed successfully!"
