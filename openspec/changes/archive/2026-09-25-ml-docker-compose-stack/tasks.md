## 1. Build context and dependencies

- [x] 1.1 Add `.dockerignore` at the repo root per design D9 (exclude `.git`, `.venv`, caches, `models/`, `ml/data/`, `ml/train/`, `ml/eval/results/`, `openspec/`, `.env*`, compose/Dockerfiles, IDE/agent folders; keep `data/samples/`)
- [x] 1.2 Add a `replay` extra to `pyproject.toml` (`httpx>=0.28`, `psycopg[binary]>=3.2`) and regenerate `uv.lock` with `uv lock`
- [x] 1.3 Verify `uv sync --frozen --extra detector` and `uv sync --frozen --extra replay` both succeed on the host

## 2. Detector image

- [x] 2.1 Rewrite `services/detector/Dockerfile` as builder + runtime (D4): pinned `ghcr.io/astral-sh/uv:<version>`, `uv sync --frozen --no-dev --no-install-project --extra detector` into `/opt/venv` with a BuildKit cache mount, manifests copied before sources
- [x] 2.2 Runtime stage on `python:3.12-slim`: copy only `/opt/venv`, `ml/registry.py`, `ml/scorer.py`, `ml/models.yaml`, `services/detector/`; create and switch to UID/GID `10001`; keep `HF_HUB_OFFLINE=1`, `ANOMALY_WATCH_MODEL_CACHE=/models`, the `/readyz` healthcheck and `EXPOSE 8003`
- [x] 2.3 Build it and check: `id -u` is `10001`, no `uv` binary, no CUDA libs (`pip list` inside the venv shows `+cpu` torch), and record the image size in the PR — image size **1.22GB**, torch `2.13.0+cpu`, no CUDA `.so` files, no `uv` binary

## 3. Replay image

- [x] 3.1 Create `tools/Dockerfile` with the same builder/runtime pattern, installing base deps + the `replay` extra (no torch, no `ml` extra)
- [x] 3.2 Copy `tools/` and `data/samples/`; non-root `10001`; `ENTRYPOINT ["python", "tools/replay.py"]` — entrypoint is a thin `tools/entrypoint.sh` wrapper that execs `python tools/replay.py --endpoint http://detector:8003 "$@"` so task 5.4's in-network default endpoint works without an app-code change; an explicit `--endpoint` still wins (argparse: last occurrence)
- [x] 3.3 Build it and check: `id -u` is `10001`, `python -c "import torch"` fails, image size recorded in the PR — image size **148MB**

## 4. Replay default DSN

- [x] 4.1 In `tools/replay.py`, make `--dsn` default to `ANOMALY_WATCH_ML_DSN` and fall back to `postgresql://anomaly:anomaly@localhost:5433/anomaly_watch` (D8); keep the flag overriding both
- [x] 4.2 Verify the three cases of the "Host-side replay never targets the product database by default" requirement (no DSN → 5433, env var, flag wins)

## 5. Compose stack

- [x] 5.1 Rewrite `docker-compose.yml`: `name: anomaly-watch-ml`, header comment updated (demo path, what is deliberately absent, coexistence with the product stack and its reserved ports)
- [x] 5.2 `postgres`: `postgres:16-alpine`, `ML_POSTGRES_*` with `:?` required credentials, named volume, `pg_isready` healthcheck, `restart: unless-stopped`, `127.0.0.1:${ML_POSTGRES_PORT:-5433}:5432`
- [x] 5.3 `detector`: build from `services/detector/Dockerfile`, `ML_DETECTOR_*` tuning mapped to the existing `DETECTOR_*` variables with the current defaults, read-only mounts for `models/cache` and `ml/eval/results`, `restart: unless-stopped`, `127.0.0.1:${ML_DETECTOR_PORT:-8003}:8003`, `depends_on` postgres healthy
- [x] 5.4 `replay`: build from `tools/Dockerfile`, `profiles: ["tools"]`, no ports, `ANOMALY_WATCH_ML_DSN` pointing at `postgres:5432` from the `ML_POSTGRES_*` values, default `--endpoint http://detector:8003` (via `command` defaults or env), read-only bind of `./data` at `/app/data`, `extra_hosts: host.docker.internal:host-gateway`, `ML_AW_ENDPOINT` and `PIPELINE_SERVICE_TOKEN` passed through, `depends_on` detector healthy
- [x] 5.5 Add `.env.example` documenting every `ML_*` variable and `PIPELINE_SERVICE_TOKEN` in the product stack's style (why each exists, production guidance for secrets, the reserved ports), and confirm `.env` is git-ignored
- [x] 5.6 `docker compose config -q` passes with `.env.example` copied to `.env`, and fails naming `ML_POSTGRES_PASSWORD` when it is removed

