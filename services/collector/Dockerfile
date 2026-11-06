# log-collector — Drain3 parsing, score cache, persistence.
#
# Single replica. Drain3 learns templates online, so a second instance would
# build a divergent template tree and the same log line would get a different
# template_id depending on which one saw it. See design.md D5.
#
# No torch: scoring is an HTTP call to detector-service, which is what makes a
# model swap a config change instead of a rebuild of this image.
FROM docker.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COLLECTOR_SPOOL_DIR=/spool

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
RUN uv pip install --system --no-cache \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" \
    "psycopg[binary]>=3.2" "httpx>=0.28" "drain3>=0.9.11" "redis>=5.0"

COPY services/common ./services/common
COPY services/collector ./services/collector
# The parser is shared with the offline replay path, and the severity keywords
# are the same ones model evaluation uses — neither gets a second copy here.
COPY tools/parser.py ./tools/
COPY ml/eval/baselines.py ./ml/eval/

EXPOSE 8005

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8005/readyz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "services.collector.app:app", "--host", "0.0.0.0", "--port", "8005"]
