import torch
import whisperx

print("🚀 Transcription worker")
print("PyTorch:", torch.__version__)
print("CUDA disponível:", torch.cuda.is_available())

if not torch.cuda.is_available():
    raise RuntimeError("GPU CUDA não está disponível")

print("GPU:", torch.cuda.get_device_name(0))
print("CUDA:", torch.version.cuda)

print("\n📦 Carregando WhisperX large-v3...")

device = "cuda"
compute_type = "float16"

model = whisperx.load_model(
    "large-v3",
    device,
    compute_type=compute_type,
    language="pt"
)

print("✅ Modelo carregado!")

print("\n🎙️ Carregando áudio...")

audio = whisperx.load_audio("test-audio.m4a")

print("🧠 Transcrevendo...")

result = model.transcribe(
    audio,
    batch_size=16,
    language="pt"
)

print("\n📝 TRANSCRIÇÃO")
print("=" * 60)

for segment in result["segments"]:
    start = segment["start"]
    end = segment["end"]
    text = segment["text"].strip()

    print(f"[{start:.3f} -> {end:.3f}] {text}")

print("=" * 60)
print("✅ Transcrição concluída!")