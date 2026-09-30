import os
import subprocess
import json
import gc

import numpy as np
import torch
from pydub import AudioSegment
import whisperx
from google.cloud import storage


# =========================
# CONFIGURAÇÃO
# =========================

BUCKET_NAME = "ai-video-editor-510018"

INPUT_PREFIX = "synced/"
OUTPUT_PREFIX = "transcripts/"

VIDEO_PATH = "/tmp/video_synced.mp4"
AUDIO_PATH = "/tmp/audio_synced.wav"
OUTPUT_PATH = "/tmp/transcription.json"

SILENCE_DB = -55
LONG_PAUSE = 0.5
MIN_SPEECH_DURATION = 0.5
PADDING = 0.10

DEVICE = "cuda"
COMPUTE_TYPE = "float16"
MODEL_SIZE = "large-v3"
LANGUAGE = "pt"
BATCH_SIZE = 16


# =========================
# GCS
# =========================

def download_from_gcs(gcs_uri, local_path):
    bucket_name, blob_name = (
        gcs_uri.replace("gs://", "", 1).split("/", 1)
    )

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    print(f"📥 Downloading {gcs_uri}...", flush=True)

    blob.download_to_filename(local_path)


def upload_to_gcs(local_path, gcs_uri):
    bucket_name, blob_name = (
        gcs_uri.replace("gs://", "", 1).split("/", 1)
    )

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    print(
        f"📤 Uploading {local_path} → {gcs_uri}...",
        flush=True
    )

    blob.upload_from_filename(
        local_path,
        content_type="application/json",
    )


def find_input_video():
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)

    blobs = list(
        bucket.list_blobs(prefix=INPUT_PREFIX)
    )

    videos = [
        blob
        for blob in blobs
        if blob.name.lower().endswith(".mp4")
    ]

    if len(videos) == 0:
        raise RuntimeError(
            "Nenhum vídeo encontrado em synced/"
        )

    if len(videos) > 1:
        raise RuntimeError(
            f"Esperado apenas um vídeo em synced/, "
            f"encontrados {len(videos)}"
        )

    return videos[0].name


# =========================
# EXTRAIR ÁUDIO
# =========================

def extract_audio(video_path, audio_path):
    print(
        "🎙️ Extraindo áudio do vídeo sincronizado...",
        flush=True
    )

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            audio_path,
        ],
        check=True,
    )


# =========================
# DETECTAR BLOCOS
# =========================

def detect_speech_segments(audio_file):
    print(
        "📌 Analisando áudio para detectar pausas...",
        flush=True
    )

    audio_file = (
        AudioSegment
        .from_file(audio_file)
        .set_channels(1)
    )

    sample_rate = audio_file.frame_rate
    duration = len(audio_file) / 1000

    samples = np.array(
        audio_file.get_array_of_samples(),
        dtype=np.float32
    )

    if audio_file.sample_width == 2:
        samples /= 32768.0

    elif audio_file.sample_width == 4:
        samples /= 2147483648.0

    window = int(sample_rate * 0.02)

    rms = np.sqrt(
        np.convolve(
            samples ** 2,
            np.ones(window) / window,
            mode="same"
        )
    )

    db = 20 * np.log10(
        np.maximum(rms, 1e-10)
    )

    is_silent = db < SILENCE_DB

    changes = np.diff(
        is_silent.astype(np.int8)
    )

    starts = np.where(changes == 1)[0]
    ends = np.where(changes == -1)[0]

    if is_silent[0]:
        starts = np.insert(starts, 0, 0)

    if is_silent[-1]:
        ends = np.append(
            ends,
            len(is_silent) - 1
        )

    long_pauses = []

    for start, end in zip(starts, ends):

        start_s = start / sample_rate
        end_s = end / sample_rate

        if end_s - start_s >= LONG_PAUSE:
            long_pauses.append(
                (start_s, end_s)
            )

    speech_segments = []
    cursor = 0.0

    for silence_start, silence_end in long_pauses:

        speech_end = silence_start - PADDING

        if speech_end - cursor >= MIN_SPEECH_DURATION:
            speech_segments.append(
                (cursor, speech_end)
            )

        cursor = silence_end + PADDING

    if duration - cursor >= MIN_SPEECH_DURATION:
        speech_segments.append(
            (cursor, duration)
        )

    speech_segments = [
        (start, end)
        for start, end in speech_segments
        if end - start >= MIN_SPEECH_DURATION
    ]

    print(
        f"\n🎙️ {len(speech_segments)} "
        f"blocos encontrados:\n",
        flush=True
    )

    for i, (start, end) in enumerate(
        speech_segments,
        1
    ):
        print(
            f"{i:03d}. "
            f"[{start:.3f} -> {end:.3f}] "
            f"({end - start:.3f}s)",
            flush=True
        )

    return speech_segments


