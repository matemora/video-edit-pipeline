
import os
import re
import tempfile
from collections import defaultdict

import google.auth
from google.auth import impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.cloud import storage, run_v2
from google.cloud.run_v2.types import EnvVar


PROJECT_ID = "ai-video-editor-510018"
BUCKET_NAME = "ai-video-editor-510018"

INPUT_FOLDER_ID = "1LEDh1MFFUyZtOcF9Z114JEeSy4p1d80S"
OUTPUT_FOLDER_ID = "126-5DwbvfSUMP90pU4noitzH93WG3Za7"

SERVICE_ACCOUNT = (
    "video-editor-orchestrator@"
    "ai-video-editor-510018.iam.gserviceaccount.com"
)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

SYNC_REGION = "southamerica-east1"
TRANSCRIPTION_REGION = "europe-west1"
EDITOR_REGION = "southamerica-east1"
RENDER_REGION = "southamerica-east1"


# =========================
# AUTENTICAÇÃO
# =========================

def get_credentials():
    credentials, _ = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/cloud-platform",
        ]
    )
    return credentials


# =========================
# GOOGLE DRIVE
# =========================

def list_drive_files(drive, folder_id):
    files = []
    page_token = None

    while True:
        result = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields=(
                "nextPageToken,"
                "files(id,name,mimeType,size,md5Checksum)"
            ),
            pageSize=100,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        files.extend(result.get("files", []))
        page_token = result.get("nextPageToken")

        if not page_token:
            return files


def group_packages(files):
    packages = defaultdict(dict)

    for file in files:
        name = file["name"]

        match = re.fullmatch(r"(.+)\.(mov|m4a)", name, re.I)
        if match:
            basename, extension = match.groups()
            packages[basename][extension.lower()] = file
            continue

        match = re.fullmatch(r"(.+)_screen\.mp4", name, re.I)
        if match:
            packages[match.group(1)]["screen"] = file

    return packages


def download_drive_file(drive, file_id, destination):
    request = drive.files().get_media(fileId=file_id)

    with open(destination, "wb") as output:
        downloader = MediaIoBaseDownload(output, request)
        done = False

        while not done:
            _, done = downloader.next_chunk()


def upload_inputs(drive, storage_client, basename, package):
    bucket = storage_client.bucket(BUCKET_NAME)
    prefix = f"input/{basename}/"

    with tempfile.TemporaryDirectory() as temp_dir:
        for extension in ("mov", "m4a", "screen"):
            file = package.get(extension)

            if not file:
                continue

            filename = file["name"]
            destination = prefix + filename

            if object_exists(storage_client, destination):
                print(
                    f"⏭️ Entrada já existe; pulando upload: {destination}",
                    flush=True,
                )
                continue

            local_path = os.path.join(temp_dir, filename)

            print(f"Baixando do Drive: {filename}", flush=True)
            download_drive_file(drive, file["id"], local_path)

            print(
                f"Enviando para gs://{BUCKET_NAME}/{destination}",
                flush=True,
            )

            bucket.blob(destination).upload_from_filename(
                local_path,
                timeout=3600,
            )

    print(f"Entradas de {basename} verificadas.", flush=True)

# =========================
# GCS
# =========================

def object_exists(storage_client, object_name):
    return storage_client.bucket(BUCKET_NAME).blob(
        object_name
    ).exists()


def download_gcs_file(storage_client, object_name, destination):
    storage_client.bucket(BUCKET_NAME).blob(
        object_name
    ).download_to_filename(destination)


# =========================
# CLOUD RUN JOBS
# =========================

def run_job(
    credentials,
    job_name,
    region,
    env_values,
    timeout=3600,
):
    client = run_v2.JobsClient(credentials=credentials)

    env_vars = [
        EnvVar(name=name, value=value)
        for name, value in env_values.items()
    ]

    request = run_v2.RunJobRequest(
        name=(
            f"projects/{PROJECT_ID}/locations/"
            f"{region}/jobs/{job_name}"
        ),
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=env_vars,
                )
            ]
        ),
    )

    print(
        f"\nIniciando {job_name} na região {region}...",
        flush=True,
    )

    operation = client.run_job(request=request)

    # Aguarda o término da execução, não apenas sua criação.
    execution = operation.result(timeout=timeout)

    print(
        f"{job_name} terminou: {execution.name}",
        flush=True,
    )


