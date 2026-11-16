import json
import os

from google import genai
from google.cloud import storage
from pydantic import BaseModel, Field

BUCKET_NAME = "ai-video-editor-510018"
PROJECT_NAME = os.environ.get("PROJECT_NAME")
if not PROJECT_NAME:
    raise ValueError("A variável PROJECT_NAME é obrigatória.")

INPUT_PREFIX = f"transcripts/{PROJECT_NAME}/"
OUTPUT_PREFIX = f"edits/{PROJECT_NAME}/"
INPUT_PATH = "/tmp/transcription.json"
OUTPUT_PATH = "/tmp/edit.json"
MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.0-flash",
    "gemini-2.5-flash",
]
TIMESTAMP_TOLERANCE = 0.002


class EditSegment(BaseModel):
    source: str
    start: float
    end: float


class EditResult(BaseModel):
    video: str = PROJECT_NAME
    segments: list[EditSegment]


class SemanticGroup(BaseModel):
    label: str = Field(description="Resumo curto da fala ou ideia.")
    segment_indices: list[int] = Field(description="Índices das tentativas equivalentes.")


class GroupingResult(BaseModel):
    groups: list[SemanticGroup]


class SelectedClip(BaseModel):
    segment_index: int
    source: str
    start: float
    end: float


class SelectionResult(BaseModel):
    clips: list[SelectedClip]


def find_input_transcription():
    bucket = storage.Client().bucket(BUCKET_NAME)
    files = [
        blob for blob in bucket.list_blobs(prefix=INPUT_PREFIX)
        if blob.name.lower().endswith(".json")
    ]
    if not files:
        raise RuntimeError(f"Nenhuma transcrição encontrada em {INPUT_PREFIX}")
    if len(files) > 1:
        raise RuntimeError(
            f"Esperado apenas um JSON em {INPUT_PREFIX}, encontrados {len(files)}"
        )
    return files[0].name


def download_from_gcs(gcs_uri, local_path):
    bucket_name, blob_name = gcs_uri.replace("gs://", "", 1).split("/", 1)
    storage.Client().bucket(bucket_name).blob(blob_name).download_to_filename(local_path)
    print(f"📥 Download concluído: {gcs_uri}", flush=True)


def upload_to_gcs(local_path, gcs_uri):
    bucket_name, blob_name = gcs_uri.replace("gs://", "", 1).split("/", 1)
    storage.Client().bucket(bucket_name).blob(blob_name).upload_from_filename(
        local_path, content_type="application/json"
    )
    print(f"📤 Upload concluído: {gcs_uri}", flush=True)


def generate_with_models(client, prompt, response_schema, stage_name):
    last_error = None
    config = {
        "response_mime_type": "application/json",
        "response_schema": response_schema,
    }
    for index, model in enumerate(MODELS, 1):
        print(f"\n🤖 {stage_name} — modelo {index}/{len(MODELS)}: {model}", flush=True)
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            print(f"✅ {stage_name} concluída com {model}", flush=True)
            return response
        except Exception as error:
            last_error = error
            print(f"❌ {stage_name}: {model} falhou: {error}", flush=True)
    raise RuntimeError(f"Todos os modelos Gemini falharam na etapa: {stage_name}") from last_error


def build_grouping_prompt(transcription):
    indexed_segments = [
        {"index": index, **segment}
        for index, segment in enumerate(transcription["segments"])
    ]
    return f"""
Você é um analista de transcrição para edição de vídeo.
Identifique quais segmentos são tentativas alternativas da MESMA fala ou ideia.
NÃO escolha ainda a melhor tentativa e NÃO monte a edição final.

CONTEXTO:
O apresentador pode gravar a mesma fala várias vezes em arquivos diferentes.
Cada segmento tem timestamps locais ao arquivo indicado em "source".

REGRAS:
1. Agrupe segmentos de takes diferentes quando forem tentativas da mesma fala,
   explicação ou unidade de conteúdo, mesmo com pequenas diferenças de palavras.
2. O critério é a intenção e o conteúdo, não igualdade literal do texto.
3. Não agrupe frases só porque começam com palavras iguais.
4. Não agrupe partes que são continuação natural uma da outra.
5. Não agrupe ideias diferentes só porque tratam do mesmo assunto.
6. Cada grupo pode conter uma única entrada quando não houver alternativa.
7. Cada índice deve aparecer exatamente uma vez na resposta.
8. Não omita segmentos e não invente índices.
9. Use os índices como identificadores, nunca timestamps.
10. Não favoreça o take mais recente nem qualquer arquivo específico.
11. Retorne grupos em ordem aproximada da sequência de conteúdo final.
12. O label deve resumir a fala/ideia comum do grupo.

Retorne somente JSON no schema solicitado.

TRANSCRIÇÃO INDEXADA:
{json.dumps(indexed_segments, ensure_ascii=False, indent=2)}
"""


