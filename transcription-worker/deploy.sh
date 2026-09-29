PROJECT_ID="ai-video-editor-510018"

CLOUD_RUN_REGION="europe-west1"
REGISTRY_REGION="southamerica-east1"

REPOSITORY="video-editor"
IMAGE="transcription-worker"

gcloud config set project "$PROJECT_ID"

IMAGE_URI="$REGISTRY_REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE:latest"

echo "🔨 Building with Cloud Build..."

gcloud builds submit \
  --tag "$IMAGE_URI" \
  .

echo "🚀 Creating/updating Cloud Run Job..."

if gcloud run jobs describe "$IMAGE" \
  --region="$CLOUD_RUN_REGION" \
  >/dev/null 2>&1; then

  gcloud run jobs update "$IMAGE" \
    --image="$IMAGE_URI" \
    --region="$CLOUD_RUN_REGION" \
    --cpu=4 \
    --memory=16Gi \
    --gpu=1 \
    --gpu-type=nvidia-l4 \
    --no-gpu-zonal-redundancy

else

  gcloud run jobs create "$IMAGE" \
    --image="$IMAGE_URI" \
    --region="$CLOUD_RUN_REGION" \
    --cpu=4 \
    --memory=16Gi \
    --gpu=1 \
    --gpu-type=nvidia-l4 \
    --no-gpu-zonal-redundancy

fi

echo "✅ Deploy concluído!"