def run_pipeline(credentials, storage_client, basename):
    prefix = f"gs://{BUCKET_NAME}"

    # 1. SYNC
    synced_object = f"synced/{basename}/video_synced.mp4"

    if object_exists(storage_client, synced_object):
        print("⏭️ Sync já concluído; pulando.", flush=True)
    else:
        run_job(
            credentials,
            "sync-worker",
            SYNC_REGION,
            {
                "VIDEO_INPUT": f"{prefix}/input/{basename}/{basename}.mov",
                "AUDIO_INPUT": f"{prefix}/input/{basename}/{basename}.m4a",
                "OUTPUT": f"{prefix}/{synced_object}",
            },
        )

    # 2. TRANSCRIÇÃO
    transcript_object = (
        f"transcripts/{basename}/video_synced.json"
    )

    if object_exists(storage_client, transcript_object):
        print("⏭️ Transcrição já concluída; pulando.", flush=True)
    else:
        run_job(
            credentials,
            "transcription-worker",
            TRANSCRIPTION_REGION,
            {"PROJECT_NAME": basename},
        )

    # 3. EDIÇÃO POR IA
    edit_object = f"edits/{basename}/edit.json"

    if object_exists(storage_client, edit_object):
        print("⏭️ Edição por IA já concluída; pulando.", flush=True)
    else:
        run_job(
            credentials,
            "ai-editor-worker",
            EDITOR_REGION,
            {"PROJECT_NAME": basename},
        )

    # 4. RENDERIZAÇÃO
    final_object = f"final/{basename}/final.mp4"

    if object_exists(storage_client, final_object):
        print("⏭️ Renderização já concluída; pulando.", flush=True)
    else:
        run_job(
            credentials,
            "render-worker",
            RENDER_REGION,
            {"PROJECT_NAME": basename},
        )

# =========================
# PUBLICAR NO DRIVE
# =========================

def publish_final(drive, storage_client, basename, replace_existing):
    output_name = f"{basename}_final.mp4"
    final_object = f"final/{basename}/final.mp4"

    if not object_exists(storage_client, final_object):
        raise RuntimeError(
            f"Vídeo final não encontrado: gs://{BUCKET_NAME}/{final_object}"
        )

    output_files = list_drive_files(drive, OUTPUT_FOLDER_ID)

    existing = next(
        (
            file for file in output_files
            if file["name"] == output_name
        ),
        None,
    )

    if existing and not replace_existing:
        print(
            f"{output_name} já existe no Drive. "
            "Nenhum novo arquivo será criado.",
            flush=True,
        )
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = os.path.join(temp_dir, output_name)

        print(
            f"Baixando vídeo final do GCS: {final_object}",
            flush=True,
        )

        download_gcs_file(
            storage_client,
            final_object,
            local_path,
        )

        media = MediaFileUpload(
            local_path,
            mimetype="video/mp4",
            resumable=True,
        )

        if existing:
            result = drive.files().update(
                fileId=existing["id"],
                media_body=media,
                supportsAllDrives=True,
                fields="id,name",
            ).execute()

            print(
                f"Vídeo atualizado no Drive: {result['name']}",
                flush=True,
            )
        else:
            result = drive.files().create(
                body={
                    "name": output_name,
                    "parents": [OUTPUT_FOLDER_ID],
                },
                media_body=media,
                supportsAllDrives=True,
                fields="id,name",
            ).execute()

            print(
                f"Vídeo publicado no Drive: {result['name']}",
                flush=True,
            )


# =========================
# PROCESSAR PACOTE
# =========================

def process_package(
    credentials,
    drive,
    storage_client,
    basename,
    package,
):
    if "mov" not in package or "m4a" not in package:
        print(
            f"SKIP {basename}: falta .mov ou .m4a",
            flush=True,
        )
        return

    final_object = f"final/{basename}/final.mp4"

    print(f"\n{'=' * 60}", flush=True)
    print(f"Projeto: {basename}", flush=True)
    print(f"{'=' * 60}", flush=True)

    # Um resultado final existente não é processado novamente.
    if object_exists(storage_client, final_object):
        print(
            "Vídeo final já existe no GCS; "
            "verificando publicação no Drive.",
            flush=True,
        )

        return

    # Se ainda não há resultado final, executa o pipeline completo.
    upload_inputs(
        drive,
        storage_client,
        basename,
        package,
    )

    run_pipeline(credentials, storage_client, basename)

    print(
        f"\nProjeto {basename} concluído!",
        flush=True,
    )


# =========================
# MAIN
# =========================

def main():
    credentials = get_credentials()

    drive = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    storage_client = storage.Client(
        project=PROJECT_ID,
        credentials=credentials,
    )

    files = list_drive_files(drive, INPUT_FOLDER_ID)

    print(
        f"Arquivos encontrados no INPUT: {len(files)}",
        flush=True,
    )

    packages = group_packages(files)

    if not packages:
        print("Nenhum pacote encontrado.", flush=True)
        return

    for basename, package in packages.items():
        try:
            process_package(
                credentials,
                drive,
                storage_client,
                basename,
                package,
            )
        except Exception as error:
            print(
                f"\nERRO no projeto {basename}: {error}",
                flush=True,
            )
            raise


if __name__ == "__main__":
    main()
