import os
import json
from google import genai
from google.cloud import storage
from pydantic import BaseModel
import time

# =========================
# CONFIGURAÇÃO
# =========================

BUCKET_NAME = "ai-video-editor-510018"

INPUT_PREFIX = "transcripts/"
OUTPUT_PREFIX = "edits/"

INPUT_PATH = "/tmp/transcription.json"
OUTPUT_PATH = "/tmp/edit.json"

MODEL = "gemini-3.6-flash"

# =========================
# OUTPUT STRUCTURE
# =========================


class EditSegment(BaseModel):
    start: float
    end: float


class EditResult(BaseModel):
    video: str
    segments: list[EditSegment]

# =========================
# GCS
# =========================


def find_input_transcription():
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)

    blobs = list(
        bucket.list_blobs(prefix=INPUT_PREFIX)
    )

    files = [
        blob
        for blob in blobs
        if blob.name.lower().endswith(".json")
    ]

    if len(files) == 0:
        raise RuntimeError(
            "Nenhuma transcrição encontrada em transcripts/"
        )

    if len(files) > 1:
        raise RuntimeError(
            f"Esperado apenas um JSON em transcripts/, "
            f"encontrados {len(files)}"
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
        content_type="application/json",
    )


def generate_with_retry(client, prompt, config, max_retries=5):
    for attempt in range(max_retries):
        try:
            print(
                f"🤖 Chamando Gemini "
                f"(tentativa {attempt + 1}/{max_retries})...",
                flush=True,
            )

            return client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=config,
            )

        except Exception as e:
            error_message = str(e)

            is_retryable = (
                "503" in error_message
                or "UNAVAILABLE" in error_message
                or "429" in error_message
                or "RESOURCE_EXHAUSTED" in error_message
            )

            if not is_retryable:
                raise

            if attempt == max_retries - 1:
                print(
                    "❌ Gemini continua indisponível "
                    "após várias tentativas.",
                    flush=True,
                )
                raise

            wait_seconds = 2 ** attempt

            print(
                f"⚠️ Gemini temporariamente indisponível. "
                f"Nova tentativa em {wait_seconds}s...",
                flush=True,
            )

            time.sleep(wait_seconds)

# =========================
# MAIN
# =========================


INPUT_BLOB = find_input_transcription()

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
    "🚀 AI Editor worker",
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
    INPUT_PATH
)

print(
    "✅ Transcrição baixada.",
    flush=True
)

# =========================
# CARREGAR JSON
# =========================

with open(
    INPUT_PATH,
    "r",
    encoding="utf-8"
) as f:
    transcription = json.load(f)

print(
    f"📝 Segmentos recebidos: "
    f"{len(transcription['segments'])}",
    flush=True
)

# =========================
# GEMINI
# =========================

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY não configurada."
    )

client = genai.Client(
    api_key=api_key
)

# =========================
# PROMPT
# =========================

prompt = f"""
Você é o editor responsável por montar o roteiro final
de um vídeo a partir de uma gravação contendo várias
tentativas do apresentador.

A transcrição abaixo contém segmentos com timestamps
originais do vídeo.

OBJETIVO:

Montar o vídeo final mais conciso, natural e coerente
possível usando somente trechos existentes na gravação.

COMO A GRAVAÇÃO FUNCIONA:

O apresentador normalmente grava uma ideia várias vezes.

O padrão costuma ser:

tentativa 1
tentativa 2
tentativa 3
...
melhor tentativa
→ próxima ideia

Por isso, quando existirem várias tentativas da mesma ideia,
você deve identificar qual delas funciona melhor.

REGRAS:

1. Escolha apenas os trechos que devem permanecer no vídeo.

2. NÃO retorne trechos descartados.

3. Remova repetições da mesma informação.

4. Remova erros, hesitações, frases incompletas e tentativas
   claramente abandonadas.

5. Quando houver várias tentativas da mesma ideia, prefira
   o melhor take completo.

6. Normalmente o melhor take é o mais recente, pois o
   apresentador costuma repetir até acertar. Porém, isso
   NÃO é uma regra absoluta. Analise o conteúdo e escolha
   o take que melhor funciona.

7. Não combine dois takes diferentes para formar uma frase,
   a menos que isso seja claramente necessário e os trechos
   funcionem naturalmente juntos.

8. Preserve a ordem original das ideias.

9. Não invente nenhum timestamp.

10. Todos os timestamps retornados devem existir na
    transcrição fornecida.

11. O resultado deve ser um roteiro conciso. Se uma ideia
    já foi explicada em um take escolhido, não escolha outro
    take que repita a mesma explicação.

12. Não faça uma análise ou explicação da decisão.
    Retorne somente a estrutura JSON solicitada.

IMPORTANTE:

O campo "segments" representa EXATAMENTE os trechos que
serão enviados ao renderizador.

Portanto, se um segmento não estiver na lista "segments",
ele será descartado.

TRANSCRIÇÃO:

{json.dumps(
    transcription,
    ensure_ascii=False,
    indent=2
)}
"""

# =========================
# GERAR EDIÇÃO
# =========================

print(
    "\n🤖 Enviando transcrição para Gemini...",
    flush=True
)

config = {
    "response_mime_type": "application/json",
    "response_schema": EditResult,
}

response = generate_with_retry(
    client,
    prompt,
    config,
)

# =========================
# VALIDAR RESULTADO
# =========================

result = EditResult.model_validate_json(
    response.text
)

# =========================
# SALVAR JSON
# =========================

output = result.model_dump()

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
    f"💾 JSON gerado: {OUTPUT_PATH}",
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
    "\n✅ AI Editor concluído!",
    flush=True
)

print(
    f"✂️ Segmentos mantidos: "
    f"{len(result.segments)}",
    flush=True
)
