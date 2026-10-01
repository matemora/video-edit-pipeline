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

    temp_dir = tempfile.mkdtemp(
        prefix="render-"
    )

    segment_files = []

    try:
        for index, segment in enumerate(
            segments,
            1
        ):
            start = segment["start"]
            end = segment["end"]

            duration = end - start

            segment_path = os.path.join(
                temp_dir,
                f"segment_{index:04d}.mp4"
            )

            print(
                f"✂️ Segmento {index}/{len(segments)} "
                f"[{start:.3f} -> {end:.3f}] "
                f"({duration:.3f}s)",
                flush=True
            )

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(start),
                    "-i",
                    video_path,
                    "-t",
                    str(duration),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "19",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    segment_path,
                ],
                check=True,
            )

            segment_files.append(
                segment_path
            )

        concat_file = os.path.join(
            temp_dir,
            "concat.txt"
        )

        with open(
            concat_file,
            "w",
            encoding="utf-8"
        ) as f:
            for segment_file in segment_files:
                f.write(
                    f"file '{segment_file}'\n"
                )

        print(
            "\n🔗 Concatenando segmentos...",
            flush=True
        )

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                output_path,
            ],
            check=True,
        )

    finally:
        for segment_file in segment_files:
            if os.path.exists(segment_file):
                os.remove(segment_file)

        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


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
