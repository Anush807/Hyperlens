FROM python:3.12.8-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

# torch first, from the CPU wheel index: the default index pulls the CUDA
# runtime and adds ~2GB to an image that only ever trains on CPU.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Migrations run before the server so a cold volume comes up schema-complete.
CMD ["bash", "-lc", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
