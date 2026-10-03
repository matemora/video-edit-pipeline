# AI Video Edit Pipeline

Automated video editing pipeline that turns raw video and audio recordings into a final edited video using **FFmpeg, WhisperX and Gemini**.

The pipeline synchronizes external audio with video, detects speech blocks, generates word-level transcription timestamps, uses Gemini to decide which parts should remain, and renders the final result from those edit decisions.

---

## 🎯 Overview

The core idea is to separate the pipeline into independent workers, with each stage producing an artifact consumed by the next one:

```text
Raw Video + External Audio
          │
          ▼
┌─────────────────────┐
│    Sync Worker      │
│       FFmpeg        │
└──────────┬──────────┘
           │
           ▼
    synced/*.mp4
           │
           ▼
┌─────────────────────┐
│ Transcription Worker│
│      WhisperX       │
│   GPU / Cloud Run   │
└──────────┬──────────┘
           │
           ▼
   transcripts/*.json
           │
           ▼
┌─────────────────────┐
│   AI Editor Worker  │
│       Gemini        │
└──────────┬──────────┘
           │
           ▼
      edits/*.json
           │
           ▼
┌─────────────────────┐
│    Render Worker    │
│       FFmpeg        │
└──────────┬──────────┘
           │
           ▼
      final/*.mp4
```

Each worker has a single responsibility and communicates with the others through **Google Cloud Storage**.

---

## 💡 Project Motivation

Creating educational and technical video content involves much more than recording.

A typical recording session may contain multiple takes, mistakes, repetitions, long pauses, and separate audio or screen recordings. Manually reviewing and cutting this material is repetitive and time-consuming, especially when producing content regularly.

This project was created to explore a different approach:

> **What if AI could make the editing decisions while deterministic tools handled the actual media processing?**

Instead of asking an AI model to manipulate video directly, the pipeline separates **understanding** from **execution**.

The AI analyzes the transcription and determines **what should remain**. It produces a structured list of timestamps representing the desired edit. FFmpeg then deterministically applies those decisions to the original media.

This architecture provides a useful balance between AI flexibility and traditional software engineering principles:

```text
Human recording
      ↓
Synchronization
      ↓
Speech understanding
      ↓
AI edit decisions
      ↓
Deterministic rendering
      ↓
Final video
```

### Why build it?

The project serves as a practical exploration of several engineering challenges that emerge when combining AI with production software:

* **AI-assisted decision making** — using an LLM for semantic understanding rather than direct media manipulation.
* **Multimodal media processing** — synchronizing video, external audio and additional video sources.
* **GPU workloads** — running WhisperX and large speech models in Cloud Run with NVIDIA GPUs.
* **Precise temporal data** — using word-level timestamps to make edits at the exact boundaries required by the video.
* **Distributed processing** — breaking a computationally expensive workflow into independent Cloud Run Jobs.
* **Reproducible pipelines** — using intermediate JSON artifacts so each stage can be inspected, debugged and re-run independently.
* **Cost-aware architecture** — reserving GPU infrastructure for transcription while keeping synchronization, AI orchestration and rendering on CPU workloads.

The goal is not simply to automate video editing, but to experiment with an architecture where **AI handles the parts that require judgment and conventional software handles the parts that require precision and reliability**.

This project is also an exploration of a broader engineering principle:

> **AI doesn't have to replace deterministic software. It can decide what should happen, while deterministic systems remain responsible for making it happen correctly.**

---

## 🧠 How the Pipeline Works

### 1. Sync Worker

**Directory:** `sync-worker/`

The Sync Worker creates the authoritative timeline for the project.

It takes:

* Raw camera video
* External high-quality audio

and synchronizes the audio with the video.

Example:

```text
input/
├── video.mov
└── audio.m4a
```

Output:

```text
synced/
└── video_synced.mp4
```

The resulting `video_synced.mp4` becomes the **timeline reference for the entire pipeline**.

The transcription worker extracts audio from this synchronized video rather than using the original external audio directly.

---

### 2. Transcription Worker

**Directory:** `transcription-worker/`

The Transcription Worker analyzes the synchronized video and produces a structured transcription.

It performs the following operations:

1. Extracts the audio from `video_synced.mp4`.
2. Calculates the audio RMS profile.
3. Estimates the noise floor dynamically.
4. Calculates an automatic silence threshold.
5. Detects long pauses.
6. Splits the recording into independent speech blocks.
7. Transcribes each block with **WhisperX `large-v3`**.
8. Aligns the transcription at the word level.
9. Converts timestamps back to the original video timeline.

