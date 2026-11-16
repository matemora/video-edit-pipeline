import os
import json
import subprocess
from google.cloud import storage

BUCKET_NAME = "ai-video-editor-510018"
PROJECT_NAME = os.environ.get("PROJECT_NAME")
if not PROJECT_NAME:
    raise ValueError("A variável PROJECT_NAME é obrigatória.")

VIDEO_PREFIX = f"synced/{PROJECT_NAME}/"
EDIT_BLOB = f"edits/{PROJECT_NAME}/edit.json"
OUTPUT_BLOB = f"final/{PROJECT_NAME}/final.mp4"
SCREEN_BLOB = f"synced/{PROJECT_NAME}/screen_synced.mp4"
SCREEN_ONLY = os.environ.get("SCREEN_ONLY", "false").lower() == "true"

def download_blob(bucket, blob_name, path):
    blob = bucket.blob(blob_name)
    if not blob.exists():
        raise RuntimeError(f"Objeto não encontrado: gs://{BUCKET_NAME}/{blob_name}")
    blob.download_to_filename(path)

def upload_blob(bucket, path, blob_name):
    bucket.blob(blob_name).upload_from_filename(
        path, content_type="video/mp4", timeout=3600
    )

def render_segments(source_paths, segments, output_path):
    if not segments:
        raise RuntimeError("Nenhum segmento encontrado no edit.json.")

    source_names = list(source_paths)
    source_indexes = {name: index for index, name in enumerate(source_names)}
    command = ["ffmpeg", "-y"]
    for name in source_names:
        command.extend(["-i", source_paths[name]])

    filters = []
    concat_inputs = []
    audio_by_source = {}
    for source, path in source_paths.items():
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True,
        )
        audio_by_source[source] = bool(probe.stdout.strip())

    for index, segment in enumerate(segments):
        source = segment.get("source") or source_names[0]
        if source not in source_indexes:
            raise RuntimeError(f"Source '{source}' não está entre os vídeos disponíveis.")
        input_index = source_indexes[source]
        start, end = float(segment["start"]), float(segment["end"])
        if start < 0 or end <= start:
            raise RuntimeError(f"Intervalo inválido para {source}: {start} -> {end}")

        filters.append(
            f"[{input_index}:v]trim=start={start}:end={end},"
            f"fps=30,format=yuv420p,setpts=PTS-STARTPTS[v{index}]"
        )
        if audio_by_source[source]:
            filters.append(
                f"[{input_index}:a]atrim=start={start}:end={end},"
                f"aresample=44100,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            filters.append(
                f"anullsrc=channel_layout=stereo:sample_rate=44100,"
                f"atrim=duration={end-start},aresample=44100,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{index}]"
            )
        concat_inputs.append(f"[v{index}][a{index}]")

    filters.append(
        "".join(concat_inputs)
        + f"concat=n={len(segments)}:v=1:a=1[outv][outa]"
    )
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-c:a", "aac", "-movflags", "+faststart", output_path,
    ])
    subprocess.run(command, check=True)

def main():
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)
    edit_path = "/tmp/edit.json"
    download_blob(bucket, EDIT_BLOB, edit_path)
    with open(edit_path, "r", encoding="utf-8") as file:
        edit = json.load(file)
    segments = edit.get("segments", [])
    if not segments:
        raise RuntimeError("Nenhum segmento encontrado no edit.json.")

    if SCREEN_ONLY:
        screen_path = "/tmp/screen.mp4"
        download_blob(bucket, SCREEN_BLOB, screen_path)
        screen_segments = [
            {**segment, "source": "screen_synced.mp4"}
            for segment in segments
        ]
        render_segments(
            {"screen_synced.mp4": screen_path},
            screen_segments,
            "/tmp/screen_final.mp4",
        )
        upload_blob(bucket, "/tmp/screen_final.mp4",
                    f"final/{PROJECT_NAME}/screen_final.mp4")
        print("Screen renderizado.", flush=True)
        return

    source_names = sorted({
        segment.get("source") or "video_synced.mp4"
        for segment in segments
    })
    source_paths = {}
    for index, source_name in enumerate(source_names):
        local_path = f"/tmp/source_{index}.mp4"
        download_blob(bucket, f"{VIDEO_PREFIX}{source_name}", local_path)
        source_paths[source_name] = local_path

    output_path = "/tmp/final.mp4"
    render_segments(source_paths, segments, output_path)
    upload_blob(bucket, output_path, OUTPUT_BLOB)
    print(f"Vídeo final publicado em gs://{BUCKET_NAME}/{OUTPUT_BLOB}", flush=True)

    # Mantém a renderização opcional do screen para projetos legados de take único.
    screen_blob = bucket.blob(SCREEN_BLOB)
    if screen_blob.exists():
        screen_path = "/tmp/screen.mp4"
        download_blob(bucket, SCREEN_BLOB, screen_path)
        screen_segments = [
            {**segment, "source": "screen_synced.mp4"}
            for segment in segments
        ]
        render_segments(
            {"screen_synced.mp4": screen_path},
            screen_segments,
            "/tmp/screen_final.mp4",
        )
        upload_blob(
            bucket, "/tmp/screen_final.mp4",
            f"final/{PROJECT_NAME}/screen_final.mp4"
        )
        print("Screen renderizado.", flush=True)

if __name__ == "__main__":
    main()
