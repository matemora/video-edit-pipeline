FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir google-cloud-storage

COPY sync.py .

CMD ["python", "sync.py"]
