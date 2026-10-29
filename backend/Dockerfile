FROM python:3.13-slim-bookworm

ARG PIP_NO_CACHE_DIR=1

ENV APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/models/sentence-transformers

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser knowledge ./knowledge
COPY --chown=appuser:appuser run.py ./

RUN mkdir -p /app/data /models/huggingface /models/sentence-transformers \
    && chown -R appuser:appuser /app/data /models

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=12 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