def build_selection_prompt(transcription, grouping):
    segments = transcription["segments"]
    groups_with_segments = []
    for group_index, group in enumerate(grouping.groups):
        candidates = [
            {"index": segment_index, **segments[segment_index]}
            for segment_index in group.segment_indices
        ]
        groups_with_segments.append({
            "group_index": group_index,
            "idea": group.label,
            "candidates": candidates,
        })

    return f"""
Você é o editor responsável por montar a edição final de um vídeo gravado em
vários takes. A etapa anterior agrupou alternativas da mesma fala/ideia.
Compare as alternativas e escolha os trechos que formarão o vídeo.

COMO ESCOLHER:
1. Compare alternativas dentro de cada grupo: completude, clareza, naturalidade,
   fluidez, ritmo, entonação sugerida pelo texto e ausência de hesitações.
2. Escolha a melhor interpretação COMPLETA para cada ideia.
3. Não misture palavras de versões diferentes para fabricar uma fala que nunca
   foi gravada.
4. Não existe preferência pelo take mais recente, antigo ou por arquivo.
5. Não force o uso de todos os takes. Um único take pode ser a melhor escolha.
6. Preserve conteúdo único que não esteja repetido em outro grupo.
7. Remova repetições descartáveis, tentativas abandonadas, erros e hesitações.
8. Preserve a ordem lógica das ideias, não a ordem numérica dos takes.
9. Um segmento pode ser parcialmente aproveitado se contiver conteúdo útil
   seguido de repetição ou tentativa abandonada.
10. Não crie falas, reescreva a transcrição ou invente timestamps.
11. Cada clip deve apontar para o índice do segmento original que o contém.
12. Copie o source exatamente do segmento apontado.
13. start e end devem estar dentro do intervalo do segmento original.
14. Os limites devem ser o início/fim original do segmento ou timestamps
    start/end de palavras existentes nele.
15. start deve ser menor que end.
16. Um grupo pode ser descartado se não tiver conteúdo aproveitável.
17. Não selecione duas alternativas completas do mesmo grupo. Só use mais de
    um clip do grupo se forem partes distintas, não repetidas e necessárias.
18. Prefira uma frase completa em uma única gravação.
19. Não escolha uma interpretação inferior apenas para variar os takes.

A edição deve soar como uma explicação única, coerente e natural.
Retorne somente JSON no schema solicitado, com "clips". Cada clip contém
segment_index, source, start e end. A ordem dos clips será a edição final.

GRUPOS E CANDIDATOS:
{json.dumps(groups_with_segments, ensure_ascii=False, indent=2)}
"""


def validate_grouping(grouping, segment_count):
    seen = []
    for group in grouping.groups:
        if not group.segment_indices:
            raise ValueError(f"Grupo sem segmentos: {group.label}")
        seen.extend(group.segment_indices)

    expected = set(range(segment_count))
    actual = set(seen)
    if len(seen) != len(actual):
        raise ValueError("O agrupamento repetiu um ou mais índices.")
    if actual != expected:
        raise ValueError(
            f"Agrupamento incompleto/inválido. Ausentes={sorted(expected - actual)}; "
            f"inválidos={sorted(actual - expected)}"
        )


def timestamp_is_valid(value, allowed_values):
    return any(
        abs(float(value) - float(candidate)) <= TIMESTAMP_TOLERANCE
        for candidate in allowed_values
    )


