## Context

Two compose stacks share one host (a developer machine today, a single PyME host later):

| Stack | Project name | Host ports published today |
|---|---|---|
| `Anomaly-Watch` (product) | `anomaly-watch` (from its directory) | `5432` db, `6379` redis, `8080` api, `${FRONTEND_PORT:-3000}` web; `8025` MailHog when used for testing |
| `anomaly-watch-ml` (this repo) | implicit, from the directory name | none |

The product stack is the reference for conventions. What it does, and this change adopts:

- Required values come from `.env`, documented in a commented `.env.example`; `.env` is git-ignored.
- `restart: unless-stopped` on long-running services, so a transient failure at startup does not leave a container `Exited` for good.
- Healthchecks on stateful dependencies, and `depends_on: condition: service_healthy`.
- Multi-stage builds whose final stage carries only the runtime (`web`: Node builds, nginx serves; `backup`: Alpine builder copies the minimal set of binaries plus `ldd` closure), non-root `USER 10001:10001` in custom images.
- `.dockerignore` per build context.
- One-off containers live under `profiles: ["tools"]` and are run with `docker compose run --rm <svc>`; they reuse images rather than adding builds where possible.
- Comments in compose explain *why* a setting exists, not what it does.

What this repo has today (on `main`): a `docker-compose.yml` with `postgres` (`pgvector/pgvector:pg16`, hardcoded `anomaly/anomaly`) and `detector` (only `expose: 8003`), a single-stage detector Dockerfile that runs as root, uses `uv:latest` and installs dependencies by name instead of from `uv.lock`, and no `.dockerignore` (the build context includes `.venv/` and ~420 MB of `models/cache/`). The demo transport `tools/replay.py` runs on the host and defaults to `localhost:8003` for the detector and `localhost:5432` for Postgres — the latter being the product database's port.

