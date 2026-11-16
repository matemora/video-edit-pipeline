
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
    """Agrupa arquivos sem sufixo como projeto legado e arquivos _N como takes."""
    packages = defaultdict(lambda: {"takes": [], "screen": None})

    for file in files:
        name = file["name"]

        screen_match = re.fullmatch(r"(.+)_screen\.mp4", name, re.I)
        if screen_match:
            packages[screen_match.group(1)]["screen"] = file
            continue

        take_match = re.fullmatch(r"(.+?)_(\d+)\.(mov|mp4|m4a)", name, re.I)
        if take_match:
            basename, number, extension = take_match.groups()
            package = packages[basename]
            number = int(number)
            take = next((item for item in package["takes"] if item["number"] == number), None)
            if take is None:
                take = {"number": number, "video": None, "audio": None}
                package["takes"].append(take)
            key = "audio" if extension.lower() == "m4a" else "video"
            if take[key] is not None:
                raise ValueError(f"Arquivo duplicado para take {number}: {name}")
            take[key] = file
            continue

        single_match = re.fullmatch(r"(.+)\.(mov|mp4|m4a)", name, re.I)
        if single_match:
            basename, extension = single_match.groups()
            package = packages[basename]
            if "legacy" not in package:
                package["legacy"] = {}
            key = extension.lower()
            if key in package["legacy"]:
                raise ValueError(f"Arquivo duplicado para projeto {basename}: {name}")
            package["legacy"][key] = file

    normalized = {}
    for basename, package in packages.items():
        if package["takes"] and package.get("legacy"):
            raise ValueError(
                f"Projeto {basename} mistura arquivos sem número e takes numerados. "
                "Use somente arquivos _1, _2... ou somente arquivos sem sufixo."
            )
        if package["takes"]:
            package["takes"].sort(key=lambda item: item["number"])
            normalized[basename] = package
        elif package.get("legacy"):
            legacy = package["legacy"]
            if "mov" in legacy or "mp4" in legacy:
                normalized[basename] = {
                    "legacy": legacy,
                    "screen": package["screen"],
                }
    return normalized

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
    files = []
    if "takes" in package:
        for take in package["takes"]:
            files.extend(file for file in (take.get("video"), take.get("audio")) if file)
    else:
        files.extend(package.get("legacy", {}).values())
    if package.get("screen"):
        files.append(package["screen"])

    with tempfile.TemporaryDirectory() as temp_dir:
        for file in files:
            filename = file["name"]
            destination = prefix + filename
            if object_exists(storage_client, destination):
                print(f"⏭️ Entrada já existe; pulando upload: {destination}", flush=True)
                continue
            local_path = os.path.join(temp_dir, filename)
            print(f"Baixando do Drive: {filename}", flush=True)
            download_drive_file(drive, file["id"], local_path)
            print(f"Enviando para gs://{BUCKET_NAME}/{destination}", flush=True)
            bucket.blob(destination).upload_from_filename(local_path, timeout=3600)
    print(f"Entradas de {basename} verificadas.", flush=True)

# =========================
# GCS
# =========================

def object_exists(storage_client, object_name):
    return storage_client.bucket(BUCKET_NAME).blob(
        object_name
    ).exists()


def ensure_screen_synced(storage_client, basename):
    bucket = storage_client.bucket(BUCKET_NAME)

    source_name = (
        f"input/{basename}/{basename}_screen.mp4"
    )
    destination_name = (
        f"synced/{basename}/screen_synced.mp4"
    )

    source = bucket.blob(source_name)
    destination = bucket.blob(destination_name)

    if not source.exists():
        print(
            f"⏭️ Screen não encontrado: {source_name}",
            flush=True,
        )
        return False

    if destination.exists():
        print(
            f"⏭️ Screen já existe: {destination_name}",
            flush=True,
        )
        return True

    bucket.copy_blob(
        source,
        bucket,
        destination_name,
    )

    print(
        f"✅ Screen copiado para: {destination_name}",
        flush=True,
    )
    return True


def ensure_video_synced(storage_client, basename, package):
    """Prepara vídeos isolados com áudio embutido sem executar o sync-worker."""
    bucket = storage_client.bucket(BUCKET_NAME)
    destination_name = f"synced/{basename}/video_synced.mp4"
    destination = bucket.blob(destination_name)

    if destination.exists():
        print(f"⏭️ Vídeo já preparado: {destination_name}", flush=True)
        return

    source_file = package.get("mp4") or package.get("mov")
    if not source_file:
        raise RuntimeError(f"Vídeo isolado não encontrado em input/{basename}/")

    source_name = f"input/{basename}/{source_file['name']}"
    source = bucket.blob(source_name)
    if not source.exists():
        raise RuntimeError(f"Vídeo de entrada não encontrado: gs://{BUCKET_NAME}/{source_name}")

    bucket.copy_blob(source, bucket, destination_name)
    print(f"✅ Vídeo copiado para synced sem sync-worker: {destination_name}", flush=True)


