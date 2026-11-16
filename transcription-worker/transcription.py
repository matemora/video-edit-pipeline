import os
import json
import gc
import subprocess

import numpy as np
import torch
from pydub import AudioSegment
import whisperx
from google.cloud import storage

# =========================
# CONFIGURAÇÃO
# =========================

BUCKET_NAME = "ai-video-editor-510018"

PROJECT_NAME = os.environ.get("PROJECT_NAME")

if not PROJECT_NAME:
    raise ValueError("A variável PROJECT_NAME é obrigatória.")

INPUT_PREFIX = f"synced/{PROJECT_NAME}/"
OUTPUT_PREFIX = f"transcripts/{PROJECT_NAME}/"

VIDEO_PATH = "/tmp/video_synced.mp4"
AUDIO_PATH = "/tmp/audio_synced.wav"
OUTPUT_PATH = "/tmp/transcription.json"


# =========================
# DETECÇÃO DE SILÊNCIO
# =========================

LONG_PAUSE = 0.5
MIN_SPEECH_DURATION = 0.5
PADDING = 0.10

# Threshold dinâmico
NOISE_PERCENTILE = 20
MARGIN_DB = 7


# =========================
# WHISPERX
# =========================

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

    print(
        f"📥 Downloading {gcs_uri}...",
        flush=True
    )

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


# =========================
# ENTRADA E SAÍDA
# =========================

INPUT_BLOB = f"{INPUT_PREFIX}video_synced.mp4"

INPUT_URI = f"gs://{BUCKET_NAME}/{INPUT_BLOB}"

OUTPUT_URI = (
    f"gs://{BUCKET_NAME}/"
    f"{OUTPUT_PREFIX}"
    "transcription.json"
)

print("🚀 Transcription worker", flush=True)
print(f"📁 Input: {INPUT_URI}", flush=True)
print(f"📄 Output: {OUTPUT_URI}", flush=True)

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
        "\n📌 Analisando perfil do áudio...",
        flush=True
    )

    audio = (
        AudioSegment
        .from_file(audio_file)
        .set_channels(1)
    )

    sample_rate = audio.frame_rate

    duration = len(audio) / 1000

    samples = np.array(
        audio.get_array_of_samples(),
        dtype=np.float32
    )

    if audio.sample_width == 2:
        samples /= 32768.0

    elif audio.sample_width == 4:
        samples /= 2147483648.0

    # =========================
    # RMS
    # =========================

    window = int(
        sample_rate * 0.02
    )

    rms = np.sqrt(
        np.convolve(
            samples ** 2,
            np.ones(window) / window,
            mode="same"
        )
    )

    db = 20 * np.log10(
        np.maximum(
            rms,
            1e-10
        )
    )

    # =========================
    # NOISE FLOOR
    # =========================

    noise_floor = np.percentile(
        db,
        NOISE_PERCENTILE
    )

    silence_db = (
        noise_floor +
        MARGIN_DB
    )

    print("\n" + "=" * 60)
    print("🎚️ PERFIL DO ÁUDIO")
    print("=" * 60)

    print(
        f"Duração:        {duration:.2f}s"
    )

    print(
        f"Noise floor:    {noise_floor:.2f} dB"
    )

    print(
        f"Margem:         {MARGIN_DB:.2f} dB"
    )

    print(
        f"Auto threshold: {silence_db:.2f} dB"
    )

    print(
        f"Long pause:     {LONG_PAUSE:.2f}s"
    )

    print(
        f"Min speech:     {MIN_SPEECH_DURATION:.2f}s"
    )

    print(
        f"Padding:        {PADDING:.2f}s"
    )

    print("=" * 60)

    # =========================
    # DETECTAR SILÊNCIO
    # =========================

    is_silent = (
        db < silence_db
    )

    changes = np.diff(
        is_silent.astype(np.int8)
    )

    starts = np.where(
        changes == 1
    )[0]

    ends = np.where(
        changes == -1
    )[0]

    if is_silent[0]:
        starts = np.insert(
            starts,
            0,
            0
        )

    if is_silent[-1]:
        ends = np.append(
            ends,
            len(is_silent) - 1
        )

    # =========================
    # PAUSAS LONGAS
    # =========================

    long_pauses = []

    for start, end in zip(
        starts,
        ends
    ):
        start_s = (
            start /
            sample_rate
        )

        end_s = (
            end /
            sample_rate
        )

        pause_duration = (
            end_s -
            start_s
        )

        if pause_duration >= LONG_PAUSE:
            long_pauses.append(
                (
                    start_s,
                    end_s
                )
            )

    # =========================
    # BLOCOS DE FALA
    # =========================

    speech_segments = []

    cursor = 0.0

    for silence_start, silence_end in long_pauses:

        speech_end = (
            silence_start -
            PADDING
        )

        if (
            speech_end - cursor
            >= MIN_SPEECH_DURATION
        ):
            speech_segments.append(
                (
                    cursor,
                    speech_end
                )
            )

        cursor = (
            silence_end +
            PADDING
        )

    # Último bloco

    if (
        duration - cursor
        >= MIN_SPEECH_DURATION
    ):
        speech_segments.append(
            (
                cursor,
                duration
            )
        )

    # =========================
    # SEGURANÇA
    # =========================

    speech_segments = [
        (
            start,
            end
        )
        for start, end in speech_segments
        if (
            end - start
            >= MIN_SPEECH_DURATION
        )
    ]

    # =========================
    # RESULTADO
    # =========================

    print(
        f"\n🎙️ {len(speech_segments)} "
        f"blocos encontrados:\n",
        flush=True
    )

    for i, (
        start,
        end
    ) in enumerate(
        speech_segments,
        1
    ):
        print(
            f"{i:03d}. "
            f"[{start:.3f} -> "
            f"{end:.3f}] "
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
    model,
    align_model,
    align_metadata
):
    print(
        "\n📌 Carregando áudio para WhisperX...",
        flush=True
    )

    audio = whisperx.load_audio(
        audio_file
    )

    transcription = []

    print(
        "\n📌 Transcrevendo blocos...\n",
        flush=True
    )

    for i, (
        block_start,
        block_end
    ) in enumerate(
        speech_segments,
        1
    ):
        print(
            f"🎙️ Bloco "
            f"{i}/{len(speech_segments)} "
            f"[{block_start:.3f} -> "
            f"{block_end:.3f}]",
            flush=True
        )

        block_audio = audio[
            int(block_start * 16000):
            int(block_end * 16000)
        ]

        # =========================
        # TRANSCRIÇÃO
        # =========================

        result = model.transcribe(
            block_audio,
            batch_size=BATCH_SIZE,
            language=LANGUAGE
        )

        # =========================
        # ALINHAMENTO POR PALAVRA
        # =========================

        aligned = whisperx.align(
            result["segments"],
            align_model,
            align_metadata,
            block_audio,
            DEVICE,
            return_char_alignments=False
        )

        # =========================
        # SEGMENTOS
        # =========================

        for segment in aligned["segments"]:

            text = (
                segment["text"]
                .strip()
            )

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

            # =========================
            # PALAVRAS
            # =========================

            words = []

            for word in segment.get(
                "words",
                []
            ):
                if (
                    "start" not in word
                    or "end" not in word
                ):
                    continue

                word_start = (
                    block_start +
                    word["start"]
                )

                word_end = (
                    block_start +
                    word["end"]
                )

                words.append({
                    "word": word["word"],
                    "start": round(
                        word_start,
                        3
                    ),
                    "end": round(
                        word_end,
                        3
                    )
                })

            transcription.append({
                "start": round(
                    start,
                    3
                ),
                "end": round(
                    end,
                    3
                ),
                "text": text,
                "words": words
            })

            print(
                f"   [{start:.3f} -> "
                f"{end:.3f}] "
                f"{text}",
                flush=True
            )

            if words:
                print(
                    f"      "
                    f"{len(words)} palavras "
                    f"alinhadas",
                    flush=True
                )

        gc.collect()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    return transcription


