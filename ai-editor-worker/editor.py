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

MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.0-flash",
    "gemini-2.5-flash",
]

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


def generate_with_models(
    client,
    prompt,
    config,
    models
):
    last_error = None

    for index, model in enumerate(models, 1):

        print(
            f"\n🤖 Modelo {index}/{len(models)}: {model}",
            flush=True
        )

        try:

            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )

            print(
                f"✅ Modelo {model} funcionou!",
                flush=True
            )

            return response

        except Exception as e:

            last_error = e

            print(
                f"❌ Modelo {model} falhou:",
                flush=True
            )

            print(
                f"   {e}",
                flush=True
            )

            print(
                "⏭️ Pulando para o próximo modelo...",
                flush=True
            )

    raise RuntimeError(
        "❌ Todos os modelos Gemini falharam."
    ) from last_error
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
originais do vídeo e timestamps individuais para cada palavra.

OBJETIVO:

Montar o vídeo final mais conciso, natural e coerente
possível usando somente trechos existentes na gravação.

O resultado deve parecer uma gravação única e bem editada,
mesmo que tenha sido construído a partir de várias tentativas.

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

IMPORTANTE:

Um segmento da transcrição NÃO é necessariamente uma unidade
indivisível de edição.

Você pode usar apenas uma parte de um segmento quando isso
produzir uma edição melhor.

Por exemplo, se um segmento contém:

"Existe o Git. O Git é uma ferramenta..."

e depois existe outro take contendo:

"O Git é uma ferramenta que vai permitir..."

você pode manter o primeiro segmento somente até
"Existe o Git." e descartar a continuação "O Git é uma
ferramenta...".

Isso é especialmente importante quando o apresentador
começa uma frase, percebe que quer refazê-la e imediatamente
repete a mesma frase em outro take.

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

7. Um take pode ser parcialmente aproveitado.

   Se apenas o início de um segmento for útil, escolha
   somente esse início.

   Se apenas o final de um segmento for útil, escolha
   somente esse final.

   Não descarte obrigatoriamente o segmento inteiro apenas
   porque uma parte dele contém uma tentativa repetida.

8. Quando um apresentador termina uma ideia e imediatamente
   começa a repetir ou refazer a próxima frase, corte o
   segmento exatamente antes do início da repetição.

9. Use os timestamps das palavras para encontrar pontos
   precisos de corte.

10. Quando fizer um corte dentro de um segmento, o timestamp
    "end" deve ser exatamente o "end" de uma palavra existente
    na transcrição.

11. Quando fizer um corte interno, o timestamp "start" também
    deve ser exatamente o "start" de uma palavra existente,
    exceto quando estiver usando o início original de um
    segmento.

12. NÃO invente timestamps.

13. Todos os timestamps retornados devem existir na
    transcrição fornecida ou corresponder exatamente aos
    timestamps "start" ou "end" de palavras existentes.

14. Não combine dois takes diferentes para formar uma frase,
    a menos que os trechos funcionem naturalmente juntos.

15. Preserve a ordem original das ideias.

16. Se uma ideia já foi explicada em um take escolhido,
    não escolha outro take que repita a mesma explicação.

17. Frases que começam de maneira semelhante NÃO são
    necessariamente duplicadas. Analise o conteúdo completo
    antes de decidir.

18. Uma frase repetida pode ser necessária quando ela aparece
    como continuação natural de uma ideia anterior.

19. Diferencie entre:

    A) repetição descartável:
       o apresentador repete uma frase para tentar novamente;

    B) repetição necessária:
       a mesma expressão aparece novamente porque faz parte
       da estrutura natural da explicação.

20. Quando houver uma tentativa incompleta seguida de uma
    tentativa melhor e completa, prefira a tentativa completa
    para aquela ideia, mas preserve a parte anterior da
    tentativa incompleta se ela contiver conteúdo útil que
    não foi repetido.

EXEMPLO DE RACIOCÍNIO:

Se existir:

[61.627 -> 73.485]
"Para a gente manter esse histórico... existe o Git.
O Git é uma ferramenta,"

e depois:

[77.378 -> 78.468]
"O Git é uma ferramenta"

e depois:

[86.892 -> 91.237]
"O Git é uma ferramenta que vai permitir você fazer
o versionamento do seu código."

o primeiro segmento pode ser cortado em:

[61.627 -> 71.740]

porque "existe o Git." completa a ideia anterior.

Depois pode ser escolhido:

[86.892 -> 91.237]

para explicar o que é o Git.

Nesse caso, NÃO mantenha:

[72.141 -> 73.485]
"O Git é uma ferramenta,"

porque essa frase está sendo refeita no take posterior.

IMPORTANTE SOBRE OS TIMESTAMPS:

Cada objeto retornado em "segments" representa EXATAMENTE
um trecho que será enviado ao renderizador.

O renderizador aceitará qualquer combinação válida de
"start" e "end".

Portanto, os limites de um segmento podem ser diferentes
dos limites do segmento original da transcrição.

FORMATO DA RESPOSTA:

Retorne SOMENTE JSON válido neste formato:

{json.dumps({
  "segments": [
    {
      "start": 61.627,
      "end": 71.740
    },
    {
      "start": 86.892,
      "end": 91.237
    }
  ]
}, ensure_ascii=False, indent=2)}

Não inclua texto explicativo.
Não inclua comentários.
Não inclua markdown.

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

response = generate_with_models(
    client,
    prompt,
    config,
    MODELS,
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
