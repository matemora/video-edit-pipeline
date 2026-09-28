import os
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal
from google.cloud import storage


VIDEO_INPUT = os.environ["VIDEO_INPUT"]
AUDIO_INPUT = os.environ["AUDIO_INPUT"]
OUTPUT = os.environ["OUTPUT"]

SYNC_ANALYSIS_SECONDS = 90
SYNC_SAMPLE_RATE = 22050

WORKDIR = Path("/tmp/work")
WORKDIR.mkdir(exist_ok=True)

video_path = WORKDIR / "video_input"
audio_path = WORKDIR / "audio_input"
cam_ref = WORKDIR / "cam_ref.wav"
mic_ref = WORKDIR / "mic_ref.wav"
aligned_audio = WORKDIR / "aligned_audio.wav"
output_path = WORKDIR / "output.mp4"


def run(cmd):
    print(">", " ".join(map(str, cmd)))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        print(result.stderr[-5000:])
        raise RuntimeError("FFmpeg falhou.")


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


def get_audio_offset(v_file, a_file, sr=22050):
    print("\n🔎 Extraindo áudio para análise...")

    run([
        "ffmpeg", "-y",
        "-i", str(v_file),
        "-t", str(SYNC_ANALYSIS_SECONDS),
        "-vn",
        "-ac", "1",
        "-ar", str(sr),
        str(cam_ref)
    ])

    run([
        "ffmpeg", "-y",
        "-i", str(a_file),
        "-t", str(SYNC_ANALYSIS_SECONDS),
        "-ac", "1",
        "-ar", str(sr),
        str(mic_ref)
    ])

    y1, _ = sf.read(cam_ref, dtype="float32")
    y2, _ = sf.read(mic_ref, dtype="float32")

    if y1.ndim > 1:
        y1 = y1.mean(axis=1)

    if y2.ndim > 1:
        y2 = y2.mean(axis=1)

    print("📊 Calculando correlação...")

    correlation = signal.correlate(
        y1,
        y2,
        mode="full",
        method="fft"
    )

    lags = signal.correlation_lags(
        len(y1),
        len(y2),
        mode="full"
    )

    offset = lags[np.argmax(correlation)] / sr

    return offset


print("🚀 Starting sync worker")

print("\n📥 Baixando arquivos...")
download_gcs(VIDEO_INPUT, video_path)
download_gcs(AUDIO_INPUT, audio_path)

print(f"🎥 Video size: {video_path.stat().st_size / 1024 / 1024:.1f} MB")
print(f"🎙️ Audio size: {audio_path.stat().st_size / 1024 / 1024:.1f} MB")

print("\n🔎 Procurando sincronização...")

offset = get_audio_offset(
    video_path,
    audio_path
)

print(f"\n⏱️ Offset encontrado: {offset:.3f}s")


print("\n🎙️ Alinhando áudio original...")

if offset > 0:
    delay_ms = int(round(offset * 1000))

    run([
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-af", f"adelay={delay_ms}:all=1",
        "-ar", "48000",
        "-ac", "2",
        str(aligned_audio)
    ])

else:
    run([
        "ffmpeg", "-y",
        "-ss", str(abs(offset)),
        "-i", str(audio_path),
        "-ar", "48000",
        "-ac", "2",
        str(aligned_audio)
    ])


print("\n🎬 Criando vídeo sincronizado...")

run([
    "ffmpeg", "-y",
    "-i", str(video_path),
    "-i", str(aligned_audio),
    "-map", "0:v:0",
    "-map", "1:a:0",
    "-c:v", "copy",
    "-c:a", "aac",
    "-b:a", "192k",
    "-shortest",
    "-movflags", "+faststart",
    str(output_path)
])


print("\n📤 Enviando resultado...")

upload_gcs(
    output_path,
    OUTPUT
)

print("\n✅ Sync worker completed successfully")
print(f"📁 Output: {OUTPUT}")