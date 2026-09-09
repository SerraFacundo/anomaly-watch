# ingest-service — HTTP entry point for log agents.
#
# No model, no torch. This image exists to be small and to start fast: it is the
# first thing an agent talks to and the last thing that should be slow to
# restart during an incident.
FROM docker.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    INGEST_SPOOL_DIR=/spool

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
RUN uv pip install --system --no-cache \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" "psycopg[binary]>=3.2"

# Only what the service imports. The wire contract is shared with the collector,
# so it is copied rather than duplicated.
COPY services/common ./services/common
COPY services/ingest ./services/ingest

EXPOSE 8004

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8004/readyz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "services.ingest.app:app", "--host", "0.0.0.0", "--port", "8004"]
