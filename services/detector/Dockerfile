# detector-service — CPU-only anomaly scoring.
#
# Model weights are NOT baked into the image. They are mounted from a cache
# volume so the image stays small and a model swap never needs a rebuild.
FROM docker.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # No runtime egress: the deployed stack serves exactly the weights that were
    # evaluated, and log data cannot leak to the Hub.
    HF_HUB_OFFLINE=1 \
    ANOMALY_WATCH_MODEL_CACHE=/models

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
RUN uv pip install --system --no-cache \
    "torch>=2.4" --index-url https://download.pytorch.org/whl/cpu \
 && uv pip install --system --no-cache \
    "transformers>=4.48,<5" "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" "pyyaml>=6.0"

# Only what the service actually imports.
COPY ml/registry.py ml/scorer.py ml/models.yaml ./ml/
COPY services/detector ./services/detector

EXPOSE 8003

HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8003/readyz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "services.detector.app:app", "--host", "0.0.0.0", "--port", "8003"]
