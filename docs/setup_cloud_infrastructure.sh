#!/bin/bash

# Configuration
PROJECT_ID=${GCP_PROJECT:-"classnote-x-dev"}
LOCATION=${TASKS_LOCATION:-"asia-northeast1"}
QUEUE_NAME=${SUMMARIZE_QUEUE:-"summarize-queue"}
SERVICE_URL=${CLOUD_RUN_SERVICE_URL:-"REQUIRED_SERVICE_URL_HERE"} # e.g., https://api-xyz.a.run.app
SERVICE_ACCOUNT_EMAIL=${SERVICE_ACCOUNT_EMAIL:-"classnote-backend-sa@${PROJECT_ID}.iam.gserviceaccount.com"}

echo "Setting up Cloud Infrastructure for Project: $PROJECT_ID in $LOCATION"

# 1. Create Cloud Tasks Queue
echo "Creating Cloud Tasks Queue: $QUEUE_NAME..."
gcloud tasks queues create $QUEUE_NAME \
    --location=$LOCATION \
    --project=$PROJECT_ID \
    --max-dispatches-per-second=10 \
    --max-concurrent-dispatches=50 \
    || echo "Queue may already exist."

# 2. Create Cloud Scheduler for Audio Cleanup (Daily at 03:00 JST / 18:00 UTC)
echo "Creating Cloud Scheduler Job: audio-cleanup-daily..."
gcloud scheduler jobs create http audio-cleanup-daily \
    --schedule="0 3 * * *" \
    --time-zone="Asia/Tokyo" \
    --location=$LOCATION \
    --project=$PROJECT_ID \
    --uri="$SERVICE_URL/internal/tasks/audio-cleanup" \
    --http-method=POST \
    --oidc-service-account-email=$SERVICE_ACCOUNT_EMAIL \
    --attempt-deadline=1800s \
    || echo "Job audio-cleanup-daily may already exist."

# 3. Create Cloud Scheduler for Daily Usage Aggregation (Daily at 01:00 JST / 16:00 UTC)
echo "Creating Cloud Scheduler Job: usage-aggregation-daily..."
gcloud scheduler jobs create http usage-aggregation-daily \
    --schedule="0 1 * * *" \
    --time-zone="Asia/Tokyo" \
    --location=$LOCATION \
    --project=$PROJECT_ID \
    --uri="$SERVICE_URL/internal/tasks/daily-usage-aggregation" \
    --http-method=POST \
    --oidc-service-account-email=$SERVICE_ACCOUNT_EMAIL \
    --message-body='{}' \
    || echo "Job usage-aggregation-daily may already exist."

echo "Setup Complete. Please verify existing resources in Google Cloud Console."