### Dynamic silence detection

The silence threshold is not hard-coded to a single dB value.

Instead:

```text
noise_floor = percentile(audio_db, 20)

silence_threshold = noise_floor + 7 dB
```

This allows the detection to adapt to different recording environments.

Current parameters:

```python
LONG_PAUSE = 0.5
MIN_SPEECH_DURATION = 0.5
PADDING = 0.10

NOISE_PERCENTILE = 20
MARGIN_DB = 7
```

### Word-level timestamps

The transcription includes timestamps for individual words.

Example:

```json
{
  "start": 61.627,
  "end": 71.740,
  "text": "Para ... existe o Git.",
  "words": [
    {
      "word": "Para",
      "start": 61.627,
      "end": 61.707
    }
  ]
}
```

These timestamps allow the AI Editor to make **precise partial cuts**, rather than being restricted to entire transcription segments.

Output:

```text
transcripts/
└── video_synced.json
```

---

### 3. AI Editor Worker

**Directory:** `ai-editor-worker/`

The AI Editor receives the transcription and determines what should remain in the final video.

It uses **Gemini** to:

* identify repeated takes;
* remove unnecessary repetitions;
* remove mistakes and incomplete attempts;
* select the best version of an idea;
* preserve the original narrative order;
* make precise cuts using word-level timestamps;
* avoid removing repetitions that are actually necessary for the explanation.

The AI does **not** generate a new script.

Instead, it produces a list of timestamps referencing the original video.

Example:

```json
{
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
}
```

This file is the **edit decision list** for the renderer.

Output:

```text
edits/
└── video_synced.json
```

---

### 4. Render Worker

**Directory:** `render-worker/`

The Render Worker applies the edit decisions using FFmpeg.

It reads:

* the synchronized video from `synced/`;
* the edit decisions from `edits/`.

It then:

1. Cuts each selected time range.
2. Concatenates the selected segments.
3. Re-encodes the result.
4. Preserves the original audio when available.
5. Supports videos without audio, such as screen recordings.
6. Uploads the final video to `final/`.

Output:

```text
final/
└── video_synced.mp4
```

The `edit.json` therefore acts as a **timeline of edit decisions**, independent from the rendering implementation.

---

## 🗄️ Google Cloud Storage

The workers communicate through a shared GCS bucket.

Default bucket:

```text
gs://ai-video-editor-510018
```

Structure:

```text
gs://ai-video-editor-510018/
│
├── input/
│   ├── video.mov
│   └── audio.m4a
│
├── synced/
│   └── video_synced.mp4
│
├── transcripts/
│   └── video_synced.json
│
├── edits/
│   └── video_synced.json
│
└── final/
    └── video_synced.mp4
```

### Directory responsibilities

| Directory      | Purpose                                           |
| -------------- | ------------------------------------------------- |
| `input/`       | Raw video and external audio                      |
| `synced/`      | Synchronization output and authoritative timeline |
| `transcripts/` | WhisperX transcription with word timestamps       |
| `edits/`       | Gemini edit decisions                             |
| `final/`       | Rendered final videos                             |

---

## 🏗️ Project Structure

```text
ai-video-editor/
│
├── sync-worker/
│   ├── Dockerfile
│   └── sync.py
│
├── transcription-worker/
│   ├── Dockerfile
│   └── transcription.py
│
├── ai-editor-worker/
│   ├── Dockerfile
│   └── editor.py
│
└── render-worker/
    ├── Dockerfile
    └── renderer.py
```

Each directory contains an independently deployable Cloud Run Job.

---

## ☁️ Google Cloud Architecture

The pipeline runs primarily as **Cloud Run Jobs**.

### Sync Worker

```text
Cloud Run Job
Region: southamerica-east1
CPU workload
FFmpeg
```

### Transcription Worker

```text
Cloud Run Job
Region: europe-west1
NVIDIA L4 GPU
WhisperX
CUDA / PyTorch
```

The GPU is required because WhisperX `large-v3` is used for transcription.

### AI Editor Worker

```text
Cloud Run Job
Region: southamerica-east1
Gemini API
```

The Gemini API key is provided through **Google Secret Manager**.

### Render Worker

```text
Cloud Run Job
Region: southamerica-east1
CPU workload
FFmpeg
```

---

## 📋 Requirements

