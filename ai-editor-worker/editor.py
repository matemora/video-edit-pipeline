import json
import os

from google import genai
from google.cloud import storage
from pydantic import BaseModel, Field

BUCKET_NAME = "ai-video-editor-510018"
PROJECT_NAME = os.environ.get("PROJECT_NAME")

INPUT_PATH = "/tmp/transcription.json"
OUTPUT_PATH = "/tmp/edit.json"
MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.0-flash",
    "gemini-2.5-flash",
]

# Distância máxima (s) para encaixar um timestamp do modelo na palavra mais próxima.
SNAP_TOLERANCE = 0.35
# Se o reparo descartar mais que esta fração dos clips, a resposta é considerada
# ruim e o próximo modelo é tentado.
MAX_DROPPED_RATIO = 0.2


# =========================
# SCHEMAS
# =========================

class EditSegment(BaseModel):
    source: str
    start: float
    end: float


class EditResult(BaseModel):
    video: str
    segments: list[EditSegment]


class SelectedClip(BaseModel):
    segment_index: int
    source: str
    start: float
    end: float


class SelectionResult(BaseModel):
    clips: list[SelectedClip]


class SemanticGroup(BaseModel):
    label: str = Field(description="Resumo curto da fala ou ideia.")
    segment_indices: list[int] = Field(description="Índices das tentativas equivalentes.")


class GroupingResult(BaseModel):
    groups: list[SemanticGroup]


class ResponseRejected(Exception):
    """A resposta do modelo veio, mas não serve; tenta o próximo modelo."""


# =========================
# GCS
# =========================

