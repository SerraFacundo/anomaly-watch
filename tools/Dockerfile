# syntax=docker/dockerfile:1
#
# replay — one-shot demo transport (Drain3 -> detector-service -> Postgres).
#
# Same builder/runtime pattern as services/detector/Dockerfile, but with the
# `replay` extra (httpx + psycopg) instead of `detector`: no torch, no
# transformers, this image only ever speaks HTTP and SQL.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra replay

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY tools ./tools
COPY data/samples ./data/samples
RUN chmod +x ./tools/entrypoint.sh

USER 10001:10001

ENTRYPOINT ["./tools/entrypoint.sh"]
