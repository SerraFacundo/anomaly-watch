# container-stack Specification

## Purpose
TBD - created by archiving change ml-docker-compose-stack. Update Purpose after archive.
## Requirements
### Requirement: Coexistence with the AnomalyWatch stack
The ML compose stack SHALL run on the same host as the AnomalyWatch product stack without any conflict of host ports, container names, networks or volumes, regardless of which stack starts first. It SHALL use the fixed compose project name `anomaly-watch-ml` and SHALL NOT publish any of the host ports reserved by the product stack: `3000`, `5432`, `6379`, `8025`, `8080`.

#### Scenario: Both stacks up, product stack first
- **WHEN** the AnomalyWatch stack is running and `docker compose up -d` is executed in this repo with default settings
- **THEN** every service of both stacks is running and no port-allocation error is reported

#### Scenario: Both stacks up, ML stack first
- **WHEN** this stack is running and the AnomalyWatch stack is then started with its default settings
- **THEN** the AnomalyWatch stack starts without port-allocation errors

#### Scenario: Stable resource names
- **WHEN** the repo is checked out in a directory with any name and the stack is started
- **THEN** its containers, network and volumes are prefixed `anomaly-watch-ml`

### Requirement: Loopback-only, configurable host exposure
The stack SHALL publish to the host only the detector HTTP port and the Postgres port, both bound to `127.0.0.1`, defaulting to `8003` and `5433` respectively and overridable through `ML_DETECTOR_PORT` and `ML_POSTGRES_PORT`. The replay service SHALL NOT publish any port.

#### Scenario: Detector reachable from the host
- **WHEN** the stack is up and healthy
- **THEN** `GET http://127.0.0.1:8003/readyz` from the host returns 200

#### Scenario: Not reachable from the network
- **WHEN** another machine on the LAN connects to the host's detector or Postgres port
- **THEN** the connection is refused

#### Scenario: Port override
- **WHEN** `ML_POSTGRES_PORT=55432` is set in `.env` and the stack is started
- **THEN** Postgres is published on `127.0.0.1:55432` and nothing is published on `5433`

### Requirement: Configuration through a documented environment file
All configuration the compose file reads SHALL come from environment variables prefixed `ML_`, except `PIPELINE_SERVICE_TOKEN`, which SHALL keep the same name as in the AnomalyWatch stack. The repo SHALL ship a commented `.env.example` listing every variable; `.env` SHALL be git-ignored. Database credentials SHALL have no default in the compose file, and a missing credential SHALL stop `docker compose` with an error naming the variable.

#### Scenario: Missing credentials
- **WHEN** `docker compose up` runs without `ML_POSTGRES_PASSWORD` defined
- **THEN** compose exits with an error that names `ML_POSTGRES_PASSWORD` and no container is created

#### Scenario: Product-stack variable in the shell
- **WHEN** `POSTGRES_PASSWORD` is exported in the shell and this stack is started
- **THEN** this stack's Postgres uses `ML_POSTGRES_PASSWORD`, unaffected by `POSTGRES_PASSWORD`

### Requirement: Minimal, reproducible, non-root images
Every custom image SHALL be built in multiple stages, where the final stage contains only the Python runtime, a virtualenv with the dependencies of that service resolved from `uv.lock`, and the source files that service imports. The final stage SHALL NOT contain `uv`, compilers or package-manager caches, and SHALL run as the non-root UID/GID `10001`. The `uv` image used in builds SHALL be pinned to an explicit version. The detector image SHALL install CPU-only torch. Model weights SHALL NOT be part of any image.

#### Scenario: Lockfile honored
- **WHEN** a dependency declared in `pyproject.toml` is not in sync with `uv.lock`
- **THEN** the image build fails instead of resolving a different version

#### Scenario: Non-root at runtime
- **WHEN** `id -u` is executed inside the running detector or replay container
- **THEN** it prints `10001`

#### Scenario: No build tooling in the final image
- **WHEN** the final detector image is inspected
- **THEN** it contains no `uv` binary and no CUDA libraries

#### Scenario: Replay image without ML runtime
- **WHEN** the replay image is inspected
- **THEN** it contains no `torch` or `transformers` packages

### Requirement: Small build context
The repository SHALL include a `.dockerignore` that excludes virtualenvs, VCS metadata, model weights, datasets, training runs, caches and environment files from the build context, while keeping the files the images need (`pyproject.toml`, `uv.lock`, service sources, `data/samples/`).

