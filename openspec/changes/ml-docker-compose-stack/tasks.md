## 1. Build context and dependencies

- [ ] 1.1 Add `.dockerignore` at the repo root per design D9 (exclude `.git`, `.venv`, caches, `models/`, `ml/data/`, `ml/train/`, `ml/eval/results/`, `openspec/`, `.env*`, compose/Dockerfiles, IDE/agent folders; keep `data/samples/`)
- [ ] 1.2 Add a `replay` extra to `pyproject.toml` (`httpx>=0.28`, `psycopg[binary]>=3.2`) and regenerate `uv.lock` with `uv lock`
- [ ] 1.3 Verify `uv sync --frozen --extra detector` and `uv sync --frozen --extra replay` both succeed on the host

## 2. Detector image

- [ ] 2.1 Rewrite `services/detector/Dockerfile` as builder + runtime (D4): pinned `ghcr.io/astral-sh/uv:<version>`, `uv sync --frozen --no-dev --no-install-project --extra detector` into `/opt/venv` with a BuildKit cache mount, manifests copied before sources
- [ ] 2.2 Runtime stage on `python:3.12-slim`: copy only `/opt/venv`, `ml/registry.py`, `ml/scorer.py`, `ml/models.yaml`, `services/detector/`; create and switch to UID/GID `10001`; keep `HF_HUB_OFFLINE=1`, `ANOMALY_WATCH_MODEL_CACHE=/models`, the `/readyz` healthcheck and `EXPOSE 8003`
- [ ] 2.3 Build it and check: `id -u` is `10001`, no `uv` binary, no CUDA libs (`pip list` inside the venv shows `+cpu` torch), and record the image size in the PR

## 3. Replay image

- [ ] 3.1 Create `tools/Dockerfile` with the same builder/runtime pattern, installing base deps + the `replay` extra (no torch, no `ml` extra)
- [ ] 3.2 Copy `tools/` and `data/samples/`; non-root `10001`; `ENTRYPOINT ["python", "tools/replay.py"]`
- [ ] 3.3 Build it and check: `id -u` is `10001`, `python -c "import torch"` fails, image size recorded in the PR

## 4. Replay default DSN

- [ ] 4.1 In `tools/replay.py`, make `--dsn` default to `ANOMALY_WATCH_ML_DSN` and fall back to `postgresql://anomaly:anomaly@localhost:5433/anomaly_watch` (D8); keep the flag overriding both
- [ ] 4.2 Verify the three cases of the "Host-side replay never targets the product database by default" requirement (no DSN → 5433, env var, flag wins)

## 5. Compose stack

- [ ] 5.1 Rewrite `docker-compose.yml`: `name: anomaly-watch-ml`, header comment updated (demo path, what is deliberately absent, coexistence with the product stack and its reserved ports)
- [ ] 5.2 `postgres`: `postgres:16-alpine`, `ML_POSTGRES_*` with `:?` required credentials, named volume, `pg_isready` healthcheck, `restart: unless-stopped`, `127.0.0.1:${ML_POSTGRES_PORT:-5433}:5432`
- [ ] 5.3 `detector`: build from `services/detector/Dockerfile`, `ML_DETECTOR_*` tuning mapped to the existing `DETECTOR_*` variables with the current defaults, read-only mounts for `models/cache` and `ml/eval/results`, `restart: unless-stopped`, `127.0.0.1:${ML_DETECTOR_PORT:-8003}:8003`, `depends_on` postgres healthy
- [ ] 5.4 `replay`: build from `tools/Dockerfile`, `profiles: ["tools"]`, no ports, `ANOMALY_WATCH_ML_DSN` pointing at `postgres:5432` from the `ML_POSTGRES_*` values, default `--endpoint http://detector:8003` (via `command` defaults or env), read-only bind of `./data` at `/app/data`, `extra_hosts: host.docker.internal:host-gateway`, `ML_AW_ENDPOINT` and `PIPELINE_SERVICE_TOKEN` passed through, `depends_on` detector healthy
- [ ] 5.5 Add `.env.example` documenting every `ML_*` variable and `PIPELINE_SERVICE_TOKEN` in the product stack's style (why each exists, production guidance for secrets, the reserved ports), and confirm `.env` is git-ignored
- [ ] 5.6 `docker compose config -q` passes with `.env.example` copied to `.env`, and fails naming `ML_POSTGRES_PASSWORD` when it is removed

## 6. End-to-end verification

- [ ] 6.1 With the AnomalyWatch stack running (`Anomaly-Watch/`, `docker compose up -d`), bring this stack up and confirm no port-allocation errors; then repeat in the opposite start order
- [ ] 6.2 `docker compose ps` shows `postgres` and `detector` healthy and prefixed `anomaly-watch-ml`; `curl http://127.0.0.1:8003/readyz` returns 200 from the host
- [ ] 6.3 `docker compose run --rm replay data/samples/HDFS_2k.log --store-postgres --assert-anomalies 1` exits 0, and the rows are in this stack's Postgres (`psql -h 127.0.0.1 -p 5433`), not in the product database
- [ ] 6.4 Host-side flow still works: `uv run python tools/replay.py data/samples/HDFS_2k.log --store-postgres` against the compose detector and Postgres
- [ ] 6.5 From the `replay` container, `ML_AW_ENDPOINT` reaches the AnomalyWatch API when the product stack is up; this stack starts normally when it is down
- [ ] 6.6 Change `ML_DETECTOR_MODEL_KEY`, recreate `detector` without `--build`, and confirm `GET /model` reports the new key
- [ ] 6.7 `docker compose down && docker compose up -d` keeps stored anomalies; `docker compose up -d` does not create a `replay` container

## 7. CI and docs

- [ ] 7.1 Add a CI step that copies `.env.example` to `.env` and runs `docker compose config -q`
- [ ] 7.2 README: "Levantar con Docker" section (setup `.env`, `up`, `run --rm replay`, port table next to AnomalyWatch's, the `down -v` note for the old volume, the BREAKING DSN default)
- [ ] 7.3 `ARCHITECTURE.md` deployment table: Postgres image `postgres:16-alpine` (pgvector deferred to RAG), detector/Postgres host bindings on loopback