# =========================
# MAIN
# =========================

client = storage.Client()
bucket = client.bucket(BUCKET_NAME)
available_videos = sorted(
    blob.name for blob in bucket.list_blobs(prefix=INPUT_PREFIX)
    if blob.name.lower().endswith(".mp4")
    and os.path.basename(blob.name) != "screen_synced.mp4"
)
take_videos = [
    name for name in available_videos
    if os.path.basename(name).startswith("take_")
]
video_blobs = take_videos or [
    name for name in available_videos
    if os.path.basename(name) == "video_synced.mp4"
]
if not video_blobs:
    raise RuntimeError(f"Nenhum vídeo sincronizado encontrado em {INPUT_PREFIX}")

print("🚀 Transcription worker", flush=True)
print(f"🎬 Takes encontrados: {len(video_blobs)}", flush=True)

model = whisperx.load_model(
    MODEL_SIZE, DEVICE, compute_type=COMPUTE_TYPE, language=LANGUAGE
)
align_model, align_metadata = whisperx.load_align_model(
    language_code=LANGUAGE, device=DEVICE
)

all_segments = []
sources = []
for video_blob in video_blobs:
    source_name = os.path.basename(video_blob)
    print(f"Processando {source_name}", flush=True)
    download_from_gcs(f"gs://{BUCKET_NAME}/{video_blob}", VIDEO_PATH)
    extract_audio(VIDEO_PATH, AUDIO_PATH)
    speech_segments = detect_speech_segments(AUDIO_PATH)
    transcription = transcribe_segments(
        AUDIO_PATH, speech_segments, model, align_model, align_metadata
    )
    for segment in transcription:
        segment["source"] = source_name
        all_segments.append(segment)
    sources.append(source_name)
    print(f"{source_name}: {len(transcription)} segmentos", flush=True)

output = {
    "project": PROJECT_NAME,
    "language": LANGUAGE,
    "sources": sources,
    "segments": all_segments,
}
with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

upload_to_gcs(
    OUTPUT_PATH,
    f"gs://{BUCKET_NAME}/{OUTPUT_PREFIX}transcription.json"
)
print(f"Transcrição concluída: {len(all_segments)} segmentos em {len(sources)} take(s).", flush=True)
