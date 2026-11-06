FROM python:3.12.8-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN pip install "fastapi==0.115.6" "uvicorn[standard]==0.34.0"

COPY . .

EXPOSE 9440
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9440"]