### Local development

* Python 3.10+
* FFmpeg
* Google Cloud CLI
* Google Cloud Storage access
* NVIDIA GPU + CUDA for local WhisperX execution

### Cloud execution

* Google Cloud Project
* Cloud Run
* Cloud Storage
* Artifact Registry
* Secret Manager
* NVIDIA GPU quota for the transcription worker

---

## 🔐 Environment

The AI Editor uses a Gemini API key.

For local development:

```bash
export GEMINI_API_KEY="your-api-key-here"
```

Google Cloud credentials can be configured locally with:

```bash
gcloud auth application-default login
```

In Cloud Run, the Gemini API key should be provided through **Secret Manager** rather than being hard-coded into the container.

---

## ▶️ Running the Pipeline

The workers are executed sequentially.

### 1. Synchronize Video and Audio

Local:

```bash
python sync-worker/sync.py
```

Cloud Run:

```bash
gcloud run jobs execute sync-worker \
  --region=southamerica-east1 \
  --update-env-vars="BUCKET=ai-video-editor-510018,VIDEO_INPUT=gs://ai-video-editor-510018/input/video.mov,AUDIO_INPUT=gs://ai-video-editor-510018/input/audio.m4a,OUTPUT=gs://ai-video-editor-510018/synced/video_synced.mp4" \
  --wait
```

---

### 2. Transcribe

```bash
python transcription-worker/transcription.py
```

The worker generates:

```text
transcripts/video_synced.json
```

---

### 3. Generate Edit Decisions

```bash
python ai-editor-worker/editor.py
```

The worker generates:

```text
edits/video_synced.json
```

---

### 4. Render

```bash
python render-worker/renderer.py
```

The final video is generated in:

```text
final/video_synced.mp4
```

---

## 🎬 Multi-Camera / Screen Recording Support

The edit decisions are based on timestamps, so the same `edit.json` can be applied to additional video sources **as long as they share the same timeline**.

For example:

```text
synced/
├── camera.mp4
├── screen.mp4
└── camera-2.mp4
```

If these videos:

* start at the same point;
* have the same temporal duration;
* are synchronized with each other;

the same edit decisions can be applied to all of them.

A video without an audio track is also supported by the Render Worker.

This makes it possible to combine:

```text
Camera
Screen recording
Second camera
Other synchronized angles
```

using a single set of AI-generated edit decisions.

---

## 🔄 Pipeline Artifacts

Each stage produces a deterministic artifact that can be inspected independently:

```text
video_synced.mp4
        ↓
transcription.json
        ↓
edit.json
        ↓
final.mp4
```

This separation makes the system easier to debug, test and evolve.

For example, the AI Editor can be re-run with the same transcription without repeating the expensive WhisperX transcription step.

Likewise, the renderer can be re-run with a different `edit.json` without running transcription or Gemini again.

---

## 🛠️ Design Principles

The pipeline follows a few core principles:

### Separation of responsibilities

Each worker performs one major task:

```text
Sync → Transcribe → Decide → Render
```

### Timeline as the source of truth

`video_synced.mp4` defines the timeline used by transcription and editing.

### AI decides, FFmpeg executes

Gemini decides **which timestamps should remain**.

FFmpeg performs the actual media manipulation.

### Reproducible intermediate artifacts

Every stage produces a file that can be inspected, replaced or regenerated independently.

### GPU only where necessary

WhisperX uses a GPU because transcription is computationally expensive.

The remaining workers use CPU-based Cloud Run Jobs.

---

## 🚧 Current Status

| Component                     | Status         |
| ----------------------------- | -------------- |
| Audio/video synchronization   | ✅ Working      |
| Dynamic silence detection     | ✅ Validated    |
| WhisperX transcription        | ✅ Working      |
| Word-level alignment          | ✅ Working      |
| Gemini edit decisions         | ✅ Working      |
| Partial word-level cuts       | ✅ Working      |
| Video rendering               | ✅ Working      |
| Video without audio           | ✅ Working      |
| Synchronized secondary video  | ✅ Validated    |
| Full production orchestration | 🚧 In progress |

---

## 🔮 Next Steps

Potential next improvements include:

* automatically triggering the next worker after each stage;
* supporting multiple synchronized videos as first-class inputs;
* improving edit decision validation;
* adding job status and error handling;
* creating a single pipeline command/API;
* storing pipeline metadata and execution history;
* adding automatic cleanup of intermediate files.
