FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    google-cloud-storage

COPY renderer.py .

CMD ["python", "-u", "renderer.py"]