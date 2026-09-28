FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY sync.py .

CMD ["python", "sync.py"]