def find_input_transcription(input_prefix):
    bucket = storage.Client().bucket(BUCKET_NAME)
    files = [
        blob for blob in bucket.list_blobs(prefix=input_prefix)
        if blob.name.lower().endswith(".json")
    ]
    if not files:
        raise RuntimeError(f"Nenhuma transcrição encontrada em {input_prefix}")
    if len(files) > 1:
        raise RuntimeError(
            f"Esperado apenas um JSON em {input_prefix}, encontrados {len(files)}"
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


# =========================
# GEMINI
# =========================

def generate_with_models(client, prompt, response_schema, stage_name, parse):
    """Tenta cada modelo; `parse` transforma o texto da resposta no resultado final.

    Erro de API ou resposta rejeitada pelo `parse` passam para o próximo modelo.
    """
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
            result = parse(response.text)
            print(f"✅ {stage_name} concluída com {model}", flush=True)
            return result
        except Exception as error:
            last_error = error
            print(f"❌ {stage_name}: {model} falhou: {error}", flush=True)
    raise RuntimeError(f"Todos os modelos Gemini falharam na etapa: {stage_name}") from last_error


def compact_json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def indexed_segments(segments, with_words=True):
    items = []
    for index, segment in enumerate(segments):
        item = {
            "index": index,
            "source": segment["source"],
            "start": segment["start"],
            "end": segment["end"],
            "text": segment["text"],
        }
        if with_words:
            item["words"] = segment.get("words", [])
        items.append(item)
    return items


# =========================
# PROMPTS
# =========================

CUT_RULES = """
COMO A GRAVAÇÃO FUNCIONA:

O apresentador normalmente grava uma ideia várias vezes, em sequência,
até acertar. O padrão costuma ser:

tentativa 1
tentativa 2
tentativa 3
...
melhor tentativa
→ próxima ideia

As tentativas da mesma ideia podem estar no MESMO arquivo (uma logo depois
da outra) ou em arquivos diferentes (takes).

IMPORTANTE:

Um segmento da transcrição NÃO é uma unidade indivisível de edição.
Você pode usar apenas uma parte de um segmento quando isso produzir uma
edição melhor.

Isso é especialmente importante quando o apresentador termina uma ideia,
começa a próxima frase, percebe que quer refazê-la e logo depois repete a
mesma frase em outra tentativa.

EXEMPLO DE RACIOCÍNIO:

Se existir:

[61.627 -> 73.485]
"Para a gente manter esse histórico... existe o Git. O Git é uma ferramenta,"

e depois:

[77.378 -> 78.468]
"O Git é uma ferramenta"

e depois:

[86.892 -> 91.237]
"O Git é uma ferramenta que vai permitir você fazer o versionamento do seu código."

o primeiro segmento deve ser cortado em [61.627 -> 71.740], porque
"existe o Git." completa a ideia anterior. Depois escolha [86.892 -> 91.237]
para explicar o que é o Git. NÃO mantenha "O Git é uma ferramenta," do
primeiro segmento nem o segundo segmento, porque essa frase é refeita depois.

REGRAS:

1. Escolha apenas os trechos que devem permanecer no vídeo.
2. Remova repetições da mesma informação.
3. Remova erros, hesitações, frases incompletas e tentativas claramente
   abandonadas.
4. Quando houver várias tentativas da mesma ideia, prefira o melhor take
   completo.
5. Normalmente o melhor take é o mais recente, pois o apresentador repete
   até acertar. Porém, isso NÃO é uma regra absoluta. Analise o conteúdo e
   escolha o take que melhor funciona.
6. Um take pode ser parcialmente aproveitado: só o início, ou só o final.
   Não descarte o segmento inteiro só porque uma parte dele é uma tentativa
   repetida.
7. Quando o apresentador termina uma ideia e logo começa a repetir ou refazer
   a próxima frase, corte o segmento exatamente antes do início da repetição.
8. Use os timestamps das palavras para achar os pontos de corte.
9. Em cortes internos, "start" deve ser o "start" de uma palavra existente
   e "end" deve ser o "end" de uma palavra existente. Também pode usar o
   início/fim original do segmento.
10. NÃO invente timestamps.
11. Não combine dois takes diferentes para formar uma frase, a menos que os
    trechos funcionem naturalmente juntos.
12. Se uma ideia já foi explicada em um trecho escolhido, não escolha outro
    trecho que repita a mesma explicação.
13. Frases que começam de maneira parecida NÃO são necessariamente
    duplicadas. Analise o conteúdo completo antes de decidir.
14. Diferencie:
    A) repetição descartável: o apresentador repete para tentar de novo;
    B) repetição necessária: a mesma expressão volta porque faz parte da
       estrutura natural da explicação.
15. Quando uma tentativa incompleta é seguida de uma melhor e completa,
    prefira a completa para aquela ideia, mas preserve a parte anterior da
    incompleta se ela tiver conteúdo útil que não foi repetido.
16. Cada clip aponta para o "index" do segmento original que o contém e
    copia o "source" desse segmento. start e end ficam dentro dele.
"""


def build_single_prompt(segments, multi_source):
    order_rule = (
        "17. Monte as ideias na ordem do roteiro: a ordem em que cada ideia "
        "aparece pela primeira vez na gravação. Os takes foram gravados em "
        "ordem numérica."
        if multi_source else
        "17. Preserve a ordem original das ideias."
    )
    return f"""
Você é o editor responsável por montar o roteiro final de um vídeo a partir
de uma gravação contendo várias tentativas do apresentador.

A transcrição abaixo tem segmentos com "index", "source" (arquivo de origem),
timestamps locais a esse arquivo e timestamps de cada palavra.

OBJETIVO:

Montar o vídeo final mais conciso, natural e coerente possível usando somente
trechos existentes na gravação. O resultado deve parecer uma gravação única e
bem editada.
{CUT_RULES}
{order_rule}

FORMATO DA RESPOSTA:

Retorne SOMENTE JSON com "clips". Cada clip tem segment_index, source, start
e end. A ordem dos clips é a ordem do vídeo final. Exemplo:

{compact_json({"clips": [
    {"segment_index": 12, "source": "video_synced.mp4", "start": 61.627, "end": 71.74},
    {"segment_index": 14, "source": "video_synced.mp4", "start": 86.892, "end": 91.237},
]})}

TRANSCRIÇÃO:
{compact_json(indexed_segments(segments))}
"""


def build_grouping_prompt(segments):
    return f"""
Você é um analista de transcrição para edição de vídeo.
Identifique quais segmentos são tentativas alternativas da MESMA fala ou ideia.
NÃO escolha ainda a melhor tentativa e NÃO monte a edição final.

CONTEXTO:
O apresentador grava a mesma fala várias vezes até acertar. As tentativas
podem estar no MESMO arquivo, uma logo depois da outra, ou em arquivos
diferentes (takes). Cada segmento tem timestamps locais ao arquivo em "source".

REGRAS:
1. Agrupe tentativas da mesma fala, explicação ou unidade de conteúdo, no
   mesmo arquivo ou em arquivos diferentes, mesmo com pequenas diferenças de
   palavras.
2. O critério é a intenção e o conteúdo, não igualdade literal do texto.
3. Não agrupe frases só porque começam com palavras iguais.
4. Não agrupe partes que são continuação natural uma da outra.
5. Não agrupe ideias diferentes só porque tratam do mesmo assunto.
6. Um grupo pode ter uma única entrada quando não houver alternativa.
7. Todo índice deve aparecer em pelo menos um grupo. Não invente índices.
8. Se um segmento termina uma ideia e começa a tentativa de outra (ex.:
   "...existe o Git. O Git é uma ferramenta,"), coloque o índice nos DOIS
   grupos.
9. Retorne os grupos na ordem do roteiro: a ordem em que cada ideia aparece
   pela primeira vez na gravação.
10. O label resume a fala/ideia comum do grupo.

Retorne somente JSON no schema solicitado.

TRANSCRIÇÃO INDEXADA:
{compact_json(indexed_segments(segments, with_words=False))}
"""


def build_selection_prompt(segments, groups):
    groups_with_segments = [
        {
            "group_index": group_index,
            "idea": group.label,
            "candidates": [
                {"index": i, **segments[i]} for i in group.segment_indices
            ],
        }
        for group_index, group in enumerate(groups)
    ]
    return f"""
Você é o editor responsável por montar a edição final de um vídeo gravado em
vários takes. A etapa anterior agrupou tentativas da mesma fala/ideia, na
ordem do roteiro. Compare as alternativas e escolha os trechos do vídeo.
{CUT_RULES}
17. Siga a ordem dos grupos. Um grupo pode ser descartado se não tiver
    conteúdo aproveitável.
18. Não selecione duas alternativas completas do mesmo grupo.
19. Um mesmo segmento pode aparecer em dois grupos: nesse caso use só a parte
    dele que pertence a cada ideia.

Retorne somente JSON com "clips" (segment_index, source, start, end).
A ordem dos clips é a ordem do vídeo final.

GRUPOS E CANDIDATOS:
{compact_json(groups_with_segments)}
"""


# =========================
# VALIDAÇÃO / REPARO
# =========================

def repair_grouping(grouping, segment_count):
    """Remove índices inválidos e adiciona como grupo próprio os que faltaram."""
    groups = []
    seen = set()
    for group in grouping.groups:
        indices = [i for i in group.segment_indices if 0 <= i < segment_count]
        if indices:
            groups.append(SemanticGroup(label=group.label, segment_indices=indices))
            seen.update(indices)

    missing = sorted(set(range(segment_count)) - seen)
    if len(missing) > segment_count * MAX_DROPPED_RATIO:
        raise ResponseRejected(f"Agrupamento deixou de fora {len(missing)} segmentos.")
    for index in missing:
        print(f"⚠️ Segmento {index} ficou sem grupo; adicionado sozinho.", flush=True)
        groups.append(SemanticGroup(label="(sem grupo)", segment_indices=[index]))
    return groups


def snap(value, candidates):
    nearest = min(candidates, key=lambda candidate: abs(candidate - value))
    if abs(nearest - value) > SNAP_TOLERANCE:
        return None
    return nearest


def find_segment_for_clip(clip, segments):
    """Segmento apontado pelo clip; se o índice estiver errado, procura pelo tempo."""
    if 0 <= clip.segment_index < len(segments):
        segment = segments[clip.segment_index]
        if (segment["source"] == clip.source
                and segment["start"] - SNAP_TOLERANCE <= clip.start <= segment["end"]):
            return segment
    for segment in segments:
        if (segment["source"] == clip.source
                and segment["start"] - SNAP_TOLERANCE <= clip.start < segment["end"]):
            return segment
    if 0 <= clip.segment_index < len(segments):
        return segments[clip.segment_index]
    return None


def repair_clips(clips, segments, chronological):
    """Encaixa timestamps em limites de palavra e remove clips inválidos/repetidos."""
    repaired = []
    dropped = 0

    for number, clip in enumerate(clips, 1):
        segment = find_segment_for_clip(clip, segments)
        if segment is None:
            print(f"⚠️ Clip {number}: segmento não encontrado; descartado.", flush=True)
            dropped += 1
            continue

        words = segment.get("words", [])
        allowed_starts = [segment["start"]] + [w["start"] for w in words if w.get("start") is not None]
        allowed_ends = [segment["end"]] + [w["end"] for w in words if w.get("end") is not None]
        start = snap(clip.start, allowed_starts)
        end = snap(clip.end, allowed_ends)
        if start is None or end is None or end <= start:
            print(
                f"⚠️ Clip {number}: [{clip.start} -> {clip.end}] não encaixa em "
                f"palavras de {segment['source']}; descartado.",
                flush=True,
            )
            dropped += 1
            continue
        repaired.append({"source": segment["source"], "start": start, "end": end})

    if chronological:
        repaired.sort(key=lambda clip: (clip["source"], clip["start"]))

    # Remove sobreposições: um trecho do vídeo original só entra uma vez.
    used = {}
    final = []
    for clip in repaired:
        intervals = used.setdefault(clip["source"], [])
        overlapping = [(s, e) for s, e in intervals if s < clip["end"] and clip["start"] < e]
        if overlapping:
            if any(s <= clip["start"] and clip["end"] <= e for s, e in overlapping):
                print(
                    f"⚠️ Clip {clip['source']} [{clip['start']} -> {clip['end']}] "
                    "repete trecho já usado; descartado.",
                    flush=True,
                )
                dropped += 1
                continue
            # Sobreposição parcial: começa depois do fim do trecho já usado.
            clip["start"] = max(e for _, e in overlapping)
            if clip["end"] - clip["start"] < 0.2:
                dropped += 1
                continue
        intervals.append((clip["start"], clip["end"]))
        final.append(clip)

    if not final:
        raise ResponseRejected("Nenhum clip válido na resposta.")
    if dropped > len(clips) * MAX_DROPPED_RATIO:
        raise ResponseRejected(f"{dropped} de {len(clips)} clips inválidos.")
    return [
        EditSegment(source=c["source"], start=round(c["start"], 3), end=round(c["end"], 3))
        for c in final
    ]


# =========================
# MAIN
# =========================

def main():
    if not PROJECT_NAME:
        raise ValueError("A variável PROJECT_NAME é obrigatória.")
    input_prefix = f"transcripts/{PROJECT_NAME}/"
    input_blob = find_input_transcription(input_prefix)
    input_uri = f"gs://{BUCKET_NAME}/{input_blob}"
    output_uri = f"gs://{BUCKET_NAME}/edits/{PROJECT_NAME}/edit.json"

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
    multi_source = len(sources) > 1
    print(f"📝 Segmentos recebidos: {len(segments)}", flush=True)
    print(f"🎬 Fontes: {', '.join(sources)}", flush=True)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY não configurada.")
    client = genai.Client(api_key=api_key)

    def parse_selection(text):
        selection = SelectionResult.model_validate_json(text)
        # Em vídeo único a ordem do arquivo é a ordem certa; não deixa o modelo embaralhar.
        return repair_clips(selection.clips, segments, chronological=not multi_source)

    if not multi_source:
        print("\n🎬 Vídeo único: montando a edição em uma etapa...", flush=True)
        final_segments = generate_with_models(
            client, build_single_prompt(segments, multi_source=False),
            SelectionResult, "Edição", parse_selection,
        )
    else:
        print("\n🔎 Etapa 1/2: agrupando falas equivalentes...", flush=True)
        groups = generate_with_models(
            client, build_grouping_prompt(segments),
            GroupingResult, "Agrupamento semântico",
            lambda text: repair_grouping(
                GroupingResult.model_validate_json(text), len(segments)
            ),
        )
        print(f"🧩 Grupos identificados: {len(groups)}", flush=True)
        for index, group in enumerate(groups, 1):
            group_sources = sorted({segments[i]["source"] for i in group.segment_indices})
            print(
                f"  Grupo {index}: {group.label} | "
                f"segmentos={group.segment_indices} | fontes={group_sources}",
                flush=True,
            )

        print("\n🎬 Etapa 2/2: comparando takes e montando a edição...", flush=True)
        final_segments = generate_with_models(
            client, build_selection_prompt(segments, groups),
            SelectionResult, "Seleção e montagem", parse_selection,
        )

    print("\n✂️ Seleção final:", flush=True)
    for index, segment in enumerate(final_segments, 1):
        print(
            f"  {index:03d}. {segment.source} "
            f"[{segment.start:.3f} -> {segment.end:.3f}]",
            flush=True,
        )

    result = EditResult(video=PROJECT_NAME, segments=final_segments)
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
