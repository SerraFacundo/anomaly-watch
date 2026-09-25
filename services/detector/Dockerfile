# syntax=docker/dockerfile:1
#
# detector-service — CPU-only anomaly scoring.
#
# Model weights are NOT baked into the image. They are mounted from a cache
# volume so the image stays small and a model swap never needs a rebuild.
#
# Builder: resolves the `detector` extra from uv.lock (CPU-only torch) into a
# standalone venv. Runtime: only that venv and the files the service imports,
# non-root. No uv, no compiler, no pip cache ships in the final image.

FROM python:3.12-slim AS builder

# Pinned, not `latest`: a build today and a build next year must resolve the
# same way.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Manifests before sources: this layer is cached until a dependency changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra detector

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # No runtime egress: the deployed stack serves exactly the weights that were
    # evaluated, and log data cannot leak to the Hub.
    HF_HUB_OFFLINE=1 \
    ANOMALY_WATCH_MODEL_CACHE=/models \
    PATH="/opt/venv/bin:${PATH}"

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Only what the service actually imports.
COPY ml/registry.py ml/scorer.py ml/models.yaml ./ml/
COPY services/detector ./services/detector

USER 10001:10001

EXPOSE 8003

HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8003/readyz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "services.detector.app:app", "--host", "0.0.0.0", "--port", "8003"]