#### Scenario: Weights not sent to the daemon
- **WHEN** an image is built with a populated `models/cache/` and `.venv/`
- **THEN** the build context transferred to the daemon excludes both directories

### Requirement: Detector service in the stack
The `detector` service SHALL load model weights from the host model cache mounted read-only, run with Hub access disabled, bound its inference threads by default, report healthy only when `/readyz` returns 200, and restart automatically unless explicitly stopped. The model key, threshold, batch size, max length and thread count SHALL be configurable through `ML_`-prefixed variables.

#### Scenario: Healthy with a cached model
- **WHEN** the configured model is present in `models/cache/` and the stack starts
- **THEN** the `detector` container reaches `healthy`

#### Scenario: Weights are read-only
- **WHEN** the detector process tries to write under `/models`
- **THEN** the write fails

#### Scenario: Model swap without rebuild
- **WHEN** `ML_DETECTOR_MODEL_KEY` is changed in `.env` and the service is recreated
- **THEN** `GET /model` reports the new key and no image was rebuilt

### Requirement: Postgres service in the stack
The `postgres` service SHALL use `postgres:16-alpine`, persist data in a named volume, expose a `pg_isready` healthcheck, and restart automatically unless explicitly stopped. Services that depend on it SHALL wait for it to be healthy.

#### Scenario: Dependents wait for the database
- **WHEN** the stack starts from scratch
- **THEN** no dependent service starts before `postgres` is `healthy`

#### Scenario: Data survives a restart
- **WHEN** anomalies are stored and the stack is stopped with `docker compose down` and started again
- **THEN** the stored anomalies are still present

### Requirement: Replay as an on-demand tool
The stack SHALL provide a `replay` service under the `tools` profile that does not start with `docker compose up` and runs `tools/replay.py` with the arguments given to `docker compose run --rm replay …`. Inside the stack it SHALL target the in-network detector and Postgres by default, SHALL wait for the detector to be healthy, and SHALL be able to read the bundled samples and any log file under `./data`.

#### Scenario: Not started by default
- **WHEN** `docker compose up -d` is executed
- **THEN** no `replay` container is created

#### Scenario: End-to-end demo inside the stack
- **WHEN** `docker compose run --rm replay data/samples/HDFS_2k.log --store-postgres --assert-anomalies 1` is executed against a healthy stack
- **THEN** it exits 0 and the flagged anomalies are stored in this stack's Postgres

#### Scenario: Custom log file
- **WHEN** a log file is placed under `./data/` on the host and its path is passed to `replay`
- **THEN** replay reads it without rebuilding the image

### Requirement: Access to the AnomalyWatch API from the stack
The `replay` service SHALL be able to reach the AnomalyWatch API published on the host without joining the product stack's network, through `host.docker.internal`, with the base URL configurable through `ML_AW_ENDPOINT` (default `http://host.docker.internal:8080`) and the token passed through `PIPELINE_SERVICE_TOKEN`. Starting this stack SHALL NOT require the product stack to be running.

#### Scenario: API reachable when the product stack is up
- **WHEN** the AnomalyWatch stack is running and an HTTP request to `ML_AW_ENDPOINT` is made from the `replay` container
- **THEN** the AnomalyWatch API answers

#### Scenario: Product stack down
- **WHEN** the AnomalyWatch stack is not running and `docker compose up -d` is executed in this repo
- **THEN** this stack starts normally

### Requirement: Host-side replay never targets the product database by default
When `tools/replay.py` runs with `--store-postgres` and without `--dsn`, it SHALL use the `ANOMALY_WATCH_ML_DSN` environment variable if set, and otherwise this stack's host-published Postgres on `localhost:5433`. It SHALL NOT default to port `5432`.

#### Scenario: No DSN configured
- **WHEN** `uv run python tools/replay.py data/samples/HDFS_2k.log --store-postgres` runs on the host without `--dsn` or `ANOMALY_WATCH_ML_DSN`
- **THEN** it connects to `localhost:5433`

#### Scenario: DSN from the environment
- **WHEN** `ANOMALY_WATCH_ML_DSN` is set and `--dsn` is not passed
- **THEN** replay connects to the DSN from the environment

#### Scenario: Explicit flag wins
- **WHEN** both `ANOMALY_WATCH_ML_DSN` and `--dsn` are provided
- **THEN** replay connects to the `--dsn` value