def ensure_multi_take_synced(credentials, storage_client, basename, package):
    """Prepara cada take separadamente; o nome do objeto identifica sua origem."""
    prefix = f"gs://{BUCKET_NAME}"
    bucket = storage_client.bucket(BUCKET_NAME)
    for take in package["takes"]:
        number = take["number"]
        destination_name = f"synced/{basename}/take_{number:03d}.mp4"
        destination = bucket.blob(destination_name)
        if destination.exists():
            print(f"⏭️ Take {number} já preparado.", flush=True)
            continue

        video = take.get("video")
        audio = take.get("audio")
        if not video:
            raise RuntimeError(f"Take {number} não possui vídeo.")

        source_video = f"input/{basename}/{video['name']}"
        if audio:
            if video["name"].lower().endswith(".mov") is False:
                raise RuntimeError(
                    f"Áudio externo no take {number} exige vídeo .mov nesta versão."
                )
            run_job(
                credentials,
                "sync-worker",
                SYNC_REGION,
                {
                    "VIDEO_INPUT": f"{prefix}/{source_video}",
                    "AUDIO_INPUT": f"{prefix}/input/{basename}/{audio['name']}",
                    "OUTPUT": f"{prefix}/{destination_name}",
                },
            )
        else:
            source = bucket.blob(source_video)
            if not source.exists():
                raise RuntimeError(f"Vídeo não encontrado: {source_video}")
            bucket.copy_blob(source, bucket, destination_name)
            print(f"✅ Take {number} copiado para {destination_name}", flush=True)


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


def run_pipeline(credentials, storage_client, basename, package):
    prefix = f"gs://{BUCKET_NAME}"
    is_multi_take = "takes" in package

    if is_multi_take:
        ensure_multi_take_synced(credentials, storage_client, basename, package)
    else:
        legacy = package["legacy"]
        video = legacy.get("mp4") or legacy.get("mov")
        audio = legacy.get("m4a")
        synced_object = f"synced/{basename}/video_synced.mp4"
        if object_exists(storage_client, synced_object):
            print("⏭️ Vídeo já preparado; pulando sync.", flush=True)
        elif video and audio:
            run_job(
                credentials, "sync-worker", SYNC_REGION,
                {
                    "VIDEO_INPUT": f"{prefix}/input/{basename}/{video['name']}",
                    "AUDIO_INPUT": f"{prefix}/input/{basename}/{audio['name']}",
                    "OUTPUT": f"{prefix}/{synced_object}",
                },
            )
        else:
            ensure_video_synced(storage_client, basename, legacy)

    transcript_object = f"transcripts/{basename}/transcription.json"
    if object_exists(storage_client, transcript_object):
        print("⏭️ Transcrição já concluída; pulando.", flush=True)
    else:
        run_job(credentials, "transcription-worker", TRANSCRIPTION_REGION, {"PROJECT_NAME": basename})

    edit_object = f"edits/{basename}/edit.json"
    if object_exists(storage_client, edit_object):
        print("⏭️ Edição por IA já concluída; pulando.", flush=True)
    else:
        run_job(credentials, "ai-editor-worker", EDITOR_REGION, {"PROJECT_NAME": basename})

    final_object = f"final/{basename}/final.mp4"
    if object_exists(storage_client, final_object):
        print("⏭️ Renderização já concluída; pulando.", flush=True)
    else:
        run_job(credentials, "render-worker", RENDER_REGION, {"PROJECT_NAME": basename})

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


def process_package(credentials, drive, storage_client, basename, package):
    is_multi_take = "takes" in package
    if is_multi_take:
        valid = bool(package["takes"]) and all(take.get("video") for take in package["takes"])
    else:
        legacy = package.get("legacy", {})
        valid = bool(legacy.get("mp4") or legacy.get("mov"))
    if not valid:
        print(f"SKIP {basename}: pacote sem vídeo válido.", flush=True)
        return

    final_object = f"final/{basename}/final.mp4"
    screen_final_object = f"final/{basename}/screen_final.mp4"

    print(f"\\n{'=' * 60}", flush=True)
    print(f"Projeto: {basename}", flush=True)
    print(f"{'=' * 60}", flush=True)

    upload_inputs(drive, storage_client, basename, package)

    # Screen continua disponível no fluxo de take único. Em multi-take,
    # uma única captura de tela não possui alinhamento temporal por take.
    has_screen = False
    if not is_multi_take:
        has_screen = ensure_screen_synced(storage_client, basename)

    if object_exists(storage_client, final_object):
        if not has_screen or object_exists(storage_client, screen_final_object):
            print("Vídeo final já existe. Nada a fazer.", flush=True)
            return
        run_job(credentials, "render-worker", RENDER_REGION,
                {"PROJECT_NAME": basename, "SCREEN_ONLY": "true"})
        return

    run_pipeline(credentials, storage_client, basename, package)
    print(f"\\nProjeto {basename} concluído!", flush=True)

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
