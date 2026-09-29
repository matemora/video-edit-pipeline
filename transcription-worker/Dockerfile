FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

WORKDIR /app

RUN pip install --no-cache-dir \
    whisperx

COPY transcription.py .

CMD ["python", "transcription.py"]