import os
import subprocess
import whisperx
from google.cloud import storage

INPUT_URI = "gs://ai-video-editor-510018/synced/video-test-output.mp4"

VIDEO_PATH = "/tmp/video_synced.mp4"
AUDIO_PATH = "/tmp/audio_synced.wav"


def download_from_gcs(gcs_uri, local_path):
    bucket_name, blob_name = gcs_uri.replace("gs://", "", 1).split("/", 1)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    print(f"📥 Downloading {gcs_uri}...")
    blob.download_to_filename(local_path)


def extract_audio(video_path, audio_path):
    print("🎙️ Extraindo áudio do vídeo sincronizado...")

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            audio_path,
        ],
        check=True,
    )


print("🚀 Transcription worker")

print(f"📁 Input: {INPUT_URI}")

download_from_gcs(INPUT_URI, VIDEO_PATH)

print("🎬 Vídeo sincronizado baixado.")

extract_audio(VIDEO_PATH, AUDIO_PATH)

print("✅ Áudio extraído.")

print("📦 Carregando WhisperX...")

device = "cuda"
compute_type = "float16"

model = whisperx.load_model(
    "large-v3",
    device,
    compute_type=compute_type,
    language="pt",
)

print("✅ Modelo carregado!")

print("🧠 Transcrevendo áudio sincronizado...")

audio = whisperx.load_audio(AUDIO_PATH)

result = model.transcribe(
    audio,
    batch_size=16,
    language="pt",
)

print("\n📝 TRANSCRIÇÃO")
print("=" * 60)

for segment in result["segments"]:
    start = segment["start"]
    end = segment["end"]
    text = segment["text"].strip()

    print(f"[{start:.3f} -> {end:.3f}] {text}")

print("=" * 60)
print("✅ Transcrição concluída!")