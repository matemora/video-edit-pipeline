import os
import json
import subprocess
import tempfile
import shutil
from google.cloud import storage


# =========================
# CONFIGURAÇÃO
# =========================

BUCKET_NAME = "ai-video-editor-510018"

VIDEO_PREFIX = "synced/"
EDIT_PREFIX = "edits/"
OUTPUT_PREFIX = "final/"

VIDEO_PATH = "/tmp/video.mp4"
EDIT_PATH = "/tmp/edit.json"
OUTPUT_PATH = "/tmp/final.mp4"


# =========================
# GCS
# =========================

def find_files(prefix, extension):
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)

    blobs = list(
        bucket.list_blobs(prefix=prefix)
    )

    files = [
        blob.name
        for blob in blobs
        if blob.name.lower().endswith(extension)
    ]

    if len(files) == 0:
        raise RuntimeError(
            f"Nenhum arquivo {extension} encontrado em {prefix}"
        )

    return files

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
        content_type="video/mp4",
        timeout=3600,
    )

# =========================
# FFMPEG
# =========================


def render_segments(video_path, segments, output_path):
    print(
        f"\n🎬 Renderizando {len(segments)} segmentos...",
        flush=True
    )

    # =========================
    # Detectar se existe áudio
    # =========================

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    has_audio = bool(probe.stdout.strip())

    print(
        f"🔊 Áudio detectado: {'sim' if has_audio else 'não'}",
        flush=True
    )

    # =========================
    # Construir filter graph
    # =========================

    filter_parts = []
    concat_inputs = []

    for index, segment in enumerate(segments):
        start = segment["start"]
        end = segment["end"]

        print(
            f"✂️ Segmento {index + 1}/{len(segments)} "
            f"[{start:.3f} -> {end:.3f}] "
            f"({end - start:.3f}s)",
            flush=True
        )

        # Vídeo
        filter_parts.append(
            f"[0:v]trim=start={start}:end={end},"
            f"setpts=PTS-STARTPTS[v{index}];"
        )

        if has_audio:
            # Áudio
            filter_parts.append(
                f"[0:a]atrim=start={start}:end={end},"
                f"asetpts=PTS-STARTPTS[a{index}];"
            )

            concat_inputs.append(
                f"[v{index}][a{index}]"
            )

        else:
            concat_inputs.append(
                f"[v{index}]"
            )

    # =========================
    # Concat
    # =========================

    if has_audio:
        concat_filter = (
            "".join(concat_inputs)
            + f"concat=n={len(segments)}:v=1:a=1[outv][outa]"
        )
    else:
        concat_filter = (
            "".join(concat_inputs)
            + f"concat=n={len(segments)}:v=1:a=0[outv]"
        )

    filter_complex = (
        "".join(filter_parts)
        + concat_filter
    )

    # =========================
    # FFmpeg
    # =========================

    print(
        "\n🎬 Executando FFmpeg...",
        flush=True
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "19",
        "-movflags",
        "+faststart",
    ]

    if has_audio:
        command.extend([
            "-map",
            "[outa]",
            "-c:a",
            "aac",
        ])

    command.append(output_path)

    subprocess.run(
        command,
        check=True,
    )

# =========================
# MAIN
# =========================

VIDEO_BLOBS = find_files(
    VIDEO_PREFIX,
    ".mp4"
)

EDIT_BLOBS = find_files(
    EDIT_PREFIX,
    ".json"
)

if len(EDIT_BLOBS) != 1:
    raise RuntimeError(
        f"Esperado exatamente um edit JSON em "
        f"{EDIT_PREFIX}, encontrados {len(EDIT_BLOBS)}"
    )

EDIT_BLOB = EDIT_BLOBS[0]

EDIT_URI = (
    f"gs://{BUCKET_NAME}/{EDIT_BLOB}"
)


print(
    "🚀 Render Worker",
    flush=True
)

print(
    f"🎬 Vídeos encontrados: {len(VIDEO_BLOBS)}",
    flush=True
)

print(
    f"✂️ Edit: {EDIT_URI}",
    flush=True
)


# =========================
# DOWNLOAD EDIT
# =========================

download_from_gcs(
    EDIT_URI,
    EDIT_PATH
)

print(
    "✅ Edit JSON baixado.",
    flush=True
)


# =========================
# LOAD EDIT
# =========================

with open(
    EDIT_PATH,
    "r",
    encoding="utf-8"
) as f:
    edit = json.load(f)

segments = edit.get(
    "segments",
    []
)

if not segments:
    raise RuntimeError(
        "Nenhum segmento encontrado no edit.json"
    )

print(
    f"✂️ Segmentos recebidos: {len(segments)}",
    flush=True
)


# =========================
# RENDER CADA VÍDEO
# =========================

for index, video_blob in enumerate(
    VIDEO_BLOBS,
    1
):
    video_uri = (
        f"gs://{BUCKET_NAME}/{video_blob}"
    )

    filename = os.path.basename(
        video_blob
    )

    name_without_extension = (
        os.path.splitext(filename)[0]
    )

    output_path = (
        f"/tmp/{name_without_extension}_final.mp4"
    )

    output_uri = (
        f"gs://{BUCKET_NAME}/"
        f"{OUTPUT_PREFIX}"
        f"{name_without_extension}.mp4"
    )

    print(
        "\n" + "=" * 60,
        flush=True
    )

    print(
        f"🎬 Vídeo {index}/{len(VIDEO_BLOBS)}",
        flush=True
    )

    print(
        f"📥 Input: {video_uri}",
        flush=True
    )

    print(
        f"📤 Output: {output_uri}",
        flush=True
    )

    print(
        "=" * 60,
        flush=True
    )

    # =========================
    # DOWNLOAD VIDEO
    # =========================

    download_from_gcs(
        video_uri,
        VIDEO_PATH
    )

    print(
        "✅ Vídeo baixado.",
        flush=True
    )

    # =========================
    # RENDER
    # =========================

    render_segments(
        VIDEO_PATH,
        segments,
        output_path
    )

    print(
        "✅ Render concluído!",
        flush=True
    )

    # =========================
    # UPLOAD
    # =========================

    upload_to_gcs(
        output_path,
        output_uri
    )

    print(
        f"🎉 Vídeo enviado: {output_uri}",
        flush=True
    )

    # Limpar arquivo anterior antes do próximo vídeo
    if os.path.exists(VIDEO_PATH):
        os.remove(VIDEO_PATH)

    if os.path.exists(output_path):
        os.remove(output_path)


print(
    "\n🎉 Render Worker concluído!",
    flush=True
)