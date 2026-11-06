# Build context is ./services so the synthetic generator (which needs the
# adapters and the metrics provider) ships in the same image as the collector.
FROM python:3.12.8-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY collector/requirements.txt ./
RUN pip install -r requirements.txt

COPY collector/ ./
COPY synthetic/ ./synthetic/

EXPOSE 5000

# One worker: APScheduler runs in-process and two workers would double every poll.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", \
     "--timeout", "300", "app.wsgi:app"]
