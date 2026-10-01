FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    google-genai \
    google-cloud-storage \
    pydantic

COPY editor.py .

CMD ["python", "-u", "editor.py"]
