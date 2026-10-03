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

def find_file(prefix, extension):
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)

    blobs = list(
        bucket.list_blobs(prefix=prefix)
    )

    files = [
        blob
        for blob in blobs
        if blob.name.lower().endswith(extension)
    ]

    if len(files) == 0:
        raise RuntimeError(
            f"Nenhum arquivo {extension} encontrado em {prefix}"
        )

    if len(files) > 1:
        raise RuntimeError(
            f"Esperado apenas um arquivo {extension} "
            f"em {prefix}, encontrados {len(files)}"
        )

    return files[0].name


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


VIDEO_BLOB = find_file(
    VIDEO_PREFIX,
    ".mp4"
)

EDIT_BLOB = find_file(
    EDIT_PREFIX,
    ".json"
)

VIDEO_URI = (
    f"gs://{BUCKET_NAME}/{VIDEO_BLOB}"
)

EDIT_URI = (
    f"gs://{BUCKET_NAME}/{EDIT_BLOB}"
)

filename = os.path.basename(
    VIDEO_BLOB
)

name_without_extension = os.path.splitext(
    filename
)[0]

OUTPUT_URI = (
    f"gs://{BUCKET_NAME}/"
    f"{OUTPUT_PREFIX}"
    f"{name_without_extension}.mp4"
)


print(
    "🚀 Render Worker",
    flush=True
)

print(
    f"🎬 Video: {VIDEO_URI}",
    flush=True
)

print(
    f"✂️ Edit: {EDIT_URI}",
    flush=True
)

print(
    f"📄 Output: {OUTPUT_URI}",
    flush=True
)


# =========================
# DOWNLOAD VIDEO
# =========================

download_from_gcs(
    VIDEO_URI,
    VIDEO_PATH
)

print(
    "✅ Vídeo baixado.",
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
# RENDER
# =========================

render_segments(
    VIDEO_PATH,
    segments,
    OUTPUT_PATH
)


print(
    "\n✅ Render concluído!",
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
    "\n🎉 Render Worker concluído!",
    flush=True
)

print(
    f"📄 Final: {OUTPUT_URI}",
    flush=True
)