# =========================
# TRANSCRIÇÃO
# =========================

def transcribe_segments(
    audio_file,
    speech_segments,
    model
):
    print(
        "\n📌 Carregando áudio para WhisperX...",
        flush=True
    )

    audio = whisperx.load_audio(audio_file)

    transcription = []

    print(
        "\n📌 Transcrevendo blocos...\n",
        flush=True
    )

    for i, (block_start, block_end) in enumerate(
        speech_segments,
        1
    ):
        print(
            f"🎙️ Bloco {i}/{len(speech_segments)} "
            f"[{block_start:.3f} -> {block_end:.3f}]",
            flush=True
        )

        block_audio = audio[
            int(block_start * 16000):
            int(block_end * 16000)
        ]

        result = model.transcribe(
            block_audio,
            batch_size=BATCH_SIZE,
            language=LANGUAGE
        )

        for segment in result["segments"]:

            text = segment["text"].strip()

            if not text:
                continue

            start = (
                block_start +
                segment["start"]
            )

            end = (
                block_start +
                segment["end"]
            )

            transcription.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text
            })

            print(
                f"   [{start:.3f} -> {end:.3f}] "
                f"{text}",
                flush=True
            )

        gc.collect()
        torch.cuda.empty_cache()

    return transcription


# =========================
# MAIN
# =========================

INPUT_BLOB = find_input_video()

INPUT_URI = (
    f"gs://{BUCKET_NAME}/{INPUT_BLOB}"
)

filename = os.path.basename(INPUT_BLOB)

name_without_extension = os.path.splitext(
    filename
)[0]

OUTPUT_URI = (
    f"gs://{BUCKET_NAME}/"
    f"{OUTPUT_PREFIX}"
    f"{name_without_extension}.json"
)


print(
    "🚀 Transcription worker",
    flush=True
)

print(
    f"📁 Input: {INPUT_URI}",
    flush=True
)

print(
    f"📄 Output: {OUTPUT_URI}",
    flush=True
)


# =========================
# DOWNLOAD
# =========================

download_from_gcs(
    INPUT_URI,
    VIDEO_PATH
)

print(
    "🎬 Vídeo sincronizado baixado.",
    flush=True
)


# =========================
# EXTRAIR ÁUDIO
# =========================

extract_audio(
    VIDEO_PATH,
    AUDIO_PATH
)

print(
    "✅ Áudio extraído.",
    flush=True
)


# =========================
# DETECTAR BLOCOS
# =========================

speech_segments = detect_speech_segments(
    AUDIO_PATH
)


# =========================
# WHISPERX
# =========================

print(
    "\n📌 Carregando WhisperX...",
    flush=True
)

model = whisperx.load_model(
    MODEL_SIZE,
    DEVICE,
    compute_type=COMPUTE_TYPE,
    language=LANGUAGE,
)

print(
    "✅ Modelo carregado!",
    flush=True
)


# =========================
# TRANSCRIBIR
# =========================

transcription = transcribe_segments(
    AUDIO_PATH,
    speech_segments,
    model
)


# =========================
# GERAR JSON
# =========================

output = {
    "video": os.path.basename(INPUT_URI),
    "language": LANGUAGE,
    "segments": transcription,
}

with open(
    OUTPUT_PATH,
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        output,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"\n💾 JSON gerado: {OUTPUT_PATH}",
    flush=True
)


# =========================
# UPLOAD
# =========================

upload_to_gcs(
    OUTPUT_PATH,
    OUTPUT_URI
)

print(
    "\n✅ Transcrição concluída!",
    flush=True
)

print(
    f"📝 {len(transcription)} segmentos",
    flush=True
)
