#!/bin/bash

set -e

PROJECT_ID="ai-video-editor-510018"
REGION="southamerica-east1"
REPOSITORY="video-editor"
IMAGE="sync-worker"

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
  --region="$REGION"

echo "✅ Deploy concluído!"
