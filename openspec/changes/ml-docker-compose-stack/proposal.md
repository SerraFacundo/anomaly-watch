## Why

Anomaly Watch is two repos that run side by side on the same host: the product stack (`Anomaly-Watch`: `db`, `redis`, `api`, `web`, `backup`) and this ML repo. With #28 (`report-pipeline-latency`, open on its own branch) they start talking to each other — `tools/replay.py --report-metrics` posts latency windows to the AnomalyWatch API. Today only the product half can be brought up with one command; this half has a `docker-compose.yml` that was written before anything could verify it, and it does not hold up next to the other stack:

- **Nothing reaches the detector from the host.** `detector` only `expose`s 8003, but the demo path (`replay.py` → `http://localhost:8003`) runs on the host. The documented flow does not work against the compose stack.
- **A silent collision waits on 5432.** AnomalyWatch publishes its Postgres on host port `5432`. `replay.py --store-postgres` defaults to `postgresql://…@localhost:5432/anomaly_watch`, so the day this stack's Postgres is published on the obvious port, one of the two fails to start — and while it is not, the default DSN points at the *product* database.
- **The detector image is neither minimal nor reproducible.** Single stage, runs as root, pulls `uv:latest`, ignores `uv.lock` and re-lists dependencies by hand, and there is no `.dockerignore`, so every build ships `.venv/` and the ~420 MB `models/cache/` to the daemon as build context.
- **It does not follow the practices the product stack already settled on**: `.env` + `.env.example` instead of hardcoded credentials, `restart: unless-stopped`, healthchecks gating `depends_on`, non-root users in multi-stage images, and one-off containers under a `tools` profile (`docker compose run --rm …`).

Now is the moment: the latency KPI (#28) is the first feature that needs both stacks up at once, and the model layer is stable enough (promoted `modernbert-ft`) to be packaged.

## What Changes

- **Coexistence contract with the product stack**: fixed compose project name `anomaly-watch-ml`, and a host-port allocation that never overlaps AnomalyWatch's (`5432`, `6379`, `8080`, `3000`, plus MailHog's `8025`). Only what a developer needs from the host is published, bound to `127.0.0.1`, on configurable ports: detector on `8003`, Postgres on `5433`.
- **Multi-stage, non-root, lockfile-driven detector image**: a builder stage resolves the `detector` extra from `uv.lock` (CPU-only torch) into a virtualenv; the runtime stage is `python:3.12-slim` with only that venv and the files the service imports, running as UID `10001`. `uv` is pinned, not `latest`.
- **New `replay` service under the `tools` profile**, with its own small multi-stage image (Drain3 + `httpx` + `psycopg`, no torch). It runs the demo path entirely inside the stack's network: `docker compose run --rm replay data/samples/HDFS_2k.log --store-postgres`. It can reach the AnomalyWatch API through the host gateway without joining the product stack's network, so `--report-metrics` works from inside the stack as soon as #28 is merged; this change does not depend on #28 landing first.
- **Minimal Postgres image**: `postgres:16-alpine` — the same image the product stack uses — instead of `pgvector/pgvector:pg16`, since nothing uses pgvector yet.
- **`.env.example` with `ML_`-prefixed variables** for credentials, host ports, detector tuning and the AnomalyWatch endpoint/token; `.env` stays ignored.
- **`.dockerignore`** that keeps `.venv/`, model weights, datasets, training runs and VCS metadata out of the build context.
- **`replay.py` default DSN** reads `ANOMALY_WATCH_ML_DSN` and falls back to `localhost:5433`, never to the product database's port. **BREAKING** for anyone relying on the implicit `localhost:5432` default: pass `--dsn` or set the variable.
- **New `replay` extra** in `pyproject.toml` (`httpx`, `psycopg[binary]`) so the replay image does not install the whole `ml` extra (datasets, scikit-learn).
- **Docs**: README section to bring the stack up next to AnomalyWatch, with the port table.

## Capabilities

### New Capabilities
- `container-stack`: how this repo is packaged and run with Docker Compose — images, services, configuration, host exposure, and how it coexists and communicates with the AnomalyWatch stack on the same host.

### Modified Capabilities
- None. `openspec/specs/` is empty; the deployment topology is still described only in the unarchived `architecture-docs` change. The DSN default change is part of `container-stack` (it exists to keep the two stacks apart), not a change to `latency-reporting`.

## Impact

- **Files**: `docker-compose.yml` (rewritten), `services/detector/Dockerfile` (rewritten), new `tools/Dockerfile`, new `.dockerignore`, new `.env.example`, `pyproject.toml` + `uv.lock` (new `replay` extra), `tools/replay.py` (DSN default only), `README.md`, `ARCHITECTURE.md` §deployment table (Postgres image, host ports).
- **Dependencies**: no new runtime libraries; `uv` image pinned to a specific version.
- **Other repo**: `Anomaly-Watch` is **not** modified. The only coupling is its published API port (`8080`) and, once #28 is merged, the shared `PIPELINE_SERVICE_TOKEN` value.
- **Branching**: built from `main`, independent of `feature/report-pipeline-latency`. Whichever merges second resolves small overlaps in `tools/replay.py` (argument block), `pyproject.toml`/`uv.lock` (extras) and `README.md`.
- **Data**: the existing `postgres-data` volume was created by a Debian-based image; switching to Alpine changes the collation provider. The demo data is regenerable with `replay`, so the volume is recreated rather than migrated.
- **Models**: weights stay out of the image and are mounted read-only. `modernbert-ft` is still a repo-relative local path (`ml/train/runs/…`) that does not resolve inside the container; the stack keeps defaulting to a model key that does, until the checkpoint is moved into the model cache.
- **CI**: a `docker compose config` validation step is added; building the torch image in CI is left out on purpose (time/cost).