def validate_selection(selection, transcription):
    source_segments = transcription["segments"]
    validated = []

    for clip_number, clip in enumerate(selection.clips, 1):
        if not 0 <= clip.segment_index < len(source_segments):
            raise ValueError(
                f"Clip {clip_number}: índice inválido {clip.segment_index}"
            )

        original = source_segments[clip.segment_index]
        if clip.source != original["source"]:
            raise ValueError(
                f"Clip {clip_number}: source {clip.source!r} não corresponde "
                f"ao segmento {clip.segment_index} ({original['source']!r})"
            )

        start, end = float(clip.start), float(clip.end)
        segment_start, segment_end = float(original["start"]), float(original["end"])
        if start >= end:
            raise ValueError(f"Clip {clip_number}: start precisa ser menor que end.")
        if start < segment_start - TIMESTAMP_TOLERANCE:
            raise ValueError(f"Clip {clip_number}: start fora do segmento original.")
        if end > segment_end + TIMESTAMP_TOLERANCE:
            raise ValueError(f"Clip {clip_number}: end fora do segmento original.")

        allowed_starts = [segment_start]
        allowed_ends = [segment_end]
        for word in original.get("words", []):
            if word.get("start") is not None:
                allowed_starts.append(float(word["start"]))
            if word.get("end") is not None:
                allowed_ends.append(float(word["end"]))

        if not timestamp_is_valid(start, allowed_starts):
            raise ValueError(
                f"Clip {clip_number}: start não corresponde ao início do segmento "
                "nem ao início de uma palavra."
            )
        if not timestamp_is_valid(end, allowed_ends):
            raise ValueError(
                f"Clip {clip_number}: end não corresponde ao fim do segmento "
                "nem ao fim de uma palavra."
            )

        validated.append(EditSegment(
            source=original["source"],
            start=round(start, 3),
            end=round(end, 3),
        ))
    return validated


def main():
    input_blob = find_input_transcription()
    input_uri = f"gs://{BUCKET_NAME}/{input_blob}"
    output_uri = f"gs://{BUCKET_NAME}/{OUTPUT_PREFIX}edit.json"

    print("🚀 AI Editor worker", flush=True)
    print(f"📁 Input: {input_uri}", flush=True)
    print(f"📄 Output: {output_uri}", flush=True)
    download_from_gcs(input_uri, INPUT_PATH)

    with open(INPUT_PATH, "r", encoding="utf-8") as file:
        transcription = json.load(file)

    segments = transcription.get("segments", [])
    if not segments:
        raise RuntimeError("A transcrição não contém segmentos.")

    # Compatibilidade com transcrições anteriores ao suporte a multi-take.
    for segment in segments:
        segment.setdefault("source", "video_synced.mp4")

    sources = sorted({segment["source"] for segment in segments})
    print(f"📝 Segmentos recebidos: {len(segments)}", flush=True)
    print(f"🎬 Fontes: {', '.join(sources)}", flush=True)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY não configurada.")
    client = genai.Client(api_key=api_key)

    print("\n🔎 Etapa 1/2: agrupando falas equivalentes...", flush=True)
    grouping_response = generate_with_models(
        client, build_grouping_prompt(transcription),
        GroupingResult, "Agrupamento semântico"
    )
    grouping = GroupingResult.model_validate_json(grouping_response.text)
    validate_grouping(grouping, len(segments))

    print(f"🧩 Grupos identificados: {len(grouping.groups)}", flush=True)
    for index, group in enumerate(grouping.groups, 1):
        group_sources = sorted({
            segments[item]["source"] for item in group.segment_indices
        })
        print(
            f"  Grupo {index}: {group.label} | "
            f"segmentos={group.segment_indices} | fontes={group_sources}",
            flush=True,
        )

    print("\n🎬 Etapa 2/2: comparando takes e montando a edição...", flush=True)
    selection_response = generate_with_models(
        client, build_selection_prompt(transcription, grouping),
        SelectionResult, "Seleção e montagem"
    )
    selection = SelectionResult.model_validate_json(selection_response.text)
    final_segments = validate_selection(selection, transcription)

    print("\n✂️ Seleção final:", flush=True)
    for index, segment in enumerate(final_segments, 1):
        print(
            f"  {index:03d}. {segment.source} "
            f"[{segment.start:.3f} -> {segment.end:.3f}]",
            flush=True,
        )

    result = EditResult(segments=final_segments)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as file:
        json.dump(result.model_dump(), file, ensure_ascii=False, indent=2)

    print(f"💾 JSON gerado: {OUTPUT_PATH}", flush=True)
    upload_to_gcs(OUTPUT_PATH, output_uri)
    print(
        f"✅ AI Editor concluído! Segmentos mantidos: {len(result.segments)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
