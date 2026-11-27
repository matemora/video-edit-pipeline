#!/bin/bash

set -e

PROJECT_ID="ai-video-editor-510018"
REGION="southamerica-east1"
REPOSITORY="video-editor"
IMAGE="ingest-service"
SERVICE_ACCOUNT="video-editor-orchestrator@ai-video-editor-510018.iam.gserviceaccount.com"

IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE:latest"

gcloud config set project "$PROJECT_ID"

echo "🔨 Building..."
docker build -t "$IMAGE" .

echo "🏷️ Tagging..."
docker tag "$IMAGE" "$IMAGE_URI"

echo "📤 Pushing..."
docker push "$IMAGE_URI"

echo "🚀 Deploying Cloud Run Job..."

# Token OAuth do dono da pasta de saída (gerado com authorize_drive.py).
# Sem ele, o job roda normal e só não publica o vídeo final no Drive.
SECRET_ARGS=()
if gcloud secrets describe DRIVE_OAUTH_TOKEN --project="$PROJECT_ID" >/dev/null 2>&1; then
  SECRET_ARGS=(--set-secrets="DRIVE_OAUTH_TOKEN=DRIVE_OAUTH_TOKEN:latest")
else
  echo "⚠️ Secret DRIVE_OAUTH_TOKEN não existe; o upload para o Drive ficará desligado."
fi

if gcloud run jobs describe "$IMAGE" \
  --region="$REGION" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then

  gcloud run jobs update "$IMAGE" \
    --image="$IMAGE_URI" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$SERVICE_ACCOUNT" \
    --cpu=1 \
    --memory=1Gi \
    --task-timeout=3600s \
    --max-retries=0 \
    "${SECRET_ARGS[@]}"

else

  gcloud run jobs create "$IMAGE" \
    --image="$IMAGE_URI" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$SERVICE_ACCOUNT" \
    --cpu=1 \
    --memory=1Gi \
    --task-timeout=3600s \
    --max-retries=0 \
    "${SECRET_ARGS[@]}"

fi

echo "✅ Deploy concluído!"
echo ""
echo "Para executar o ingest:"
echo "gcloud run jobs execute $IMAGE --region=$REGION --project=$PROJECT_ID --wait"