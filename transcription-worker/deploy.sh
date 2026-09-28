#!/bin/bash

set -e

PROJECT_ID="ai-video-editor-510018"
REGION="southamerica-east1"
REPOSITORY="video-editor"
JOB="transcription-worker"

IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$JOB:latest"

echo "🔨 Building..."
gcloud builds submit \
  --tag "$IMAGE"

echo "🚀 Deploying Cloud Run Job..."
gcloud run jobs deploy "$JOB" \
  --image "$IMAGE" \
  --region "$REGION" \
  --cpu 4 \
  --memory 16Gi \
  --gpu 1 \
  --gpu-type nvidia-l4 \
  --max-retries 1

echo "✅ Deploy concluído!"
