## Why

AnomalyWatch shows every monitored node as `ONLINE` and `CONNECTED` forever: nothing tells it when a host stops sending logs. The pipeline already knows. `ingest-service` stamps `agents.last_seen` every time an agent ships a batch, and that is exactly the fact AnomalyWatch needs.

AnomalyWatch's `add-node-heartbeat-and-alert-risk` change extends its collector heartbeat to carry one entry per host. This change is the sending side.

## What Changes

- `log-collector` reports node heartbeats to AnomalyWatch once a minute: `POST /api/v1/collectors/{code}/heartbeat` with the collector's agent credential and `nodes: [{host, lastSeenAt}]`, one entry per host read from `agents.last_seen`.
- `lastSeenAt` is the last sighting, not the time of sending, so a silent host goes stale on the AnomalyWatch side even while the collector keeps reporting.
- Off unless `ANOMALYWATCH_API_URL`, `COLLECTOR_CODE` and `COLLECTOR_TOKEN` are set. A failure is logged and retried on the next tick; it never touches the pipeline.
- Hosts AnomalyWatch rejects (`UNKNOWN_HOST`, `FOREIGN_NODE`, `INVALID`) are shown in `GET /status` under `node_heartbeat`.
- A `unit-tests` CI job and a `dev` dependency group with pytest.

## Capabilities

### New Capabilities
- `node-heartbeat-reporting`: the collector reports when each host was last heard from.

### Modified Capabilities
- None.

## Impact

- `services/collector/heartbeat.py` (new), `config.py`, `app.py`.
- `docker-compose.yml`: four optional environment variables on `collector`.
- `pyproject.toml` / `uv.lock`: pytest in a `dev` group (not installed in the containers).