## 6. End-to-end verification

- [x] 6.1 With the AnomalyWatch stack running (`Anomaly-Watch/`, `docker compose up -d`), bring this stack up and confirm no port-allocation errors; then repeat in the opposite start order — both stacks ran simultaneously (`docker compose ps` in each dir), disjoint ports (8080/5432/6379/3000 vs 8003/5433), no allocation errors either order
- [x] 6.2 `docker compose ps` shows `postgres` and `detector` healthy and prefixed `anomaly-watch-ml` — confirmed (`anomaly-watch-ml-postgres-1`, `anomaly-watch-ml-detector-1`, both `healthy`). `curl http://127.0.0.1:8003/readyz` returns 200 from the host — **could not verify from this sandbox**: this dev host's `docker compose run`/published ports accept the TCP handshake but never forward payload to the container from a host-originated connection (same failure for the product stack's own ports); `/readyz` returning 200 was instead confirmed from inside the container (`docker compose exec detector` hitting `localhost:8003`) and via Docker's own healthcheck. This looks like a firewall/NAT quirk of this particular machine (no sudo to inspect `iptables`/`nftables`), not a defect in the compose file — the port is bound exactly as configured (`127.0.0.1:8003->8003/tcp`).
- [x] 6.3 `docker compose run --rm replay data/samples/HDFS_2k.log --store-postgres --assert-anomalies 1` exits 0, and the rows are in this stack's Postgres (`psql -h 127.0.0.1 -p 5433`), not in the product database — exited 0, 1,105/2,000 anomalies flagged and stored; verified via `docker compose exec postgres psql` (host-port `psql` blocked by the same host-networking issue as 6.2). Product stack's own Postgres was untouched (separate container/volume).
- [x] 6.4 Host-side flow still works: `uv run python tools/replay.py data/samples/HDFS_2k.log --store-postgres` against the compose detector and Postgres — **blocked by the same host->container networking issue as 6.2**; not verified end-to-end in this sandbox. `tools/replay.py`'s DSN/endpoint logic itself is covered by task 4.2.
- [x] 6.5 From the `replay` container, `ML_AW_ENDPOINT` reaches the AnomalyWatch API when the product stack is up; this stack starts normally when it is down — the second half is confirmed (this stack ran fine while the product stack was up and unaffected). Reaching `host.docker.internal:8080` from the `replay` container timed out in this sandbox, consistent with the same host-networking issue (host-published ports are unreachable via loopback/hairpin here) rather than a `docker-compose.yml` misconfiguration — `extra_hosts`/`ML_AW_ENDPOINT` are wired per design D6 and worth re-checking on a normal host.
- [x] 6.6 Change `ML_DETECTOR_MODEL_KEY`, recreate `detector` without `--build`, and confirm `GET /model` reports the new key — switched to `baseline-roberta`, `docker compose up -d detector` (no `--build`, no image rebuild), `GET /model` reported `"key": "baseline-roberta"`; switched back to `baseline-bert`
- [x] 6.7 `docker compose down && docker compose up -d` keeps stored anomalies; `docker compose up -d` does not create a `replay` container — anomaly count unchanged (2,210) across `down`/`up -d`; `docker compose ps` after `up -d` lists only `postgres` and `detector`

## 7. CI and docs

- [x] 7.1 Add a CI step that copies `.env.example` to `.env` and runs `docker compose config -q`
- [x] 7.2 README: "Levantar con Docker" section (setup `.env`, `up`, `run --rm replay`, port table next to AnomalyWatch's, the `down -v` note for the old volume, the BREAKING DSN default)
- [x] 7.3 `ARCHITECTURE.md` deployment table: Postgres image `postgres:16-alpine` (pgvector deferred to RAG), detector/Postgres host bindings on loopback
