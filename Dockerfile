FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src /app/src
COPY prompts /app/prompts
COPY README.md /app/README.md

ENV PYTHONUNBUFFERED=1

CMD ["bash", "-lc", "uvicorn src.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