`feature/report-pipeline-latency` (#28) adds `--report-metrics`, which makes `replay.py` call the AnomalyWatch API. It is not on `main` yet; this change is built from `main` and must not depend on it.

## Goals / Non-Goals

**Goals:**
- `docker compose up -d --build` in this repo brings up the ML half with zero port conflicts, whether or not the product stack is running, in either start order.
- Images as small as the workload allows, built reproducibly from `uv.lock`, running as non-root.
- The demo path runs end to end from inside the stack, and from the host with `uv run` as before.
- The replay container can reach the AnomalyWatch API with no change to the `Anomaly-Watch` repo.
- Same conventions as the product stack, so someone who knows one knows the other.

**Non-Goals:**
- The target topology in `ARCHITECTURE.md` (Kafka, Ollama, rag-engine, notifier, Nginx). Services without an implementation stay out, as the current compose already argues.
- Merging both stacks into one compose file or one network.
- Baking model weights into images, or building the torch image in CI.
- GPU images. The PyME host is CPU-only.
- Making `modernbert-ft` loadable in the container (it is a repo-relative local path; moving it into the cache is its own task in `ml-fastpath-demo`).

## Decisions

### D1. Explicit project name `anomaly-watch-ml`
Top-level `name: anomaly-watch-ml`. Container, network and volume names get a stable prefix that cannot collide with the product stack's `anomaly-watch_*`, whatever the checkout directory is called.
*Alternative*: rely on the directory name — works on most machines, breaks on a clone into `anomaly-watch/` or a worktree with a different name.

### D2. Host exposure: loopback only, configurable, non-overlapping ports
| Service | Container port | Host binding (default) | Why published |
|---|---|---|---|
| `detector` | 8003 | `127.0.0.1:${ML_DETECTOR_PORT:-8003}` | host-side `uv run python tools/replay.py`, `curl /readyz` |
| `postgres` | 5432 | `127.0.0.1:${ML_POSTGRES_PORT:-5433}` | host-side `--store-postgres` and inspection with `psql` |
| `replay` | — | not published | a client, it listens on nothing |

Reserved and never used by this stack: `3000`, `5432`, `6379`, `8025`, `8080`. Binding to `127.0.0.1` keeps the "nothing but the future Nginx is reachable from the network" stance of the current compose while making the dev flow work.
*Alternatives*: keep `expose` only (the host-side flow stays broken); publish on `0.0.0.0` like the product stack (exposes an unauthenticated scoring API and a database with demo credentials to the LAN, for no benefit).

### D3. `ML_`-prefixed variables in `.env`
Every variable this compose reads is `ML_*` (`ML_POSTGRES_USER`, `ML_POSTGRES_PASSWORD`, `ML_POSTGRES_DB`, `ML_POSTGRES_PORT`, `ML_DETECTOR_PORT`, `ML_DETECTOR_MODEL_KEY`, …, `ML_AW_ENDPOINT`), except `PIPELINE_SERVICE_TOKEN`, which is intentionally the same name and value as in the product stack. Compose gives shell variables precedence over `.env`, so a developer who exported `POSTGRES_PASSWORD` for the product stack would otherwise silently configure this one too. The product stack already applies the same reasoning (`API_UPSTREAM` vs `DEV_API_PROXY_TARGET`).
Credentials have no defaults in compose (`${ML_POSTGRES_PASSWORD:?…}`), so a missing `.env` fails fast with a clear message; tuning knobs keep defaults.

### D4. Detector image: builder venv from `uv.lock` → slim runtime
- **Builder** (`python:3.12-slim` + pinned `ghcr.io/astral-sh/uv:<version>`): `uv sync --frozen --no-dev --no-install-project --extra detector` into `/opt/venv`, with `UV_LINK_MODE=copy`, `UV_COMPILE_BYTECODE=1` and a BuildKit cache mount for `/root/.cache/uv`. Manifests are copied before sources so the dependency layer is cached. The CPU-only torch index is already declared in `pyproject.toml`, so the lockfile resolves CPU wheels.
- **Runtime** (`python:3.12-slim`): copies `/opt/venv` and only `ml/registry.py`, `ml/scorer.py`, `ml/models.yaml`, `services/detector/`. Creates UID/GID `10001`, `USER 10001:10001`. Keeps `HF_HUB_OFFLINE=1`, the model cache at `/models` (read-only mount) and the existing `/readyz` healthcheck.
- No compiler toolchain, no `uv` binary, no `pip` cache in the final image.
*Alternatives*: Alpine (musl; torch has no musl wheels — builds from source or does not install); distroless Python (no matching 3.12 + glibc variant with a shell-free healthcheck that stays simple; savings are marginal against ~700 MB of torch); keep single stage (leaves `uv` and build caches in the image, and does not honor the lockfile).
*Expected size*: dominated by CPU torch; the win is reproducibility, non-root, and dropping the ~420 MB build context, more than raw MB.

### D5. `replay` as a `tools`-profile service with its own light image
`tools/Dockerfile`, same builder/runtime pattern, installing the base dependencies plus a new `replay` extra (`httpx`, `psycopg[binary]`) — no torch, no `datasets`/`scikit-learn`. Copies `tools/` and `data/samples/`; `ENTRYPOINT ["python", "tools/replay.py"]` so arguments pass straight through:

```
docker compose run --rm replay data/samples/HDFS_2k.log --store-postgres
```

Inside the stack it gets `--endpoint http://detector:8003` and the DSN from the environment, and `depends_on` the detector being healthy. Other log files are passed through a read-only bind mount of `./data` at `/app/data`.
*Alternative*: reuse the detector image for replay (the `restore`-reuses-`backup` pattern). Rejected: it would pull ~1 GB of torch into a client that only does HTTP and Drain3; the product stack reuses an image only when both services need the same binaries.

### D6. Reaching AnomalyWatch through the host gateway
`replay` declares `extra_hosts: ["host.docker.internal:host-gateway"]`, and `ML_AW_ENDPOINT` defaults to `http://host.docker.internal:8080`. The product stack publishes `8080` on the host, so this works on Docker Desktop and Linux Engine alike, and in any start order.
*Alternative*: join the product stack's network as `external: true` (`anomaly-watch_default`, as the MailHog recipe does). Rejected: `docker compose up` in this repo would fail whenever the product stack is not up, couples to a name derived from the other repo's directory, and puts the ML containers on the same L2 network as the product database.
Until #28 is merged the variable and the host mapping are inert; nothing in this change calls the API.

### D7. `postgres:16-alpine` instead of `pgvector/pgvector:pg16`
Same major version and image as the product stack; smaller and one fewer image to pull when both stacks run. pgvector returns when the RAG engine exists (it will need `CREATE EXTENSION vector` anyway, and the image choice is revisited then).
*Trade-off*: see the collation risk below.

### D8. `replay.py` default DSN no longer points at `5432`
`--dsn` defaults to `os.environ.get("ANOMALY_WATCH_ML_DSN")`, falling back to `postgresql://anomaly:anomaly@localhost:5433/anomaly_watch`. The `replay` service sets `ANOMALY_WATCH_ML_DSN` to the in-network `postgres:5432` address built from the same `ML_POSTGRES_*` variables. This is the only application-code change.

### D9. `.dockerignore` at the repo root
Both images build with the repo root as context (they need `pyproject.toml` + `uv.lock`). Exclude: `.git`, `.venv`, `**/__pycache__`, `.pytest_cache`, `models/`, `ml/data/`, `ml/train/`, `ml/eval/results/`, `openspec/`, `.env*` (keeping nothing secret in context), `docker-compose*`, `**/Dockerfile*`, IDE/agent folders. Allow `data/samples/`.

### D10. Runtime hardening that matches the product stack
`restart: unless-stopped` on `postgres` and `detector` (not on `replay`, which is one-shot). Healthcheck-gated `depends_on`. Read-only mounts for weights and eval results. `DETECTOR_NUM_THREADS` stays bounded by default so inference cannot starve the co-located product stack.

## Risks / Trade-offs

- [Existing `postgres-data` volume was initialized by a Debian image; Alpine uses a different collation provider, so text indexes can silently misbehave] → the data is demo output regenerable with `replay`; migration instructions say `docker compose down -v` once. The volume also gets a new name under the `anomaly-watch-ml` project, so the old one is simply orphaned, never reused by accident.
- [**BREAKING** default DSN: host scripts or muscle memory relying on `localhost:5432`] → called out in README and `.env.example`; `--dsn` still overrides.
- [`host.docker.internal` resolves to the host, so the replay container can reach every port published on the host] → it is a one-shot client under the `tools` profile, not a long-running service; acceptable for a dev/demo tool.
- [Pinned `uv` and base images age] → pins are deliberate (reproducible builds); bump them in a normal PR.
- [Default model key in compose is not the registry default (`modernbert-ft`)] → documented; the container fails fast at `/readyz` with the registry's own error if someone sets an unresolvable key.
- [Overlap with `feature/report-pipeline-latency` in `tools/replay.py`, `pyproject.toml`, `uv.lock`, `README.md`] → changes here are confined to the DSN default, one new extra and a new README section; whichever branch merges second regenerates `uv.lock` with `uv lock`.
- [No image build in CI] → `docker compose config -q` catches schema and interpolation errors; building is verified locally as a task.

## Migration Plan

1. `cp .env.example .env` and fill in credentials.
2. Stop the old stack if it was ever started (`docker compose down -v`; the old project name derived from the directory).
3. `docker compose up -d --build`; `docker compose run --rm replay data/samples/HDFS_2k.log --assert-anomalies 1`.
4. Rollback: `git revert`; the previous compose file has no persisted state worth keeping.

## Open Questions

- Should the stack default `ML_DETECTOR_MODEL_KEY` switch to `modernbert-ft` once its checkpoint lives in `models/cache/`? (Tracked with `ml-fastpath-demo` task 6.6, not here.)
- Should CI eventually build and smoke-test the images (e.g. nightly), given the torch layer's build time?
