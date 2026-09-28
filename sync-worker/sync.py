import os
import subprocess
from pathlib import Path
from google.cloud import storage

BUCKET = os.environ["BUCKET"]
VIDEO_INPUT = os.environ["VIDEO_INPUT"]
AUDIO_INPUT = os.environ["AUDIO_INPUT"]
OUTPUT = os.environ["OUTPUT"]

WORKDIR = Path("/tmp/work")
WORKDIR.mkdir(exist_ok=True)

video_path = WORKDIR / "video_input"
audio_path = WORKDIR / "audio_input"
output_path = WORKDIR / "output.mp4"


def download_gcs(uri, destination):
    bucket_name, blob_name = uri.replace("gs://", "", 1).split("/", 1)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    print(f"Downloading gs://{bucket_name}/{blob_name}")
    blob.download_to_filename(destination)


def upload_gcs(source, uri):
    bucket_name, blob_name = uri.replace("gs://", "", 1).split("/", 1)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    print(f"Uploading {source} -> gs://{bucket_name}/{blob_name}")
    blob.upload_from_filename(source)


print("Starting sync worker")

download_gcs(VIDEO_INPUT, video_path)
download_gcs(AUDIO_INPUT, audio_path)

print("Video size:", video_path.stat().st_size)
print("Audio size:", audio_path.stat().st_size)

# Apenas teste: copia o vídeo para o output.
# A sincronização real entra na próxima etapa.
subprocess.run(
    [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c",
        "copy",
        str(output_path),
    ],
    check=True,
)

upload_gcs(output_path, OUTPUT)

print("Sync worker completed successfully")
