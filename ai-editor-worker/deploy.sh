#!/bin/bash

set -e

PROJECT_ID="ai-video-editor-510018"
REGION="southamerica-east1"
REPOSITORY="video-editor"
IMAGE="ai-editor-worker"

gcloud config set project $PROJECT_ID

echo "🔨 Building..."
docker build -t "$IMAGE" .

echo "🏷️ Tagging..."
docker tag "$IMAGE" \
  "$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE:latest"

echo "📤 Pushing..."
docker push \
  "$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE:latest"

echo "🚀 Updating Cloud Run Job..."
gcloud run jobs update "$IMAGE" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE:latest" \
  --region="$REGION" \
  --set-secrets="GEMINI_API_KEY=GEMINI_API_KEY:latest"

echo "✅ Deploy concluído!"
